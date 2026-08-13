#!/usr/bin/env python3
"""
Generate Open Graph share images (1200x630 PNG) for every URL listed in
sitemap.xml, using the same json/ stats data the sitemap was built from.

Standalone - run this from the terminal (or your own cron entry)
independently of the Flask app, after generate_sitemap.py. Images are
written to website/static/og/<slug>.png; app.py's og_image_url() Jinja
helper picks them up automatically by deriving the same slug from the
current request path.

Usage: python3 scripts/generate_og_images.py
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
SITEMAP_FILE = REPO_ROOT / "sitemap.xml"
JSON_FOLDER = REPO_ROOT / "json"
OUTPUT_FOLDER = REPO_ROOT / "website" / "static" / "og"

WIDTH, HEIGHT = 1200, 630
BG_COLOR = (2, 132, 199)          # sky-600, matches the site's brand color
TITLE_COLOR = (255, 255, 255)
SUBTITLE_COLOR = (224, 242, 254)  # sky-100
MARGIN = 80

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def load_font(size, bold=True):
    path = FONT_BOLD if bold else FONT_REGULAR
    if Path(path).exists():
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# Mirrors website/app.py's _slugify_path() - both must derive the same
# slug from a URL path, or the images generated here won't line up with
# what the template's og_image_url() looks for.
def slugify_path(path):
    path = path.strip("/")
    if not path:
        return "home"
    return re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-").lower()


def sitemap_paths():
    if not SITEMAP_FILE.exists():
        print(
            f"Error: {SITEMAP_FILE} not found. Run generate_sitemap.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tree = ET.parse(SITEMAP_FILE)
    for url in tree.getroot().findall("sm:url", ns):
        loc_el = url.find("sm:loc", ns)
        if loc_el is not None and loc_el.text:
            yield urlparse(loc_el.text).path


def title_and_subtitle(path):
    """Derive display text for a path using the same json data the sitemap was built from."""
    parts = [p for p in path.strip("/").split("/") if p]

    if not parts:
        summary = load_json(JSON_FOLDER / "summary.json") or {}
        return (
            "Domain Zone Data",
            f"{summary.get('domains', 0):,} domains across {summary.get('tlds', 0):,} TLDs",
        )

    if parts[0] == "dropped":
        summary = load_json(JSON_FOLDER / "summary.json") or {}
        return (
            "Dropped Domains",
            f"-{summary.get('deleted', 0):,} domains dropped in the last 24h",
        )

    if parts[0] == "patterns":
        return (
            "Domain Pattern Availability",
            "One-letter, two-letter, numeric & one-word domains by TLD",
        )

    if parts[0] == "deleted" and len(parts) == 1:
        return (
            "Deleted Domain Patterns",
            "Browse recently deleted domains by short-name pattern",
        )

    if parts[0] == "deleted" and len(parts) == 2:
        return f"Deleted: {parts[1]}", "Recently deleted domains matching this pattern"

    if parts[0] == "tld" and len(parts) >= 2:
        tld = parts[1]
        data = load_json(JSON_FOLDER / tld / "latest") or {}
        return f".{tld} Domains", f"{data.get('domains', 0):,} registered domains"

    return "Domain Zone Data", path


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_image(title, subtitle, out_path):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = load_font(64)
    subtitle_font = load_font(34, bold=False)
    brand_font = load_font(26, bold=False)

    title_lines = wrap_text(draw, title, title_font, WIDTH - 2 * MARGIN)[:2]
    subtitle_lines = wrap_text(draw, subtitle, subtitle_font, WIDTH - 2 * MARGIN)[:2]

    block_height = len(title_lines) * 78 + 20 + len(subtitle_lines) * 44
    y = (HEIGHT - block_height) // 2

    for line in title_lines:
        draw.text((MARGIN, y), line, font=title_font, fill=TITLE_COLOR)
        y += 78

    y += 20
    for line in subtitle_lines:
        draw.text((MARGIN, y), line, font=subtitle_font, fill=SUBTITLE_COLOR)
        y += 44

    draw.text((MARGIN, HEIGHT - 70), "domain-zone-data", font=brand_font, fill=SUBTITLE_COLOR)

    img.save(out_path, "PNG", optimize=True)


def main():
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    count = 0
    for path in sitemap_paths():
        slug = slugify_path(path)
        title, subtitle = title_and_subtitle(path)
        render_image(title, subtitle, OUTPUT_FOLDER / f"{slug}.png")
        count += 1

    print(f"Generated {count} OG images -> {OUTPUT_FOLDER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
