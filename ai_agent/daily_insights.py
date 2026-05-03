"""
ai_agent/daily_insights.py — Generate daily transit insights using Claude.

Queries silver_arrivals for a specific day, sends structured data
to Claude, and stores narrative + tweet + headline in ai_daily_insights.

Usage:
    python -m ai_agent.daily_insights
    python -m ai_agent.daily_insights --date 2026-04-18
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ET = ZoneInfo("America/New_York")

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def today_et() -> date:
    return datetime.now(ET).date()


def get_default_date() -> date:
    """Return yesterday (ET) as the default date."""
    return today_et() - timedelta(days=1)


def is_service_idle(conn) -> bool:
    """Return True when passenger service appears to have ended for the day.

    Two-tier signal:

    1. Hard wall-clock floor: any time at or after 23:00 ET counts as
       idle, regardless of API noise. Empirically the Avail feed keeps
       echoing non-zero speeds on passenger routes well past midnight
       (deadhead returns, stale GPS at depot, last-known-speed echoes),
       which left the previous bronze-only check firing "buses still
       active" indefinitely — 0 auto-digests in 13K+ pipeline runs.

    2. Below 23:00 ET, count moving pings in the last 45 minutes from
       bronze, but exclude:
         - synthetic non-passenger routes (98 AM Tripper, 99 PM Tripper,
           999 Deadhead) — consistent with every other analytics query
         - speed ≤ 5 mph — above GPS jitter / parked-bus drift
         - is_on_route = FALSE — buses sitting at depot
       If zero qualifying pings, treat as idle.
    """
    if datetime.now(ET).hour >= 23:
        return True
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM bronze_vehicle_pings
            WHERE observed_at >= NOW() - INTERVAL '45 minutes'
              AND speed > 5
              AND is_on_route = TRUE
              AND route_id NOT IN (98, 99, 999)
        """)
        return cur.fetchone()[0] == 0


def had_service_today(conn) -> bool:
    """Return True if today (ET) had at least 100 moving-bus pings.

    Guards against triggering the digest at midnight when very few pings
    have been collected (e.g. early morning before buses start), or on a
    day when the feed was offline.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM bronze_vehicle_pings
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date
                  = (NOW() AT TIME ZONE 'America/New_York')::date
              AND speed > 2
        """)
        return cur.fetchone()[0] >= 100


def digest_generated_recently(conn, report_date) -> bool:
    """Return True if the digest for this date was already stored within the last 2 hours.

    Prevents every 5-min pipeline run from re-calling Claude once the buses
    have stopped and the day is done.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM ai_daily_insights
            WHERE report_date = %s
              AND created_at >= NOW() - INTERVAL '2 hours'
        """, (report_date,))
        return cur.fetchone() is not None


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


