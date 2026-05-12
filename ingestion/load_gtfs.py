"""
load_gtfs.py — Download EMTA's GTFS feed and refresh gtfs_shapes / gtfs_trips.

Used by the dashboard's Route Corridor map to draw each route's actual
road geometry. Without GTFS the corridor is invisible on long sparse
routes (16, 105, 261) because ping density falls below the readability
floor.

The feed is published at the same Avail server as the realtime API:
    https://emta.availtec.com/InfoPoint/GTFS-Zip.ashx

This script is idempotent — it truncates and reloads the two GTFS tables
on every run. Run it manually after schedule changes, or wire it into a
weekly cron alongside the existing pipeline.

Usage:
    python -m ingestion.load_gtfs
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile

import psycopg2
from psycopg2.extras import execute_values
import requests

from ingestion.config import SUPABASE_DB_URL

GTFS_URL = "https://emta.availtec.com/InfoPoint/GTFS-Zip.ashx"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def fetch_gtfs_zip() -> zipfile.ZipFile:
    log.info("Downloading GTFS from %s", GTFS_URL)
    resp = requests.get(GTFS_URL, timeout=30)
    resp.raise_for_status()
    log.info("Got %d bytes", len(resp.content))
    return zipfile.ZipFile(io.BytesIO(resp.content))


def parse_shapes(zf: zipfile.ZipFile) -> list[tuple]:
    with zf.open("shapes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
        rows = []
        for r in reader:
            try:
                rows.append((
                    r["shape_id"],
                    int(r["shape_pt_sequence"]),
                    float(r["shape_pt_lat"]),
                    float(r["shape_pt_lon"]),
                ))
            except (KeyError, ValueError, TypeError):
                continue
    log.info("Parsed %d shape points", len(rows))
    return rows


def parse_trips(zf: zipfile.ZipFile) -> list[tuple]:
    with zf.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
        rows = []
        for r in reader:
            try:
                direction = r.get("direction_id")
                direction_int = int(direction) if direction not in (None, "") else None
                rows.append((
                    r["route_id"],
                    r["trip_id"],
                    r.get("shape_id") or None,
                    direction_int,
                ))
            except (KeyError, ValueError, TypeError):
                continue
    log.info("Parsed %d trip rows", len(rows))
    return rows


def parse_stops(zf: zipfile.ZipFile) -> list[tuple]:
    with zf.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
        rows = []
        for r in reader:
            try:
                rows.append((
                    r["stop_id"],
                    r.get("stop_name") or None,
                    float(r["stop_lat"]),
                    float(r["stop_lon"]),
                ))
            except (KeyError, ValueError, TypeError):
                continue
    log.info("Parsed %d stops", len(rows))
    return rows


def main():
    zf = fetch_gtfs_zip()
    shapes = parse_shapes(zf)
    trips = parse_trips(zf)
    stops = parse_stops(zf)

    # Supabase enforces a session statement_timeout on the pooler. Plain
    # SET (no LOCAL) persists across statements within this connection.
    # autocommit lets DELETE / SET behave like one-shot calls so a slow
    # bulk insert can't roll back the table clear. execute_values batches
    # thousands of rows per round trip — ~100x faster than executemany
    # for bulk loads.
    conn = psycopg2.connect(SUPABASE_DB_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '120000'")
            cur.execute("DELETE FROM gtfs_shapes")
            cur.execute("DELETE FROM gtfs_trips")
            cur.execute("DELETE FROM gtfs_stops")
            execute_values(
                cur,
                "INSERT INTO gtfs_shapes (shape_id, shape_pt_sequence, "
                "shape_pt_lat, shape_pt_lon) VALUES %s",
                shapes,
                page_size=5000,
            )
            execute_values(
                cur,
                "INSERT INTO gtfs_trips (route_id, trip_id, shape_id, "
                "direction_id) VALUES %s",
                trips,
                page_size=2000,
            )
            execute_values(
                cur,
                "INSERT INTO gtfs_stops (stop_id, stop_name, stop_lat, "
                "stop_lon) VALUES %s",
                stops,
                page_size=2000,
            )
        log.info("Loaded %d shape points, %d trips, %d stops",
                 len(shapes), len(trips), len(stops))
    except Exception:
        log.exception("Failed to load GTFS")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
