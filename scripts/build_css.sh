#!/bin/bash
# Builds a static, purged, minified website/static/css/style.css from
# website/static/css/input.css + tailwind.config.js, replacing the
# cdn.tailwindcss.com <script> (which Tailwind's own docs say is not
# meant for production - no purging, recompiles in-browser on every
# page load).
#
# Uses the standalone Tailwind CLI binary (no Node/npm/node_modules
# required). Run this once after install, and again any time
# website/templates/*.html or tailwind.config.js change.
set -e

# Pinned to the v3 line on purpose: v3's CLI takes a JS config file
# (--config, with a `content` array) the way tailwind.config.js here is
# written. v4 changed both the CLI flags and content-detection model, so
# "latest" silently produces an empty stylesheet against a v3-style config.
TAILWIND_VERSION="v3.4.17"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$REPO_DIR/.tailwindcss"

if [ ! -f "$BIN" ]; then
    ARCH="$(uname -m)"
    OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

    case "$OS-$ARCH" in
        linux-x86_64)  FILE="tailwindcss-linux-x64" ;;
        linux-aarch64) FILE="tailwindcss-linux-arm64" ;;
        darwin-x86_64) FILE="tailwindcss-macos-x64" ;;
        darwin-arm64)  FILE="tailwindcss-macos-arm64" ;;
        *) echo "Unsupported platform: $OS-$ARCH"; exit 1 ;;
    esac

    echo "Downloading Tailwind standalone CLI $TAILWIND_VERSION ($FILE)..."
    curl -sL "https://github.com/tailwindlabs/tailwindcss/releases/download/$TAILWIND_VERSION/$FILE" -o "$BIN"
    chmod +x "$BIN"
fi

"$BIN" \
    -i "$REPO_DIR/website/static/css/input.css" \
    -o "$REPO_DIR/website/static/css/style.css" \
    -c "$REPO_DIR/tailwind.config.js" \
    --minify

echo "Built website/static/css/style.css"
