#!/usr/bin/env bash
# Double-click me to start the Telegram poll fetcher.
# Reads the bot token from .env in this folder.

set -e
cd "$(dirname "$0")"

# Load .env (lines like KEY=value)
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN not set. Check .env in this folder."
  read -p "Press Enter to close…"
  exit 1
fi

# One-time dependency install
python3 -c "import telegram" 2>/dev/null || {
  echo "→ Installing python-telegram-bot…"
  pip3 install "python-telegram-bot[ext]>=21" --break-system-packages
}

echo "→ Starting bot. Add @BadmintonShareBot to your Telegram group first."
echo "→ Polls and votes will be saved to: $(pwd)/polls.json"
echo "→ Press Ctrl-C to stop."
echo

python3 fetch_telegram_polls.py --output polls.json
