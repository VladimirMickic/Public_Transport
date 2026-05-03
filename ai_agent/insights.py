"""
ai_agent/insights.py — Generate weekly transit insights using Claude.

Queries gold_route_reliability for the past week, sends structured data
to Claude, and stores narrative + tweet + headline in ai_weekly_insights.

Usage:
    python -m ai_agent.insights
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

import anthropic
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

SUPABASE_DB_URL = os.environ["SUPABASE_DB_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]


def get_week_start():
    """Return the Monday of the current week as a date."""
    today = date.today()
    return today - timedelta(days=today.weekday())


def already_generated(conn, week_start):
    """Check if insights for this week already exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ai_weekly_insights WHERE week_start = %s",
            (week_start,),
        )
        return cur.fetchone() is not None


def fetch_gold_summary(conn):
    """Fetch aggregated reliability data for Claude's context.

    Excludes synthetic non-passenger routes (98 AM Tripper, 99 PM
    Tripper, 999 Deadhead) — they have no schedule to adhere to and
    their stale adherence counters always rank as the "worst" buckets
    even though no rider is affected.
    """
    sql = """
        SELECT route_name, day_name, hour_of_day,
               total_pings, on_time_pct, avg_adherence_minutes,
               reliability_score
        FROM gold_route_reliability
        WHERE total_pings >= 5
          AND route_id NOT IN ('98', '99', '999')
        ORDER BY reliability_score ASC
        LIMIT 40
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


def _clean_row(d):
    """Coerce a RealDict row to JSON-serialisable primitives."""
    out = {}
    for k, v in d.items():
        if v is None:
            out[k] = None
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif hasattr(v, "__float__") and not isinstance(v, (bool,)):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def fetch_weekly_kpi_snapshot(conn, week_start) -> dict:
    """Build the weekly KPI snapshot persisted alongside the narrative.

    Captures system-wide totals for the week, a daily OTP arc
    (Mon–Sat; Sunday is excluded since EMTA does not operate), and
    top-3 worst routes.
    """
    week_end = week_start + timedelta(days=6)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                ROUND(COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                      / NULLIF(COUNT(*), 0), 1) AS otp_pct,
                ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                COUNT(DISTINCT route_id)                  AS active_routes,
                COUNT(*)                                  AS total_pings,
                COUNT(*) FILTER (WHERE delay_bucket = 'very_late') AS very_late
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date
                  BETWEEN %s AND %s
              AND speed > 2
            """,
            (week_start, week_end),
        )
        kpi = _clean_row(dict(cur.fetchone() or {}))

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                (observed_at AT TIME ZONE 'America/New_York')::date AS day,
                TO_CHAR((observed_at AT TIME ZONE 'America/New_York')::date, 'Dy') AS day_name,
                ROUND(COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                      / NULLIF(COUNT(*), 0), 1) AS otp_pct,
                COUNT(*) AS pings
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date
                  BETWEEN %s AND %s
              AND speed > 2
            GROUP BY day, day_name
            ORDER BY day
            """,
            (week_start, week_end),
        )
        daily_arc = [_clean_row(dict(r)) for r in cur.fetchall()]

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT route_id, route_name,
                   GREATEST(0, ROUND(
                       (100 - LEAST(100, AVG(LEAST(30, ABS(adherence_minutes))) * 10))::numeric, 1
                   )) AS reliability,
                   ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                   COUNT(*) AS pings
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date
                  BETWEEN %s AND %s
              AND speed > 2
              AND adherence_minutes IS NOT NULL
              AND route_name IS NOT NULL
              AND route_id NOT IN ('98', '99', '999')
            GROUP BY route_id, route_name
            HAVING COUNT(*) >= 20
            ORDER BY AVG(ABS(adherence_minutes)) DESC
            LIMIT 3
            """,
            (week_start, week_end),
        )
        worst_routes = [_clean_row(dict(r)) for r in cur.fetchall()]

    return {
        **kpi,
        "week_start": str(week_start),
        "week_end": str(week_end),
        "daily_arc": daily_arc,
        "worst_routes": worst_routes,
    }


