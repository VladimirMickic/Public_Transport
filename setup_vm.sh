#!/bin/bash
# Run this on your Oracle Cloud VM after SSH-ing in.
# Usage: bash setup_vm.sh
set -e

REPO_URL="https://github.com/VladimirMickic/Public_Transport.git"
PROJECT_DIR="$HOME/Public_Transport"

echo "=== 1. Updating system packages ==="
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git

echo "=== 2. Cloning repo ==="
if [ -d "$PROJECT_DIR" ]; then
    echo "Directory already exists, pulling latest..."
    git -C "$PROJECT_DIR" pull
else
    git clone "$REPO_URL" "$PROJECT_DIR"
fi

echo "=== 3. Setting up Python virtual environment ==="
cd "$PROJECT_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "Dependencies installed."

echo "=== 4. Creating .env file ==="
echo "Enter your secrets (input is hidden):"

read -p "SUPABASE_DB_URL: " -s SUPABASE_DB_URL; echo
read -p "ANTHROPIC_API_KEY: " -s ANTHROPIC_API_KEY; echo

cat > "$PROJECT_DIR/.env" <<EOF
SUPABASE_DB_URL=$SUPABASE_DB_URL
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
EOF
chmod 600 "$PROJECT_DIR/.env"
echo ".env written."

echo "=== 5. Setting up crontab ==="
PYTHON="$PROJECT_DIR/venv/bin/python"
LOG="$HOME/pipeline.log"
DIGEST_LOG="$HOME/digest.log"
PRUNE_LOG="$HOME/prune.log"

# Oracle Cloud VMs default to UTC. All cron expressions below assume UTC.
# ET = UTC-4 (EDT, Mar–Nov) or UTC-5 (EST, Nov–Mar). Where wall-clock
# matters (end-of-day digest, weekly digest), the cron is set in UTC such
# that it fires at the desired ET moment during EDT (the longer half of
# the year); EST drifts the trigger by exactly one hour, which the
# digest scripts handle internally via their own ET clock.

# Every 5 min — ETL pipeline (bronze → silver → gold) plus the
# end-of-day daily-digest auto-trigger. --if-idle is a no-op until
# buses have been idle for 30+ min, the day is non-Sunday, and no
# digest exists for today within the last 2 hours; it fires exactly
# once per service day from whichever cron tick first sees the idle
# window. Wall-clock floor at 23:00 ET guarantees firing even if the
# Avail feed keeps echoing stale speeds past midnight.
CRON_LINE="*/5 * * * * cd $PROJECT_DIR && $PYTHON -m ingestion.fetch_realtime >> $LOG 2>&1 && $PYTHON -m transform.silver --days-back 0.05 >> $LOG 2>&1 && $PYTHON -m transform.gold >> $LOG 2>&1 && $PYTHON -m ai_agent.daily_insights --if-idle >> $DIGEST_LOG 2>&1"

# Daily 03:50 UTC (= 23:50 EDT / 22:50 EST) — backup daily digest run.
# --auto targets today (ET) regardless of activity, bypasses the manual
# regeneration cap, and UPDATEs in place without bumping the counter.
# Belt-and-braces: if --if-idle missed the window for any reason
# (cron skip, transient DB error, very-late buses), this guarantees a
# digest exists by midnight ET. If --if-idle already ran in the last
# couple of hours it just overwrites with the same numbers — idempotent.
DIGEST_BACKUP_LINE="50 3 * * * cd $PROJECT_DIR && $PYTHON -m ai_agent.daily_insights --auto >> $DIGEST_LOG 2>&1"

# Sunday 12:00 UTC (= 08:00 EDT / 07:00 EST) — weekly insights for the
# Mon–Sun week that just closed. After Saturday's full service is in
# silver_arrivals and before Monday morning. Idempotent: skips if a row
# for this week_start already exists.
WEEKLY_LINE="0 12 * * 0 cd $PROJECT_DIR && $PYTHON -m ai_agent.insights >> $DIGEST_LOG 2>&1"

# Daily at 03:15 UTC — storage-driven prune. The script checks
# pg_database_size and only deletes the oldest one day of bronze/silver
# rows when the database exceeds 400 MB (≈80% of the 500 MB Supabase
# free tier). Below that threshold it exits as a no-op, so usable
# history is preserved as long as there is room. Gold aggregates and
# AI insight tables are never touched.
PRUNE_LINE="15 3 * * * cd $PROJECT_DIR && $PYTHON -m maintenance.prune_old_data >> $PRUNE_LOG 2>&1"

# Strip previous EMTA-related entries (idempotent re-runs of setup_vm.sh)
# before re-adding the current lines. Match the script module names so
# we catch every variant of the cron line that has ever shipped.
(crontab -l 2>/dev/null \
    | grep -v "fetch_realtime" \
    | grep -v "run_pipeline" \
    | grep -v "ai_agent.daily_insights" \
    | grep -v "ai_agent.insights" \
    | grep -v "maintenance.prune_old_data"; \
 echo "$CRON_LINE"; \
 echo "$DIGEST_BACKUP_LINE"; \
 echo "$WEEKLY_LINE"; \
 echo "$PRUNE_LINE") | crontab -

echo "Crontab installed:"
crontab -l

echo ""
echo "=== Setup complete! ==="
echo "Pipeline + daily digest auto-trigger run every 5 minutes. Logs: $LOG, $DIGEST_LOG"
echo "Daily digest backup runs 03:50 UTC. Weekly digest runs Sunday 12:00 UTC."
echo "Prune runs daily at 03:15 UTC. Logs at: $PRUNE_LOG"
echo "To test manually: cd $PROJECT_DIR && source venv/bin/activate && python -m ingestion.fetch_realtime"
echo "To force the daily digest now: source venv/bin/activate && python -m ai_agent.daily_insights --auto"
echo "To force the weekly digest now: source venv/bin/activate && python -m ai_agent.insights"