def fetch_kpi_snapshot(conn, report_date: date) -> dict:
    """Build the KPI snapshot persisted alongside the narrative.

    This is the SINGLE source of truth for the digest. Claude's prompt is
    built from these exact numbers (see build_prompt) and the dashboard
    renders KPIs/chart/table from them too. Capturing once eliminates the
    drift that used to occur when Claude saw one Silver read and the
    snapshot captured another 30 seconds later (after the API call), with
    a 5-min ETL Silver rebuild landing in between.

    Captures system-wide totals, service window, an hourly OTP arc, and
    top-3 worst routes so archived digests keep rendering after
    silver_arrivals is pruned.
    """
    # System KPIs + service window — exclude synthetic routes (98/99/999)
    # so the snapshot matches the dashboard KPI strip exactly.
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                ROUND(COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                      / NULLIF(COUNT(*), 0), 1) AS otp_pct,
                ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                COUNT(DISTINCT route_id)                  AS active_routes,
                COUNT(*)                                  AS total_pings,
                COUNT(*) FILTER (WHERE delay_bucket = 'very_late') AS very_late,
                MIN(observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York') AS first_ride,
                MAX(observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York') AS last_ride
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date = %s
              AND speed > %s
              AND route_id NOT IN ('98', '99', '999')
            """,
            (report_date, MOVING_SPEED_MPH),
        )
        kpi = _clean_row(dict(cur.fetchone() or {}))

    # Hourly OTP arc — same exclusion so the arc and the system OTP align.
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT hour_of_day,
                   ROUND(COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                         / NULLIF(COUNT(*), 0), 1) AS otp_pct,
                   COUNT(*) AS pings
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date = %s
              AND speed > %s
              AND route_id NOT IN ('98', '99', '999')
            GROUP BY hour_of_day
            ORDER BY hour_of_day
            """,
            (report_date, MOVING_SPEED_MPH),
        )
        hourly_arc = [_clean_row(dict(r)) for r in cur.fetchall()]

    # Worst routes
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT route_id, route_name,
                   ROUND(
                       COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                       / NULLIF(COUNT(*), 0), 1
                   ) AS reliability,
                   ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
                   COUNT(*) AS pings
            FROM silver_arrivals
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date = %s
              AND speed > %s
              AND adherence_minutes IS NOT NULL
              AND route_name IS NOT NULL
              AND route_id NOT IN ('98', '99', '999')
            GROUP BY route_id, route_name
            HAVING COUNT(*) >= 20
            ORDER BY COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 1.0
                     / NULLIF(COUNT(*), 0) ASC
            LIMIT 3
            """,
            (report_date, MOVING_SPEED_MPH),
        )
        worst_routes = [_clean_row(dict(r)) for r in cur.fetchall()]

    # data_through_et stamps the snapshot with the moment it was frozen so
    # the dashboard can render past-day digests without drift (read from the
    # snapshot, not a fresh Silver query) and label today's narrative as
    # "as of HH:MM ET". Without this stamp, KPIs and narrative drift apart
    # every 5 minutes as new pings land.
    return {
        **kpi,
        "hourly_arc": hourly_arc,
        "worst_routes": worst_routes,
        "data_through_et": datetime.now(ET).isoformat(),
    }


def fetch_existing_row(conn, report_date):
    """Return (id, generation_count) for this date, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, generation_count FROM ai_daily_insights WHERE report_date = %s",
            (report_date,),
        )
        return cur.fetchone()


MOVING_SPEED_MPH = 2  # Pings below this are treated as parked/idle and excluded


