#!/bin/bash
set -e

cd "/Users/hugorabbit/Public Transport"
source venv/bin/activate

echo "--- $(date) ---"
python -m ingestion.fetch_realtime
python -m transform.silver --days-back 0.05
python -m transform.gold

# End-of-day daily digest. --if-idle is a no-op until buses have been
# idle for 30+ minutes, the day is non-Sunday, and no digest has been
# generated for today within the last 2 hours. Fires exactly once per
# service day from whichever cron tick first sees the idle window.
python -m ai_agent.daily_insights --if-idle
