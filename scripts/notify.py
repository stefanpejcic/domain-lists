#!/usr/bin/env python3
"""Send a plain-text alert email via the local sendmail MTA.

Usage: notify.py <subject> <body>
"""
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / "website" / ".env")

ALERT_EMAIL = os.getenv("EMAIL", "stefan@pejcic.rs")
SENDMAIL = "/usr/sbin/sendmail"


def send_alert(subject, body):
    message = f"To: {ALERT_EMAIL}\nSubject: {subject}\n\n{body}\n"
    try:
        subprocess.run(
            [SENDMAIL, "-t"],
            input=message.encode("utf-8"),
            check=True,
            timeout=30,
        )
    except Exception as e:
        print(f"notify.py: failed to send alert email: {e}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: notify.py <subject> <body>", file=sys.stderr)
        sys.exit(1)
    send_alert(sys.argv[1], sys.argv[2])
