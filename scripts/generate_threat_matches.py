#!/usr/bin/env python3
"""
Cross-reference today's newly registered domains (lists/<date>/new/all.txt.gz,
produced by process_zones.py) against public threat-intel feeds to flag
domains that are newly registered AND already known-malicious.

Feeds used (both free, public, no API key required):
- OpenPhish community feed:
  https://raw.githubusercontent.com/openphish/public_feed/main/feed.txt
- URLhaus plain-text feed:
  https://urlhaus.abuse.ch/downloads/text/

Matching: each feed entry is a full URL. We extract its hostname and walk
its dot-suffixes (evil.sub.example.co.uk -> example.co.uk -> co.uk...)
against the set of today's new domains, taking the first (most specific)
hit. This avoids needing a public-suffix-list - if "example.co.uk" is
what actually got registered, that's exactly what's in our own domain
set, so no TLD-structure guessing is required.

Standalone - run this from the terminal (or your own cron entry) after
process_zones.py has produced today's new/all.txt.gz.

Note: these are third-party feeds and can have false positives - the
matched list is a lead to investigate, not a verified verdict.

Usage: python3 scripts/generate_threat_matches.py
"""
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
LISTS_FOLDER = REPO_ROOT / "lists"
JSON_FOLDER = REPO_ROOT / "json"

TIMEZONE_NAME = "Europe/Belgrade"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

FEEDS = {
    "openphish": "https://raw.githubusercontent.com/openphish/public_feed/main/feed.txt",
    "urlhaus": "https://urlhaus.abuse.ch/downloads/text/",
}

HEADERS = {"User-Agent": "domain-lists-threat-check/1.0"}
REQUEST_TIMEOUT = 30


def fetch_feed(url):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def extract_hostnames(feed_text):
    hosts = set()
    for line in feed_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            host = urlparse(line).hostname
        except ValueError:
            continue
        if host:
            hosts.add(host.lower().rstrip("."))
    return hosts


def latest_date_with_new_domains():
    if not LISTS_FOLDER.exists():
        return None
    dates = [
        d.name for d in LISTS_FOLDER.iterdir()
        if d.is_dir() and (d / "new" / "all.txt.gz").exists()
    ]
    return sorted(dates, reverse=True)[0] if dates else None


def load_new_domains(date):
    path = LISTS_FOLDER / date / "new" / "all.txt.gz"
    domains = set()
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            domain = line.strip()
            if domain:
                domains.add(domain)
    return domains


def match_domain(hostname, domain_set):
    """Walk dot-suffixes of hostname; return the first that's a known new domain."""
    labels = hostname.split(".")
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        if candidate in domain_set:
            return candidate
    return None


def write_domain_list(domains, out_file):
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out_file) + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write("\n".join(sorted(domains)))
        if domains:
            f.write("\n")
    os.replace(tmp, out_file)


def main():
    date = latest_date_with_new_domains()
    if date is None:
        print(
            "Error: no lists/<date>/new/all.txt.gz found. Run process_zones.py first.",
            file=sys.stderr,
        )
        return 1

    new_domains = load_new_domains(date)
    print(f"Loaded {len(new_domains):,} newly registered domains for {date}")

    out_folder = LISTS_FOLDER / date / "threats"

    matches_by_source = {}
    all_matches = set()

    for source, url in FEEDS.items():
        try:
            feed_text = fetch_feed(url)
        except Exception as e:
            print(f"{source}: FAILED to fetch feed - {e}", file=sys.stderr)
            matches_by_source[source] = set()
            continue

        hostnames = extract_hostnames(feed_text)
        print(f"{source}: {len(hostnames):,} hostnames in feed")

        matched = set()
        for host in hostnames:
            hit = match_domain(host, new_domains)
            if hit:
                matched.add(hit)

        matches_by_source[source] = matched
        all_matches |= matched

        out_file = out_folder / f"{source}.txt.gz"
        write_domain_list(matched, out_file)
        print(f"{source}: {len(matched):,} matches -> {out_file}")

    all_file = out_folder / "all.txt.gz"
    write_domain_list(all_matches, all_file)
    print(f"all: {len(all_matches):,} matches -> {all_file}")

    updated_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")

    summary = {
        "date": date,
        "sources": {source: len(matched) for source, matched in matches_by_source.items()},
        "total": len(all_matches),
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    }

    JSON_FOLDER.mkdir(parents=True, exist_ok=True)
    summary_file = JSON_FOLDER / "threats.json"
    tmp = Path(str(summary_file) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, summary_file)

    print(f"Summary -> {summary_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
