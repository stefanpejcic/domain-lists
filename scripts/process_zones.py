#!/usr/bin/env python3
"""
Download, parse and diff ICANN CZDS zone files.

For every approved zone, one at a time per worker:
    download -> extract delegated domains -> sort -u -> diff with the
    previous snapshot -> write lists + json -> delete the zone file.

Zones are streamed through gzip/awk/sort/comm, so neither RAM nor disk ever
has to hold more than one zone's worth of data (com is ~5GB gzipped,
~25GB uncompressed).

Re-running on the same day only picks up zones that are not done yet, so the
cron job can run several times a day: later runs retry failures and
otherwise exit immediately with code 2.

Usage:
    process_zones.py              # every approved zone not yet done today
    process_zones.py com net      # only these (re-processed even if done)
    process_zones.py --force      # re-process everything
    process_zones.py --local      # process zone files already in downloads/
                                  # instead of downloading (files are kept)

Exit codes: 0 = all OK, 1 = some zones failed, 2 = nothing to do.
"""
import fcntl
import gzip
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from download_zones import DOWNLOAD_FOLDER, approved_tlds, download_zone, get_token
from notify import send_alert

# ==============================================================
# Configuration
# ==============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

LISTS_FOLDER = REPO_ROOT / "lists"
JSON_FOLDER = REPO_ROOT / "json"
TMP_FOLDER = REPO_ROOT / "tmp"

LOCK_FILE = REPO_ROOT / ".process_zones.lock"
LAST_ALERT_FILE = REPO_ROOT / ".last_alert"
CZDS_ZONES_FILE = JSON_FOLDER / "czds_zones.json"

TIMEZONE_NAME = "Europe/Belgrade"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

LOG_FILE = REPO_ROOT / "zone_processing_log.txt"

NOW = datetime.now(TIMEZONE)
TODAY = NOW.strftime("%Y-%m-%d")

# The container is capped at 1GB RAM, shared with the website. Each worker
# runs one `sort`, so WORKERS x the largest SORT_MEMORY must stay well below
# that. `sort` spills to (gzip-compressed) temp files beyond this.
WORKERS = 2
SORT_MEMORY_SMALL = "64M"
SORT_MEMORY_LARGE = "256M"
LARGE_ZONE_BYTES = 500_000_000

# Dated lists/json older than this are removed. Full domain snapshots are
# kept only for the newest date per TLD regardless (they're only needed
# for the next diff and for downloads).
KEEP_DAYS = 30

# Force byte-order collation: `comm` requires its inputs sorted exactly
# the way `sort` sorted them.
C_ENV = dict(os.environ, LC_ALL="C")

# Print delegated (NS) owner names that sit under the zone, minus the
# trailing dot. Zone files are sorted by owner, so skipping repeats of the
# previous owner drops most duplicates before they ever reach `sort`.
AWK_EXTRACT = r"""
tolower($4) == "ns" {
    o = tolower($1)
    if (o == last) next
    last = o
    n = length(o)
    if (n > length(suf) && substr(o, n - length(suf) + 1) == suf)
        print substr(o, 1, n - 1)
}
"""

# Split `comm` output: no indent = only in current (new),
# one tab = only in previous (deleted), two tabs = in both.
AWK_DIFF = r"""
/^\t\t/ { common++; next }
/^\t/   { deleted++; if (write) print substr($0, 2) > del; next }
        { added++; if (write) print > new }
END     { printf "%d %d %d\n", common, added, deleted }
"""

# ==============================================================
# Logging
# ==============================================================

_log_lock = threading.Lock()