def fetch_daily_summary(conn, report_date):
    """Fetch daily aggregated reliability data from silver_arrivals.

    Excludes synthetic non-passenger routes (98 AM Tripper, 99 PM Tripper,
    999 Deadhead) — they have no schedule to adhere to and would otherwise
    appear in the worst-routes block fed to Claude. Only includes pings
    where the bus is moving (speed > MOVING_SPEED_MPH).
    """
    sql = """
        SELECT
            route_name,
            COUNT(*) AS total_pings,
            ROUND(COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0 / NULLIF(COUNT(*), 0), 2) AS on_time_pct,
            ROUND(AVG(adherence_minutes)::numeric, 2) AS avg_delay
        FROM silver_arrivals
        WHERE observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' >= %s::date
          AND observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' < (%s::date + INTERVAL '1 day')
          AND speed > %s
          AND route_id NOT IN ('98', '99', '999')
        GROUP BY route_name
        HAVING COUNT(*) >= 5
        ORDER BY avg_delay DESC
        LIMIT 40
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (report_date, report_date, MOVING_SPEED_MPH))
        return cur.fetchall()


def build_prompt(report_date, summary_data, snapshot, is_partial_day=False):
    """Build the Claude prompt from the persisted KPI snapshot.

    The snapshot is the single source of truth — Claude sees the same
    numbers the dashboard later renders, so the narrative cannot drift
    from the KPI strip. snapshot must be the dict produced by
    fetch_kpi_snapshot().
    """
    # Format daily data as a readable table
    data_lines = []
    for row in summary_data:
        data_lines.append(
            f"  {row['route_name']:<30} "
            f"OTP={row['on_time_pct']}%  "
            f"AvgDelay={row['avg_delay']}min  "
            f"Pings={row['total_pings']}"
        )
    data_block = "\n".join(data_lines)

    snap = snapshot or {}

    # first_ride / last_ride come from fetch_kpi_snapshot as ISO strings
    # in UTC (postgres returns timezone-aware timestamps; _clean_row
    # serialised them via .isoformat()). Convert to ET for display so
    # Claude reports the service window in EMTA's local time.
    def _hhmm_et(iso):
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso).astimezone(ET).strftime("%H:%M")
        except (ValueError, TypeError):
            return None

    first_hhmm = _hhmm_et(snap.get("first_ride"))
    last_hhmm = _hhmm_et(snap.get("last_ride"))

    # Format snapshot numbers as integers / 1-decimal floats so Claude
    # quotes "3336" rather than "3336.0" in the narrative. The snapshot
    # stores these as floats (psycopg2 ROUND result + JSON), but the
    # narrative reads cleaner with bare integers.
    def _int(v):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return v if v is not None else "N/A"

    def _f1(v):
        try:
            return f"{float(v):.1f}"
        except (TypeError, ValueError):
            return v if v is not None else "N/A"

    snap_total_pings   = _int(snap.get("total_pings"))
    snap_active_routes = _int(snap.get("active_routes"))
    snap_very_late     = _int(snap.get("very_late"))
    snap_otp_pct       = _f1(snap.get("otp_pct"))
    snap_avg_delay     = _f1(snap.get("avg_delay"))
    window_line = ""
    if first_hhmm and last_hhmm:
        window_line = (
            f"- Service window (first to last moving bus): "
            f"{first_hhmm} – {last_hhmm} ET\n"
        )

    # Every digest is a snapshot — stamp it with the snapshot time so the
    # narrative reads as a frozen artefact, not a live update. Past-day
    # digests get an end-of-day stamp; today's digest gets a "so far"
    # stamp. Either way the reader sees that the numbers below are
    # frozen, which prevents the "narrative says 68.1%, KPI strip says
    # 69.3%" complaint that would otherwise arise as more pings land.
    now_et_hhmm = datetime.now(ET).strftime("%H:%M")
    if is_partial_day:
        partial_line = (
            f"- Data through: {now_et_hhmm} ET (PARTIAL DAY — service is still active; "
            f"numbers reflect pings collected so far today).\n"
        )
        tense_guidance = (
            "This is a partial-day snapshot. Write in present/progressive tense "
            "(\"so far today\", \"as of HH:MM ET\"). Do NOT write as if the day is over, "
            "and do NOT project or predict end-of-day totals."
        )
    else:
        partial_line = (
            f"- Snapshot generated at: {now_et_hhmm} ET on {report_date.isoformat()} "
            f"(end-of-day, complete service day).\n"
        )
        tense_guidance = (
            "Write in past tense; the service day is complete. The numbers below "
            "are a frozen snapshot — do not invent updates beyond this data."
        )

    # Claude is asked for THREE paragraphs (hook, route-level, insight). The
    # executive-summary paragraph is built programmatically from the snapshot
    # and inserted between Claude's hook and route-level paragraphs (see
    # generate_daily_insights). This is deliberate: even with strong "quote
    # verbatim" instructions, Claude reliably hallucinated system numbers
    # (saw 72.2% in prompt, wrote 73.8% in narrative). Removing the system
    # numbers from Claude's writing surface makes the mismatch impossible.
    prompt = f"""You are a transit data analyst writing a daily reliability report for the
Erie Metropolitan Transit Authority (EMTA) in Erie, PA. Your audience is EMTA
leadership and engaged riders who want real numbers, not corporate filler.

The report date is: {report_date.strftime('%A, %B %d, %Y')}.

System-wide context for this date (moving buses only). These numbers are
shown to you ONLY for situational awareness — DO NOT cite or repeat any of
them in your output. A separate executive-summary paragraph quoting these
numbers will be inserted automatically.
- Total vehicle pings tracked: {snap_total_pings}
- Active routes: {snap_active_routes}
- System-wide on-time percentage: {snap_otp_pct}%
- Average delay: {snap_avg_delay} minutes
- Very late incidents (>15 min): {snap_very_late}
{window_line}{partial_line}

Here are the worst-performing routes from that day (sorted by Average Delay descending):

{data_block}

{tense_guidance}

Write three outputs, separated by exact markers:

