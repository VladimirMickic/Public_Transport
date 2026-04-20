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

CRON_LINE="*/5 * * * * cd $PROJECT_DIR && $PYTHON -m ingestion.fetch_realtime >> $LOG 2>&1 && $PYTHON -m transform.silver --days-back 1 >> $LOG 2>&1 && $PYTHON -m transform.gold >> $LOG 2>&1"

(crontab -l 2>/dev/null | grep -v "fetch_realtime"; echo "$CRON_LINE") | crontab -
echo "Crontab installed:"
crontab -l

echo ""
echo "=== Setup complete! ==="
echo "Pipeline runs every 5 minutes. Logs at: $LOG"
echo "To test manually: cd $PROJECT_DIR && source venv/bin/activate && python -m ingestion.fetch_realtime"
