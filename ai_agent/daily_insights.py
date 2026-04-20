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

SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def get_default_date() -> date:
    """Return yesterday as the default date."""
    return date.today() - timedelta(days=1)


def already_generated(conn, report_date):
    """Check if insights for this date already exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ai_daily_insights WHERE report_date = %s",
            (report_date,),
        )
        return cur.fetchone() is not None


def fetch_daily_summary(conn, report_date):
    """Fetch daily aggregated reliability data from silver_arrivals."""
    sql = """
        SELECT 
            route_name,
            COUNT(*) AS total_pings,
            ROUND(COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0 / NULLIF(COUNT(*), 0), 2) AS on_time_pct,
            ROUND(AVG(adherence_minutes)::numeric, 2) AS avg_delay
        FROM silver_arrivals
        WHERE observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' >= %s::date
          AND observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' < (%s::date + INTERVAL '1 day')
        GROUP BY route_name
        HAVING COUNT(*) >= 5
        ORDER BY on_time_pct ASC
        LIMIT 40
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (report_date, report_date))
        return cur.fetchall()


def fetch_system_stats(conn, report_date):
    """Fetch high-level system stats for the daily narrative."""
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
        WHERE observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' >= %s::date
          AND observed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York' < (%s::date + INTERVAL '1 day')
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (report_date, report_date))
        return cur.fetchone()


def build_prompt(report_date, summary_data, system_stats):
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

    prompt = f"""You are a transit data analyst writing a daily reliability report for the
Erie Metropolitan Transit Authority (EMTA) in Erie, PA.

The report date is for yesterday: {report_date.strftime('%A, %B %d, %Y')}.

Here is the system-wide performance for this specific date:
- Total vehicle pings tracked: {stats.get('total_pings', 'N/A')}
- Active routes: {stats.get('routes', 'N/A')}
- System-wide on-time percentage: {stats.get('system_on_time_pct', 'N/A')}%
- Average delay: {stats.get('avg_delay', 'N/A')} minutes
- Very late incidents (>15 min): {stats.get('very_late_count', 'N/A')}

Here are the worst-performing routes from that day (sorted by worst On-Time Percentage):

{data_block}

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


def generate_daily_insights(target_date: date):
    """Fetch data, call Claude, store daily results."""
    if not SUPABASE_DB_URL or not ANTHROPIC_API_KEY:
        log.error("Missing SUPABASE_DB_URL or ANTHROPIC_API_KEY environment variables.")
        return

    conn = psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10)

    try:
        # Guard: don't run more than once per day
        if already_generated(conn, target_date):
            log.info("Insights for date %s already exist. Skipping.", target_date)
            return

        # Fetch data
        summary_data = fetch_daily_summary(conn, target_date)
        if not summary_data:
            log.info("No silver_arrivals data available for %s. Cannot generate insights.", target_date)
            return

        system_stats = fetch_system_stats(conn, target_date)
        prompt = build_prompt(target_date, summary_data, system_stats)

        # Call Claude
        log.info("Calling Claude for daily report on %s ...", target_date)
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_response = message.content[0].text
        log.info("Raw Claude response generated.")

        # Parse
        narrative, tweet, headline = parse_response(raw_response)
        log.info("Stored Headline: %s", headline)

        # Insert
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_daily_insights
                    (report_date, narrative, tweet_draft, headline_text)
                VALUES (%s, %s, %s, %s)
                """,
                (target_date, narrative, tweet, headline),
            )
        conn.commit()
        log.info("Successfully stored daily insights for %s", target_date)
    except Exception:
        conn.rollback()
        log.exception("Failed to store daily insights")
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Generate daily AI transit analysis")
    parser.add_argument("--date", type=str, help="YYYY-MM-DD format. Defaults to yesterday.")
    args = parser.parse_args()

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            log.error("Invalid date format. Use YYYY-MM-DD.")
            return
    else:
        target_date = get_default_date()

    generate_daily_insights(target_date)


if __name__ == "__main__":
    main()
