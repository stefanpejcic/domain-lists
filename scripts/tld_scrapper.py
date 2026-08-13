#!/usr/bin/env python3
import os
import re
import sys
import json
import argparse
import concurrent.futures
from pathlib import Path

import requests

HEADERS = {'User-Agent': 'Python TLD Scraper'}
MAX_WORKERS = 2

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
JSON_FOLDER = REPO_ROOT / "json"


def fetch_html(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"  fetch failed: {url} ({e})", file=sys.stderr)
        return None


def idn_to_ascii(utld):
    try:
        return utld.encode('idna').decode('ascii')
    except Exception:
        return utld


def parse_section(html, title):
    m = re.search(title + r'(.*?)(<h[23]>|Name Servers|Registry Information|$)', html, re.S | re.I)
    if not m:
        return {'address': '', 'email': [], 'e164Voice': []}

    section = m.group(1)
    section = re.sub(r'<br\s*/?>', '\n', section, flags=re.I)
    section = re.sub(r'<[^>]+>', '', section)
    lines = [l.strip() for l in section.split('\n')]

    emails, phones, address = [], [], []
    for line in lines:
        if not line:
            continue
        if 'email:' in line.lower():
            emails.append(re.sub(r'(?i)email:', '', line).strip())
        elif 'voice:' in line.lower():
            phones.append(re.sub(r'[^\d+]', '', line))
        else:
            address.append(line)

    return {'address': '\n'.join(address), 'email': emails, 'e164Voice': phones}


def parse_tld_html(raw_html):
    m = re.search(r'<main>(.*?)</main>', raw_html, re.S | re.I)
    html = m.group(1) if m else raw_html

    m = re.search(r'Delegation Record for \.([a-z0-9-]+)', html, re.I)
    utld = m.group(1).lower() if m else ''
    atld = idn_to_ascii(utld)

    description = ''
    m = re.search(r'<h1>.*?</h1>.*?<p>\((.*?)\)</p>', html, re.S | re.I)
    if m:
        description = m.group(1).strip()

    tld_type = 'unknown'
    d = description.lower()
    if 'generic' in d:
        tld_type = 'gTLD'
    elif 'country' in d:
        tld_type = 'ccTLD'
    elif 'test' in d:
        tld_type = 'testTLD'
    elif 'restricted' in d:
        tld_type = 'restrictedgTLD'
    elif 'infrastructure' in d:
        tld_type = 'infrastructureTLD'
    elif 'sponsored' in d:
        tld_type = 'sponsoredTLD'
    elif len(atld) == 2:
        tld_type = 'ccTLD'

    sponsor = ''
    m = re.search(r'Sponsoring Organisation(.*?)(<h[23]>|Administrative Contact)', html, re.S | re.I)
    if m:
        s = re.sub(r'<br\s*/?>', '\n', m.group(1), flags=re.I)
        sponsor = re.sub(r'<[^>]+>', '', s).strip()

    admin = parse_section(html, 'Administrative Contact')
    tech = parse_section(html, 'Technical Contact')

    nameservers = []
    m = re.search(r'Name Servers(.*?)(<h[23]>|Registry Information|$)', html, re.S | re.I)
    if m:
        ns_text = re.sub(r'<br\s*/?>', '\n', m.group(1), flags=re.I)
        ns_text = re.sub(r'<[^>]+>', '', ns_text)
        current = None
        for line in [l.strip() for l in ns_text.split('\n')]:
            if not line:
                continue
            if not re.search(r'\d', line):
                current = line
                nameservers.append({'name': current, 'addr': []})
            elif nameservers:
                nameservers[-1]['addr'].append(line)

    registration_url = whois_server = rdap_server = ''
    m = re.search(r'<h2>Registry Information</h2>(.*?)$', html, re.S | re.I)
    if m:
        reg = m.group(1)
        um = re.search(r'<b>\s*URL for registration services:\s*</b>\s*<a href="([^"]+)"', reg, re.I)
        if um:
            registration_url = um.group(1).strip()
        wm = re.search(r'<b>\s*WHOIS Server:\s*</b>\s*([^\s<]+)', reg, re.I)
        if wm:
            whois_server = wm.group(1).strip()
        rm = re.search(r'<b>\s*RDAP Server:\s*</b>\s*([^\s<]+)', reg, re.I)
        if rm:
            rdap_server = rm.group(1).strip()

    reg_date_m = re.search(r'Registration date (\d{4}-\d{2}-\d{2})', html, re.I)
    upd_date_m = re.search(r'Record last updated (\d{4}-\d{2}-\d{2})', html, re.I)

    return {
        'aName': atld,
        'uName': utld,
        'description': description,
        'type': tld_type,
        'registrationServiceURL': registration_url,
        'whois_server': whois_server,
        'rdap_server': rdap_server,
        'registrationDate': reg_date_m.group(1) if reg_date_m else '',
        'lastUpdate': upd_date_m.group(1) if upd_date_m else '',
        'sponsor': sponsor,
        'admin': admin,
        'tech': tech,
        'nameservers': nameservers,
    }


def process_tld(tld):
    print(f"Processing {tld}...")
    url = f"https://www.iana.org/domains/root/db/{tld}.html"
    html = fetch_html(url)
    if not html:
        return tld, None

    data = parse_tld_html(html)
    tld_folder = JSON_FOLDER / tld
    tld_folder.mkdir(parents=True, exist_ok=True)
    with open(tld_folder / "info.json", 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return tld, data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tld', help='Process a single TLD instead of the full list')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS)
    args = parser.parse_args()

    JSON_FOLDER.mkdir(parents=True, exist_ok=True)

    if args.tld:
        tlds = [args.tld.lower()]
    else:
        tld_list_data = fetch_html('https://data.iana.org/TLD/tlds-alpha-by-domain.txt')
        tlds = [
            line.strip().lower()
            for line in tld_list_data.splitlines()
            if line.strip() and not line.startswith('#')
        ]

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_tld, tld): tld for tld in tlds}
        for future in concurrent.futures.as_completed(futures):
            tld, data = future.result()
            if data:
                results[tld] = data

    if not args.tld:
        all_tlds = [results[t] for t in tlds if t in results]
        with open(REPO_ROOT / "info.json", 'w') as f:
            json.dump({'tlds': all_tlds}, f, indent=2, ensure_ascii=False)
        print("Saved combined info.json")
    else:
        print(json.dumps(results.get(args.tld.lower(), {}), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
