#!/bin/bash
set -e

cd "/Users/hugorabbit/Public Transport"
source venv/bin/activate

echo "--- $(date) ---"
python -m ingestion.fetch_realtime
python -m transform.silver --days-back 0.05
python -m transform.gold
