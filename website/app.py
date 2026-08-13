#!/usr/bin/env python3
import json
import gzip
import time
import uuid
import threading
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
DOWNLOAD_KINDS = ("domains", "new", "deleted")
DOWNLOAD_TOKEN_TTL = 10 * 60  # seconds a prepared download link stays valid

# In-memory store for single-use download tokens: token -> spec dict.
# Each spec also carries an "expires" timestamp. Tokens are popped (and thus
# invalidated) the moment they are redeemed, so a link can never be reused.
_download_tokens = {}
_download_tokens_lock = threading.Lock()

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

        tlds.append(data)

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
                results.append(domain)
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


def punycode_to_unicode(value):
    """Render punycode (xn--) domains/TLDs as Unicode for display."""
    if not value:
        return value
    try:
        return value.lower().encode("ascii").decode("idna")
    except (UnicodeError, ValueError):
        return value


app.jinja_env.filters["to_unicode"] = punycode_to_unicode


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
# Guarded browser downloads: captcha landing page + single-use links
#
# There is no direct/permanent download URL. Browser users land on a
# page that (eventually) makes them pass a captcha and a short
# countdown, which then hands them a randomized, one-time-use download
# link that can never be reused. A future API will offer key-based
# direct access; until then this is the only way to get a file.
# ==============================================================
def _resolve_download_spec(spec):
    """Resolve a token spec to (file_path, download_name, mimetype), or None."""
    if spec.get("type") == "tld":
        tld = spec.get("tld", "")
        kind = spec.get("kind")
        if kind not in DOWNLOAD_KINDS:
            return None

        date = latest_date_with_domains(tld)
        if date is None:
            return None

        file_path = LISTS_FOLDER / date / kind / f"{tld}.txt.gz"
        if not file_path.exists():
            return None

        return file_path, f"{tld}-{kind}-{date}.txt.gz", "application/gzip"

    if spec.get("type") == "pattern":
        pattern_type = spec.get("pattern_type", "")
        if pattern_type not in VALID_PATTERNS:
            return None

        date = latest_date_with_pattern(pattern_type)
        if date is None:
            return None

        file_path = LISTS_FOLDER / date / pattern_type / "deleted.txt.gz"
        if not file_path.exists():
            return None

        return file_path, f"deleted-{pattern_type}-{date}.txt.gz", "application/gzip"

    return None


def _cleanup_download_tokens():
    now = time.time()
    for token in [t for t, spec in _download_tokens.items() if spec["expires"] < now]:
        _download_tokens.pop(token, None)


@app.route("/download/deleted/<pattern_type>")
def download_pattern_landing(pattern_type):
    pattern_type = pattern_type.lower().strip()
    if pattern_type not in VALID_PATTERNS:
        abort(404)

    date = latest_date_with_pattern(pattern_type)
    if not date:
        abort(404)

    file_path = LISTS_FOLDER / date / pattern_type / "deleted.txt.gz"
    if not file_path.exists():
        abort(404)

    return render_template(
        "download.html",
        heading=f"Download deleted domains — {VALID_PATTERNS[pattern_type]}",
        file_size=file_path.stat().st_size,
        payload={"type": "pattern", "pattern_type": pattern_type},
    )


@app.route("/download/<tld>")
def download_landing(tld):
    tld = tld.lower().strip()
    if "/" in tld or "\\" in tld or "." in tld:
        abort(404)

    kind = request.args.get("kind", "domains")
    if kind not in DOWNLOAD_KINDS:
        abort(404)

    if tld == "all":
        display_tld = "all zones"
    else:
        if load_json(JSON_FOLDER / tld / "latest") is None:
            abort(404)
        display_tld = "." + punycode_to_unicode(tld)

    sizes = get_download_sizes(tld)
    file_size = sizes.get(kind)
    if file_size is None:
        abort(404)

    kind_label = {
        "domains": "all registered",
        "new": "newly registered",
        "deleted": "deleted",
    }[kind]

    return render_template(
        "download.html",
        heading=f"Download {kind_label} {display_tld} domains",
        file_size=file_size,
        payload={"type": "tld", "tld": tld, "kind": kind},
    )


@app.route("/download/prepare", methods=["POST"])
def download_prepare():
    payload = request.get_json(silent=True) or {}
    dtype = payload.get("type")

    if dtype == "tld":
        tld = str(payload.get("tld", "")).lower().strip()
        kind = payload.get("kind")
        if "/" in tld or "\\" in tld or "." in tld or kind not in DOWNLOAD_KINDS:
            abort(400)
        spec = {"type": "tld", "tld": tld, "kind": kind}
    elif dtype == "pattern":
        pattern_type = str(payload.get("pattern_type", "")).lower().strip()
        if pattern_type not in VALID_PATTERNS:
            abort(400)
        spec = {"type": "pattern", "pattern_type": pattern_type}
    else:
        abort(400)

    if _resolve_download_spec(spec) is None:
        return jsonify({"error": "File not available"}), 404

    token = uuid.uuid4().hex
    with _download_tokens_lock:
        _cleanup_download_tokens()
        spec["expires"] = time.time() + DOWNLOAD_TOKEN_TTL
        _download_tokens[token] = spec

    return jsonify({"token": token, "expires_in": DOWNLOAD_TOKEN_TTL})


@app.route("/download/file/<token>")
def download_file(token):
    with _download_tokens_lock:
        spec = _download_tokens.pop(token, None)

    if spec is None or spec["expires"] < time.time():
        abort(404)

    resolved = _resolve_download_spec(spec)
    if resolved is None:
        abort(404)

    file_path, download_name, mimetype = resolved
    return send_file(
        file_path,
        as_attachment=True,
        download_name=download_name,
        mimetype=mimetype,
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
