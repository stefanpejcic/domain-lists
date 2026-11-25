#!/bin/bash
# Runs inside the "domains" container (ofelia job, every hour, no-overlap).
# Registries publish their zones in CZDS at different times (00:00 to
# ~10:00 UTC), so each run processes only the zones published since the
# last one (and retries failures); runs where nothing new was published
# exit right away. Failure alerts are emailed by process_zones.py itself.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="python3 -B"
# venv from install.sh (non-container installs)
[ -x "$REPO_DIR/venv/bin/python" ] && PY="$REPO_DIR/venv/bin/python -B"

# download + process every zone CZDS has published a newer version of
$PY "$REPO_DIR/scripts/process_zones.py"
if [ $? -eq 2 ]; then
    echo "Nothing new to process."
    exit 0
fi

# combine per-TLD DNS provider stats (/providers)
$PY "$REPO_DIR/scripts/generate_providers.py"

# generate trending keyword lists (needs today's new/all.txt.gz from process_zones.py)
$PY "$REPO_DIR/scripts/generate_keyword_lists.py"

# cross-reference today's new domains against threat intel feeds
$PY "$REPO_DIR/scripts/generate_threat_matches.py"

# short deleted domains (/deleted/<pattern>) and pattern counters (/patterns)
$PY "$REPO_DIR/scripts/extract_short_domains.py"
$PY "$REPO_DIR/scripts/generate_distribution.py"

# 3. download TLD info
# NO LONGER USED: $PY "$REPO_DIR/scripts/tld_scrapper.py"

# generate sitemaps
$PY "$REPO_DIR/scripts/generate_sitemap.py"

# generate OG images
$PY "$REPO_DIR/scripts/generate_og_images.py"

# cleanup (zones are deleted as they're processed; this catches leftovers)
rm -rf "$REPO_DIR/downloads/" "$REPO_DIR/tmp/"

# The website reads everything from disk on each request, so no restart
# is needed.
