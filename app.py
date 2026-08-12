#!/usr/bin/env python3
import json
from pathlib import Path
from flask import Flask, render_template, abort, send_file

app = Flask(__name__)

# ==============================================================
# Configuration
# ==============================================================
BASE_DIR = Path(__file__).resolve().parent
JSON_FOLDER = BASE_DIR / "json"


# ==============================================================
# Helpers
# ==============================================================
def load_json(filename):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_summary():
    return load_json(JSON_FOLDER / "summary.json")


def load_tld_info(tld):
    """IANA registry info scraped into json/<tld>/info.json"""
    return load_json(JSON_FOLDER / tld / "info.json")


def load_tlds():
    """
    Load the latest zone-stats JSON for every TLD, merged with its
    IANA registry info (json/<tld>/info.json) when available.
    """
    tlds = []
    if not JSON_FOLDER.exists():
        return tlds

    for folder in JSON_FOLDER.iterdir():
        if not folder.is_dir():
            continue

        latest_file = folder / "latest"
        if not latest_file.exists():
            continue

        data = load_json(latest_file)
        if not data:
            continue

        info = load_json(folder / "info.json")
        if info:
            data = {**data, "info": info}

        tlds.append(data)

    return sorted(tlds, key=lambda x: x.get("tld", ""))


# ==============================================================
# Routes
# ==============================================================
@app.route("/")
def index():
    summary = load_summary()
    if summary is None:
        summary = {
            "date": None,
            "tlds": 0,
            "domains": 0,
            "new": 0,
            "deleted": 0,
            "last_updated": None,
            "timezone": None,
        }

    tlds = load_tlds()
    return render_template(
        "index.html",
        summary=summary,
        tlds=tlds,
    )

def human_size(num_bytes):
    if num_bytes is None:
        return None
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def get_download_sizes(tld):
    date = latest_date_with_domains(tld)
    if date is None:
        return {"domains": None, "new": None, "deleted": None}

    sizes = {}
    for kind in ("domains", "new", "deleted"):
        path = LISTS_FOLDER / date / kind / f"{tld}.txt.gz"
        sizes[kind] = path.stat().st_size if path.exists() else None
    return sizes

@app.route("/tld/<tld>")
def tld_detail(tld):
    tld = tld.lower().strip()
    if "/" in tld or "\\" in tld:
        abort(404)

    latest_file = JSON_FOLDER / tld / "latest"
    data = load_json(latest_file)
    if data is None:
        abort(404)

    info = load_tld_info(tld)
    sizes = get_download_sizes(tld)

    return render_template(
        "tld.html",
        data=data,
        info=info,
        sizes=sizes,
    )


# ==============================================================
# Downloads
# ==============================================================
LISTS_FOLDER = BASE_DIR / "lists"


def latest_date_with_domains(tld):
    """
    Find the most recent date folder that has a domains file
    for this TLD (mirrors process_zones.py's snapshot logic).
    """
    if not LISTS_FOLDER.exists():
        return None

    dates = []
    for date_folder in LISTS_FOLDER.iterdir():
        if not date_folder.is_dir():
            continue
        if (date_folder / "domains" / f"{tld}.txt.gz").exists():
            dates.append(date_folder.name)

    if not dates:
        return None

    return sorted(dates, reverse=True)[0]


@app.route("/tld/<tld>/download/<kind>")
def download_tld_list(tld, kind):
    tld = tld.lower().strip()
    if "/" in tld or "\\" in tld or "." in tld:
        abort(404)
    if kind not in ("domains", "new", "deleted"):
        abort(404)

    date = latest_date_with_domains(tld)
    if date is None:
        abort(404)

    file_path = LISTS_FOLDER / date / kind / f"{tld}.txt.gz"
    if not file_path.exists():
        abort(404)

    download_name = f"{tld}-{kind}-{date}.txt.gz"
    return send_file(
        file_path,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/gzip",
    )


# ==============================================================
# Run
# ==============================================================
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=80,
        debug=False,
    )
