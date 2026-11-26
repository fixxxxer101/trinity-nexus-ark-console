#!/usr/bin/env python3
"""One-click updater for the Trinity Nexus ARK Console.

The software lives in exactly one place - the public GitHub repo -
and every install updates itself from there. Personal data is never
touched: the Nitrado token config lives outside this folder
(~/.arkmap/config.json), and everything under arkmap/save/ (scan
state, player names, the kept copies of the server logs) plus the
app log is local data, not software.

    python update.py

Also wired into RUN-ARK-MAP.bat: press U at the start prompt, or
run  RUN-ARK-MAP.bat update . A replaced file's old copy is kept
under update-backup/ so an update can always be rolled back by
hand. The --pause flag (used by the launcher and the window's
Update button) holds the window open at the end until Enter, so
the result never vanishes before it is read.
"""
import json
import py_compile
import re
import shutil
import socket
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_RAW = ("https://raw.githubusercontent.com/fixxxxer101/"
            "trinity-nexus-ark-console/main")
STAGING = ROOT / "update-staging"
BACKUP_ROOT = ROOT / "update-backup"

# The software. Anything not listed here (save/, logs, config, the
# .venv) is data and is never downloaded or overwritten.
FILES = [
    "RUN-ARK-MAP.bat",
    "setup.py",
    "update.py",
    "arkmap/arkmap.py",
    "arkmap/arkgui.py",
    "arkmap/creature_names.json",
    "arkmap/emblem.png",
    "arkmap/web/index.html",
    "arkmap/web/map.html",
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

VERSION_RE = re.compile(rb'VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"')


def fetch(path: str) -> bytes:
    req = urllib.request.Request(
        REPO_RAW + "/" + path,
        headers={"User-Agent": "TrinityNexusArkConsole-Updater"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def version_of(data: bytes):
    m = VERSION_RE.search(data)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def version_str(v) -> str:
    return ".".join(str(n) for n in v)


def console_is_running() -> bool:
    """The console serves its web page while it runs; if the port
    answers, updating now would swap code under a live app."""
    port = 8770
    try:
        cfg = json.loads(
            (Path.home() / ".arkmap" / "config.json").read_text(
                encoding="utf-8"))
        port = int(cfg.get("port") or 8770)
    except Exception:
        pass
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.75):
            return True
    except OSError:
        return False


def main() -> int:
    print("Trinity Nexus ARK Console - updater")
    print("Source: github.com/fixxxxer101/trinity-nexus-ark-console")
    print()

    local_main = ROOT / "arkmap" / "arkmap.py"
    if not local_main.exists():
        print("ERROR: arkmap/arkmap.py not found - run this from the "
              "console's own folder.")
        return 1
    local_version = version_of(local_main.read_bytes())
    if local_version is None:
        print("ERROR: could not read the installed version.")
        return 1
    print(f"Installed version: {version_str(local_version)}")

    # The window's Update button starts this updater and then
    # closes the console, so give a running console a short grace
    # period to shut down before refusing.
    waited = 0.0
    while console_is_running() and waited < 20:
        if waited == 0:
            print("The console is running - waiting for it to close...")
        time.sleep(0.5)
        waited += 0.5
    if console_is_running():
        print()
        print("The console is still running. Close it first "
              "(window or tray icon), then run the update again - "
              "swapping code under a live app is how installs break.")
        return 2

    try:
        remote_main = fetch("arkmap/arkmap.py")
    except Exception as exc:
        print(f"ERROR: could not reach GitHub ({exc}). "
              "Check the internet connection and try again.")
        return 1
    remote_version = version_of(remote_main)
    if remote_version is None:
        print("ERROR: the copy on GitHub has no readable version. "
              "Nothing was changed.")
        return 1
    print(f"Latest version:    {version_str(remote_version)}")
    if remote_version <= local_version:
        print()
        print("Already up to date - nothing to do.")
        return 0

    print()
    print(f"Updating {version_str(local_version)} -> "
          f"{version_str(remote_version)} ...")
    shutil.rmtree(STAGING, ignore_errors=True)
    staged = {}
    try:
        for rel in FILES:
            data = remote_main if rel == "arkmap/arkmap.py" \
                else fetch(rel)
            if not data:
                raise RuntimeError(f"{rel} downloaded empty")
            dest = STAGING / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            staged[rel] = data
            print(f"  downloaded {rel} ({len(data)} bytes)")
        # Validate before anything is swapped: Python files must
        # compile, the JSON table must parse.
        for rel in FILES:
            path = STAGING / rel
            if rel.endswith(".py"):
                py_compile.compile(str(path), doraise=True)
            elif rel.endswith(".json"):
                json.loads(path.read_text(encoding="utf-8"))
        print("  all files checked out")
    except Exception as exc:
        print(f"ERROR: update stopped before changing anything ({exc}).")
        shutil.rmtree(STAGING, ignore_errors=True)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUP_ROOT / (
        f"v{version_str(local_version)}-to-v{version_str(remote_version)}"
        f"-{stamp}")
    swapped = []
    try:
        for rel in FILES:
            target = ROOT / rel
            if target.exists():
                keep = backup_dir / rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, keep)
            else:
                (backup_dir / (rel + ".was-absent")).parent.mkdir(
                    parents=True, exist_ok=True)
                (backup_dir / (rel + ".was-absent")).write_text(
                    "", encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(STAGING / rel, target)
            swapped.append(rel)
    except Exception as exc:
        print(f"ERROR while installing ({exc}) - rolling back...")
        for rel in swapped:
            target = ROOT / rel
            keep = backup_dir / rel
            try:
                if keep.exists():
                    shutil.copyfile(keep, target)
                elif (backup_dir / (rel + ".was-absent")).exists():
                    target.unlink(missing_ok=True)
            except Exception:
                pass
        print(f"Rolled back. Your previous files are in {backup_dir}")
        shutil.rmtree(STAGING, ignore_errors=True)
        return 1

    # If the installer itself changed, the launcher refreshes the
    # app environment on its next run.
    old_setup = backup_dir / "setup.py"
    if old_setup.exists() and \
            old_setup.read_bytes() != staged.get("setup.py", b""):
        (ROOT / ".needs-setup").write_text(
            "setup.py changed in this update\n", encoding="utf-8")

    shutil.rmtree(STAGING, ignore_errors=True)
    print()
    print(f"Updated to {version_str(remote_version)}. "
          f"Old files kept in update-backup/{backup_dir.name}/")
    print("Run RUN-ARK-MAP.bat to start the new version.")
    return 0


if __name__ == "__main__":
    code = main()
    if "--pause" in sys.argv:
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass
    sys.exit(code)
