"""
ai_agent/insights.py — Generate weekly transit insights using Claude.

Queries gold_route_reliability for the past week, sends structured data
to Claude, and stores narrative + tweet + headline in ai_weekly_insights.

Usage:
    python -m ai_agent.insights
"""
from __future__ import annotations

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
    """Fetch aggregated reliability data for Claude's context."""
    sql = """
        SELECT route_name, day_name, hour_of_day,
               total_pings, on_time_pct, avg_adherence_minutes,
               reliability_score
        FROM gold_route_reliability
        WHERE total_pings >= 5
        ORDER BY reliability_score ASC
        LIMIT 40
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


def fetch_system_stats(conn):
    """Fetch high-level system stats for the narrative."""
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
        WHERE observed_at >= NOW() - INTERVAL '7 days'
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return cur.fetchone()


def build_prompt(gold_data, system_stats):
    """Build the Claude prompt with real data."""
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
Erie Metropolitan Transit Authority (EMTA) in Erie, PA.

Here is this week's system-wide performance:
- Total vehicle pings tracked: {stats.get('total_pings', 'N/A')}
- Active routes: {stats.get('routes', 'N/A')}
- System-wide on-time percentage: {stats.get('system_on_time_pct', 'N/A')}%
- Average delay: {stats.get('avg_delay', 'N/A')} minutes
- Very late incidents (>15 min): {stats.get('very_late_count', 'N/A')}

Here are the 40 worst-performing route/hour/day combinations (sorted by reliability score):

{data_block}

Write three outputs, separated by exact markers:

1. A 3-4 paragraph narrative analysis. You MUST mention at least three specific
   route numbers/names and at least two specific time windows (e.g., "Route 5
   at 8am on weekdays"). Identify patterns: which routes struggle, when, and
   any day-of-week trends. Be specific and data-driven, not generic.

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

    system_stats = fetch_system_stats(conn)
    prompt = build_prompt(gold_data, system_stats)

    # Call Claude
    log.info("Calling Claude for week of %s ...", week_start)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_response = message.content[0].text
    log.info("Raw Claude response:\n%s", raw_response)

    # Parse
    narrative, tweet, headline = parse_response(raw_response)
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
                    (week_start, narrative, tweet_draft, headline_text)
                VALUES (%s, %s, %s, %s)
                """,
                (week_start, narrative, tweet, headline),
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
