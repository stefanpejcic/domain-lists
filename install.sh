#!/usr/bin/env bash
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

apt install -y python3-full python3-venv python3-pip

python3 -m venv "$REPO_DIR/venv"

"$REPO_DIR/venv/bin/pip" install --upgrade pip
"$REPO_DIR/venv/bin/pip" install flask
"$REPO_DIR/venv/bin/pip" install requests
"$REPO_DIR/venv/bin/pip" install python-dotenv

"$REPO_DIR/venv/bin/python" -c "import flask; print(flask.__version__)"

#test "$REPO_DIR/venv/bin/python" "$REPO_DIR/website/app.py"

# Add daily cron job at 03:05
CRON_JOB="5 3 * * * bash $REPO_DIR/daily.sh"

(crontab -l 2>/dev/null | grep -F "$CRON_JOB") || (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "DONE"

#pkill -f '/home/domain-lists/venv/bin/python /home/domain-lists/website/app.py' 2>/dev/null; /home/domain-lists/venv/bin/python /home/domain-lists/website/app.py &
