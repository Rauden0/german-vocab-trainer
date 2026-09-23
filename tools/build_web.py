#!/usr/bin/env python3
"""Build the phone / offline version into docs/ (served by GitHub Pages).

Usage: python3 tools/build_web.py
Re-run after changing static/index.html, web/engine.js or the word lists in data/.
"""
import hashlib
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import parse_rows  # noqa: E402  (same CSV rules as the desktop app)

DOCS = ROOT / "docs"

HEAD_EXTRA = """<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#2f5bd3">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="icon-180.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Wortschatz">
"""
SCRIPT_EXTRA = """<script src="engine.js"></script>
<script>
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js");
</script>
"""

SW = """// Offline support: cache the app and word list. Bumped automatically by tools/build_web.py.
const CACHE = "wortschatz-%s";
const FILES = ["./", "index.html", "engine.js", "words.json", "manifest.webmanifest",
               "icon-180.png", "icon-192.png", "icon-512.png"];
self.addEventListener("install", e => e.waitUntil(
  caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  e.respondWith(caches.match(e.request, {ignoreSearch: true}).then(r => r || fetch(e.request)));
});
"""

MANIFEST = {
    "name": "Wortschatz → C2",
    "short_name": "Wortschatz",
    "description": "Adaptive German vocabulary trainer (B2–C2)",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#f6f5f1",
    "theme_color": "#2f5bd3",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}


def png(size):
    """German-flag icon (black / red / gold stripes), written without any image library."""
    colors = [(0, 0, 0), (221, 0, 0), (255, 206, 0)]
    rows = b"".join(b"\x00" + bytes(colors[min(2, y * 3 // size)]) * size for y in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def main():
    DOCS.mkdir(exist_ok=True)
    # Word list: same merge rule as the desktop app (files in name order, first entry wins).
    words, seen = [], set()
    for f in sorted((ROOT / "data").glob("*.csv")):
        for de, en, level, example, example_en, rank in parse_rows(f.read_text(encoding="utf-8")):
            if de not in seen:
                seen.add(de)
                words.append([de, en, level, example, example_en, rank])
    (DOCS / "words.json").write_text(json.dumps({"words": words}, ensure_ascii=False, separators=(",", ":")),
                                     encoding="utf-8")

    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "</head>" in html and "<script>" in html
    html = html.replace("</head>", HEAD_EXTRA + "</head>", 1).replace("<script>", SCRIPT_EXTRA + "<script>", 1)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    shutil.copy(ROOT / "web" / "engine.js", DOCS / "engine.js")
    (DOCS / "manifest.webmanifest").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    for size in (180, 192, 512):
        (DOCS / f"icon-{size}.png").write_bytes(png(size))
    (DOCS / ".nojekyll").write_text("")

    digest = hashlib.sha256()
    for name in ("index.html", "engine.js", "words.json"):
        digest.update((DOCS / name).read_bytes())
    (DOCS / "sw.js").write_text(SW % digest.hexdigest()[:12], encoding="utf-8")
    print(f"docs/ built: {len(words)} words, version {digest.hexdigest()[:12]}")


if __name__ == "__main__":
    main()
