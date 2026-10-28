#!/usr/bin/env python3
"""Send a plain-text alert email.

Uses the local sendmail MTA when installed, otherwise SMTP to SMTP_HOST
(default: host.containers.internal:25, i.e. the host's mail server as seen
from inside the container).

Usage: notify.py <subject> <body>
"""
import os
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / "website" / ".env")

ALERT_EMAIL = os.getenv("EMAIL") or "stefan@pejcic.rs"
SENDER_EMAIL = os.getenv("ALERT_FROM") or ALERT_EMAIL
SMTP_HOST = os.getenv("SMTP_HOST") or "host.containers.internal"
SMTP_PORT = int(os.getenv("SMTP_PORT") or 25)
HELO_NAME = "domain-lists.pejcic.rs"
SENDMAIL = "/usr/sbin/sendmail"


def send_alert(subject, body):
    message = EmailMessage()
    message["To"] = ALERT_EMAIL
    message["From"] = SENDER_EMAIL
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=HELO_NAME)
    message.set_content(body)

    try:
        if shutil.which(SENDMAIL):
            subprocess.run(
                [SENDMAIL, "-t"],
                input=message.as_bytes(),
                check=True,
                timeout=30,
            )
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, local_hostname=HELO_NAME, timeout=30) as smtp:
                smtp.send_message(message)
    except Exception as e:
        print(f"notify.py: failed to send alert email: {e}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: notify.py <subject> <body>", file=sys.stderr)
        sys.exit(1)
    send_alert(sys.argv[1], sys.argv[2])
