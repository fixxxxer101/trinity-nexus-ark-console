#!/usr/bin/env python3
"""Build update-manifest.json for the current release.

Ship-side tool only - it is not part of the app and is never
downloaded by the updater. The manifest is the release's own
description of itself: its version, and every file the release
consists of with size + SHA-256. update.py carries no file list;
it fetches this manifest and pulls exactly what it says, so a
release that adds a file reaches every install - the failure of
2026-11-26 (a hardcoded updater list predating the files it was
installing) cannot recur.

Run this after every version bump, before pushing:
    python make_manifest.py
then push update-manifest.json together with the release's files.
The canonical file list lives HERE now - add new app files to
FILES below when a release introduces them.
"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

FILES = [
    "RUN-ARK-MAP.bat",
    "setup.py",
    "update.py",
    "arkmap/arkmap.py",
    "arkmap/arkgui.py",
    "arkmap/creature_names.json",
    "arkmap/creatures.json",
    "arkmap/items.json",
    "arkmap/emblem.png",
    "arkmap/web/index.html",
    "arkmap/web/map.html",
    "arkmap/web/pois.json",
    "arkmap/web/spawns.json",
    "arkmap/web/emblem.webp",
    "arkmap/web/map_valguero.png",
    "arkmap/web/leaflet.css",
    "arkmap/web/leaflet.js",
    "arkmap/web/images/layers.png",
    "arkmap/web/images/layers-2x.png",
    "arkmap/web/images/marker-icon.png",
    "arkmap/web/images/marker-icon-2x.png",
    "arkmap/web/images/marker-shadow.png",
]


def main() -> int:
    main_py = (ROOT / "arkmap" / "arkmap.py").read_bytes()
    m = re.search(rb'VERSION\s*=\s*"(\d+\.\d+\.\d+)"', main_py)
    if not m:
        print("ERROR: no VERSION in arkmap/arkmap.py")
        return 1
    version = m.group(1).decode()
    entries = []
    for rel in FILES:
        path = ROOT / rel
        if not path.exists():
            print(f"ERROR: listed file missing from the repo: {rel}")
            return 1
        data = path.read_bytes()
        entries.append({
            "path": rel,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    doc = {"version": version, "files": entries}
    out = ROOT / "update-manifest.json"
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    total = sum(e["size"] for e in entries)
    print(f"update-manifest.json written: version {version}, "
          f"{len(entries)} files, {total} bytes total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
