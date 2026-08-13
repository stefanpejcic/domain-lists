#!/bin/bash

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_DIR/venv/bin"

# 1. download zone files
"$VENV/python3" "$REPO_DIR/scripts/download_zones.py"

# 2. compare with previous date and geenerate lists
"$VENV/python" "$REPO_DIR/scripts/process_zones.py"

# 3. download tld info
#NO LONGER USED# "$VENV/python" "$REPO_DIR/scripts/tld_scrapper.py"

# todo: generate counters and distribution


# 4. cleanup (~9.6G)
rm -rf "$REPO_DIR/downloads/"

# 5. restart app
pkill -f "$VENV/python $REPO_DIR/website/app.py" 2>/dev/null; "$VENV/python" "$REPO_DIR/website/app.py" &
