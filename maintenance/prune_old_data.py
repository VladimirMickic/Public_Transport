"""
Prune bronze / silver rows older than a retention window.

Why this exists: the free Supabase plan caps database size, and raw ping
storage in bronze/silver dominates disk use. Gold is an aggregate rewritten
every run (no time dimension to prune) and the AI insight tables are tiny
and deliberately preserved forever.

Behaviour:
  - Deletes bronze_vehicle_pings WHERE observed_at < now - N days
  - Deletes silver_arrivals      WHERE observed_at < now - N days
  - Leaves gold_route_reliability / ai_weekly_insights / ai_daily_insights untouched
  - Runs VACUUM-lite (ANALYZE) after deletes so Postgres updates its stats

CLI:
    python -m maintenance.prune_old_data                # default 7 days
    python -m maintenance.prune_old_data --days 14
    python -m maintenance.prune_old_data --dry-run

Schedule: Oracle VM crontab runs this once a day (see setup_vm.sh).
"""
from __future__ import annotations

import argparse
import logging
from typing import TypedDict

import psycopg2

from ingestion.config import SUPABASE_DB_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7


class PruneResult(TypedDict):
    bronze_deleted: int
    silver_deleted: int
    retention_days: int
    dry_run: bool


def prune_old_data(days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> PruneResult:
    """Delete rows older than `days` from bronze and silver tables."""
    if days < 1:
        raise ValueError("retention days must be >= 1")

    result: PruneResult = {
        "bronze_deleted": 0,
        "silver_deleted": 0,
        "retention_days": days,
        "dry_run": dry_run,
    }

    with psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            count_sql = (
                "SELECT COUNT(*) FROM {table} "
                "WHERE observed_at < NOW() - INTERVAL %s"
            )
            delete_sql = (
                "DELETE FROM {table} "
                "WHERE observed_at < NOW() - INTERVAL %s"
            )
            interval = f"{days} days"

            for table, key in (
                ("bronze_vehicle_pings", "bronze_deleted"),
                ("silver_arrivals", "silver_deleted"),
            ):
                cur.execute(count_sql.format(table=table), (interval,))
                n = cur.fetchone()[0]
                result[key] = n
                if dry_run:
                    logger.info("[dry-run] would delete %s rows from %s", n, table)
                    continue
                if n == 0:
                    logger.info("no rows older than %s days in %s", days, table)
                    continue
                cur.execute(delete_sql.format(table=table), (interval,))
                logger.info("deleted %s rows from %s", n, table)

        if not dry_run:
            conn.commit()
            # ANALYZE can't run in a transaction; open a new autocommit session.
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("ANALYZE bronze_vehicle_pings")
                cur.execute("ANALYZE silver_arrivals")
            logger.info("ANALYZE complete")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune old bronze/silver rows.")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_RETENTION_DAYS,
        help=f"Retention window in days (default {DEFAULT_RETENTION_DAYS}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count rows that would be deleted without deleting them.",
    )
    args = parser.parse_args()

    result = prune_old_data(days=args.days, dry_run=args.dry_run)
    prefix = "[dry-run] " if result["dry_run"] else ""
    logger.info(
        "%sdone — bronze=%s silver=%s (retention=%s days)",
        prefix,
        result["bronze_deleted"],
        result["silver_deleted"],
        result["retention_days"],
    )


if __name__ == "__main__":
    main()
