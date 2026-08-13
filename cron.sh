#!/bin/bash

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_DIR/venv/bin"

notify() {
    "$VENV/python3" "$REPO_DIR/scripts/notify.py" "$1" "$2"
}

# 1. download zone files
"$VENV/python3" "$REPO_DIR/scripts/download_zones.py"
if [ $? -ne 0 ]; then
    notify "domain-lists: download_zones.py failed" "$(tail -n 50 "$REPO_DIR/icann_download_log.txt" 2>/dev/null)"
fi

# 2. compare with previous date and geenerate lists
"$VENV/python" "$REPO_DIR/scripts/process_zones.py"
if [ $? -ne 0 ]; then
    notify "domain-lists: process_zones.py failed" "$(tail -n 50 "$REPO_DIR/zone_processing_log.txt" 2>/dev/null)"
fi

# 3. download tld info
#NO LONGER USED# "$VENV/python" "$REPO_DIR/scripts/tld_scrapper.py"

# todo: generate counters and distribution

# 4. cleanup (~9.6G)
rm -rf "$REPO_DIR/downloads/"

# 5. restart app
pkill -f "$VENV/python $REPO_DIR/website/app.py" 2>/dev/null; "$VENV/python" "$REPO_DIR/website/app.py" &
