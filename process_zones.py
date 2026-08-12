#!/usr/bin/env python3
import gzip
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ==============================================================
# Configuration
# ==============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

DOWNLOAD_FOLDER = SCRIPT_DIR / "downloads"
LISTS_FOLDER = SCRIPT_DIR / "lists"
JSON_FOLDER = SCRIPT_DIR / "json"

TIMEZONE_NAME = "Europe/Belgrade"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

LOG_FILE = SCRIPT_DIR / "zone_processing_log.txt"

NOW = datetime.now(TIMEZONE)
TODAY = NOW.strftime("%Y-%m-%d")

# Zone files at/above this size are processed alone (1 worker).
# Below it, up to PARALLEL_SMALL are processed concurrently.
LARGE_FILE_THRESHOLD_BYTES = 1_000_000_000  # 1GB
PARALLEL_SMALL = 2

# Memory cap passed to `sort -u`. Leaves headroom on an 11GB box
# even when 2 small jobs run at once (2 x SORT_MEMORY < RAM).
SORT_MEMORY = "2G"

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
# Extract domains from zone file -> raw unsorted text file
# ==============================================================

def extract_domains_to_file(zone_file, tld, raw_path):
    """
    Stream the gzip zone file and write one owner-name-per-line
    to raw_path. No dedup, no in-memory set - dedup/sort is done
    afterwards by external `sort -u`, which is disk-backed and
    won't blow up RAM regardless of zone size.
    """

    zone_name = f"{tld}."
    written = 0

    with gzip.open(
        zone_file, "rt", encoding="utf-8", errors="replace"
    ) as f, open(raw_path, "w", encoding="utf-8") as out:

        for line in f:

            line = line.strip()

            if not line or line.startswith(";"):
                continue

            if ";" in line:
                line = line.split(";", 1)[0].strip()

            parts = line.split()

            if len(parts) < 4:
                continue

            owner = parts[0].lower()
            record_type = parts[3].lower()

            if record_type != "ns":
                continue

            if owner == zone_name:
                continue

            if not owner.endswith("."):
                continue

            domain = owner[:-1]

            if not domain.endswith(f".{tld}"):
                continue

            out.write(domain + "\n")
            written += 1

    return written


def sort_unique(raw_path, sorted_path, memory=SORT_MEMORY):
    """
    Dedup + sort via external `sort -u -S <memory>`. Spills to
    disk instead of holding everything in RAM - this is what
    actually fixes the OOM, not chunking the input file.
    """

    subprocess.run(
        [
            "sort", "-u",
            "-S", memory,
            "-o", str(sorted_path),
            str(raw_path),
        ],
        check=True,
    )


def gunzip_to_file(gz_path, out_path):
    """
    Decompress a gzip file to a plain text file on disk, streamed
    in chunks - never holds the full content in memory.
    """
    with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as fin, \
            open(out_path, "w", encoding="utf-8") as fout:
        while True:
            chunk = fin.read(1024 * 1024 * 16)
            if not chunk:
                break
            fout.write(chunk)


def diff_sorted_files_on_disk(current_sorted_path, previous_sorted_path,
                               new_out_path, deleted_out_path):
    """
    Compute new/deleted domains between two sorted files using
    `comm`, entirely on disk - no domains held in RAM. Both inputs
    must already be sorted, which they are: current_sorted_path
    comes straight from sort_unique, and previous_sorted_path is a
    decompressed previous snapshot that was itself written sorted.
    """

    with open(new_out_path, "w", encoding="utf-8") as out:
        subprocess.run(
            ["comm", "-23", str(current_sorted_path), str(previous_sorted_path)],
            stdout=out,
            check=True,
        )

    with open(deleted_out_path, "w", encoding="utf-8") as out:
        subprocess.run(
            ["comm", "-13", str(current_sorted_path), str(previous_sorted_path)],
            stdout=out,
            check=True,
        )


def gzip_file(src_path, dest_path, compresslevel=6):
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(dest_path) + ".tmp")
    with open(src_path, "rb") as fin, gzip.open(
        tmp, "wb", compresslevel=compresslevel
    ) as fout:
        while True:
            chunk = fin.read(1024 * 1024 * 16)
            if not chunk:
                break
            fout.write(chunk)
    os.replace(tmp, dest_path)


def count_lines(path):
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for _ in f:
            count += 1
    return count


# ==============================================================
# Find latest previous snapshot
# ==============================================================

def find_previous_snapshot(tld, today):

    if not LISTS_FOLDER.exists():
        return None

    previous_dates = []

    for date_folder in LISTS_FOLDER.iterdir():

        if not date_folder.is_dir():
            continue

        date_name = date_folder.name

        try:
            datetime.strptime(date_name, "%Y-%m-%d")
        except ValueError:
            continue

        if date_name >= today:
            continue

        domains_file = date_folder / "domains" / f"{tld}.txt.gz"

        if domains_file.exists():
            previous_dates.append(date_name)

    if not previous_dates:
        return None

    previous_dates.sort(reverse=True)

    return LISTS_FOLDER / previous_dates[0] / "domains" / f"{tld}.txt.gz"