def fetch_system_stats(conn, week_start):
    """Fetch high-level system stats for the narrative.

    Window is the same Mon–Sun (ET) range used by the KPI snapshot, so
    the narrative numbers always agree with the dashboard's KPI strip.
    Filters mirror the snapshot (speed > 2, ET timezone) and excludes
    synthetic routes 98/99/999 from counts.
    """
    week_end = week_start + timedelta(days=6)
    sql = """
        SELECT
            COUNT(*) AS total_pings,
            COUNT(DISTINCT route_id) AS routes,
            ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
            ROUND(
                COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                / NULLIF(COUNT(*), 0), 1
            ) AS system_on_time_pct,
            COUNT(*) FILTER (WHERE delay_bucket = 'very_late') AS very_late_count
        FROM silver_arrivals
        WHERE (observed_at AT TIME ZONE 'America/New_York')::date
              BETWEEN %s AND %s
          AND speed > 2
          AND route_id NOT IN ('98', '99', '999')
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (week_start, week_end))
        return cur.fetchone()


def build_prompt(gold_data, system_stats, week_start):
    """Build the Claude prompt with real data.

    System-wide numbers are passed for situational awareness only —
    Claude is forbidden from citing them in its output. A deterministic
    executive-summary paragraph is inserted programmatically after
    Claude's hook (see _scrub_and_inject_summary). Even with strong
    "quote verbatim" instructions Claude reliably hallucinated weekly
    system numbers (saw 71% in prompt, wrote 73.5% in narrative), and
    riders flagged the mismatch against the dashboard KPI strip. Same
    guardrail the daily digest uses.
    """
    week_end = week_start + timedelta(days=6)
    week_label = (
        f"{week_start.strftime('%B %d, %Y')} – {week_end.strftime('%B %d, %Y')}"
    )

    # Format gold data as a readable table
    data_lines = []
    for row in gold_data:
        data_lines.append(
            f"  {row['route_name']:<30} {row['day_name']:<10} "
            f"{row['hour_of_day']:>2}:00  "
            f"OTP={row['on_time_pct']}%  "
            f"AvgDelay={row['avg_adherence_minutes']}min  "
            f"Score={row['reliability_score']}"
        )
    data_block = "\n".join(data_lines)

    stats = system_stats or {}

    prompt = f"""You are a transit data analyst writing a weekly reliability report for the
Erie Metropolitan Transit Authority (EMTA) in Erie, PA. Your audience is EMTA
leadership and engaged riders who want real numbers, not corporate filler.

The report covers the week of {week_label}.

System-wide context for this week (moving buses only, synthetic routes
excluded). These numbers are shown to you ONLY for situational awareness —
DO NOT cite or repeat any of them in your output. A separate executive-summary
paragraph quoting these numbers will be inserted automatically.
- Total vehicle pings tracked: {stats.get('total_pings', 'N/A')}
- Active routes: {stats.get('routes', 'N/A')}
- System-wide on-time percentage: {stats.get('system_on_time_pct', 'N/A')}%
- Average delay: {stats.get('avg_delay', 'N/A')} minutes
- Very late incidents (>15 min): {stats.get('very_late_count', 'N/A')}

Here are the 40 worst-performing route/hour/day combinations from this week
(sorted by reliability score, ascending):

{data_block}

Write three outputs, separated by exact markers:

