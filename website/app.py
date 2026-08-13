#!/usr/bin/env python3
import json
import gzip
from pathlib import Path
from flask import Flask, render_template, abort, send_file, request, jsonify

app = Flask(__name__)

# ==============================================================
# Configuration
# ==============================================================
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
JSON_FOLDER = REPO_ROOT / "json"
LISTS_FOLDER = REPO_ROOT / "lists"
DOMAINS_PER_PAGE = 100

KIND_LABELS = {
    "domains": "All",
    "new": "New",
    "deleted": "Deleted",
}

VALID_PATTERNS = {
    "one-letter": "One Letter",
    "2-letters": "2 Letters",
    "3-letters": "3 Letters",
    "4-letters": "4 Letters",
    "one-number": "One Number",
    "two-numbers": "Two Numbers (NN)",
    "one-word": "One Word",
}


# ==============================================================
# Helpers
# ==============================================================
def unicode_domain(domain):
    """Convert an IDN/Punycode domain to its Unicode representation."""
    try:
        return domain.encode("ascii").decode("idna")
    except (UnicodeError, UnicodeEncodeError):
        return domain

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
        if not folder.is_dir() or folder.name in ("short_patterns", "2letter"):
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

    return sorted(tlds, key=lambda x: x.get("tld", ""))


def latest_date_with_pattern(pattern_type=None):
    """
    Find the most recent date folder that contains the pattern file,
    or general deleted short_patterns data.
    """
    if not LISTS_FOLDER.exists():
        return None

    dates = []
    for date_folder in LISTS_FOLDER.iterdir():
        if not date_folder.is_dir():
            continue

        if pattern_type:
            file_path = date_folder / pattern_type / "deleted.txt.gz"
            if file_path.exists():
                dates.append(date_folder.name)
        else:
            # Check if short_patterns json exists for this date
            if (JSON_FOLDER / "short_patterns" / f"{date_folder.name}.json").exists():
                dates.append(date_folder.name)

    if not dates:
        return None

    return sorted(dates, reverse=True)[0]


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


def get_download_sizes(tld):
    date = latest_date_with_domains(tld)
    if date is None:
        return {"domains": None, "new": None, "deleted": None}

    sizes = {}
    for kind in ("domains", "new", "deleted"):
        path = LISTS_FOLDER / date / kind / f"{tld}.txt.gz"
        sizes[kind] = path.stat().st_size if path.exists() else None
    return sizes


def browse_domains_from_path(path, search, page):
    """
    Stream a sorted gzip file, optionally filter by substring,
    and return one page of results.
    """
    if not path.exists():
        return [], False, 0

    search = (search or "").strip().lower()
    start = (page - 1) * DOMAINS_PER_PAGE
    end = start + DOMAINS_PER_PAGE

    results = []
    matched_count = 0
    has_more = False

    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            domain = line.strip()
            if not domain:
                continue
            if search and search not in domain:
                continue

            if matched_count >= start and matched_count < end:
                results.append(unicode_domain(domain))
            matched_count += 1

            if matched_count >= end:
                has_more = True
                break

    return results, has_more, matched_count


def browse_domains(tld, date, kind, search, page):
    path = LISTS_FOLDER / date / kind / f"{tld}.txt.gz"
    return browse_domains_from_path(path, search, page)


def validate_tld(tld):
    tld = tld.lower().strip()
    if "/" in tld or "\\" in tld:
        abort(404)
    return tld


# ==============================================================
# Routes
# ==============================================================
@app.route("/")
def index():
    summary = load_summary()
    sizes = get_download_sizes('all')
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

    for tld in tlds:
        tld["tld_unicode"] = unicode_domain(tld.get("tld", ""))
        
    return render_template(
        "index.html",
        sizes=sizes,
        summary=summary,
        tlds=tlds,
    )


