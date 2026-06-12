"""Capture LigaPokemon page+sprite pairs in-session for decoder fixtures.

The sprite URL is randomised per page load, so the HTML and its sprite must be
saved from the *same* response. Use this to refresh the digit-template bank
(issue #12) or the decoder fixtures. Polite: browser UA, one request at a time,
a short delay before each sprite fetch and between renders (FRD §4, §17).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import httpx

UA = {"User-Agent": "PokemonCardWatcher/1.0"}
_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_SPRITE_URL_RE = re.compile(r"background-image:\s*url\(([^)]*imgnum[^)]*)\)")


def capture_once(client: httpx.Client, url: str, out_dir: Path, stem: str) -> Path:
    """Save one HTML + sprite pair under out_dir/<stem>.html and <stem>_sprite.jpg."""
    html = client.get(url, headers=UA, timeout=20, follow_redirects=True).text
    css = "\n".join(_STYLE_RE.findall(html))
    match = _SPRITE_URL_RE.search(css)
    if match is None:
        raise SystemExit(f"No /imgnum/ sprite URL found for {stem}; is this a precoCss page?")
    sprite_url = match.group(1).strip().strip("\"'")
    if sprite_url.startswith("//"):
        sprite_url = "https:" + sprite_url

    time.sleep(2)  # intra-card sprite delay (FRD §4)
    sprite = client.get(sprite_url, headers=UA, timeout=20).content

    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{stem}.html"
    sprite_path = out_dir / f"{stem}_sprite.jpg"
    html_path.write_text(html, encoding="utf-8")
    sprite_path.write_bytes(sprite)
    print(f"saved {html_path}  ({len(html)} bytes) + {sprite_path} ({len(sprite)} bytes)")
    return html_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture LigaPokemon page+sprite pairs.")
    parser.add_argument("url")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    parser.add_argument("--stem", default="capture", help="Base filename stem.")
    parser.add_argument("--count", type=int, default=1, help="Number of renders to capture.")
    parser.add_argument("--delay", type=float, default=5.0, help="Seconds between renders.")
    args = parser.parse_args(argv)

    with httpx.Client() as client:
        for index in range(args.count):
            stem = args.stem if args.count == 1 else f"{args.stem}_{index:02d}"
            capture_once(client, args.url, args.out, stem)
            if index + 1 < args.count:
                time.sleep(args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