1. A 3-paragraph narrative. Each paragraph separated by a blank line. Each
   paragraph must contain AT LEAST 3 SENTENCES — the previous version was
   too thin and riders said it skimmed past the actual story.
   - Paragraph 1 (hook): 3 to 4 sentences that pull the reader in immediately.
     Lead with the single most striking ROUTE-LEVEL fact from the per-route
     table above (e.g. a specific route's delay or OTP). Build on that with
     one or two sentences of context. Do NOT cite system-wide totals (total
     pings, system OTP, system avg delay, very-late count, active route
     count) — those go in the auto-inserted summary paragraph. Punchy but
     substantive. No throat-clearing, no "Today's report covers...", no
     date recap.
   - Paragraph 2 (route-level): 4 to 6 sentences. You MUST name at least
     three specific route numbers/names in **bold**. Cite per-route OTP,
     avg delay, and ping counts from the table above. Compare two of the
     routes against each other to give the reader a sense of scale. If a
     route shows a wildly negative average delay (e.g. -100+ min), flag it
     as a likely data-reporting issue rather than real early arrivals,
     and explain briefly what kind of glitch this looks like.
   - Paragraph 3 (insight): 3 to 5 sentences. What do these patterns
     suggest about operational friction or rider experience? Concrete
     observations, not vague hope. End with a sentence that names the
     practical takeaway for an EMTA dispatcher or rider planning their
     trip. Do NOT cite system-wide totals here either.

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
   attention-grabbing headline (≤100 characters, no hashtags) suitable for an
   email subject line. It should make someone want to click.

Start the narrative immediately — no preamble or title."""

    return prompt


# Phrases that strongly indicate a paragraph is Claude's hallucinated
# system-stats paragraph (which we want to replace with our deterministic
# build_summary_paragraph output). Matched case-insensitively. Each phrase
# is something a stats paragraph would say but a route-level or insight
# paragraph would not — e.g. "system-wide", "across all routes",
# "vehicle pings", "tracked so far". Adding "(very )?late incidents" and
# "EMTA is posting" / "EMTA tracked" catches the variants Claude has
# generated in past iterations.
_SYSTEM_STATS_GIVEAWAYS = (
    "system-wide",
    "system performance",
    "across all routes",
    "across the network",
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

    A paragraph qualifies if it contains a system-stats giveaway phrase AND
    a percentage sign — the combination distinguishes a stats paragraph
    from a route-level paragraph that happens to mention "system" in passing.
    """
    if not paragraph:
        return False
    lower = paragraph.lower()
    if "%" not in lower:
        return False
    return any(phrase in lower for phrase in _SYSTEM_STATS_GIVEAWAYS)


def _scrub_and_inject_summary(
    narrative: str, snapshot: dict, is_partial_day: bool
) -> str:
    """Drop Claude's hallucinated stats paragraph(s) and insert our own.

    The deterministic summary paragraph is inserted at position 1 (right
    after Claude's hook). If Claude's response had no hook, the summary
    becomes the first paragraph and the rest follows.
    """
    summary_para = build_summary_paragraph(snapshot, is_partial_day=is_partial_day)
    paragraphs = [p.strip() for p in (narrative or "").split("\n\n") if p.strip()]
    cleaned = [p for p in paragraphs if not _looks_like_system_stats_paragraph(p)]

    if not summary_para:
        return "\n\n".join(cleaned) if cleaned else (narrative or "")

    if not cleaned:
        return summary_para

    cleaned.insert(1, summary_para) if len(cleaned) >= 1 else cleaned.append(summary_para)
    return "\n\n".join(cleaned)


def build_summary_paragraph(snapshot: dict, is_partial_day: bool) -> str:
    """Deterministic executive-summary paragraph built from the snapshot.

    Inserted between Claude's hook (paragraph 1) and route-level paragraph
    (paragraph 2) so the system-wide numbers in the narrative are
    mathematically guaranteed to match the KPI strip. Claude is explicitly
    forbidden from citing these numbers in the prompt — they're only
    rendered here.
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
        verdict = "a strong day"
    elif otp_val >= 60:
        verdict = "a mixed day"
    else:
        verdict = "a poor day"

    pings_str = f"{pings:,}"
    very_late_str = f"{very_late:,}" if very_late is not None else "0"
    routes_str = str(routes) if routes is not None else "—"

    if is_partial_day:
        now_et_hhmm = datetime.now(ET).strftime("%H:%M")
        opener = f"As of {now_et_hhmm} ET, system-wide on-time performance sits at {otp}%"
    else:
        opener = f"System-wide on-time performance closed at {otp}%"

    return (
        f"{opener} across {pings_str} moving-bus pings on {routes_str} active "
        f"routes, with average adherence at {avg_delay} minutes and {very_late_str} "
        f"pings logged more than 15 minutes behind schedule. That makes it {verdict} "
        f"for riders by the headline numbers, with the route-by-route picture below "
        f"telling a more uneven story."
    )


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
        truncated = tweet[:280]
        last_punct = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
        if last_punct > 0:
            tweet = truncated[: last_punct + 1] + "..."
        else:
            last_space = truncated.rfind(" ")
            tweet = truncated[:last_space] + "..." if last_space > 0 else truncated

    # Enforce headline ≤100 chars
    if len(headline) > 100:
        headline = headline[:97] + "..."

    return narrative, tweet, headline


def generate_daily_insights(
    target_date: date,
    manual: bool = True,
    force_refresh: bool = False,
) -> str:
    """Fetch data, call Claude, store daily results.

    Rules:
    - Historical date (< today ET): cache-first by default. If a row exists,
      return "exists". Pass force_refresh=True to overwrite a bad/hallucinated
      digest; this bumps generation_count so the audit trail shows the row
      was re-rolled.
    - Today (ET): always produce a fresh snapshot using all data so far.
        * manual=True: regenerates and increments generation_count (audit trail).
        * manual=False (cron / auto): regenerates, does NOT increment counter.

    Returns one of:
        "generated", "regenerated", "exists", "no_data", "missing_env".
    """
    if not SUPABASE_DB_URL or not ANTHROPIC_API_KEY:
        log.error("Missing SUPABASE_DB_URL or ANTHROPIC_API_KEY environment variables.")
        return "missing_env"

    conn = psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10)

    try:
        is_today = target_date == today_et()
        existing = fetch_existing_row(conn, target_date)

        if existing and not is_today and not force_refresh:
            log.info("Insights for historical date %s already exist. Cache hit.", target_date)
            return "exists"

        # Fetch data
        summary_data = fetch_daily_summary(conn, target_date)
        if not summary_data:
            log.info("No silver_arrivals data available for %s. Cannot generate insights.", target_date)
            return "no_data"

        # Capture the KPI snapshot BEFORE calling Claude. The prompt is
        # built from this exact dict, and the same dict is persisted on
        # the row, so the narrative and the dashboard KPI strip cannot
        # disagree. (Previously the snapshot was captured AFTER the API
        # call returned, ~30s later, by which time the 5-min ETL had
        # often rebuilt Silver and shifted the numbers — producing the
        # "narrative says 73.8%, KPI strip says 71.4%" mismatch.)
        kpi_snapshot = fetch_kpi_snapshot(conn, target_date)
        snapshot_json = json.dumps(kpi_snapshot, default=str)

        prompt = build_prompt(target_date, summary_data, kpi_snapshot, is_partial_day=is_today)

        # Call Claude
        log.info(
            "Calling Claude for daily report on %s (manual=%s, partial_day=%s) ...",
            target_date, manual, is_today,
        )
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_response = message.content[0].text
        log.info("Raw Claude response generated.")

        narrative, tweet, headline = parse_response(raw_response)

        # Defensive scrub: even though the prompt forbids Claude from
        # citing system-wide totals, Claude reliably ignores that rule
        # and produces a stats paragraph anyway, with hallucinated
        # numbers (saw 72.2% in prompt, wrote 73.8% in narrative). Detect
        # any paragraph that reads like a system-stats paragraph and drop
        # it — _looks_like_system_stats_paragraph picks up the giveaway
        # phrases ("system-wide", "vehicle pings", "tracked so far",
        # "across all routes"). The deterministic summary paragraph is
        # then inserted at position 1 (right after the hook), giving us
        # back the 4-paragraph structure with mathematically-correct
        # system numbers.
        narrative = _scrub_and_inject_summary(
            narrative, kpi_snapshot, is_partial_day=is_today
        )

        log.info("Stored Headline: %s", headline)

        with conn.cursor() as cur:
            if existing is None:
                cur.execute(
                    """
                    INSERT INTO ai_daily_insights
                        (report_date, narrative, tweet_draft, headline_text,
                         generation_count, kpi_snapshot)
                    VALUES (%s, %s, %s, %s, 1, %s)
                    """,
                    (target_date, narrative, tweet, headline, snapshot_json),
                )
                result = "generated"
            else:
                # Today + row exists: update in place. Increment count only for manual calls.
                if manual:
                    cur.execute(
                        """
                        UPDATE ai_daily_insights
                           SET narrative        = %s,
                               tweet_draft      = %s,
                               headline_text    = %s,
                               created_at       = NOW(),
                               generation_count = generation_count + 1,
                               kpi_snapshot     = %s
                         WHERE report_date      = %s
                        """,
                        (narrative, tweet, headline, snapshot_json, target_date),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE ai_daily_insights
                           SET narrative     = %s,
                               tweet_draft   = %s,
                               headline_text = %s,
                               created_at    = NOW(),
                               kpi_snapshot  = %s
                         WHERE report_date   = %s
                        """,
                        (narrative, tweet, headline, snapshot_json, target_date),
                    )
                result = "regenerated"

        conn.commit()
        log.info("Successfully stored daily insights for %s (%s)", target_date, result)
        return result
    except Exception:
        conn.rollback()
        log.exception("Failed to store daily insights")
        raise
    finally:
        conn.close()


