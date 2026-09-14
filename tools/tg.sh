#!/usr/bin/env bash
# Send a progress note from this project to the user's Telegram home channel.
# Reads TELEGRAM_BOT_TOKEN / TELEGRAM_HOME_CHANNEL from ~/.hermes/.env (never echoed).
# Usage: tools/tg.sh "text"      |      tools/tg.sh -f file.txt
set -euo pipefail
set -a; . /root/.hermes/.env; set +a
CHAT="${TELEGRAM_CHAT_ID:-$TELEGRAM_HOME_CHANNEL}"
if [ "${1:-}" = "-f" ]; then
  curl -sS --max-time 30 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${CHAT}" -d "disable_web_page_preview=true" --data-urlencode "text@$2"
else
  curl -sS --max-time 30 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${CHAT}" -d "disable_web_page_preview=true" --data-urlencode "text=$1"
fi
echo
