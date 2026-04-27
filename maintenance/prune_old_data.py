"""
Storage-driven pruning of bronze / silver rows.

Why this exists: Supabase's free tier caps database size at ~500 MB. Bronze
and silver pings dominate disk use; gold is a fixed-size aggregate and the
AI insight tables are tiny. The old behaviour was a flat "delete anything
older than 7 days" which threw away usable history even when 80% of the
500 MB tier was unused. This version only prunes when the database is
actually approaching the cap.

Behaviour:
  - Reads pg_database_size(current_database()).
  - If it's below the high-water mark (default 400 MB ≈ 80% of free tier),
    exits without touching anything.
  - If above, deletes the OLDEST one day of data from bronze and silver,
    runs ANALYZE, and exits. The cron runs daily, so the database catches
    up one day at a time over successive nights — predictable and safe.
  - Refuses to prune below a safety floor (default 2 days of history
    retained). Beyond that point the user must upgrade Supabase.
  - Gold / ai_weekly_insights / ai_daily_insights are never touched.

Note on disk reclamation: pg_database_size reports actual on-disk size,
which only shrinks after a VACUUM FULL (which takes an exclusive lock).
Routine DELETE + ANALYZE marks rows dead and lets autovacuum reclaim them
into free space inside existing pages over time. The intent here is rate-
limited steady-state pruning, not instant size reduction. If a one-shot
emergency reclaim is needed, run with --vacuum-full.

CLI:
    python -m maintenance.prune_old_data                 # storage-driven (default)
    python -m maintenance.prune_old_data --max-mb 350    # custom high-water mark
    python -m maintenance.prune_old_data --force-days 14 # delete older than 14d unconditionally
    python -m maintenance.prune_old_data --dry-run       # report only, no deletes
    python -m maintenance.prune_old_data --vacuum-full   # add VACUUM FULL after delete
"""
from __future__ import annotations

import argparse
import logging
from typing import TypedDict

import psycopg2

from ingestion.config import SUPABASE_DB_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Supabase free tier is 500 MB; start pruning at 80% utilisation.
DEFAULT_MAX_MB = 400
# Don't let the dashboard lose its recent-history view.
SAFETY_FLOOR_DAYS = 2


class PruneResult(TypedDict):
    db_size_mb: float
    max_mb: int
    pruned: bool
    bronze_deleted: int
    silver_deleted: int
    cutoff: str | None
    dry_run: bool
    reason: str


def _db_size_bytes(cur) -> int:
    cur.execute("SELECT pg_database_size(current_database())")
    return int(cur.fetchone()[0])


def _bronze_span_days(cur) -> float | None:
    """Days between oldest and newest bronze ping. None if table empty."""
    cur.execute(
        "SELECT EXTRACT(EPOCH FROM (MAX(observed_at) - MIN(observed_at))) / 86400.0 "
        "FROM bronze_vehicle_pings"
    )
    span = cur.fetchone()[0]
    return float(span) if span is not None else None


