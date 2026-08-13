import os
import sys
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json

# ---------------------------------------
# 1. Authenticate and get CZDS API token
# ---------------------------------------

AUTH_URL = "https://account-api.icann.org/api/authenticate"

USERNAME = ""
PASSWORD = ""

auth_payload = {
    "username": USERNAME,
    "password": PASSWORD
}

auth_headers = {
    "Content-Type": "application/json"
}

auth_response = requests.post(AUTH_URL, headers=auth_headers, data=json.dumps(auth_payload))

if auth_response.status_code != 200:
    raise Exception(f"Authentication failed: {auth_response.status_code} -> {auth_response.text}")

auth_data = auth_response.json()
API_TOKEN = auth_data.get("accessToken")

if not API_TOKEN:
    raise Exception("Authentication response does not contain an access token.")

print("Token acquired successfully.")




# ------------------------------
# Setup folders
# ------------------------------
script_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.dirname(script_dir)
output_folder = os.path.join(repo_root, "downloads")
os.makedirs(output_folder, exist_ok=True)

log_file = os.path.join(repo_root, "icann_download_log.txt")

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ------------------------------
# Download TLD list from IANA
# ------------------------------
start_time = time.time()
start_tld = sys.argv[1].lower() if len(sys.argv) > 1 else None

iana_tld_url = "https://data.iana.org/TLD/tlds-alpha-by-domain.txt"

log(f"----------------------------------------------")
log("Downloading TLD list from IANA...")
response = requests.get(iana_tld_url)
if response.status_code != 200:
    raise Exception(f"Failed to download TLD list: {response.status_code}")

tlds = [line.strip().lower() for line in response.text.splitlines() 
        if line.strip() and not line.startswith("#")]
log(f"Total TLDs in {iana_tld_url} list: {len(tlds)}")

if start_tld:
    if start_tld in tlds:
        start_index = tlds.index(start_tld)
        tlds = tlds[start_index:]
        log(f"Starting from TLD '{start_tld}' (index {start_index}).")
    else:
        log(f"Start TLD '{start_tld}' not found. Starting from beginning.")

log(f"Processing {len(tlds)} TLDs in batches of 5 at a time")
headers = {"Authorization": f"Bearer {API_TOKEN}"}

# ------------------------------
# Download function
# ------------------------------
def download_tld(tld):
    url = f"https://czds-api.icann.org/czds/downloads/{tld}.zone"
    output_file = os.path.join(output_folder, f"{tld}.txt.gz")
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=120)
        if r.status_code == 200:
            with open(output_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return tld, True, None
        else:
            return tld, False, r.status_code
    except Exception as e:
        return tld, False, str(e)

# ------------------------------
# Download all TLDs in parallel
# ------------------------------
success_count = 0
fail_count = 0

with ThreadPoolExecutor(max_workers=5) as executor:
    futures = {executor.submit(download_tld, tld): tld for tld in tlds}
    for future in as_completed(futures):
        tld = futures[future]
        try:
            tld_name, success, reason = future.result()
            if success:
                log(f"Downloading {tld_name}... Success ({reason or 'Downloaded'})")
                success_count += 1
            else:
                log(f"Downloading {tld_name}... Failed ({reason})")
                fail_count += 1
        except Exception as e:
            log(f"Downloading {tld}... Failed ({e})")
            fail_count += 1

# ------------------------------
# Summary
# ------------------------------
elapsed = time.time() - start_time
log("\nDownload Summary:")
log(f"Successful downloads: {success_count}")
log(f"Failed downloads: {fail_count}")
log(f"Total elapsed time: {elapsed:.2f} seconds")
log(f"----------------------------------------------")
