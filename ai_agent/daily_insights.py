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
        ORDER BY on_time_pct ASC
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
Erie Metropolitan Transit Authority (EMTA) in Erie, PA.

The report date is: {report_date.strftime('%A, %B %d, %Y')}.

Here is the system-wide performance for this specific date (moving buses only):
- Total vehicle pings tracked: {stats.get('total_pings', 'N/A')}
- Active routes: {stats.get('routes', 'N/A')}
- System-wide on-time percentage: {stats.get('system_on_time_pct', 'N/A')}%
- Average delay: {stats.get('avg_delay', 'N/A')} minutes
- Very late incidents (>15 min): {stats.get('very_late_count', 'N/A')}
{window_line}{partial_line}

Here are the worst-performing routes from that day (sorted by worst On-Time Percentage):

{data_block}

{tense_guidance}

Write three outputs, separated by exact markers:

1. A 3 paragraph narrative analysis. You MUST mention at least three specific
   route numbers/names. Identify patterns from this specific day. Was the system particularly
   late? Be specific and data-driven, not generic.

2. After the marker ---TWEET--- on its own line, write a single tweet (≤280
   characters) summarizing the key finding. Include one specific route and
   one specific number. No hashtags.

3. After the marker ---HEADLINE--- on its own line, write a one-sentence
   headline (≤100 characters, no hashtags) suitable for an email subject line.

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

        with conn.cursor() as cur:
            if existing is None:
                cur.execute(
                    """
                    INSERT INTO ai_daily_insights
                        (report_date, narrative, tweet_draft, headline_text, generation_count)
                    VALUES (%s, %s, %s, %s, 1)
                    """,
                    (target_date, narrative, tweet, headline),
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
                               generation_count = generation_count + 1
                         WHERE report_date      = %s
                        """,
                        (narrative, tweet, headline, target_date),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE ai_daily_insights
                           SET narrative     = %s,
                               tweet_draft   = %s,
                               headline_text = %s,
                               created_at    = NOW()
                         WHERE report_date   = %s
                        """,
                        (narrative, tweet, headline, target_date),
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


def main():
    parser = argparse.ArgumentParser(description="Generate daily AI transit analysis")
    parser.add_argument("--date", type=str, help="YYYY-MM-DD format. Defaults to yesterday (ET).")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Mark this as a cron/auto run: targets today (ET) by default, "
             "bypasses the manual daily cap, does not increment the counter.",
    )
    args = parser.parse_args()

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