def prune_old_data(
    *,
    max_mb: int = DEFAULT_MAX_MB,
    force_days: int | None = None,
    dry_run: bool = False,
    vacuum_full: bool = False,
) -> PruneResult:
    """Storage-driven prune. Deletes one day from bronze/silver only when
    pg_database_size exceeds max_mb (or always, when force_days is set).

    Args:
        max_mb: high-water mark in MiB. Below this, no rows are deleted.
        force_days: if set, ignore the size check and delete rows older
                    than this many days (legacy behaviour, manual override).
        dry_run: report what would happen without committing.
        vacuum_full: run VACUUM FULL after delete to reclaim disk space.
                     Takes an exclusive lock; only use during quiet hours.
    """
    result: PruneResult = {
        "db_size_mb": 0.0,
        "max_mb": max_mb,
        "pruned": False,
        "bronze_deleted": 0,
        "silver_deleted": 0,
        "cutoff": None,
        "dry_run": dry_run,
        "reason": "",
    }

    with psycopg2.connect(SUPABASE_DB_URL, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            size_bytes = _db_size_bytes(cur)
            size_mb = size_bytes / (1024 * 1024)
            result["db_size_mb"] = round(size_mb, 1)
            logger.info("database size: %.1f MB (cap %d MB)", size_mb, max_mb)

            # Decide whether to prune and at what cutoff.
            if force_days is not None:
                if force_days < SAFETY_FLOOR_DAYS:
                    raise ValueError(
                        f"--force-days must be >= {SAFETY_FLOOR_DAYS} (safety floor)"
                    )
                cutoff_sql = "NOW() - INTERVAL %s"
                cutoff_param = (f"{force_days} days",)
                result["cutoff"] = f"NOW() - {force_days} days"
                result["reason"] = f"force-days={force_days}"
            else:
                if size_mb < max_mb:
                    result["reason"] = (
                        f"under cap ({size_mb:.1f} MB < {max_mb} MB) — nothing to prune"
                    )
                    logger.info(result["reason"])
                    return result

                span = _bronze_span_days(cur)
                if span is None or span <= SAFETY_FLOOR_DAYS:
                    result["reason"] = (
                        f"safety floor reached (bronze spans {span} days, floor "
                        f"{SAFETY_FLOOR_DAYS}) — upgrade Supabase or raise --max-mb"
                    )
                    logger.warning(result["reason"])
                    return result

                # Drop the oldest single day. Use bronze's actual MIN observed_at
                # so we delete a real day's worth even if data is sparse.
                cutoff_sql = (
                    "(SELECT MIN(observed_at) FROM bronze_vehicle_pings) "
                    "+ INTERVAL '1 day'"
                )
                cutoff_param = None
                result["cutoff"] = "MIN(observed_at) + 1 day"
                result["reason"] = (
                    f"over cap ({size_mb:.1f} MB >= {max_mb} MB) — pruning oldest day"
                )
                logger.info(result["reason"])

            for table, key in (
                ("bronze_vehicle_pings", "bronze_deleted"),
                ("silver_arrivals", "silver_deleted"),
            ):
                count_q = (
                    f"SELECT COUNT(*) FROM {table} WHERE observed_at < {cutoff_sql}"
                )
                delete_q = (
                    f"DELETE FROM {table} WHERE observed_at < {cutoff_sql}"
                )
                if cutoff_param:
                    cur.execute(count_q, cutoff_param)
                else:
                    cur.execute(count_q)
                n = cur.fetchone()[0]
                result[key] = n
                if dry_run:
                    logger.info("[dry-run] would delete %s rows from %s", n, table)
                    continue
                if n == 0:
                    logger.info("no qualifying rows in %s", table)
                    continue
                if cutoff_param:
                    cur.execute(delete_q, cutoff_param)
                else:
                    cur.execute(delete_q)
                logger.info("deleted %s rows from %s", n, table)

            result["pruned"] = (
                not dry_run
                and (result["bronze_deleted"] > 0 or result["silver_deleted"] > 0)
            )

        if not dry_run and result["pruned"]:
            conn.commit()
            # ANALYZE / VACUUM FULL can't run in a transaction.
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("ANALYZE bronze_vehicle_pings")
                cur.execute("ANALYZE silver_arrivals")
                logger.info("ANALYZE complete")
                if vacuum_full:
                    logger.info("running VACUUM FULL (exclusive lock)…")
                    cur.execute("VACUUM FULL bronze_vehicle_pings")
                    cur.execute("VACUUM FULL silver_arrivals")
                    new_size_mb = _db_size_bytes(cur) / (1024 * 1024)
                    logger.info(
                        "VACUUM FULL complete — size now %.1f MB", new_size_mb
                    )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Storage-driven prune of bronze/silver rows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Default: prune one day of oldest data ONLY when database size\n"
            "exceeds --max-mb. With --force-days, behave like the legacy\n"
            "time-window prune."
        ),
    )
    parser.add_argument(
        "--max-mb", type=int, default=DEFAULT_MAX_MB,
        help=(
            f"High-water mark in MiB (default {DEFAULT_MAX_MB}). "
            f"Pruning starts only when pg_database_size exceeds this."
        ),
    )
    parser.add_argument(
        "--force-days", type=int, default=None,
        help=(
            "Manual override: delete rows older than N days regardless of "
            "database size. Must be >= safety floor "
            f"({SAFETY_FLOOR_DAYS})."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count rows that would be deleted without deleting them.",
    )
    parser.add_argument(
        "--vacuum-full", action="store_true",
        help=(
            "After deleting, run VACUUM FULL to reclaim disk space "
            "immediately (takes an exclusive lock — quiet hours only)."
        ),
    )
    args = parser.parse_args()

    result = prune_old_data(
        max_mb=args.max_mb,
        force_days=args.force_days,
        dry_run=args.dry_run,
        vacuum_full=args.vacuum_full,
    )
    prefix = "[dry-run] " if result["dry_run"] else ""
    logger.info(
        "%sdone — size=%.1f MB cap=%d MB pruned=%s bronze=%s silver=%s (%s)",
        prefix,
        result["db_size_mb"],
        result["max_mb"],
        result["pruned"],
        result["bronze_deleted"],
        result["silver_deleted"],
        result["reason"],
    )


if __name__ == "__main__":
    main()
