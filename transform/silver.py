"""
silver.py — Transform bronze_vehicle_pings → silver_arrivals.

Reads raw pings, filters to on-route vehicles, classifies delay buckets,
and enriches with Eastern-time hour/day fields.

Usage:
    python -m transform.silver --days-back 1
"""

import argparse
import logging
from datetime import datetime, timezone, timedelta

import psycopg2

from ingestion.config import (
    SUPABASE_DB_URL,
    EARLY_THRESHOLD,
    ON_TIME_MAX,
    LATE_MAX,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

EASTERN = timezone(timedelta(hours=-4))  # EDT

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def classify_delay(minutes: int) -> str:
    """Classify adherence into a delay bucket."""
    if minutes < EARLY_THRESHOLD:
        return "early"
    elif minutes <= ON_TIME_MAX:
        return "on_time"
    elif minutes <= LATE_MAX:
        return "late"
    else:
        return "very_late"


SELECT_BRONZE = """
    SELECT vehicle_id, route_id, route_name, trip_id, direction,
           latitude, longitude, speed, adherence_minutes,
           display_status, is_on_route, observed_at
    FROM bronze_vehicle_pings
    WHERE observed_at >= %s
      AND is_on_route = TRUE
      AND adherence_minutes IS NOT NULL
    ORDER BY observed_at
"""

INSERT_SILVER = """
    INSERT INTO silver_arrivals (
        vehicle_id, route_id, route_name, trip_id, direction,
        latitude, longitude, speed, adherence_minutes,
        delay_bucket, display_status, is_on_route,
        observed_at, hour_of_day, day_of_week, day_name
    ) VALUES (
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s, %s
    )
"""

DELETE_SILVER_RANGE = """
    DELETE FROM silver_arrivals WHERE observed_at >= %s
"""


def main():
    parser = argparse.ArgumentParser(description="Bronze → Silver transform")
    parser.add_argument("--days-back", type=float, default=1.0,
                        help="Process bronze pings from the last N days (can be decimal, e.g. 0.05 for ~1 hour)")
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days_back)
    log.info("Processing bronze pings since %s", cutoff.isoformat())

    conn = psycopg2.connect(SUPABASE_DB_URL)
    try:
        with conn.cursor() as cur:
            # Read bronze
            cur.execute(SELECT_BRONZE, (cutoff,))
            bronze_rows = cur.fetchall()
            log.info("Read %d qualifying bronze rows", len(bronze_rows))

            if not bronze_rows:
                log.info("No rows to process. Done.")
                return

            # Build silver rows
            silver_rows = []
            for row in bronze_rows:
                (vehicle_id, route_id, route_name, trip_id, direction,
                 latitude, longitude, speed, adherence_minutes,
                 display_status, is_on_route, observed_at) = row

                delay_bucket = classify_delay(adherence_minutes)

                # Convert observed_at to Eastern for time fields
                observed_et = observed_at.astimezone(EASTERN)
                hour_of_day = observed_et.hour
                day_of_week = observed_et.weekday()  # 0=Monday (ISO)
                day_name = DAY_NAMES[day_of_week]

                silver_rows.append((
                    vehicle_id, route_id, route_name, trip_id, direction,
                    latitude, longitude, speed, adherence_minutes,
                    delay_bucket, display_status, is_on_route,
                    observed_at, hour_of_day, day_of_week, day_name,
                ))

            # Clear existing silver rows in this range, then re-insert (idempotent)
            cur.execute(DELETE_SILVER_RANGE, (cutoff,))
            log.info("Cleared existing silver rows since %s", cutoff.isoformat())

            cur.executemany(INSERT_SILVER, silver_rows)
            conn.commit()
            log.info("Inserted %d rows into silver_arrivals", len(silver_rows))

            # Log bucket distribution
            cur.execute("""
                SELECT delay_bucket, COUNT(*)
                FROM silver_arrivals
                WHERE observed_at >= %s
                GROUP BY delay_bucket
                ORDER BY COUNT(*) DESC
            """, (cutoff,))
            for bucket, count in cur.fetchall():
                log.info("  %s: %d", bucket, count)

    except Exception:
        conn.rollback()
        log.exception("Failed to transform silver_arrivals")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
