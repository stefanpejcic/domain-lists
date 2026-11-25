#!/usr/bin/env python3
"""
Download, parse and diff ICANN CZDS zone files.

For every approved zone, one at a time per worker:
    download -> extract delegated domains with their DNS provider and
    DNSSEC flag -> sort -u -> join with the previous snapshot (new,
    deleted, moved to another provider) -> write lists + json -> delete
    the zone file.

A zone is processed when CZDS publishes a new version of it (its
Last-Modified is newer than our snapshot), not on a fixed schedule:
registries publish anywhere from 00:00 to ~10:00 UTC and the time drifts,
so a fixed daily run sometimes got yesterday's file twice (0 new / 0
deleted) and the day after showed two days of changes. Each snapshot is
dated by the zone's own Last-Modified (Europe/Belgrade), and the cron job
runs every hour; runs where nothing was published exit with code 2.

Zones are streamed through gzip/awk/sort/comm, so neither RAM nor disk ever
has to hold more than one zone's worth of data (com is ~5GB gzipped,
~25GB uncompressed).

Per snapshot date, lists/<date>/ holds:
    domains/<tld>.txt.gz     every delegated domain (one per line)
    ns/<tld>.txt.gz          domain<TAB>provider<TAB>dnssec(0/1), the
                             newest one per TLD only (needed for the next diff)
    new/, deleted/           domains added / removed since the previous snapshot
    new_active/              new domains that are not on a parking provider
    moved/                   domain<TAB>old provider<TAB>new provider
and json/<tld>/providers/<date>.json the per-provider counts.

Usage:
    process_zones.py              # every approved zone with a newer CZDS version
    process_zones.py com net      # only these (re-processed even if done)
    process_zones.py --force      # re-process everything
    process_zones.py --local      # process zone files already in downloads/
                                  # instead of downloading (files are kept)
    process_zones.py --pending    # only rebuild the pending-drop lists from
                                  # the lists already on disk

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from download_zones import DOWNLOAD_FOLDER, approved_tlds, download_zone, get_token, zone_last_modified
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
PROVIDERS_CONFIG = REPO_ROOT / "providers.json"

TIMEZONE_NAME = "Europe/Belgrade"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

LOG_FILE = REPO_ROOT / "zone_processing_log.txt"

NOW = datetime.now(TIMEZONE)
TODAY = NOW.strftime("%Y-%m-%d")

# The container is capped at 2GB RAM, shared with the website. Each worker
# runs one `sort`, and zones go biggest first, so the worst case is .com
# (HUGE) next to another LARGE zone: 1G + 384M plus the awk/gzip stages and
# the website stays well below the cap. `sort` spills to (gzip-compressed)
# temp files beyond this; fewer spills is what makes .com faster.
WORKERS = 2
SORT_MEMORY_SMALL = "64M"
SORT_MEMORY_LARGE = "384M"
SORT_MEMORY_HUGE = "1G"
LARGE_ZONE_BYTES = 500_000_000
HUGE_ZONE_BYTES = 2_000_000_000  # only .com (~5GB gzipped)

# Dated lists/json older than this are removed. Full domain snapshots are
# kept only for the newest date per TLD regardless (they're only needed
# for the next diff and for downloads).
KEEP_DAYS = 40

# Pending drops: a gTLD domain that leaves the zone because it expired sits
# in the redemption grace period (~30 days) and pending delete (5 days)
# before anyone can register it again. So a domain that left the zone
# DROP_AFTER_DAYS ago and hasn't come back since is expected to drop about
# now. lists/pending_drop/<tld>.txt.gz holds domain<TAB>left-zone date for
# every domain that left PENDING_MIN_DAYS..PENDING_MAX_DAYS ago and is still
# out; by_date/<expected drop date>.txt.gz combines them for the site.
DROP_AFTER_DAYS = 35
PENDING_MIN_DAYS = 28
PENDING_MAX_DAYS = 38  # must stay below KEEP_DAYS: the lists have to still exist
PENDING_FOLDER = LISTS_FOLDER / "pending_drop"
# A date's deleted list only dates a departure if the snapshot before it is
# at most this many days older (after a gap in processing it holds weeks of
# departures, most of them long dropped already).
PENDING_MAX_GAP_DAYS = 3

# CZDS publishes each zone once a day. A zone whose snapshot is younger
# than this isn't even asked about (one HEAD request per zone otherwise,
# every hour, until the registry publishes).
RECHECK_AFTER = timedelta(hours=20)
HEAD_WORKERS = 4

# How many providers / provider-to-provider flows each TLD keeps in its
# providers json (the long tail is mostly one-off self-hosted names).
TOP_PROVIDERS = 500
TOP_FLOWS = 300

# Force byte-order collation: `comm` requires its inputs sorted exactly
# the way `sort` sorted them.
C_ENV = dict(os.environ, LC_ALL="C")

# For every NS and DS record whose owner sits under the zone, print
#     domain<TAB>N<provider>   or   domain<TAB>D
# where provider is the nameserver host reduced to its registrable label
# without trailing digits (ns1.cloudflare.com -> cloudflare,
# ns-12.awsdns-34.co.uk -> awsdns, dns1.registrar-servers.com ->
# registrar-servers), or "(self)" when the domain is its own nameserver
# (ns1.example.com for example.com). Zone files are mostly sorted by owner,
# so skipping repeats of the previous line drops most duplicates (e.g.
# ns1 + ns2 of the same provider) before they ever reach `sort`.
AWK_EXTRACT = r"""
function provider(host,   n, a, i, lab) {
    host = tolower(host)
    sub(/\.$/, "", host)
    n = split(host, a, ".")
    if (n < 2) return host
    i = n - 1
    if (n >= 3 && length(a[n]) == 2 && a[n-1] ~ /^(co|com|net|org|gov|edu|ac|ne|or|gen|biz|info|ltd|plc|web|nom)$/)
        i = n - 2
    lab = a[i]
    sub(/-?[0-9]+$/, "", lab)
    if (lab == "") lab = a[i]
    return lab
}
{
    t = tolower($4)
    if (t != "ns" && t != "ds") next
    o = tolower($1)
    n = length(o)
    if (n <= length(suf) || substr(o, n - length(suf) + 1) != suf) next
    d = substr(o, 1, n - 1)
    if (t == "ns") {
        p = provider($5)
        if (p == substr(d, 1, index(d, ".") - 1)) p = "(self)"
        line = d "\tN" p
    } else {
        line = d "\tD"
    }
    if (line == last) next
    last = line
    print line
}
"""

# Collapse the sorted, unique records of each domain into
#     domain<TAB>provider[+provider...]<TAB>dnssec
# on stdout and the bare domain into `dom` (a shell command). Domains with
# a DS record but no NS (not delegated) are skipped, like before.
AWK_AGGREGATE = r"""
BEGIN { FS = OFS = "\t" }
function flush() {
    if (cur != "" && provs != "") {
        print cur, provs, ds
        print cur | dom
    }
}
$1 != cur { flush(); cur = $1; provs = ""; ds = 0 }
$2 == "D" { ds = 1; next }
{ p = substr($2, 2); provs = (provs == "" ? p : provs "+" p) }
END {
    flush()
    if (close(dom) != 0) exit 1
}
"""

# Input: `join -a1 -a2 -e - -o 0,1.2,1.3,2.2,2.3 current previous`, i.e.
#     domain  cur_provider  cur_dnssec  prev_provider  prev_dnssec
# with "-" for the side the domain is missing from. A previous snapshot
# from before provider tracking has prev_provider "?" (unknown: never a
# move). Writes the new/deleted/new_active/moved lists (when write=1) and
# prints the counters:
#     S total new deleted moved new_parked dnssec went_live
#     P provider domains new new_parked gained lost deleted dnssec
#     F from>to count
AWK_DIFF = r"""
BEGIN {
    FS = "\t"
    n = split(parking, a, ",")
    for (i = 1; i <= n; i++) park[a[i]] = 1
}
function parked(p,   n, a, i) {
    n = split(p, a, "+")
    for (i = 1; i <= n; i++) if (!(a[i] in park)) return 0
    return 1
}
$2 == "-" {
    deleted++; seen[$4] = 1; dp[$4]++
    if (write) print $1 > del
    next
}
{
    total++; seen[$2] = 1; tot[$2]++
    if ($3 == 1) { dsn++; dsp[$2]++ }
}
$4 == "-" {
    added++; np[$2]++
    if (parked($2)) { nparked++; npp[$2]++ }
    else if (write) print $1 > act
    if (write) print $1 > new
    next
}
$4 != "?" && $4 != $2 {
    moved++; seen[$4] = 1; gain[$2]++; lose[$4]++; flow[$4 ">" $2]++
    print $1 "\t" $4 "\t" $2 > mv
    # from a parking / for-sale page to real nameservers: the site is going live
    if (parked($4) && !parked($2)) { live++; print $1 "\t" $4 "\t" $2 > wl }
}
END {
    printf "S\t%d\t%d\t%d\t%d\t%d\t%d\t%d\n", total, added, deleted, moved, nparked, dsn, live
    for (p in seen)
        printf "P\t%s\t%d\t%d\t%d\t%d\t%d\t%d\t%d\n", p, tot[p], np[p], npp[p], gain[p], lose[p], dp[p], dsp[p]
    for (f in flow) printf "F\t%s\t%d\n", f, flow[f]
}
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