def log(msg):
    timestamp = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    with _log_lock:
        print(line, flush=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ==============================================================
# Shell pipelines
# ==============================================================

def run_pipeline(commands, stdout):
    """
    Run commands as `cmd1 | cmd2 | ...`, writing the last one's output to
    the open file `stdout`. Raises if any stage exits non-zero (a plain
    shell pipe would hide e.g. a truncated gzip upstream).
    """
    procs = []
    prev_stdout = None

    for i, cmd in enumerate(commands):
        last = i == len(commands) - 1
        p = subprocess.Popen(
            cmd,
            stdin=prev_stdout,
            stdout=stdout if last else subprocess.PIPE,
            env=C_ENV,
        )
        if prev_stdout is not None:
            prev_stdout.close()
        prev_stdout = p.stdout
        procs.append(p)

    codes = [p.wait() for p in procs]
    if any(codes):
        names = " | ".join(c[0] for c in commands)
        raise RuntimeError(f"`{names}` failed with exit codes {codes}")


def extract_sorted_domains(zone_file, tld, out_gz, work_dir):
    """zone .gz -> sorted, unique domain list .gz"""
    size = zone_file.stat().st_size
    memory = SORT_MEMORY_LARGE if size >= LARGE_ZONE_BYTES else SORT_MEMORY_SMALL

    with open(out_gz, "wb") as out:
        run_pipeline(
            [
                ["gzip", "-dc", str(zone_file)],
                ["awk", "-v", f"suf=.{tld}.", AWK_EXTRACT],
                ["sort", "-u", "-S", memory, "--parallel=2",
                 "-T", str(work_dir), "--compress-program=gzip"],
                ["gzip", "-c", "-6"],
            ],
            out,
        )


def diff_snapshots(current_gz, previous_gz, new_txt, deleted_txt):
    """
    Compare two sorted .gz snapshots with `comm`, streaming both.
    previous_gz=None means first snapshot: count only, write no lists.

    Returns (total, new, deleted).
    """
    p_cur = subprocess.Popen(["gzip", "-dc", str(current_gz)], stdout=subprocess.PIPE)
    if previous_gz is not None:
        p_prev = subprocess.Popen(["gzip", "-dc", str(previous_gz)], stdout=subprocess.PIPE)
        prev_arg = f"/dev/fd/{p_prev.stdout.fileno()}"
        fds = (p_cur.stdout.fileno(), p_prev.stdout.fileno())
    else:
        p_prev = None
        prev_arg = "/dev/null"
        fds = (p_cur.stdout.fileno(),)

    p_comm = subprocess.Popen(
        ["comm", f"/dev/fd/{p_cur.stdout.fileno()}", prev_arg],
        stdout=subprocess.PIPE,
        pass_fds=fds,
        env=C_ENV,
    )
    p_cur.stdout.close()
    if p_prev:
        p_prev.stdout.close()

    p_awk = subprocess.Popen(
        [
            "awk",
            "-v", f"write={0 if previous_gz is None else 1}",
            "-v", f"new={new_txt}",
            "-v", f"del={deleted_txt}",
            AWK_DIFF,
        ],
        stdin=p_comm.stdout,
        stdout=subprocess.PIPE,
        env=C_ENV,
    )
    p_comm.stdout.close()

    output = p_awk.communicate()[0].decode()
    codes = [p.wait() for p in (p_cur, p_prev, p_comm, p_awk) if p is not None]
    if any(codes):
        raise RuntimeError(f"`gzip | comm | awk` diff failed with exit codes {codes}")

    common, added, deleted = (int(x) for x in output.split())
    return common + added, added, deleted


def gzip_file(src_path, dest_path):
    """gzip src to dest atomically (via a .tmp file)."""
    dest_path = Path(dest_path)
    tmp = Path(str(dest_path) + ".tmp")
    with open(src_path, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 16 * 1024 * 1024)
    os.replace(tmp, dest_path)


def concat_gzip_files(sources, dest_path):
    """
    Concatenated gzip members are a valid gzip file, so combined lists
    are built by plain byte copying: no decompression, sort or RAM.
    """
    dest_path = Path(dest_path)
    tmp = Path(str(dest_path) + ".tmp")
    with open(tmp, "wb") as out:
        for src in sources:
            with open(src, "rb") as f:
                shutil.copyfileobj(f, out, 16 * 1024 * 1024)
    os.replace(tmp, dest_path)


# ==============================================================
# Snapshots
# ==============================================================

def date_folders():
    """Dated folders in lists/, newest first."""
    if not LISTS_FOLDER.exists():
        return []

    folders = []
    for folder in LISTS_FOLDER.iterdir():
        try:
            datetime.strptime(folder.name, "%Y-%m-%d")
        except ValueError:
            continue
        if folder.is_dir():
            folders.append(folder)

    return sorted(folders, key=lambda f: f.name, reverse=True)


def find_previous_snapshot(tld, before=TODAY):
    for folder in date_folders():
        if folder.name >= before:
            continue
        path = folder / "domains" / f"{tld}.txt.gz"
        if path.exists():
            return path
    return None


def done_today(tld):
    return (
        (JSON_FOLDER / tld / f"{TODAY}.json").exists()
        and (LISTS_FOLDER / TODAY / "domains" / f"{tld}.txt.gz").exists()
    )


# ==============================================================
# Process one TLD
# ==============================================================

def process_tld(tld, zone_file):
    start_time = time.time()

    today_folder = LISTS_FOLDER / TODAY
    for kind in ("domains", "new", "deleted"):
        (today_folder / kind).mkdir(parents=True, exist_ok=True)

    work_dir = TMP_FOLDER / tld
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)

    try:
        sorted_gz = work_dir / "domains.txt.gz"
        new_txt = work_dir / "new.txt"
        deleted_txt = work_dir / "deleted.txt"

        extract_sorted_domains(zone_file, tld, sorted_gz, work_dir)

        previous_file = find_previous_snapshot(tld)
        if previous_file is None:
            log(f"{tld}: No previous snapshot found - skipping diff")

        total, added, deleted = diff_snapshots(sorted_gz, previous_file, new_txt, deleted_txt)

        if previous_file is not None:
            gzip_file(new_txt, today_folder / "new" / f"{tld}.txt.gz")
            gzip_file(deleted_txt, today_folder / "deleted" / f"{tld}.txt.gz")
        else:
            added = deleted = 0

        # Domains file last: together with the JSON it marks the TLD done.
        os.replace(sorted_gz, today_folder / "domains" / f"{tld}.txt.gz")

        result = {
            "tld": tld,
            "success": True,
            "domains": total,
            "new": added,
            "deleted": deleted,
            "elapsed": time.time() - start_time,
        }
        write_tld_json(result, datetime.now(TIMEZONE).isoformat(timespec="seconds"))

        log(
            f"{tld}: {total:,} total, {added:,} new, {deleted:,} deleted, "
            f"{result['elapsed']:.2f}s"
            + (f" (vs {previous_file.parent.parent.name})" if previous_file else "")
        )
        return result

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_job(tld, token, local):
    """Download (unless local) + process + clean up one zone. Never raises."""
    zone_file = DOWNLOAD_FOLDER / f"{tld}.txt.gz"

    try:
        if not local:
            zone_file, reason = download_zone(tld, token)
            if zone_file is None:
                return {"tld": tld, "success": False, "reason": f"Download failed: {reason}"}
        elif not zone_file.exists():
            return {"tld": tld, "success": False, "reason": f"Zone file not found: {zone_file}"}

        return process_tld(tld, zone_file)

    except Exception as e:
        return {"tld": tld, "success": False, "reason": str(e)}

    finally:
        if not local:
            (DOWNLOAD_FOLDER / f"{tld}.txt.gz").unlink(missing_ok=True)


