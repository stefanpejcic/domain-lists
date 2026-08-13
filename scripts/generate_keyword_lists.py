#!/usr/bin/env python3
"""
Filter today's newly registered domains (lists/<date>/new/all.txt.gz,
produced by process_zones.py) into per-keyword lists, e.g. "AI domains
registered today".

Matching is a simple case-insensitive substring match against the
domain name with its TLD label stripped off (so a .ai domain doesn't
automatically match the "ai" keyword just because of its TLD). This is
a discovery tool, not a precision filter - it will have some false
positives (e.g. "domain" contains "ai"), the same way similar keyword
lists elsewhere do; browsing/searching the result is expected.

Standalone - run this from the terminal (or your own cron entry) after
process_zones.py has produced today's new/all.txt.gz.

Keywords are configured in keywords.json at the repo root - edit that
file to add/remove tracked keywords, no code changes needed.

Usage: python3 scripts/generate_keyword_lists.py
"""
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
LISTS_FOLDER = REPO_ROOT / "lists"
JSON_FOLDER = REPO_ROOT / "json"
KEYWORDS_CONFIG = REPO_ROOT / "keywords.json"

TIMEZONE_NAME = "Europe/Belgrade"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)


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


def latest_date_with_new_domains():
    if not LISTS_FOLDER.exists():
        return None
    dates = [
        d.name for d in LISTS_FOLDER.iterdir()
        if d.is_dir() and (d / "new" / "all.txt.gz").exists()
    ]
    return sorted(dates, reverse=True)[0] if dates else None


def strip_tld(domain):
    labels = domain.split(".")
    return ".".join(labels[:-1]) if len(labels) > 1 else domain


def main():
    keywords = load_keywords()

    date = latest_date_with_new_domains()
    if date is None:
        print(
            "Error: no lists/<date>/new/all.txt.gz found. Run process_zones.py first.",
            file=sys.stderr,
        )
        return 1

    source_file = LISTS_FOLDER / date / "new" / "all.txt.gz"
    out_folder = LISTS_FOLDER / date / "keywords"
    out_folder.mkdir(parents=True, exist_ok=True)

    matches = {kw: [] for kw in keywords}

    with gzip.open(source_file, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            domain = line.strip()
            if not domain:
                continue
            stripped = strip_tld(domain).lower()
            for kw in keywords:
                if kw in stripped:
                    matches[kw].append(domain)

    counts = {}
    for kw in keywords:
        domains = sorted(matches[kw])
        counts[kw] = len(domains)

        out_file = out_folder / f"{kw}.txt.gz"
        tmp = Path(str(out_file) + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write("\n".join(domains))
            if domains:
                f.write("\n")
        os.replace(tmp, out_file)

        print(f"{kw}: {len(domains):,} matches -> {out_file}")

    updated_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")

    summary = {
        "date": date,
        "keywords": counts,
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    }

    JSON_FOLDER.mkdir(parents=True, exist_ok=True)
    summary_file = JSON_FOLDER / "keywords.json"
    tmp = Path(str(summary_file) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, summary_file)

    print(f"Summary -> {summary_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
