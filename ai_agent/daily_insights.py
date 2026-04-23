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
    """Return True when no moving buses have been seen in the last 30 minutes.

    Used by --if-idle to detect end-of-service day automatically.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM bronze_vehicle_pings
            WHERE observed_at >= NOW() - INTERVAL '30 minutes'
              AND speed > 2
        """)
        return cur.fetchone()[0] == 0


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

    Captures system-wide totals, an hourly OTP arc, and top-3 worst routes
    so archived digests keep rendering after silver_arrivals is pruned.
    """
    # System KPIs
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
            WHERE (observed_at AT TIME ZONE 'America/New_York')::date = %s
              AND speed > %s
            """,
            (report_date, MOVING_SPEED_MPH),
        )
        kpi = _clean_row(dict(cur.fetchone() or {}))

    # Hourly OTP arc
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
                   GREATEST(0, ROUND(
                       (100 - LEAST(100, AVG(LEAST(30, ABS(adherence_minutes))) * 10))::numeric, 1
                   )) AS reliability,
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
            ORDER BY AVG(ABS(adherence_minutes)) DESC
            LIMIT 3
            """,
            (report_date, MOVING_SPEED_MPH),
        )
        worst_routes = [_clean_row(dict(r)) for r in cur.fetchall()]

    return {
        **kpi,
        "hourly_arc": hourly_arc,
        "worst_routes": worst_routes,
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

    Only includes pings where the bus is moving (speed > MOVING_SPEED_MPH),
    which naturally bounds the window from first to last active ride.
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
        GROUP BY route_name
        HAVING COUNT(*) >= 5
        ORDER BY avg_delay DESC
        LIMIT 40
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (report_date, report_date, MOVING_SPEED_MPH))
        return cur.fetchall()


def fetch_system_stats(conn, report_date):
    """Fetch high-level system stats for the daily narrative.

    Moving-buses only; first/last active ride defines the reporting window.
    """
    sql = """
        SELECT
            COUNT(*) AS total_pings,
            COUNT(DISTINCT route_id) AS routes,
            ROUND(AVG(adherence_minutes)::numeric, 1) AS avg_delay,
            ROUND(
                COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0
                / NULLIF(COUNT(*), 0), 1
            ) AS system_on_time_pct,
            COUNT(*) FILTER (WHERE delay_bucket = 'very_late') AS very_late_count,
            MIN(observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York') AS first_ride,
            MAX(observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York') AS last_ride
        FROM silver_arrivals
        WHERE observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' >= %s::date
          AND observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' < (%s::date + INTERVAL '1 day')
          AND speed > %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (report_date, report_date, MOVING_SPEED_MPH))
        return cur.fetchone()


def build_prompt(report_date, summary_data, system_stats, is_partial_day=False):
    """Build the Claude prompt with real daily data."""
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

    stats = system_stats or {}

    first_ride = stats.get("first_ride")
    last_ride = stats.get("last_ride")
    window_line = ""
    if first_ride and last_ride:
        window_line = (
            f"- Service window (first to last moving bus): "
            f"{first_ride.strftime('%H:%M')} – {last_ride.strftime('%H:%M')} ET\n"
        )

    partial_line = ""
    tense_guidance = (
        "Write in past tense; the service day is complete."
    )
    if is_partial_day:
        now_et = datetime.now(ET).strftime("%H:%M")
        partial_line = (
            f"- Data through: {now_et} ET (PARTIAL DAY — service is still active; "
            f"numbers reflect pings collected so far today).\n"
        )
        tense_guidance = (
            "This is a partial-day snapshot. Write in present/progressive tense "
            "(\"so far today\", \"as of HH:MM ET\"). Do NOT write as if the day is over, "
            "and do NOT project or predict end-of-day totals."
        )

    prompt = f"""You are a transit data analyst writing a daily reliability report for the
Erie Metropolitan Transit Authority (EMTA) in Erie, PA. Your audience is EMTA
leadership and engaged riders who want real numbers, not corporate filler.

The report date is: {report_date.strftime('%A, %B %d, %Y')}.

Here is the system-wide performance for this specific date (moving buses only):
- Total vehicle pings tracked: {stats.get('total_pings', 'N/A')}
- Active routes: {stats.get('routes', 'N/A')}
- System-wide on-time percentage: {stats.get('system_on_time_pct', 'N/A')}%
- Average delay: {stats.get('avg_delay', 'N/A')} minutes
- Very late incidents (>15 min): {stats.get('very_late_count', 'N/A')}
{window_line}{partial_line}

Here are the worst-performing routes from that day (sorted by Average Delay descending):

{data_block}

{tense_guidance}

Write three outputs, separated by exact markers:

1. A 4-paragraph narrative analysis.
   - Paragraph 1 (hook): One or two sentences that pull the reader in immediately.
     Lead with the single most striking number or fact from this day. Short and
     punchy. No throat-clearing, no "Today's report covers...", no date recap.
   - Paragraph 2 (executive summary): System-wide context. Was it a strong,
     mixed, or poor day? Give the key numbers and what they mean for a rider.
   - Paragraph 3 (route-level): You MUST name at least three specific route
     numbers/names in **bold**. Highlight the most significant anomalies. If a
     route shows a wildly negative average delay (e.g. -100+ min), flag it as a
     likely data-reporting issue rather than real early arrivals.
   - Paragraph 4 (insight): What do these patterns suggest about operational
     friction or rider experience? Concrete observations, not vague hope.

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

2. After the marker ---TWEET--- on its own line, write a single tweet (≤280
   characters) summarizing the key finding. Include one specific route and
   one specific number. No hashtags.

3. After the marker ---HEADLINE--- on its own line, write a one-sentence
   attention-grabbing headline (≤100 characters, no hashtags) suitable for an
   email subject line. It should make someone want to click.

Start the narrative immediately — no preamble or title."""

    return prompt


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


def generate_daily_insights(target_date: date, manual: bool = True) -> str:
    """Fetch data, call Claude, store daily results.

    Rules:
    - Historical date (< today ET): cache-first. If a row exists, return "exists".
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

        if existing and not is_today:
            log.info("Insights for historical date %s already exist. Cache hit.", target_date)
            return "exists"

        # Fetch data
        summary_data = fetch_daily_summary(conn, target_date)
        if not summary_data:
            log.info("No silver_arrivals data available for %s. Cannot generate insights.", target_date)
            return "no_data"

        system_stats = fetch_system_stats(conn, target_date)
        prompt = build_prompt(target_date, summary_data, system_stats, is_partial_day=is_today)

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
        log.info("Stored Headline: %s", headline)

        kpi_snapshot = fetch_kpi_snapshot(conn, target_date)
        snapshot_json = json.dumps(kpi_snapshot, default=str)

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

    Skips when:
      - It is before 22:00 ET (service may still be running).
      - Today is Sunday in ET (EMTA does not operate Sundays).
      - Any moving bus has been seen in the last 30 minutes.

    Idempotent with the manual regen path: reuses --auto semantics (no counter
    bump) and relies on the existing unique key on (report_date).
    """
    if not SUPABASE_DB_URL or not ANTHROPIC_API_KEY:
        log.error("--if-idle: missing SUPABASE_DB_URL or ANTHROPIC_API_KEY.")
        return

    now_et = datetime.now(ET)
    # Python weekday: Monday=0 … Sunday=6
    if now_et.weekday() == 6:
        log.info("--if-idle: Sunday in ET — no EMTA service, skipping.")
        return
    if now_et.hour < 22:
        log.info("--if-idle: before 22:00 ET (%s), skipping.", now_et.strftime("%H:%M"))
        return

    conn = psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10)
    try:
        if not is_service_idle(conn):
            log.info("--if-idle: buses still active in last 30 min, skipping.")
            return
    finally:
        conn.close()

    target_date = today_et()
    log.info("--if-idle: service idle after 22:00 ET. Generating digest for %s.", target_date)
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