1. A 3-paragraph narrative. Each paragraph separated by a blank line. Each
   paragraph must contain AT LEAST 3 SENTENCES.
   - Paragraph 1 (hook): 3 to 4 sentences. Lead with the single most striking
     ROUTE-LEVEL fact from the per-route table (a specific route's score,
     OTP, or avg delay at a specific time window). Build on it with one or
     two sentences of context. Do NOT cite system-wide totals (total pings,
     system OTP, system avg delay, very-late count, active route count) —
     those go in the auto-inserted summary paragraph. No throat-clearing,
     no "This week's report covers...", no date recap.
   - Paragraph 2 (route-level): 4 to 6 sentences. You MUST name at least
     three specific route numbers/names in **bold** and at least two
     specific time windows (e.g. "Route 5 at 8am on weekdays"). Cite
     per-route OTP, avg delay, and reliability scores from the table above.
     Compare two routes against each other to give the reader scale. If a
     route shows a wildly negative average delay (e.g. -100+ min), flag it
     as a likely data-reporting glitch rather than real early arrivals.
   - Paragraph 3 (insight): 3 to 5 sentences. What patterns repeat across
     the week — afternoon corridor drag, weekday peaks, a specific
     route/time combo that keeps showing up? End with a concrete takeaway
     for an EMTA dispatcher or a regular rider planning their week. Do NOT
     cite system-wide totals here either.

   WRITING RULES — read carefully:
   - Vary sentence length. Short punchy ones. Longer ones that build context.
   - Have opinions. "That is a brutal number" beats "This represents a
     significant challenge."
   - Be specific. Exact percentages and route names beat "some routes struggled."
   - No em dashes. Use commas, periods, or parentheses instead.
   - Avoid these words entirely: pivotal, underscores, testament, showcasing,
     vibrant, crucial, vital, reflects broader, highlights the importance,
     ensuring, fostering, encompasses, landscape, realm, delve, tapestry.
   - No rule-of-three patterns ("speed, precision, and efficiency").
   - End on a concrete observation, never a vague hopeful statement.
   - Never mention or name routes 98 (AM Tripper), 99 (PM Tripper), or 999
     (Deadhead). They are synthetic non-passenger routes with no schedule
     and are excluded from every aggregate above. If you see "Tripper" or
     "Deadhead" anywhere in the data block, ignore those rows entirely.

2. After the marker ---TWEET--- on its own line, write a single tweet (≤280
   characters) summarizing the key finding. Include one specific route and
   one specific number from the per-route table (not a system-wide total).
   No hashtags.

3. After the marker ---HEADLINE--- on its own line, write a one-sentence
   headline (≤100 characters, no hashtags) suitable for an email subject line.

Start the narrative immediately — no preamble or title."""

    return prompt


# Phrases that strongly indicate a paragraph is Claude's hallucinated
# system-stats paragraph (which we want to replace with our deterministic
# build_summary_paragraph output). Mirrors the daily digest's filter and
# adds weekly-specific phrasing ("this week", "across the week").
_SYSTEM_STATS_GIVEAWAYS = (
    "system-wide",
    "system performance",
    "across all routes",
    "across the network",
    "across the week",
    "this week emta",
    "vehicle pings",
    "tracked so far",
    "tracked across",
    "very late incidents",
    "late incidents",
    "emta is posting",
    "emta tracked",
    "system on-time",
    "system-level",
)


def _looks_like_system_stats_paragraph(paragraph: str) -> bool:
    """True if the paragraph reads like Claude's hallucinated stats paragraph.

    Requires both a system-stats giveaway phrase AND a percent sign — the
    combination distinguishes a stats paragraph from a route-level paragraph
    that happens to mention "system" in passing.
    """
    if not paragraph:
        return False
    lower = paragraph.lower()
    if "%" not in lower:
        return False
    return any(phrase in lower for phrase in _SYSTEM_STATS_GIVEAWAYS)


def build_summary_paragraph(snapshot: dict, week_start) -> str:
    """Deterministic executive-summary paragraph built from the snapshot.

    Inserted between Claude's hook and route-level paragraphs so the
    system-wide numbers in the narrative are mathematically guaranteed
    to match the KPI strip on the dashboard.
    """
    snap = snapshot or {}

    def _int(v):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None

    def _f1(v):
        try:
            return f"{float(v):.1f}"
        except (TypeError, ValueError):
            return None

    otp        = _f1(snap.get("otp_pct"))
    avg_delay  = _f1(snap.get("avg_delay"))
    pings      = _int(snap.get("total_pings"))
    routes     = _int(snap.get("active_routes"))
    very_late  = _int(snap.get("very_late"))

    if otp is None or pings is None:
        return ""

    otp_val = float(otp)
    if otp_val >= 80:
        verdict = "a strong week"
    elif otp_val >= 60:
        verdict = "a mixed week"
    else:
        verdict = "a poor week"

    pings_str = f"{pings:,}"
    very_late_str = f"{very_late:,}" if very_late is not None else "0"
    routes_str = str(routes) if routes is not None else "—"
    week_end = week_start + timedelta(days=6)
    week_label = (
        f"{week_start.strftime('%b %d')}–{week_end.strftime('%b %d')}"
    )

    avg_phrase = (
        f"Average adherence ran {avg_delay} minutes off schedule"
        if avg_delay is not None
        else "Average adherence data is unavailable"
    )

    return (
        f"For the week of {week_label}, system-wide on-time performance "
        f"closed at {otp}% across {pings_str} moving-bus pings on "
        f"{routes_str} active routes — {verdict}. {avg_phrase}, with "
        f"{very_late_str} pings logged more than 15 minutes late."
    )


def _scrub_and_inject_summary(narrative: str, snapshot: dict, week_start) -> str:
    """Drop Claude's hallucinated stats paragraph(s) and insert our own."""
    summary_para = build_summary_paragraph(snapshot, week_start)
    paragraphs = [p.strip() for p in (narrative or "").split("\n\n") if p.strip()]
    cleaned = [p for p in paragraphs if not _looks_like_system_stats_paragraph(p)]

    if not summary_para:
        return "\n\n".join(cleaned) if cleaned else (narrative or "")

    if not cleaned:
        return summary_para

    cleaned.insert(1, summary_para) if len(cleaned) >= 1 else cleaned.append(summary_para)
    return "\n\n".join(cleaned)