# ==============================================================
# JSON output
# ==============================================================

def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def write_tld_json(result, updated_at):
    tld = result["tld"]
    tld_folder = JSON_FOLDER / tld
    json_file = tld_folder / f"{TODAY}.json"

    write_json_atomic(json_file, {
        "tld": tld,
        "date": TODAY,
        "domains": result.get("domains", 0),
        "new": result.get("new", 0),
        "deleted": result.get("deleted", 0),
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    })

    latest_link = tld_folder / "latest"
    tmp_link = tld_folder / ".latest.tmp"

    try:
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(json_file.name)
        os.replace(tmp_link, latest_link)
    except Exception as e:
        log(f"{tld}: Failed to update latest symlink: {e}")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def generate_summary_json(updated_at):
    """Totals over every TLD's latest stats."""
    summary = {
        "date": TODAY,
        "tlds": 0,
        "domains": 0,
        "new": 0,
        "deleted": 0,
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    }

    for tld_folder in sorted(JSON_FOLDER.iterdir()):
        latest_file = tld_folder / "latest"
        if not latest_file.is_symlink():
            continue
        data = load_json(latest_file)
        if not data:
            continue
        summary["tlds"] += 1
        for key in ("domains", "new", "deleted"):
            summary[key] += int(data.get(key, 0))

    write_json_atomic(JSON_FOLDER / "summary.json", summary)
    log(f"Summary JSON updated -> {JSON_FOLDER / 'summary.json'}")
    return summary