def extract_snapshot(zone_file, tld, ns_gz, domains_gz, work_dir):
    """
    zone .gz -> ns_gz (domain<TAB>provider<TAB>dnssec) and domains_gz
    (bare domains), both sorted by domain, in one pass over the zone.

    The whole "domain<TAB>..." line is sorted; TAB sorts below every
    character a domain can contain, so the domains come out in exactly the
    order `sort` gives the bare names (what comm/join and older snapshots
    expect).
    """
    size = zone_file.stat().st_size
    if size >= HUGE_ZONE_BYTES:
        memory = SORT_MEMORY_HUGE
    elif size >= LARGE_ZONE_BYTES:
        memory = SORT_MEMORY_LARGE
    else:
        memory = SORT_MEMORY_SMALL

    with open(ns_gz, "wb") as out:
        run_pipeline(
            [
                ["gzip", "-dc", str(zone_file)],
                ["awk", "-v", f"suf=.{tld}.", AWK_EXTRACT],
                ["sort", "-u", "-S", memory, "--parallel=2",
                 "-T", str(work_dir), "--compress-program=gzip"],
                ["awk", "-v", f"dom=gzip -c -6 > '{domains_gz}'", AWK_AGGREGATE],
                ["gzip", "-c", "-6"],
            ],
            out,
        )


