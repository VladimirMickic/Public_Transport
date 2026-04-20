"""
Configuration for EMTA transit pipeline.
All secrets read from environment variables — nothing hardcoded.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Database ─────────────────────────────────────────────
SUPABASE_DB_URL = os.environ["SUPABASE_DB_URL"]

# ── Avail InfoPoint API ──────────────────────────────────
API_BASE = "https://emta.availtec.com/InfoPoint/rest"
VEHICLES_URL = f"{API_BASE}/Vehicles/GetAllVehicles"
ROUTES_URL = f"{API_BASE}/Routes/GetAllRoutes"

# ── Service hours (Eastern) ──────────────────────────────
SERVICE_START_HOUR = 5   # 5 AM ET
SERVICE_END_HOUR = 23    # 11 PM ET

# ── Delay bucket thresholds (minutes) ────────────────────
EARLY_THRESHOLD = -1       # < -1 min  → early
ON_TIME_MAX = 5            # ≤ 5 min   → on_time
LATE_MAX = 15              # ≤ 15 min  → late
                           # > 15 min  → very_late
