#!/usr/bin/env bash
# Cron entrypoint. Logs to logs/monitor.log.
cd "$(dirname "$0")" || exit 1
mkdir -p logs
if [ -d .venv ]; then source .venv/bin/activate; fi
python3 monitor.py "$@" >> logs/monitor.log 2>&1
