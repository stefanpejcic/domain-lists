#!/usr/bin/env python3
"""
Download zone files from ICANN CZDS.

Used two ways:
  - as a module by process_zones.py, which downloads, processes and deletes
    one zone at a time (so the full ~10GB of zone files never sits on disk);
  - standalone, to just download zones into downloads/:

      python3 scripts/download_zones.py            # every approved zone
      python3 scripts/download_zones.py com net    # only these
      python3 scripts/download_zones.py --from org # approved zones from 'org' on

Only zones this CZDS account is approved for are requested (taken from the
CZDS /downloads/links endpoint), so ccTLDs and unapproved gTLDs are no longer
hammered with requests that always 404/403.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# ------------------------------
# Configuration
# ------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DOWNLOAD_FOLDER = REPO_ROOT / "downloads"
LOG_FILE = REPO_ROOT / "icann_download_log.txt"

load_dotenv(REPO_ROOT / ".env")

AUTH_URL = "https://account-api.icann.org/api/authenticate"
LINKS_URL = "https://czds-api.icann.org/czds/downloads/links"
ZONE_URL = "https://czds-api.icann.org/czds/downloads/{tld}.zone"

DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_SECONDS = 30
# (connect, read) - read is per chunk, not the whole transfer
TIMEOUT = (30, 300)
PARALLEL_DOWNLOADS = 4


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ------------------------------
# CZDS API
# ------------------------------
def get_token():
    """Authenticate against ICANN and return a CZDS access token (valid 24h)."""
    username = os.getenv("CZDS_USERNAME")
    password = os.getenv("CZDS_PASSWORD")

    if not username or not password:
        raise RuntimeError("CZDS_USERNAME and CZDS_PASSWORD must be set in .env")

    last_error = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            r = requests.post(
                AUTH_URL,
                json={"username": username, "password": password},
                timeout=TIMEOUT,
            )
            if r.status_code == 200 and r.json().get("accessToken"):
                return r.json()["accessToken"]
            last_error = f"{r.status_code} -> {r.text[:300]}"
            # Bad credentials won't fix themselves on retry.
            if r.status_code in (401, 403):
                break
        except requests.RequestException as e:
            last_error = str(e)
        time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError(f"CZDS authentication failed: {last_error}")


def approved_tlds(token):
    """Return the sorted list of TLDs this account may download."""
    r = requests.get(
        LINKS_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    tlds = []
    for url in r.json():
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".zone"):
            tlds.append(name[: -len(".zone")].lower())

    return sorted(set(tlds))


def download_zone(tld, token, output_folder=DOWNLOAD_FOLDER):
    """
    Download one zone to <output_folder>/<tld>.txt.gz, retrying transient
    errors. Writes to a .part file first, so a half-finished download is
    never mistaken for a complete zone.

    Returns (path, None) on success or (None, reason) on failure.
    """
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    output_file = output_folder / f"{tld}.txt.gz"
    part_file = output_folder / f"{tld}.txt.gz.part"

    headers = {"Authorization": f"Bearer {token}"}
    reason = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            with requests.get(
                ZONE_URL.format(tld=tld), headers=headers, stream=True, timeout=TIMEOUT
            ) as r:
                if r.status_code == 200:
                    with open(part_file, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            f.write(chunk)
                    os.replace(part_file, output_file)
                    return output_file, None

                reason = f"HTTP {r.status_code}"
                # Not approved / no such zone: retrying won't help.
                if r.status_code in (401, 403, 404):
                    break
        except (requests.RequestException, OSError) as e:
            reason = str(e)

        part_file.unlink(missing_ok=True)
        if attempt < DOWNLOAD_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    part_file.unlink(missing_ok=True)
    return None, reason


# ------------------------------
# Standalone usage
# ------------------------------
def main():
    start_time = time.time()
    args = [a.lower() for a in sys.argv[1:]]

    log("----------------------------------------------")

    try:
        token = get_token()
        tlds = approved_tlds(token)
    except Exception as e:
        log(f"ERROR: {e}")
        return 1

    log(f"Approved zones in CZDS: {len(tlds)}")

    if len(args) == 2 and args[0] == "--from":
        tlds = [t for t in tlds if t >= args[1]]
    elif args:
        tlds = [t for t in tlds if t in args]

    log(f"Downloading {len(tlds)} zones, {PARALLEL_DOWNLOADS} at a time")

    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as executor:
        futures = {executor.submit(download_zone, tld, token): tld for tld in tlds}
        for future in as_completed(futures):
            tld = futures[future]
            path, reason = future.result()
            if path:
                log(f"Downloading {tld}... Success")
                success_count += 1
            else:
                log(f"Downloading {tld}... Failed ({reason})")
                fail_count += 1

    log("Download Summary:")
    log(f"Successful downloads: {success_count}")
    log(f"Failed downloads: {fail_count}")
    log(f"Total elapsed time: {time.time() - start_time:.2f} seconds")
    log("----------------------------------------------")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