def parse_response(text):
    """Parse Claude's response into narrative, tweet, and headline."""
    parts = text.split("---TWEET---")
    narrative = parts[0].strip()

    tweet = ""
    headline = ""
    if len(parts) > 1:
        remaining = parts[1]
        headline_parts = remaining.split("---HEADLINE---")
        tweet = headline_parts[0].strip()
        if len(headline_parts) > 1:
            headline = headline_parts[1].strip()

    # Enforce tweet ≤280 chars
    if len(tweet) > 280:
        # Truncate at last complete sentence before 280
        truncated = tweet[:280]
        last_period = truncated.rfind(".")
        last_excl = truncated.rfind("!")
        last_q = truncated.rfind("?")
        cut_at = max(last_period, last_excl, last_q)
        if cut_at > 0:
            tweet = truncated[: cut_at + 1] + "..."
        else:
            # Fall back to last space
            last_space = truncated.rfind(" ")
            tweet = truncated[:last_space] + "..." if last_space > 0 else truncated

    # Enforce headline ≤100 chars
    if len(headline) > 100:
        headline = headline[:97] + "..."

    return narrative, tweet, headline


def generate_insights():
    """Main entrypoint: fetch data, call Claude, store results."""
    conn = psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10)
    week_start = get_week_start()

    # Guard: don't run more than once per week
    if already_generated(conn, week_start):
        log.info("Insights for week of %s already exist. Skipping.", week_start)
        conn.close()
        return

    # Fetch data
    gold_data = fetch_gold_summary(conn)
    if not gold_data:
        log.info("No gold data available. Cannot generate insights.")
        conn.close()
        return

    system_stats = fetch_system_stats(conn, week_start)
    weekly_snapshot = fetch_weekly_kpi_snapshot(conn, week_start)
    snapshot_json = json.dumps(weekly_snapshot, default=str)
    prompt = build_prompt(gold_data, system_stats, week_start)

    # Call Claude
    log.info("Calling Claude for week of %s ...", week_start)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_response = message.content[0].text
    log.info("Raw Claude response:\n%s", raw_response)

    # Parse, then scrub any hallucinated system-stats paragraph and
    # inject the deterministic summary built from the KPI snapshot.
    narrative, tweet, headline = parse_response(raw_response)
    narrative = _scrub_and_inject_summary(narrative, weekly_snapshot, week_start)
    log.info("Narrative length: %d chars", len(narrative))
    log.info("Tweet (%d chars): %s", len(tweet), tweet)
    log.info("Headline (%d chars): %s", len(headline), headline)

    # Ensure headline column exists (idempotent)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE ai_weekly_insights
                ADD COLUMN IF NOT EXISTS headline_text TEXT
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("Could not add headline_text column (may already exist)")

    # Insert
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_weekly_insights
                    (week_start, narrative, tweet_draft, headline_text, kpi_snapshot)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (week_start, narrative, tweet, headline, snapshot_json),
            )
        conn.commit()
        log.info("Stored insights for week of %s", week_start)
    except Exception:
        conn.rollback()
        log.exception("Failed to store insights")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    generate_insights()
