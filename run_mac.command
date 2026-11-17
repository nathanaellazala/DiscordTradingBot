#!/bin/bash
set -e
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  echo 'First install the dependencies using the README.'
  exit 1
fi
source .venv/bin/activate
python3 discord_webull_bot.py --web-monitor
