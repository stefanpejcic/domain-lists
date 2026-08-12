#!/usr/bin/env bash
set -e

apt install -y python3-full python3-venv python3-pip

python3 -m venv /home/venv

/home/venv/bin/pip install --upgrade pip
/home/venv/bin/pip install flask

/home/venv/bin/python -c "import flask; print(flask.__version__)"

#test /home/venv/bin/python app.py

# Add daily cron job at 03:05
CRON_JOB="5 3 * * * bash /home/daily.sh"

(crontab -l 2>/dev/null | grep -F "$CRON_JOB") || (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "DONE"