def load_parking_providers():
    """Provider labels (see AWK_EXTRACT) that only park domains, from providers.json."""
    try:
        with open(PROVIDERS_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return sorted({p.strip().lower() for p in data.get("parking", []) if p.strip()})


PARKING = load_parking_providers()


def diff_snapshots(current_ns, previous, previous_has_ns, out):
    """
    Join the current ns snapshot with the previous one (an ns snapshot, or
    a bare domains snapshot from before provider tracking, or None for the
    first snapshot: count only, write no lists), streaming both.

    `out` maps new/deleted/new_active/moved to the text files to write.
    Returns (stats dict, providers dict, flows dict).
    """
    procs = []

    def stream(cmd_list, stdin=None):
        p = subprocess.Popen(cmd_list, stdin=stdin, stdout=subprocess.PIPE, env=C_ENV)
        procs.append(p)
        return p

    p_cur = stream(["gzip", "-dc", str(current_ns)])
    fds = [p_cur.stdout.fileno()]
    if previous is not None:
        p_prev = stream(["gzip", "-dc", str(previous)])
        if not previous_has_ns:
            p_raw = p_prev
            p_prev = stream(["awk", '{ print $0 "\t?\t0" }'], stdin=p_raw.stdout)
            p_raw.stdout.close()
        prev_arg = f"/dev/fd/{p_prev.stdout.fileno()}"
        fds.append(p_prev.stdout.fileno())
    else:
        p_prev = None
        prev_arg = "/dev/null"

    p_join = subprocess.Popen(
        ["join", "-t", "\t", "-a1", "-a2", "-e", "-", "-o", "0,1.2,1.3,2.2,2.3",
         f"/dev/fd/{p_cur.stdout.fileno()}", prev_arg],
        stdout=subprocess.PIPE,
        pass_fds=fds,
        env=C_ENV,
    )
    procs.append(p_join)
    p_cur.stdout.close()
    if p_prev:
        p_prev.stdout.close()

    p_awk = subprocess.Popen(
        [
            "awk",
            "-v", f"write={0 if previous is None else 1}",
            "-v", f"parking={','.join(PARKING)}",
            "-v", f"new={out['new']}",
            "-v", f"del={out['deleted']}",
            "-v", f"act={out['new_active']}",
            "-v", f"mv={out['moved']}",
            "-v", f"wl={out['went_live']}",
            AWK_DIFF,
        ],
        stdin=p_join.stdout,
        stdout=subprocess.PIPE,
        env=C_ENV,
    )
    p_join.stdout.close()

    output = p_awk.communicate()[0].decode("utf-8", "replace")
    codes = [p.wait() for p in procs] + [p_awk.returncode]
    if any(codes):
        raise RuntimeError(f"`gzip | join | awk` diff failed with exit codes {codes}")

    stats, providers, flows = {}, {}, {}
    for line in output.splitlines():
        parts = line.split("\t")
        if parts[0] == "S":
            keys = ("domains", "new", "deleted", "moved", "new_parked", "dnssec", "went_live")
            stats = dict(zip(keys, map(int, parts[1:])))
        elif parts[0] == "P" and parts[1] not in ("", "-", "?"):
            keys = ("domains", "new", "new_parked", "gained", "lost", "deleted", "dnssec")
            providers[parts[1]] = dict(zip(keys, map(int, parts[2:])))
        elif parts[0] == "F":
            flows[parts[1]] = int(parts[2])

    if previous is None:
        stats.update(new=0, deleted=0, moved=0, new_parked=0, went_live=0)
        for p in providers.values():
            p.update(new=0, new_parked=0, gained=0, lost=0, deleted=0)
    return stats, providers, flows


def top_providers(providers, flows):
    """Trim to what the site shows: biggest providers + anything notable today."""
    keep = set(sorted(providers, key=lambda p: providers[p]["domains"], reverse=True)[:TOP_PROVIDERS])
    for key in ("new", "gained", "lost"):
        keep.update(sorted(providers, key=lambda p: providers[p][key], reverse=True)[:100])
    kept = {p: providers[p] for p in keep if any(providers[p].values())}

    other = {}
    for p, v in providers.items():
        if p not in kept:
            for k, n in v.items():
                other[k] = other.get(k, 0) + n

    top_flows = dict(sorted(flows.items(), key=lambda kv: kv[1], reverse=True)[:TOP_FLOWS])
    return kept, other, top_flows


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


def latest_snapshot(tld, kind="domains"):
    """Newest lists/<date>/<kind>/<tld>.txt.gz, or None."""
    for folder in date_folders():
        path = folder / kind / f"{tld}.txt.gz"
        if path.exists():
            return path
    return None


def zone_date(last_modified):
    """Snapshot date for a zone published at `last_modified` (aware datetime)."""
    return last_modified.astimezone(TIMEZONE).strftime("%Y-%m-%d")


def snapshot_is_current(tld, last_modified):
    """
    True when our newest snapshot already is the CZDS version published at
    `last_modified`: the snapshot file was written after it was published
    (works for snapshots from before Last-Modified was recorded, too).
    """
    snapshot = latest_snapshot(tld)
    if snapshot is None:
        return False
    written = datetime.fromtimestamp(snapshot.stat().st_mtime, timezone.utc)
    return written >= last_modified


def record_published(tld, last_modified):
    """
    Note the publication time on a snapshot made before it was recorded,
    so recently_published() stops asking CZDS about it every hour.
    """
    latest = JSON_FOLDER / tld / "latest"
    data = load_json(latest)
    if not data or data.get("zone_published"):
        return
    data["zone_published"] = last_modified.isoformat(timespec="seconds")
    write_json_atomic(latest.resolve(), data)


def recently_published(tld):
    """The newest snapshot is a zone published less than RECHECK_AFTER ago."""
    data = load_json(JSON_FOLDER / tld / "latest") or {}
    try:
        published = datetime.fromisoformat(data["zone_published"])
    except (KeyError, TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - published < RECHECK_AFTER


# ==============================================================
# Process one TLD
# ==============================================================

LIST_KINDS = ("new", "deleted", "new_active", "moved", "went_live")


def process_tld(tld, zone_file, published):
    """
    Build the snapshot for the zone published at `published` (aware
    datetime), dated by that. The previous snapshot is our newest one,
    whatever its date: if it carries the same date (a second publication
    the same day, or a snapshot from before dating by Last-Modified), it is
    diffed against and then replaced.
    """
    start_time = time.time()
    date = zone_date(published)

    date_folder = LISTS_FOLDER / date
    for kind in ("domains", "ns") + LIST_KINDS:
        (date_folder / kind).mkdir(parents=True, exist_ok=True)

    work_dir = TMP_FOLDER / tld
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)

    try:
        ns_gz = work_dir / "ns.txt.gz"
        domains_gz = work_dir / "domains.txt.gz"
        out = {kind: work_dir / f"{kind}.txt" for kind in LIST_KINDS}
        # awk only creates these on the first line it writes, a zone with nothing new or deleted would have none
        for path in out.values():
            path.touch()

        extract_snapshot(zone_file, tld, ns_gz, domains_gz, work_dir)

        previous_domains = latest_snapshot(tld, "domains")
        previous_ns = latest_snapshot(tld, "ns")
        # the ns snapshot only counts if it belongs to the newest domains snapshot
        has_ns = (
            previous_ns is not None and previous_domains is not None
            and previous_ns.parent.parent == previous_domains.parent.parent
        )
        previous = previous_ns if has_ns else previous_domains
        if previous is None:
            log(f"{tld}: No previous snapshot found - skipping diff")
        elif not has_ns:
            log(f"{tld}: previous snapshot has no providers yet - moves start tomorrow")

        stats, providers, flows = diff_snapshots(ns_gz, previous, has_ns, out)

        if previous is not None:
            for kind in LIST_KINDS:
                gzip_file(out[kind], date_folder / kind / f"{tld}.txt.gz")

        # Snapshots last: together with the JSON they mark the TLD done.
        os.replace(ns_gz, date_folder / "ns" / f"{tld}.txt.gz")
        os.replace(domains_gz, date_folder / "domains" / f"{tld}.txt.gz")

        result = {
            "tld": tld,
            "date": date,
            "success": True,
            **stats,
            "zone_published": published.isoformat(timespec="seconds"),
            "elapsed": time.time() - start_time,
        }
        write_tld_json(result, datetime.now(TIMEZONE).isoformat(timespec="seconds"))
        write_providers_json(tld, date, providers, flows)
        result["pending_drop"] = update_pending_drop(tld, date)

        vs = ""
        if previous is not None:
            vs = f" (vs {previous.parent.parent.name})"
            if previous.parent.parent.name == date:
                vs = f" (replacing the earlier {date} snapshot)"
        log(
            f"{tld}: {stats['domains']:,} total, {stats['new']:,} new "
            f"({stats['new_parked']:,} parked), {stats['deleted']:,} deleted, "
            f"{stats['moved']:,} moved ({stats['went_live']:,} went live), "
            f"{result['elapsed']:.2f}s{vs}"
        )
        return result

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def read_list(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield line
    except OSError:
        return


def update_pending_drop(tld, date):
    """
    Rewrite lists/pending_drop/<tld>.txt.gz: domains that left the zone
    PENDING_MIN_DAYS..PENDING_MAX_DAYS before `date` and haven't been in a
    new list since. Walks the (small) daily new/deleted lists in date
    order, so a domain that came back and left again counts from its last
    departure. Returns the number of pending domains.
    """
    day = datetime.strptime(date, "%Y-%m-%d")
    oldest = (day - timedelta(days=PENDING_MAX_DAYS)).strftime("%Y-%m-%d")
    newest = (day - timedelta(days=PENDING_MIN_DAYS)).strftime("%Y-%m-%d")

    pending = {}
    previous_day = None
    for folder in reversed(date_folders()):  # oldest first
        folder_day = datetime.strptime(folder.name, "%Y-%m-%d")
        gap = (folder_day - previous_day).days if previous_day else None
        previous_day = folder_day
        if not (oldest <= folder.name <= date):
            continue
        for domain in read_list(folder / "new" / f"{tld}.txt.gz"):
            pending.pop(domain, None)
        if folder.name <= newest and gap is not None and gap <= PENDING_MAX_GAP_DAYS:
            for domain in read_list(folder / "deleted" / f"{tld}.txt.gz"):
                pending[domain] = folder.name

    PENDING_FOLDER.mkdir(parents=True, exist_ok=True)
    out = PENDING_FOLDER / f"{tld}.txt.gz"
    tmp = Path(str(out) + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        for domain in sorted(pending):
            f.write(f"{domain}\t{pending[domain]}\n")
    os.replace(tmp, out)
    return len(pending)


def write_pending_drop_combined(updated_at):
    """
    lists/pending_drop/by_date/<expected drop date>.txt.gz from every TLD's
    pending file, and json/pending_drop.json with the counts.
    """
    by_date_folder = PENDING_FOLDER / "by_date"
    by_date_folder.mkdir(parents=True, exist_ok=True)
    tmp_folder = PENDING_FOLDER / "by_date.tmp"
    shutil.rmtree(tmp_folder, ignore_errors=True)
    tmp_folder.mkdir()

    writers, counts, by_tld = {}, {}, {}
    try:
        for path in sorted(PENDING_FOLDER.glob("*.txt.gz")):
            tld = path.name[: -len(".txt.gz")]
            for line in read_list(path):
                domain, _, left = line.partition("\t")
                try:
                    drop = (datetime.strptime(left, "%Y-%m-%d") + timedelta(days=DROP_AFTER_DAYS)).strftime("%Y-%m-%d")
                except ValueError:
                    continue
                if drop not in writers:
                    writers[drop] = gzip.open(tmp_folder / f"{drop}.txt.gz", "wt", encoding="utf-8", compresslevel=6)
                writers[drop].write(domain + "\n")
                counts[drop] = counts.get(drop, 0) + 1
                by_tld.setdefault(drop, {})[tld] = by_tld.get(drop, {}).get(tld, 0) + 1
    finally:
        for w in writers.values():
            w.close()

    shutil.rmtree(by_date_folder, ignore_errors=True)
    os.replace(tmp_folder, by_date_folder)

    write_json_atomic(JSON_FOLDER / "pending_drop.json", {
        "drop_after_days": DROP_AFTER_DAYS,
        "dates": dict(sorted(counts.items())),
        "top_tlds": {
            d: dict(sorted(t.items(), key=lambda kv: kv[1], reverse=True)[:15])
            for d, t in sorted(by_tld.items())
        },
        "last_updated": updated_at,
        "timezone": TIMEZONE_NAME,
    })
    log(f"Pending drops: {sum(counts.values()):,} domains over {len(counts)} expected drop dates")


def run_job(tld, token, local, published):
    """Download (unless local) + process + clean up one zone. Never raises."""
    zone_file = DOWNLOAD_FOLDER / f"{tld}.txt.gz"

    try:
        if not local:
            zone_file, reason = download_zone(tld, token)
            if zone_file is None:
                return {"tld": tld, "success": False, "reason": f"Download failed: {reason}"}
        elif not zone_file.exists():
            return {"tld": tld, "success": False, "reason": f"Zone file not found: {zone_file}"}

        return process_tld(tld, zone_file, published)

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
    json_file = tld_folder / f"{result['date']}.json"

    write_json_atomic(json_file, {
        "tld": tld,
        "date": result["date"],
        "domains": result.get("domains", 0),
        "new": result.get("new", 0),
        "deleted": result.get("deleted", 0),
        "new_parked": result.get("new_parked", 0),
        "moved": result.get("moved", 0),
        "went_live": result.get("went_live", 0),
        "dnssec": result.get("dnssec", 0),
        "zone_published": result.get("zone_published"),
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


def write_providers_json(tld, date, providers, flows):
    kept, other, top_flows = top_providers(providers, flows)
    write_json_atomic(JSON_FOLDER / tld / "providers" / f"{date}.json", {
        "tld": tld,
        "date": date,
        "providers": dict(sorted(kept.items(), key=lambda kv: kv[1]["domains"], reverse=True)),
        "other": other,
        "flows": top_flows,
    })


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


SUMMED_KEYS = ("domains", "new", "deleted", "new_parked", "moved", "went_live", "dnssec")


def generate_summary_json(updated_at):
    """Totals over every TLD's latest stats."""
    summary = {
        "date": None,
        "tlds": 0,
        **{key: 0 for key in SUMMED_KEYS},
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
        summary["date"] = max(summary["date"] or "", data.get("date") or "") or None
        for key in SUMMED_KEYS:
            summary[key] += int(data.get(key, 0) or 0)

    write_json_atomic(JSON_FOLDER / "summary.json", summary)
    log(f"Summary JSON updated -> {JSON_FOLDER / 'summary.json'}")
    return summary


def write_combined_lists(date, summary, updated_at):
    """
    lists/<date>/domains/all.txt.gz - latest snapshot of every TLD
    lists/<date>/<kind>/all.txt.gz  - <date>'s new/deleted/new_active/moved, all TLDs
    json/<date>.json                - counts for the above
    """
    date_folder = LISTS_FOLDER / date

    latest_snapshots = {}
    for folder in reversed(date_folders()):  # oldest first, newer overwrite
        if folder.name > date:
            continue
        for f in (folder / "domains").glob("*.txt.gz"):
            if f.name != "all.txt.gz":
                latest_snapshots[f.name] = f

    counts = {"tlds": len(latest_snapshots), "domains": summary["domains"]}
    for key in SUMMED_KEYS[1:]:
        counts[key] = 0
    for tld_json in JSON_FOLDER.glob(f"*/{date}.json"):
        data = load_json(tld_json) or {}
        for key in SUMMED_KEYS[1:]:
            counts[key] += int(data.get(key, 0) or 0)

    sources = {"domains": [latest_snapshots[name] for name in sorted(latest_snapshots)]}
    for kind in LIST_KINDS:
        sources[kind] = sorted(
            f for f in (date_folder / kind).glob("*.txt.gz") if f.name != "all.txt.gz"
        )

    for kind, files in sources.items():
        output_file = date_folder / kind / "all.txt.gz"
        try:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            concat_gzip_files(files, output_file)
            log(f"Combined {kind} ({date}): {len(files):,} files -> {output_file}")
        except Exception as e:
            log(f"Failed to combine {kind} ({date}): {e}")

    write_json_atomic(JSON_FOLDER / f"{date}.json", {
        "date": date,
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
    for kind in ("domains", "ns"):
        seen = set()
        for folder in folders:
            for f in sorted((folder / kind).glob("*.txt.gz")):
                if f.name in seen:
                    f.unlink(missing_ok=True)
                else:
                    seen.add(f.name)

    # Everything else dated: keep KEEP_DAYS, except still-latest snapshots.
    for folder in folders:
        if folder.name >= cutoff:
            continue
        for child in folder.iterdir():
            if child.name not in ("domains", "ns"):
                shutil.rmtree(child, ignore_errors=True)
        for kind in ("domains", "ns"):
            try:
                (folder / kind).rmdir()  # only succeeds if empty
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass

    for json_file in JSON_FOLDER.glob("**/????-??-??.json"):
        if json_file.stem >= cutoff:
            continue
        tld_folder = json_file.parent
        if tld_folder.name == "providers":
            tld_folder = tld_folder.parent
        latest = tld_folder / "latest"
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

def select_published_zones(tlds, token, local, check):
    """
    Map each zone that needs processing to its CZDS publication time.
    check=False (explicit TLDs, --force) takes every zone as it is.
    """
    now = datetime.now(timezone.utc)
    if local:
        return {
            t: datetime.fromtimestamp((DOWNLOAD_FOLDER / f"{t}.txt.gz").stat().st_mtime, timezone.utc)
            for t in tlds
        }

    candidates = [t for t in tlds if not (check and recently_published(t))]
    skipped = len(tlds) - len(candidates)

    def head(tld):
        try:
            return tld, zone_last_modified(tld, token)
        except Exception as e:
            log(f"{tld}: Last-Modified check failed ({e}) - processing anyway")
            return tld, None

    published = {}
    unchanged = 0
    with ThreadPoolExecutor(max_workers=HEAD_WORKERS) as executor:
        for tld, last_modified in executor.map(head, candidates):
            last_modified = last_modified or now
            if check and snapshot_is_current(tld, last_modified):
                unchanged += 1
                record_published(tld, last_modified)
                continue
            published[tld] = last_modified

    if check:
        log(
            f"Zones published less than {RECHECK_AFTER} ago: {skipped}, "
            f"checked: {len(candidates)}, not republished yet: {unchanged}, new: {len(published)}"
        )
    return published


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

    if "--pending" in args:
        for tld_folder in sorted(JSON_FOLDER.iterdir()):
            data = load_json(tld_folder / "latest") if tld_folder.is_dir() else None
            if data and data.get("date"):
                update_pending_drop(data.get("tld", tld_folder.name), data["date"])
        write_pending_drop_combined(datetime.now(TIMEZONE).isoformat(timespec="seconds"))
        return 0

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

    published = select_published_zones(tlds, token, local, check=not (requested or force))
    tlds = sorted(published)

    if not tlds:
        log("Nothing to do - no zone has a newer version in CZDS yet.")
        return 2

    # Biggest first (by last snapshot size), so the multi-GB zones run
    # alongside the long tail of small ones instead of after it.
    def previous_size(tld):
        prev = latest_snapshot(tld)
        return prev.stat().st_size if prev else 0

    tlds.sort(key=previous_size, reverse=True)
    dates = sorted({zone_date(published[t]) for t in tlds})

    log(f"Zones to process: {len(tlds)} ({WORKERS} at a time), snapshot dates: {', '.join(dates)}")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(run_job, tld, token, local, published[tld]) for tld in tlds]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if not result["success"]:
                log(f"{result['tld']}: FAILED - {result['reason']}")

    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    updated_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    summary = generate_summary_json(updated_at)
    for date in sorted({r["date"] for r in succeeded}):
        write_combined_lists(date, summary, updated_at)
    if succeeded:
        write_pending_drop_combined(updated_at)
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