def _run_if_idle() -> None:
    """End-of-day auto-trigger: generate today's digest once the buses stop.

    Uses dynamic detection — no fixed end-of-service time. Skips when:
      - Today is Sunday in ET (EMTA does not operate Sundays).
      - Today has fewer than 100 moving pings (buses haven't meaningfully run yet).
      - Any moving bus has been seen in the last 45 minutes.
      - An end-of-day digest was already generated within the last 2 hours
        (prevents every 5-min pipeline run from re-calling Claude).

    Idempotent with the manual regen path: uses --auto semantics (no counter bump).
    """
    if not SUPABASE_DB_URL or not ANTHROPIC_API_KEY:
        log.error("--if-idle: missing SUPABASE_DB_URL or ANTHROPIC_API_KEY.")
        return

    now_et = datetime.now(ET)
    if now_et.weekday() == 6:
        log.info("--if-idle: Sunday in ET — no EMTA service, skipping.")
        return

    conn = psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10)
    try:
        if not had_service_today(conn):
            log.info("--if-idle: fewer than 100 moving pings today, buses haven't started or feed is offline, skipping.")
            return
        if not is_service_idle(conn):
            log.info("--if-idle: buses still active in last 45 min, skipping.")
            return
        target_date = today_et()
        if digest_generated_recently(conn, target_date):
            log.info("--if-idle: digest for %s already generated within the last 2 hours, skipping.", target_date)
            return
    finally:
        conn.close()

    log.info("--if-idle: service idle and no recent digest. Generating end-of-day digest for %s.", target_date)
    generate_daily_insights(target_date, manual=False)


def main():
    parser = argparse.ArgumentParser(description="Generate daily AI transit analysis")
    parser.add_argument("--date", type=str, help="YYYY-MM-DD format. Defaults to yesterday (ET).")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Mark this as a cron/auto run: targets today (ET) by default, "
             "bypasses the manual daily cap, does not increment the counter.",
    )
    parser.add_argument(
        "--if-idle",
        dest="if_idle",
        action="store_true",
        help="Only generate if past 22:00 ET on a non-Sunday and buses have been "
             "idle for 30+ min. Designed to run from the 5-min ETL pipeline.",
    )
    args = parser.parse_args()

    if args.if_idle:
        _run_if_idle()
        return

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            log.error("Invalid date format. Use YYYY-MM-DD.")
            return
    elif args.auto:
        target_date = today_et()
    else:
        target_date = get_default_date()

    generate_daily_insights(target_date, manual=not args.auto)


if __name__ == "__main__":
    main()
