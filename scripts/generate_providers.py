#!/usr/bin/env python3
"""
Combine every TLD's latest json/<tld>/providers/<date>.json (written by
process_zones.py) into json/providers.json, which the /providers pages and
the API read:

    totals     domains, dnssec, new, new_parked, moved over all TLDs
    providers  the biggest providers (and today's biggest movers), each with
               its counts and its largest TLDs
    flows      the biggest provider-to-provider moves ("from>to": count)

Providers are the labels process_zones.py derives from nameserver hosts;
providers.json at the repo root gives them display names.

Usage: python3 scripts/generate_providers.py
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
JSON_FOLDER = REPO_ROOT / "json"
OUTPUT_FILE = JSON_FOLDER / "providers.json"

TIMEZONE_NAME = "Europe/Belgrade"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

TOP_PROVIDERS = 300
TOP_MOVERS = 50
TOP_FLOWS = 300
TOP_TLDS_PER_PROVIDER = 30
COUNTERS = ("domains", "new", "new_parked", "gained", "lost", "deleted", "dnssec")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def main():
    providers = {}
    by_tld = {}
    flows = {}
    totals = {"tlds": 0, "domains": 0, "dnssec": 0, "new": 0, "new_parked": 0, "moved": 0}
    newest = None

    for folder in sorted(JSON_FOLDER.iterdir()):
        if not folder.is_dir():
            continue
        latest = load_json(folder / "latest")
        if not latest or not latest.get("date"):
            continue
        data = load_json(folder / "providers" / f"{latest['date']}.json")
        if not data:
            continue

        tld = latest.get("tld", folder.name)
        totals["tlds"] += 1
        for key in ("domains", "dnssec", "new", "new_parked", "moved"):
            totals[key] += int(latest.get(key, 0) or 0)
        newest = max(newest or "", latest["date"])

        for label, counts in data.get("providers", {}).items():
            p = providers.setdefault(label, dict.fromkeys(COUNTERS, 0) | {"tlds": 0})
            for key in COUNTERS:
                p[key] += int(counts.get(key, 0) or 0)
            if counts.get("domains"):
                p["tlds"] += 1
                by_tld.setdefault(label, {})[tld] = int(counts["domains"])

        for flow, count in data.get("flows", {}).items():
            flows[flow] = flows.get(flow, 0) + int(count)

    if not providers:
        print("No json/<tld>/providers/ data yet - run process_zones.py first.", file=sys.stderr)
        return 1

    keep = set(sorted(providers, key=lambda p: providers[p]["domains"], reverse=True)[:TOP_PROVIDERS])
    for key in ("new", "gained", "lost"):
        keep.update(sorted(providers, key=lambda p: providers[p][key], reverse=True)[:TOP_MOVERS])

    out_providers = {}
    for label in sorted(keep, key=lambda p: providers[p]["domains"], reverse=True):
        tlds = by_tld.get(label, {})
        top = sorted(tlds.items(), key=lambda kv: kv[1], reverse=True)[:TOP_TLDS_PER_PROVIDER]
        out_providers[label] = {**providers[label], "by_tld": dict(top)}

    output = {
        "date": newest,
        "totals": totals,
        "providers": out_providers,
        "flows": dict(sorted(flows.items(), key=lambda kv: kv[1], reverse=True)[:TOP_FLOWS]),
        "last_updated": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "timezone": TIMEZONE_NAME,
    }

    tmp = Path(str(OUTPUT_FILE) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, OUTPUT_FILE)

    print(f"{len(out_providers):,} providers, {len(output['flows']):,} flows -> {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
