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

LOG_FILE = SCRIPT_DIR / "pattern_processing_log.txt"

NOW = datetime.now(TIMEZONE)
TODAY = NOW.strftime("%Y-%m-%d")


def log(msg):
    timestamp = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ==============================================================
# Domain Pattern Matchers
# ==============================================================

def get_sld(domain: str) -> str:
    """Extracts the Second-Level Domain (SLD) part before the first dot."""
    domain = domain.strip().lower()
    if not domain:
        return ""
    return domain.split(".")[0]


def is_one_letter(sld: str) -> bool:
    return len(sld) == 1 and sld.isalpha()


def is_two_letters(sld: str) -> bool:
    return len(sld) == 2 and sld.isalpha()


def is_three_letters(sld: str) -> bool:
    return len(sld) == 3 and sld.isalpha()


def is_four_letters(sld: str) -> bool:
    return len(sld) == 4 and sld.isalpha()


def is_one_number(sld: str) -> bool:
    return len(sld) == 1 and sld.isdigit()


def is_two_numbers(sld: str) -> bool:
    # NN format (exactly two digits, e.g., '07', '99')
    return len(sld) == 2 and sld.isdigit()


def is_one_word(sld: str) -> bool:
    # Pure alphabetic SLD of any length
    return sld.isalpha()


PATTERN_MATCHERS = {
    "one-letter": is_one_letter,
    "2-letters": is_two_letters,
    "3-letters": is_three_letters,
    "4-letters": is_four_letters,
    "one-number": is_one_number,
    "two-numbers": is_two_numbers,
    "one-word": is_one_word,
}


# ==============================================================
# Extraction Logic
# ==============================================================

def extract_pattern(target_date: str, pattern_key: str, matcher_func) -> int:
    date_folder = LISTS_FOLDER / target_date
    deleted_gz = date_folder / "deleted" / "all.txt.gz"

    if not deleted_gz.exists():
        log(f"Error: Deleted file not found: {deleted_gz}")
        return 0

    out_folder = date_folder / pattern_key
    out_folder.mkdir(parents=True, exist_ok=True)
    output_gz = out_folder / "deleted.txt.gz"

    log(f"Extracting [{pattern_key}] deleted domains...")

    count = 0
    tmp_output = Path(str(output_gz) + ".tmp")

    with gzip.open(deleted_gz, "rt", encoding="utf-8", errors="replace") as fin, \
         gzip.open(tmp_output, "wt", encoding="utf-8") as fout:
        
        for line in fin:
            domain = line.strip()
            sld = get_sld(domain)
            if matcher_func(sld):
                fout.write(domain + "\n")
                count += 1

    os.replace(tmp_output, output_gz)
    log(f"-> Saved {count:,} domains to {output_gz}")
    return count


def main():
    start_time = time.time()
    
    # Arg parsing: $1 can be pattern or date if pattern isn't provided
    raw_arg1 = sys.argv[1].lower().strip() if len(sys.argv) > 1 else "all"
    target_date = sys.argv[2].strip() if len(sys.argv) > 2 else TODAY

    # Determine patterns to run
    if raw_arg1 in PATTERN_MATCHERS:
        selected_patterns = [raw_arg1]
    elif raw_arg1 in ("all", "*"):
        selected_patterns = list(PATTERN_MATCHERS.keys())
    else:
        log(f"Unknown pattern '{raw_arg1}'. Options: {', '.join(PATTERN_MATCHERS.keys())}, or 'all'")
        return 1

    log("==============================================")
    log(f"Processing Deleted Short Domains")
    log(f"Date: {target_date}")
    log(f"Selected Pattern(s): {', '.join(selected_patterns)}")
    log("==============================================")

    # ----------------------------------------------------------
    # Read existing combined JSON (if running single patterns)
    # ----------------------------------------------------------
    json_dir = JSON_FOLDER / "short_patterns"
    json_dir.mkdir(parents=True, exist_ok=True)
    summary_json_path = json_dir / f"{target_date}.json"

    json_data = {
        "date": target_date,
        "last_updated": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "timezone": TIMEZONE_NAME,
        "patterns": {}
    }

    if summary_json_path.exists():
        try:
            with open(summary_json_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
                json_data["patterns"] = existing.get("patterns", {})
        except Exception:
            pass

    # ----------------------------------------------------------
    # Process selected patterns
    # ----------------------------------------------------------
    for pattern in selected_patterns:
        count = extract_pattern(target_date, pattern, PATTERN_MATCHERS[pattern])
        json_data["patterns"][pattern] = count

    # Update timestamp and write single combined JSON
    json_data["last_updated"] = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    tmp_json = Path(str(summary_json_path) + ".tmp")
    
    with open(tmp_json, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_json, summary_json_path)

    elapsed = time.time() - start_time
    log(f"Combined JSON saved -> {summary_json_path}")
    log(f"Completed in {elapsed:.2f} seconds.")
    log("==============================================")

    return 0


if __name__ == "__main__":
    sys.exit(main())
