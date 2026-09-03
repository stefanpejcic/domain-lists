#!/usr/bin/env bash
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

apt install -y python3-full python3-venv python3-pip

python3 -m venv "$REPO_DIR/venv"

"$REPO_DIR/venv/bin/pip" install --upgrade pip
"$REPO_DIR/venv/bin/pip" install -r requirements.txt

"$REPO_DIR/venv/bin/python" -c "import flask; print(flask.__version__)"

# Build the production Tailwind CSS (downloads the standalone CLI on first run)
bash "$REPO_DIR/scripts/build_css.sh"

# Add twice daily cron
CRON_JOB="5 3,15 * * * bash $REPO_DIR/cron.sh"
(crontab -l 2>/dev/null | grep -F "$CRON_JOB") || (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "DONE"

#pkill -f '/home/domain-lists/venv/bin/python /home/domain-lists/website/app.py' 2>/dev/null; /home/domain-lists/venv/bin/python /home/domain-lists/website/app.py &