def write_combined_lists(summary, updated_at):
    """
    lists/TODAY/domains/all.txt.gz - latest snapshot of every TLD
    lists/TODAY/new/all.txt.gz     - today's new domains, all TLDs
    lists/TODAY/deleted/all.txt.gz - today's deleted domains, all TLDs
    json/TODAY.json                - counts for the above
    """
    today_folder = LISTS_FOLDER / TODAY

    latest_snapshots = {}
    for folder in reversed(date_folders()):  # oldest first, newer overwrite
        for f in (folder / "domains").glob("*.txt.gz"):
            if f.name != "all.txt.gz":
                latest_snapshots[f.name] = f

    counts = {"tlds": len(latest_snapshots), "domains": summary["domains"], "new": 0, "deleted": 0}

    for tld_json in JSON_FOLDER.glob(f"*/{TODAY}.json"):
        data = load_json(tld_json) or {}
        counts["new"] += int(data.get("new", 0))
        counts["deleted"] += int(data.get("deleted", 0))

    sources = {
        "domains": [latest_snapshots[name] for name in sorted(latest_snapshots)],
        "new": sorted(f for f in (today_folder / "new").glob("*.txt.gz") if f.name != "all.txt.gz"),
        "deleted": sorted(f for f in (today_folder / "deleted").glob("*.txt.gz") if f.name != "all.txt.gz"),
    }

    for kind, files in sources.items():
        output_file = today_folder / kind / "all.txt.gz"
        try:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            concat_gzip_files(files, output_file)
            log(f"Combined {kind}: {counts[kind]:,} domains -> {output_file}")
        except Exception as e:
            log(f"Failed to combine {kind}: {e}")

    write_json_atomic(JSON_FOLDER / f"{TODAY}.json", {
        "date": TODAY,
        **counts,
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    })


# ==============================================================
# Retention
# ==============================================================