# ==============================================================
# Deleted Short Pattern Routes
# ==============================================================
@app.route("/deleted", defaults={"pattern_type": None})
@app.route("/deleted/<pattern_type>")
def deleted(pattern_type):
    if pattern_type:
        pattern_type = pattern_type.lower().strip()

        if pattern_type not in VALID_PATTERNS:
            abort(404)

        date = latest_date_with_pattern(pattern_type)
        if not date:
            abort(404)

        summary_data = load_json(
            JSON_FOLDER / "short_patterns" / f"{date}.json"
        ) or {}

        counts = summary_data.get("patterns", {})

        file_path = (
            LISTS_FOLDER
            / date
            / pattern_type
            / "deleted.txt.gz"
        )

        file_size = file_path.stat().st_size if file_path.exists() else 0

        return render_template(
            "deleted_browse.html",
            pattern_type=pattern_type,
            pattern_label=VALID_PATTERNS[pattern_type],
            count=counts.get(pattern_type, 0),
            date=date,
            file_size=file_size,
            patterns=VALID_PATTERNS,
        )

    date = latest_date_with_pattern()

    if not date:
        return jsonify({
            "error": "No pattern statistics found",
            "patterns": {}
        }), 404

    json_path = JSON_FOLDER / "short_patterns" / f"{date}.json"
    data = load_json(json_path)

    if not data:
        return jsonify({
            "error": "JSON file missing",
            "patterns": {}
        }), 404

    return render_template(
        "deleted_browse.html",
        patterns=VALID_PATTERNS,
        data=data,
    )


@app.route("/deleted/<pattern_type>/browse")
def browse_deleted_pattern(pattern_type):
    """API endpoint for paginated AJAX results for deleted domain patterns."""
    pattern_type = pattern_type.lower().strip()
    if pattern_type not in VALID_PATTERNS:
        abort(404)

    search = request.args.get("search", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    date = latest_date_with_pattern(pattern_type)
    if not date:
        return jsonify({"domains": [], "has_more": False, "page": page})

    file_path = LISTS_FOLDER / date / pattern_type / "deleted.txt.gz"
    domains, has_more, matched_count = browse_domains_from_path(file_path, search, page)

    return jsonify({
        "domains": domains,
        "has_more": has_more,
        "page": page,
        "per_page": DOMAINS_PER_PAGE,
    })


@app.route("/deleted/<pattern_type>/download")
def download_deleted_pattern(pattern_type):
    """Download compressed list for a specific pattern."""
    pattern_type = pattern_type.lower().strip()
    if pattern_type not in VALID_PATTERNS:
        abort(404)

    date = latest_date_with_pattern(pattern_type)
    if not date:
        abort(404)

    file_path = LISTS_FOLDER / date / pattern_type / "deleted.txt.gz"
    if not file_path.exists():
        abort(404)

    download_name = f"deleted-{pattern_type}-{date}.txt.gz"
    return send_file(
        file_path,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/gzip",
    )


# ==============================================================
# Browse pages: /tld/<tld>/all, /tld/<tld>/new, /tld/<tld>/deleted
# ==============================================================
BROWSE_KIND_BY_SLUG = {
    "all": "domains",
    "new": "new",
    "deleted": "deleted",
}


@app.route("/tld/<tld>/<slug>")
def tld_browse_page(tld, slug):
    tld = validate_tld(tld)

    if slug not in BROWSE_KIND_BY_SLUG:
        abort(404)
    kind = BROWSE_KIND_BY_SLUG[slug]

    latest_file = JSON_FOLDER / tld / "latest"
    data = load_json(latest_file)
    if data is None:
        abort(404)
    data["tld_unicode"] = unicode_domain(data.get("tld", ""))

    sizes = get_download_sizes(tld)
    
    return render_template(
        "tld_browse.html",
        data=data,
        sizes=sizes,
        kind=kind,
        slug=slug,
    )


@app.route("/tld/<tld>/browse")
def browse_tld_domains(tld):
    tld = validate_tld(tld)

    kind = request.args.get("kind", "domains")
    if kind not in ("domains", "new", "deleted"):
        abort(404)

    search = request.args.get("search", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    date = latest_date_with_domains(tld)
    if date is None:
        return jsonify({"domains": [], "has_more": False, "page": page})

    domains, has_more, matched_count = browse_domains(tld, date, kind, search, page)

    return jsonify({
        "domains": domains,
        "has_more": has_more,
        "page": page,
        "per_page": DOMAINS_PER_PAGE,
    })


# ==============================================================
# Downloads
# ==============================================================
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
# Domain Pattern Availability
# ==============================================================
@app.route("/patterns")
def pattern_availability():
    """
    Display domain pattern availability for all TLDs.

    Data is generated by the pattern counter script and stored in:
        json/pattern_counters.json
    """

    json_path = JSON_FOLDER / "pattern_counters.json"
    data = load_json(json_path)

    if not data:
        abort(404)

    return render_template(
        "patterns.html",
        data=data,
        tlds=data.get("tlds", {}),
        patterns=data.get("patterns", {}),
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
