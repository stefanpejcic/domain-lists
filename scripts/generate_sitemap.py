#!/usr/bin/env python3
"""
Generate sitemap.xml from the data already produced by process_zones.py.

Standalone - run this from the terminal (or your own cron entry)
independently of the Flask app, e.g. after the daily zone processing
completes. Reads json/ for the list of TLDs and their last-updated
dates, and writes sitemap.xml to the repo root, where app.py's
/sitemap.xml route serves it as-is.

Usage: python3 scripts/generate_sitemap.py
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
JSON_FOLDER = REPO_ROOT / "json"
OUTPUT_FILE = REPO_ROOT / "sitemap.xml"

load_dotenv(REPO_ROOT / "website" / ".env")
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")

# Mirrors website/app.py's VALID_PATTERNS keys.
DELETED_PATTERNS = [
    "one-letter", "2-letters", "3-letters", "4-letters",
    "one-number", "two-numbers", "one-word",
]


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def iter_tlds():
    if not JSON_FOLDER.exists():
        return
    for folder in sorted(JSON_FOLDER.iterdir()):
        if not folder.is_dir() or folder.name in ("short_patterns", "2letter"):
            continue
        data = load_json(folder / "latest")
        if data and data.get("tld"):
            yield data


def main():
    if not SITE_URL:
        print(
            "Error: SITE_URL must be set in website/.env "
            "(e.g. SITE_URL=https://example.com)",
            file=sys.stderr,
        )
        return 1

    urls = []

    def add(path, lastmod=None, changefreq="daily", priority="0.5"):
        urls.append((path, lastmod, changefreq, priority))

    add("/", None, "daily", "1.0")
    add("/dropped", None, "daily", "0.8")
    add("/patterns", None, "daily", "0.6")
    add("/deleted", None, "daily", "0.5")

    for pattern in DELETED_PATTERNS:
        add(f"/deleted/{pattern}", None, "daily", "0.4")

    tld_count = 0
    for data in iter_tlds():
        tld = data["tld"]
        lastmod = (data.get("last_updated") or "")[:10] or None
        add(f"/tld/{tld}/all", lastmod, "daily", "0.6")
        tld_count += 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for path, lastmod, changefreq, priority in urls:
            f.write("  <url>\n")
            f.write(f"    <loc>{escape(SITE_URL + path)}</loc>\n")
            f.write(f"    <lastmod>{lastmod or today}</lastmod>\n")
            f.write(f"    <changefreq>{changefreq}</changefreq>\n")
            f.write(f"    <priority>{priority}</priority>\n")
            f.write("  </url>\n")
        f.write("</urlset>\n")

    print(f"Wrote {len(urls)} URLs ({tld_count} TLD pages) -> {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
