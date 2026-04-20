"""
fetch_realtime.py — Ingest vehicle pings from EMTA Avail API into bronze_vehicle_pings.

Usage:
    python -m ingestion.fetch_realtime
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone, timedelta

import psycopg2
import requests

from ingestion.config import (
    SUPABASE_DB_URL,
    VEHICLES_URL,
    ROUTES_URL,
    SERVICE_START_HOUR,
    SERVICE_END_HOUR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Timezone ─────────────────────────────────────────────
EASTERN = timezone(timedelta(hours=-4))  # EDT; swap to -5 for EST
# For production, use zoneinfo.ZoneInfo("America/New_York") on Python 3.9+


def is_service_hours() -> bool:
    """Return True if current Eastern time is within EMTA service window."""
    now_et = datetime.now(EASTERN)
    return SERVICE_START_HOUR <= now_et.hour < SERVICE_END_HOUR


def parse_dotnet_date(date_str):
    """
    Parse .NET JSON date format: /Date(1678886400000-0400)/
    Returns a timezone-aware UTC datetime, or None if unparseable.
    """
    if not date_str:
        return None
    match = re.search(r"/Date\((\d+)([+-]\d{4})?\)/", date_str)
    if not match:
        return None
    epoch_ms = int(match.group(1))
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)


def fetch_vehicles():
    """GET all vehicles from Avail API. Returns list of vehicle dicts."""
    log.info("Fetching vehicles from %s", VEHICLES_URL)
    resp = requests.get(VEHICLES_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # Handle both plain list and {"Data": [...]} wrapper
    if isinstance(data, dict) and "Data" in data:
        data = data["Data"]

    log.info("Received %d vehicles", len(data))
    return data


def fetch_route_names():
    """GET all routes and return {route_id: route_long_name} mapping."""
    log.info("Fetching route names from %s", ROUTES_URL)
    resp = requests.get(ROUTES_URL, timeout=15)
    resp.raise_for_status()
    routes = resp.json()
    route_map = {}
    for r in routes:
        route_map[r["RouteId"]] = r.get("LongName") or r.get("ShortName", "Unknown")
    log.info("Loaded %d route names", len(route_map))
    return route_map


def vehicle_to_row(v, route_map):
    """
    Map one Avail vehicle JSON object to a bronze_vehicle_pings row tuple.
    Each field is annotated with the actual Avail API field name.
    """
    route_id = v.get("RouteId")             # Avail field: RouteId
    trip_id = v.get("TripId")               # Avail field: TripId

    return (
        v.get("VehicleId"),                  # Avail field: VehicleId
        route_id,                            # Avail field: RouteId
        route_map.get(route_id),             # Looked up from /Routes/GetAllRoutes
        trip_id,                             # Avail field: TripId
        v.get("Direction"),                  # Avail field: Direction (short code, e.g. "NbI")
        v.get("Latitude"),                   # Avail field: Latitude
        v.get("Longitude"),                  # Avail field: Longitude
        v.get("Heading"),                    # Avail field: Heading (degrees)
        v.get("Speed"),                      # Avail field: Speed (GPS speed)
        v.get("Deviation"),                  # Avail field: Deviation (minutes, positive=late)
        v.get("DisplayStatus"),              # Avail field: DisplayStatus ("On Time", "Late")
        v.get("OpStatus"),                   # Avail field: OpStatus (machine-readable)
        route_id is not None and trip_id is not None,  # Derived: is_on_route
        v.get("Destination"),                # Avail field: Destination
        v.get("LastStop"),                   # Avail field: LastStop
        v.get("CommStatus"),                 # Avail field: CommStatus ("GOOD", etc.)
        v.get("Name"),                       # Avail field: Name (bus display name)
        parse_dotnet_date(v.get("LastUpdated")),  # Avail field: LastUpdated (.NET date)
        json.dumps(v),                       # Full raw JSON for debugging
    )


INSERT_SQL = """
    INSERT INTO bronze_vehicle_pings (
        vehicle_id, route_id, route_name, trip_id, direction,
        latitude, longitude, heading, speed, adherence_minutes,
        display_status, op_status, is_on_route, destination, last_stop,
        comm_status, vehicle_name, last_updated_api, raw_json
    ) VALUES (
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s
    )
"""


def main():
    if not is_service_hours():
        log.info("Outside EMTA service hours (%d:00–%d:00 ET). Skipping.",
                 SERVICE_START_HOUR, SERVICE_END_HOUR)
        return

    # 1. Fetch route name lookup
    route_map = fetch_route_names()

    # 2. Fetch vehicles
    vehicles = fetch_vehicles()
    if not vehicles:
        log.info("No vehicles returned (possibly outside service hours). Nothing to insert.")
        return

    # 3. Map to rows
    rows = [vehicle_to_row(v, route_map) for v in vehicles]
    log.info("Mapped %d vehicles to bronze rows", len(rows))

    # 4. Batch insert into bronze
    conn = psycopg2.connect(SUPABASE_DB_URL)
    try:
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, rows)
        conn.commit()
        log.info("Inserted %d rows into bronze_vehicle_pings", len(rows))
    except Exception:
        conn.rollback()
        log.exception("Failed to insert into bronze_vehicle_pings")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
