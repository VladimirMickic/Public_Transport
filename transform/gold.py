"""
gold.py — Aggregate silver_arrivals → gold_route_reliability.

Computes per-route, per-hour, per-day-of-week reliability scores.

Usage:
    python -m transform.gold
"""

import logging
from datetime import datetime, timezone

import psycopg2

from ingestion.config import SUPABASE_DB_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


UPSERT_GOLD = """
    INSERT INTO gold_route_reliability (
        route_id, route_name, hour_of_day, day_of_week, day_name,
        total_pings, on_time_count, early_count, late_count, very_late_count,
        on_time_pct, avg_adherence_minutes, reliability_score, computed_at
    )
    SELECT
        route_id,
        route_name,
        hour_of_day,
        day_of_week,
        day_name,
        COUNT(*)                                                    AS total_pings,
        COUNT(*) FILTER (WHERE delay_bucket = 'on_time')            AS on_time_count,
        COUNT(*) FILTER (WHERE delay_bucket = 'early')              AS early_count,
        COUNT(*) FILTER (WHERE delay_bucket = 'late')               AS late_count,
        COUNT(*) FILTER (WHERE delay_bucket = 'very_late')          AS very_late_count,
        ROUND(
            COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0 / COUNT(*), 2
        )                                                           AS on_time_pct,
        ROUND(AVG(adherence_minutes)::numeric, 2)                   AS avg_adherence_minutes,
        -- Reliability score: 70% on-time pct + 30% delay penalty
        -- Uses |AVG(adherence)| (capped at 15 min) so running ~10 min early
        -- is penalised like running ~10 min late — both miss the schedule.
        ROUND(
            (COUNT(*) FILTER (WHERE delay_bucket = 'on_time') * 100.0 / COUNT(*)) * 0.7
            + (1.0 - LEAST(ABS(AVG(adherence_minutes)), 15) / 15.0) * 100 * 0.3,
            2
        )                                                           AS reliability_score,
        NOW()                                                       AS computed_at
    FROM silver_arrivals
    GROUP BY route_id, route_name, hour_of_day, day_of_week, day_name
    ON CONFLICT (route_id, hour_of_day, day_of_week)
    DO UPDATE SET
        route_name            = EXCLUDED.route_name,
        day_name              = EXCLUDED.day_name,
        total_pings           = EXCLUDED.total_pings,
        on_time_count         = EXCLUDED.on_time_count,
        early_count           = EXCLUDED.early_count,
        late_count            = EXCLUDED.late_count,
        very_late_count       = EXCLUDED.very_late_count,
        on_time_pct           = EXCLUDED.on_time_pct,
        avg_adherence_minutes = EXCLUDED.avg_adherence_minutes,
        reliability_score     = EXCLUDED.reliability_score,
        computed_at           = EXCLUDED.computed_at
"""


def main():
    log.info("Aggregating silver_arrivals → gold_route_reliability")

    conn = psycopg2.connect(SUPABASE_DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(UPSERT_GOLD)
            conn.commit()
            log.info("Upserted gold_route_reliability (%d rows affected)", cur.rowcount)

            # Spot-check: log top routes by reliability
            cur.execute("""
                SELECT route_name, on_time_pct, reliability_score
                FROM gold_route_reliability
                ORDER BY reliability_score DESC
                LIMIT 10
            """)
            results = cur.fetchall()
            if results:
                log.info("Top routes by reliability_score:")
                for name, otp, score in results:
                    log.info("  %-30s on_time=%.1f%%  score=%.1f", name, otp, score)
            else:
                log.info("No gold rows produced (silver_arrivals may be empty).")

    except Exception:
        conn.rollback()
        log.exception("Failed to aggregate gold_route_reliability")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
