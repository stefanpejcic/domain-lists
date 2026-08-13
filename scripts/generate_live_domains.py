#!/usr/bin/env python3
"""
Cross-reference today's trending keyword domains (lists/<date>/keywords/,
produced by generate_keyword_lists.py) against a self-hosted Certificate
Transparency log API to flag domains that are both newly registered AND
already have a live HTTPS certificate - a much stronger signal than
registration alone, since most newly registered domains sit dormant for
weeks before anything is deployed on them.

Scoped to the keyword lists (a few hundred domains/day) rather than every
new domain (which can be hundreds of thousands/day) to keep the number of
CT API calls reasonable - one GET request per candidate domain.

Requires CT_LOGS in website/.env, pointing at the base URL of the
self-hosted CT API. Uses its GET /domain/<name> endpoint, which returns
every CT log entry seen for that exact domain (or an empty list if none).

Standalone - run this from the terminal (or your own cron entry) after
generate_keyword_lists.py.

Usage: python3 scripts/generate_live_domains.py
"""
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
LISTS_FOLDER = REPO_ROOT / "lists"
JSON_FOLDER = REPO_ROOT / "json"
KEYWORDS_CONFIG = REPO_ROOT / "keywords.json"

TIMEZONE_NAME = "Europe/Belgrade"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

load_dotenv(REPO_ROOT / "website" / ".env")
CT_LOGS = os.getenv("CT_LOGS", "").rstrip("/")

REQUEST_TIMEOUT = 10


def load_keywords():
    try:
        with open(KEYWORDS_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading {KEYWORDS_CONFIG}: {e}", file=sys.stderr)
        sys.exit(1)

    keywords = [k.strip().lower() for k in data.get("keywords", []) if k.strip()]
    if not keywords:
        print(f"Error: no keywords configured in {KEYWORDS_CONFIG}", file=sys.stderr)
        sys.exit(1)
    return keywords


def latest_date_with_keywords():
    if not LISTS_FOLDER.exists():
        return None
    dates = [
        d.name for d in LISTS_FOLDER.iterdir()
        if d.is_dir() and (d / "keywords").is_dir()
    ]
    return sorted(dates, reverse=True)[0] if dates else None


def load_domain_list(path):
    domains = []
    if not path.exists():
        return domains
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            domain = line.strip()
            if domain:
                domains.append(domain)
    return domains


def has_live_cert(domain):
    """True if the CT API has ever seen a certificate for this exact domain."""
    resp = requests.get(f"{CT_LOGS}/domain/{domain}", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return bool(resp.json())


def write_domain_list(domains, out_file):
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out_file) + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write("\n".join(sorted(domains)))
        if domains:
            f.write("\n")
    os.replace(tmp, out_file)


def main():
    if not CT_LOGS:
        print(
            "Error: CT_LOGS must be set in website/.env "
            "(e.g. CT_LOGS=https://ct.example.com)",
            file=sys.stderr,
        )
        return 1

    keywords = load_keywords()

    date = latest_date_with_keywords()
    if date is None:
        print(
            "Error: no lists/<date>/keywords/ found. Run generate_keyword_lists.py first.",
            file=sys.stderr,
        )
        return 1

    keywords_folder = LISTS_FOLDER / date / "keywords"
    out_folder = LISTS_FOLDER / date / "live"

    counts = {}
    all_live = set()

    for keyword in keywords:
        candidates = load_domain_list(keywords_folder / f"{keyword}.txt.gz")

        live = []
        for domain in candidates:
            try:
                if has_live_cert(domain):
                    live.append(domain)
            except requests.RequestException as e:
                print(f"{keyword}: CT lookup failed for {domain} - {e}", file=sys.stderr)

        counts[keyword] = len(live)
        all_live.update(live)

        out_file = out_folder / f"{keyword}.txt.gz"
        write_domain_list(live, out_file)
        print(f"{keyword}: {len(live):,}/{len(candidates):,} candidates have a live cert -> {out_file}")

    all_file = out_folder / "all.txt.gz"
    write_domain_list(all_live, all_file)
    print(f"all: {len(all_live):,} live domains -> {all_file}")

    updated_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")

    summary = {
        "date": date,
        "keywords": counts,
        "total": len(all_live),
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    }

    JSON_FOLDER.mkdir(parents=True, exist_ok=True)
    summary_file = JSON_FOLDER / "live.json"
    tmp = Path(str(summary_file) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, summary_file)

    print(f"Summary -> {summary_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
