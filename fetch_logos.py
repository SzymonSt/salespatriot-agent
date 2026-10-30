#!/usr/bin/env python3
"""Download company logos (site icons) for the web UI.

Sources, best first: Google's favicon service at 128 px, then the icons the
website itself declares (apple-touch-icon, sized icons), then the lead image of
the organisation's Wikipedia article (emblem or logo, for WIKIPEDIA_TITLES only),
then a small Google icon as a last resort. Logos go to web/logos/<profile id>.<ext> with an index in
web/logos/logos.json; companies without a usable icon keep initials in the UI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


FAVICON_URL = "https://www.google.com/s2/favicons"
SIZE = 128
# Smaller icons look blurry in the UI's 40 px circles on high-density screens.
MIN_PIXELS = 48
LAST_RESORT_PIXELS = 32
VECTOR = 10_000  # stands in for the width of an SVG
# Generic icons that say nothing about the company (e.g. WordPress's default site icon).
GENERIC_ICON_SHA1 = {"fce576d17f7b2cbb3b8d94ea0f5285571d502c9b"}
# The profile website is wrong or missing for these: use the organisation's own site.
DOMAIN_OVERRIDES = {
    "buyer-deu-991-19518-88": "www.bundeswehr.de",  # profile has the e-procurement portal
    "buyer-esp-10000140000201": "ejercito.defensa.gob.es",
}
# Organisations whose sites only offer tiny icons (or are unreachable) but whose
# Wikipedia article leads with their logo or emblem.
WIKIPEDIA_TITLES = {
    "supplier-pol-9482182612": "Fabryka_Broni_Łucznik",
    "buyer-esp-10000140000201": "Spanish_Army",
    "buyer-aut-9110010282738": "Austrian_Armed_Forces",
}
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
# Logos drawn in white for a dark site header; the UI shows them on this colour.
DARK_BACKGROUND = {"supplier-prt-500022089": "#1d1d1f"}


def domain_for(profile: dict) -> str:
    if profile["id"] in DOMAIN_OVERRIDES:
        return DOMAIN_OVERRIDES[profile["id"]]
    site = profile.get("website") or (profile.get("offering") or {}).get("website") or ""
    if not site:
        return ""
    return urlparse(site if "//" in site else f"//{site}").netloc


def image_info(data: bytes) -> tuple[int, str]:
    """Return (width in px, file extension) for PNG, ICO, JPEG or SVG data."""
    if hashlib.sha1(data).hexdigest() in GENERIC_ICON_SHA1:
        return 0, ""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">I", data[16:20])[0], "png"
    if data[:4] == b"\x00\x00\x01\x00":
        count = struct.unpack("<H", data[4:6])[0]
        widths = [data[6 + 16 * i] or 256 for i in range(count) if 6 + 16 * i < len(data)]
        return max(widths, default=0), "ico"
    if data[:2] == b"\xff\xd8":
        return MIN_PIXELS, "jpg"  # JPEG icons are rare and normally large enough
    if b"<svg" in data[:500].lower():
        return VECTOR, "svg"
    return 0, ""


def get(url: str) -> bytes | None:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (salespatriot-logos/1.0)"})
    try:
        with urlopen(request, timeout=20) as response:
            return response.read(3_000_000)
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        # Google answers 404 (with a generic globe) when it has no icon.
        return None


def google_icon(domain: str) -> bytes | None:
    return get(f"{FAVICON_URL}?{urlencode({'domain': domain, 'sz': SIZE})}")


def declared_icons(domain: str) -> list[str]:
    """Icon URLs the homepage declares, largest declared size first."""
    base = f"https://{domain}/"
    html = (get(base) or b"").decode("utf-8", errors="replace")
    found = []
    for tag in re.findall(r"<link\b[^>]*>", html, flags=re.I):
        rel = re.search(r'rel=["\']([^"\']+)', tag, flags=re.I)
        href = re.search(r'href=["\']([^"\']+)', tag, flags=re.I)
        if not rel or not href or "icon" not in rel.group(1).lower():
            continue
        sizes = re.search(r'sizes=["\'](\d+)x', tag, flags=re.I)
        size = int(sizes.group(1)) if sizes else (180 if "apple" in rel.group(1).lower() else 0)
        found.append((size, urljoin(base, href.group(1))))
    found.append((180, urljoin(base, "apple-touch-icon.png")))
    return [url for _, url in sorted(found, reverse=True)]


def wikipedia_image(title: str) -> tuple[bytes, int, str, str] | None:
    summary = get(WIKIPEDIA_SUMMARY + quote(title))
    if not summary:
        return None
    url = (json.loads(summary).get("thumbnail") or {}).get("source", "").split("?")[0]
    data = get(url) if url else None
    width, ext = image_info(data) if data else (0, "")
    return (data, width, ext, url) if width >= MIN_PIXELS else None


def best_icon(domain: str, wikipedia_title: str = "") -> tuple[bytes, int, str, str] | None:
    """Return (data, width, extension, source) of the best icon found, or None."""
    fallback = None
    google = google_icon(domain)
    if google:
        width, ext = image_info(google)
        if width >= MIN_PIXELS:
            return google, width, ext, "Google favicon service"
        if width >= LAST_RESORT_PIXELS:
            fallback = (google, width, ext, "Google favicon service (small)")
    for url in declared_icons(domain):
        data = get(url)
        width, ext = image_info(data) if data else (0, "")
        if width >= MIN_PIXELS:
            return data, width, ext, url
    if wikipedia_title:
        return wikipedia_image(wikipedia_title) or fallback
    return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description="Download company logos for the web UI.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out-dir", type=Path, default=Path("web/logos"))
    args = parser.parse_args()

    profiles = [
        profile
        for name in ("suppliers.json", "buyers.json")
        for profile in json.loads((args.data_dir / "profiles" / name).read_text(encoding="utf-8"))
    ]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    index = {}
    for profile in profiles:
        domain = domain_for(profile)
        if not domain and profile["id"] not in WIKIPEDIA_TITLES:
            print(f"- {profile['id']}: no website, keeping initials")
            continue
        icon = best_icon(domain, WIKIPEDIA_TITLES.get(profile["id"], ""))
        if icon is None:
            print(f"- {profile['id']}: no usable icon for {domain}, keeping initials")
            continue
        data, width, ext, source = icon
        for old in args.out_dir.glob(f"{profile['id']}.*"):
            old.unlink()
        path = args.out_dir / f"{profile['id']}.{ext}"
        path.write_bytes(data)
        index[profile["id"]] = {
            "file": path.name,
            "domain": domain,
            "pixels": "vector" if width == VECTOR else width,
            "source": source,
            "fetched_at": date.today().isoformat(),
        }
        if profile["id"] in DARK_BACKGROUND:
            index[profile["id"]]["background"] = DARK_BACKGROUND[profile["id"]]
        print(f"+ {profile['id']}: {domain} ({index[profile['id']]['pixels']}px, {source[:60]})")

    (args.out_dir / "logos.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(index)} of {len(profiles)} logos to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
