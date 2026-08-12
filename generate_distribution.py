#!/usr/bin/env python3

import gzip
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ==============================================================
# Configuration
# ==============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

LISTS_FOLDER = SCRIPT_DIR / "lists"
JSON_FOLDER = SCRIPT_DIR / "json"

TIMEZONE_NAME = "Europe/Belgrade"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

LOG_FILE = SCRIPT_DIR / "pattern_counters_log.txt"

NOW = datetime.now(TIMEZONE)
TODAY = NOW.strftime("%Y-%m-%d")

OUTPUT_FILE = JSON_FOLDER / "pattern_counters.json"


# ==============================================================
# Logging
# ==============================================================

def log(msg):
    timestamp = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"

    print(line, flush=True)

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ==============================================================
# Pattern Definitions
# ==============================================================

PATTERNS = {
    "C": {
        "category": "English letters, numbers",
        "character_count": 1,
        "characters": 36,
        "maximum": 36,
    },

    "CC": {
        "category": "English letters, numbers",
        "character_count": 2,
        "characters": 36,
        "maximum": 36 ** 2,
    },

    "CCC": {
        "category": "English letters, numbers",
        "character_count": 3,
        "characters": 36,
        "maximum": 36 ** 3,
    },

    "CCCC": {
        "category": "English letters, numbers",
        "character_count": 4,
        "characters": 36,
        "maximum": 36 ** 4,
    },

    "N": {
        "category": "Numbers",
        "character_count": 1,
        "characters": 10,
        "maximum": 10,
    },

    "NN": {
        "category": "Numbers",
        "character_count": 2,
        "characters": 10,
        "maximum": 10 ** 2,
    },

    "NNN": {
        "category": "Numbers",
        "character_count": 3,
        "characters": 10,
        "maximum": 10 ** 3,
    },

    "NNNN": {
        "category": "Numbers",
        "character_count": 4,
        "characters": 10,
        "maximum": 10 ** 4,
    },

    "L": {
        "category": "English letters",
        "character_count": 1,
        "characters": 26,
        "maximum": 26,
    },

    "LL": {
        "category": "English letters",
        "character_count": 2,
        "characters": 26,
        "maximum": 26 ** 2,
    },

    "LLL": {
        "category": "English letters",
        "character_count": 3,
        "characters": 26,
        "maximum": 26 ** 3,
    },

    "LLLL": {
        "category": "English letters",
        "character_count": 4,
        "characters": 26,
        "maximum": 26 ** 4,
    },
}


# ==============================================================
# Domain Helpers
# ==============================================================

def get_sld(domain):
    """
    Extract the part before the first dot.

    Example:
        example.com -> example
        abc.xyz     -> abc
    """

    domain = domain.strip().lower()

    if not domain:
        return ""

    return domain.split(".", 1)[0]


def classify_sld(sld):
    """
    Return all matching patterns for an SLD.

    A domain can belong to multiple patterns.

    Example:
        abc -> CCC + LLL
        123 -> CCC + NNN
        a1  -> CC
    """

    if not sld:
        return []

    length = len(sld)

    if length == 1:
        if sld.isalpha() and sld.isascii():
            return ["C", "L"]

        if sld.isdigit():
            return ["C", "N"]

        return []

    if length == 2:
        if sld.isascii() and sld.isalnum():
            patterns = ["CC"]

            if sld.isalpha():
                patterns.append("LL")
            elif sld.isdigit():
                patterns.append("NN")

            return patterns

        return []

    if length == 3:
        if sld.isascii() and sld.isalnum():
            patterns = ["CCC"]

            if sld.isalpha():
                patterns.append("LLL")
            elif sld.isdigit():
                patterns.append("NNN")

            return patterns

        return []

    if length == 4:
        if sld.isascii() and sld.isalnum():
            patterns = ["CCCC"]

            if sld.isalpha():
                patterns.append("LLLL")
            elif sld.isdigit():
                patterns.append("NNNN")

            return patterns

        return []

    return []


# ==============================================================
# Process TLD
# ==============================================================

def process_tld(domain_file):
    """
    Process one TLD domain list and return counters.
    """

    counts = {
        pattern: 0
        for pattern in PATTERNS
    }

    total_domains = 0

    log(f"Processing {domain_file.name}...")

    try:
        with gzip.open(
            domain_file,
            "rt",
            encoding="utf-8",
            errors="replace"
        ) as f:

            for line in f:
                domain = line.strip()

                if not domain:
                    continue

                total_domains += 1

                sld = get_sld(domain)

                for pattern in classify_sld(sld):
                    counts[pattern] += 1

    except Exception as e:
        log(f"ERROR processing {domain_file}: {e}")
        return None

    return {
        "domains": total_domains,
        "patterns": counts,
    }


# ==============================================================
# Build JSON
# ==============================================================

def main():

    start_time = time.time()

    date_folder = LISTS_FOLDER / TODAY
    domains_folder = date_folder / "domains"

    if not domains_folder.exists():
        log(f"ERROR: Domains folder not found: {domains_folder}")
        return 1

    JSON_FOLDER.mkdir(parents=True, exist_ok=True)

    log("==============================================")
    log("Generating Domain Pattern Counters")
    log(f"Date: {TODAY}")
    log(f"Source: {domains_folder}")
    log(f"Output: {OUTPUT_FILE}")
    log("==============================================")

    domain_files = sorted(
        f for f in domains_folder.glob("*.txt.gz")
        if f.name != "all.txt.gz"
    )

    if not domain_files:
        log("ERROR: No .txt.gz domain files found.")
        return 1

    log(f"Found {len(domain_files):,} TLD files.")

    tlds = {}

    # ----------------------------------------------------------
    # Process every TLD
    # ----------------------------------------------------------

    for index, domain_file in enumerate(domain_files, 1):

        tld = domain_file.name

        if tld.endswith(".txt.gz"):
            tld = tld[:-7]

        result = process_tld(domain_file)

        if result is None:
            continue

        patterns = {}

        for pattern, definition in PATTERNS.items():

            registered = result["patterns"][pattern]
            maximum = definition["maximum"]

            available = max(0, maximum - registered)

            patterns[pattern] = {
                "registered": registered,
                "available": available,
                "maximum": maximum,
            }

        tlds[tld] = {
            "domains": result["domains"],
            "patterns": patterns,
        }

        log(
            f"[{index:,}/{len(domain_files):,}] "
            f"{tld}: {result['domains']:,} domains"
        )

    # ----------------------------------------------------------
    # Create JSON
    # ----------------------------------------------------------

    json_data = {
        "date": TODAY,
        "last_updated": datetime.now(TIMEZONE).isoformat(
            timespec="seconds"
        ),
        "timezone": TIMEZONE_NAME,

        "patterns": PATTERNS,

        "tlds": tlds,
    }

    # Atomic write
    tmp_file = Path(str(OUTPUT_FILE) + ".tmp")

    with open(
        tmp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            json_data,
            f,
            indent=2,
            ensure_ascii=False
        )

        f.write("\n")

    os.replace(tmp_file, OUTPUT_FILE)

    elapsed = time.time() - start_time

    log("==============================================")
    log(f"Saved -> {OUTPUT_FILE}")
    log(f"TLDs processed: {len(tlds):,}")
    log(f"Completed in {elapsed:.2f} seconds.")
    log("==============================================")

    return 0


if __name__ == "__main__":
    sys.exit(main())