# ==============================================================
# Process one TLD (runs in a worker process)
# ==============================================================

def process_tld(tld, today, script_dir_str):

    # Re-derive paths inside the worker (ProcessPoolExecutor
    # pickles args, not module globals reliably across platforms).
    script_dir = Path(script_dir_str)
    download_folder = script_dir / "downloads"
    lists_folder = script_dir / "lists"

    start_time = time.time()

    zone_file = download_folder / f"{tld}.txt.gz"

    if not zone_file.exists():
        return {
            "tld": tld,
            "success": False,
            "reason": f"Zone file not found: {zone_file}",
        }

    log(f"Processing {tld}...")

    today_folder = lists_folder / today
    domains_folder = today_folder / "domains"
    new_folder = today_folder / "new"
    deleted_folder = today_folder / "deleted"

    domains_folder.mkdir(parents=True, exist_ok=True)
    new_folder.mkdir(parents=True, exist_ok=True)
    deleted_folder.mkdir(parents=True, exist_ok=True)

    work_dir = Path("/tmp") / f"zonefile_{tld}_{today}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # All temp paths declared up front so `finally` can always
    # clean them up, even if an early step raises before a given
    # path is otherwise referenced.
    raw_path = work_dir / "raw.txt"
    sorted_path = work_dir / "sorted.txt"
    previous_sorted_path = work_dir / "previous_sorted.txt"
    new_raw_path = work_dir / "new_raw.txt"
    deleted_raw_path = work_dir / "deleted_raw.txt"

    try:
        try:
            extract_domains_to_file(zone_file, tld, raw_path)
        except Exception as e:
            return {
                "tld": tld,
                "success": False,
                "reason": f"Failed to parse zone file: {e}",
            }

        try:
            sort_unique(raw_path, sorted_path)
        except subprocess.CalledProcessError as e:
            return {
                "tld": tld,
                "success": False,
                "reason": f"sort -u failed: {e}",
            }

        domain_count = count_lines(sorted_path)
        log(f"{tld}: Found {domain_count:,} domains")

        domains_file = domains_folder / f"{tld}.txt.gz"
        gzip_file(sorted_path, domains_file)
        log(f"{tld}: Saved domains -> {domains_file}")

        previous_file = find_previous_snapshot(tld, today)

        if previous_file is None:
            log(f"{tld}: No previous snapshot found - skipping diff")
            elapsed = time.time() - start_time
            return {
                "tld": tld,
                "success": True,
                "domains": domain_count,
                "new": 0,
                "deleted": 0,
                "first_snapshot": True,
                "elapsed": elapsed,
            }

        log(f"{tld}: Comparing with {previous_file}")

        try:
            gunzip_to_file(previous_file, previous_sorted_path)
        except Exception as e:
            return {
                "tld": tld,
                "success": False,
                "reason": f"Failed to decompress previous snapshot: {e}",
            }

        try:
            diff_sorted_files_on_disk(
                sorted_path, previous_sorted_path,
                new_raw_path, deleted_raw_path,
            )
        except subprocess.CalledProcessError as e:
            return {
                "tld": tld,
                "success": False,
                "reason": f"comm diff failed: {e}",
            }

        new_count = count_lines(new_raw_path)
        deleted_count = count_lines(deleted_raw_path)

        new_file = new_folder / f"{tld}.txt.gz"
        gzip_file(new_raw_path, new_file)

        deleted_file = deleted_folder / f"{tld}.txt.gz"
        gzip_file(deleted_raw_path, deleted_file)

        elapsed = time.time() - start_time

        log(
            f"{tld}: {domain_count:,} total, "
            f"{new_count:,} new, "
            f"{deleted_count:,} deleted, "
            f"{elapsed:.2f}s"
        )

        return {
            "tld": tld,
            "success": True,
            "domains": domain_count,
            "new": new_count,
            "deleted": deleted_count,
            "first_snapshot": False,
            "elapsed": elapsed,
        }

    finally:
        for p in (raw_path, sorted_path, previous_sorted_path,
                  new_raw_path, deleted_raw_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            work_dir.rmdir()
        except Exception:
            pass


# ==============================================================
# Write per-TLD JSON
# ==============================================================

def write_tld_json(result, updated_at):

    tld = result["tld"]
    tld_folder = JSON_FOLDER / tld
    tld_folder.mkdir(parents=True, exist_ok=True)
    json_file = tld_folder / f"{TODAY}.json"

    data = {
        "tld": tld,
        "date": TODAY,
        "domains": result.get("domains", 0),
        "new": result.get("new", 0),
        "deleted": result.get("deleted", 0),
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    }

    tmp = Path(str(json_file) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, json_file)

    latest_link = tld_folder / "latest"
    tmp_link = tld_folder / ".latest.tmp"

    try:
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(json_file.name)
        os.replace(tmp_link, latest_link)
    except Exception as e:
        log(f"{tld}: Failed to update latest symlink: {e}")

    log(f"{tld}: JSON -> {json_file}")


# ==============================================================
# Generate summary JSON
# ==============================================================

def generate_summary_json(updated_at):

    JSON_FOLDER.mkdir(parents=True, exist_ok=True)

    total_tlds = 0
    total_domains = 0
    total_new = 0
    total_deleted = 0

    for tld_folder in sorted(JSON_FOLDER.iterdir()):

        if not tld_folder.is_dir():
            continue

        latest_file = tld_folder / "latest"

        if not latest_file.is_symlink() or not latest_file.exists():
            continue

        try:
            with open(latest_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            total_tlds += 1
            total_domains += int(data.get("domains", 0))
            total_new += int(data.get("new", 0))
            total_deleted += int(data.get("deleted", 0))

        except Exception as e:
            log(f"Failed to read {latest_file}: {e}")

    summary = {
        "date": TODAY,
        "tlds": total_tlds,
        "domains": total_domains,
        "new": total_new,
        "deleted": total_deleted,
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    }

    summary_file = JSON_FOLDER / "summary.json"
    tmp = Path(str(summary_file) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, summary_file)

    log(f"Summary JSON updated -> {summary_file}")

    return summary


# ==============================================================
# Main
# ==============================================================

def main():

    start_time = time.time()

    requested_tld = (
        sys.argv[1].lower().strip() if len(sys.argv) > 1 else None
    )

    log("")
    log("==============================================")
    log("Zone File Processing")
    log(f"Date: {TODAY}")
    log(f"Timezone: {TIMEZONE_NAME}")
    log("==============================================")

    DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    LISTS_FOLDER.mkdir(parents=True, exist_ok=True)
    JSON_FOLDER.mkdir(parents=True, exist_ok=True)

    if requested_tld:
        zone_files = [DOWNLOAD_FOLDER / f"{requested_tld}.txt.gz"]
    else:
        zone_files = sorted(DOWNLOAD_FOLDER.glob("*.txt.gz"))

    if not zone_files:
        log("No zone files found.")
        return 1

    log(f"Zone files to process: {len(zone_files)}")

    # ----------------------------------------------------------
    # Split into large (serial, one at a time) vs small
    # (parallel, PARALLEL_SMALL at a time) by file size.
    # ----------------------------------------------------------

    large_tlds = []
    small_tlds = []

    for zone_file in zone_files:
        tld = zone_file.name[: -len(".txt.gz")].lower()
        try:
            size = zone_file.stat().st_size
        except FileNotFoundError:
            size = 0
        if size >= LARGE_FILE_THRESHOLD_BYTES:
            large_tlds.append(tld)
        else:
            small_tlds.append(tld)

    log(
        f"Large TLDs (serial, >= "
        f"{LARGE_FILE_THRESHOLD_BYTES / 1e9:.0f}GB): "
        f"{len(large_tlds)}"
    )
    log(f"Small TLDs (parallel x{PARALLEL_SMALL}): {len(small_tlds)}")

    results = []

    # Large files: one at a time, full machine available to `sort`/`comm`.
    for tld in large_tlds:
        results.append(process_tld(tld, TODAY, str(SCRIPT_DIR)))

    # Small files: up to PARALLEL_SMALL concurrently.
    if small_tlds:
        with ProcessPoolExecutor(max_workers=PARALLEL_SMALL) as executor:
            futures = {
                executor.submit(process_tld, tld, TODAY, str(SCRIPT_DIR)): tld
                for tld in small_tlds
            }
            for future in as_completed(futures):
                results.append(future.result())

    # ----------------------------------------------------------
    # Tally
    # ----------------------------------------------------------

    success_count = 0
    fail_count = 0
    total_domains = 0
    total_new = 0
    total_deleted = 0

    for result in results:
        if result["success"]:
            success_count += 1
            total_domains += result.get("domains", 0)
            total_new += result.get("new", 0)
            total_deleted += result.get("deleted", 0)
        else:
            fail_count += 1
            log(f"{result['tld']}: FAILED - {result.get('reason', 'Unknown error')}")

    updated_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")

    for result in results:
        if result["success"]:
            write_tld_json(result, updated_at)

    summary = generate_summary_json(updated_at)

    elapsed = time.time() - start_time

    log("")
    log("==============================================")
    log("Processing Summary")
    log("==============================================")
    log(f"Successful TLDs: {success_count}")
    log(f"Failed TLDs:     {fail_count}")
    log(f"Processed domains: {total_domains:,}")
    log(f"Processed new:     {total_new:,}")
    log(f"Processed deleted: {total_deleted:,}")
    log(f"All available TLDs in summary: {summary['tlds']:,}")
    log(f"All available domains in summary: {summary['domains']:,}")
    log(f"Last updated: {updated_at} ({TIMEZONE_NAME})")
    log(f"Total elapsed: {elapsed:.2f} seconds")
    log("==============================================")
    log("")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