def prune_old_data():
    cutoff = (NOW - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    folders = date_folders()

    # Full snapshots: keep only the newest per TLD (incl. all.txt.gz).
    seen = set()
    for folder in folders:
        for f in sorted((folder / "domains").glob("*.txt.gz")):
            if f.name in seen:
                f.unlink(missing_ok=True)
            else:
                seen.add(f.name)

    # Everything else dated: keep KEEP_DAYS, except still-latest snapshots.
    for folder in folders:
        if folder.name >= cutoff:
            continue
        for child in folder.iterdir():
            if child.name != "domains":
                shutil.rmtree(child, ignore_errors=True)
        try:
            (folder / "domains").rmdir()  # only succeeds if empty
            folder.rmdir()
        except OSError:
            pass

    for json_file in JSON_FOLDER.glob("**/????-??-??.json"):
        if json_file.stem >= cutoff:
            continue
        latest = json_file.parent / "latest"
        if latest.is_symlink() and os.readlink(latest) == json_file.name:
            continue
        json_file.unlink(missing_ok=True)


# ==============================================================
# CZDS approvals
# ==============================================================

def check_approved_zones(tlds):
    """
    CZDS access is granted per zone and expires (typically after a year),
    after which a zone just silently disappears from the links list.
    Alert when that happens so access can be re-requested.
    """
    previous = set((load_json(CZDS_ZONES_FILE) or {}).get("tlds", []))
    current = set(tlds)

    lost = sorted(previous - current)
    gained = sorted(current - previous) if previous else []

    if lost:
        log(f"WARNING: {len(lost)} zones no longer approved in CZDS: {', '.join(lost)}")
        send_alert(
            f"domain-lists: {len(lost)} CZDS zones no longer approved",
            "These zones disappeared from the CZDS download list (access expired "
            "or was revoked). Re-request access at https://czds.icann.org/\n\n"
            + "\n".join(lost),
        )
    if gained:
        log(f"{len(gained)} newly approved zones: {', '.join(gained)}")

    write_json_atomic(CZDS_ZONES_FILE, {
        "tlds": sorted(current),
        "checked": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
    })


def alert_failures(failed):
    """Email failed TLDs - at most once a day for the same set."""
    key = f"{TODAY} {' '.join(sorted(r['tld'] for r in failed))}"
    try:
        if LAST_ALERT_FILE.read_text() == key:
            return
    except OSError:
        pass

    body = "\n".join(f"{r['tld']}: {r.get('reason', 'Unknown error')}" for r in failed)
    body += f"\n\nThey will be retried on the next cron run. Log: {LOG_FILE}"
    send_alert(f"domain-lists: {len(failed)} zones failed on {TODAY}", body)
    LAST_ALERT_FILE.write_text(key)


# ==============================================================
# Main
# ==============================================================

def disk_free():
    usage = shutil.disk_usage(REPO_ROOT)
    return f"{usage.free / 1e9:.1f}GB free"


def main():
    start_time = time.time()

    args = [a.lower().strip() for a in sys.argv[1:]]
    local = "--local" in args
    force = "--force" in args
    requested = [a for a in args if not a.startswith("--")]

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another process_zones.py run is in progress - exiting.")
        return 2

    log("")
    log("==============================================")
    log("Zone File Processing")
    log(f"Date: {TODAY} ({TIMEZONE_NAME})")
    log(f"Disk: {disk_free()}")
    log("==============================================")

    for folder in (DOWNLOAD_FOLDER, LISTS_FOLDER, JSON_FOLDER):
        folder.mkdir(parents=True, exist_ok=True)
    # Leftovers from an interrupted run.
    shutil.rmtree(TMP_FOLDER, ignore_errors=True)
    TMP_FOLDER.mkdir()

    token = None
    if local:
        tlds = sorted(f.name[: -len(".txt.gz")] for f in DOWNLOAD_FOLDER.glob("*.txt.gz"))
    else:
        try:
            token = get_token()
            tlds = approved_tlds(token)
        except Exception as e:
            log(f"ERROR: {e}")
            send_alert("domain-lists: cannot reach CZDS", f"{e}\n\nLog: {LOG_FILE}")
            return 1
        log(f"Approved zones in CZDS: {len(tlds)}")
        if not requested:
            check_approved_zones(tlds)

    if requested:
        tlds = [t for t in tlds if t in requested]
    elif not force:
        tlds = [t for t in tlds if not done_today(t)]

    if not tlds:
        log(f"Nothing to do - every zone is already processed for {TODAY}.")
        return 2

    # Biggest first (by last snapshot size), so the multi-GB zones run
    # alongside the long tail of small ones instead of after it.
    def previous_size(tld):
        prev = find_previous_snapshot(tld, before="9999")
        return prev.stat().st_size if prev else 0

    tlds.sort(key=previous_size, reverse=True)

    log(f"Zones to process: {len(tlds)} ({WORKERS} at a time)")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(run_job, tld, token, local) for tld in tlds]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if not result["success"]:
                log(f"{result['tld']}: FAILED - {result['reason']}")

    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    updated_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    summary = generate_summary_json(updated_at)
    if succeeded:
        write_combined_lists(summary, updated_at)
    prune_old_data()

    log("")
    log("==============================================")
    log("Processing Summary")
    log("==============================================")
    log(f"Successful TLDs: {len(succeeded)}")
    log(f"Failed TLDs:     {len(failed)}")
    for r in failed:
        log(f"  {r['tld']}: {r['reason']}")
    log(f"Processed domains: {sum(r['domains'] for r in succeeded):,}")
    log(f"Processed new:     {sum(r['new'] for r in succeeded):,}")
    log(f"Processed deleted: {sum(r['deleted'] for r in succeeded):,}")
    log(f"All available TLDs in summary: {summary['tlds']:,}")
    log(f"All available domains in summary: {summary['domains']:,}")
    log(f"Disk: {disk_free()}")
    log(f"Total elapsed: {time.time() - start_time:.2f} seconds")
    log("==============================================")
    log("")

    if failed:
        alert_failures(failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
