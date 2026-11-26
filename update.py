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

HOW IT KNOWS WHAT TO PULL - THE MANIFEST
----------------------------------------
This updater carries NO list of the app's files. Every run it
fetches one file from the repo - update-manifest.json - which
names the current release: its version and every file that
release consists of, each with its size and SHA-256 hash. The
updater then pulls exactly what the manifest says: any listed
file that is missing on disk, or whose bytes differ from the
manifest's hash, is downloaded, hash-checked, and installed;
anything already matching is left alone.

That design is deliberate, and it replaced a hardcoded file list
after the failure of 2026-11-26: an updater can only fetch files
its own list names, and the updater that runs an update is always
the PREVIOUS release's - so when v2.13.0 added the map's data
files, the v2.12.4 updater that installed it had never heard of
them, they never arrived, and a version-only "already up to date"
check never looked again. With the manifest living in the repo
next to the release it describes, a release can add files freely:
every installed updater reads the new manifest and pulls them,
sight unseen. "Already up to date" now means every file in the
manifest is on disk with the manifest's hash - a claim about
bytes, not about a version number.

ARKMAP_UPDATE_BASE overrides the download base URL (used by the
test rig and by forks); it defaults to the repo's raw address.
"""
import hashlib
import json
import os
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
REPO_RAW = os.environ.get(
    "ARKMAP_UPDATE_BASE",
    "https://raw.githubusercontent.com/fixxxxer101/"
    "trinity-nexus-ark-console/main").rstrip("/")
MANIFEST_NAME = "update-manifest.json"
STAGING = ROOT / "update-staging"
BACKUP_ROOT = ROOT / "update-backup"

VERSION_RE = re.compile(rb'VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"')


def fetch(path: str) -> bytes:
    # The cache-busting query is load-bearing: raw.githubusercontent
    # .com is served through a CDN that keeps serving the previous
    # release for minutes after a push, so a plain fetch can hand
    # back a stale file and the version check reports "already up to
    # date" against a copy that is not (seen live 2026-11-26: the
    # CDN still served v2.12.4's arkmap.py minutes after v2.13.0
    # was pushed and verified through the API). The unique query
    # makes each fetch a fresh origin read.
    url = REPO_RAW + "/" + path + "?v=" + str(int(time.time()))
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TrinityNexusArkConsole-Updater",
                 "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def version_of(data: bytes):
    m = VERSION_RE.search(data)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def parse_version(text):
    try:
        parts = tuple(int(p) for p in str(text).strip().split("."))
    except Exception:
        return None
    return parts if len(parts) == 3 else None


def version_str(v) -> str:
    return ".".join(str(n) for n in v)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest() -> dict:
    """The manifest is the release's own description of itself:
    {"version": "2.15.0", "files": [{"path", "size", "sha256"}]}.
    Anything malformed is a hard stop - an updater that guesses
    its file list is the bug this design exists to remove."""
    raw = fetch(MANIFEST_NAME)
    doc = json.loads(raw.decode("utf-8"))
    version = parse_version(doc.get("version"))
    files = doc.get("files")
    if version is None or not isinstance(files, list) or not files:
        raise ValueError("manifest has no readable version/file list")
    entries = []
    for item in files:
        path = str(item["path"])
        size = int(item["size"])
        digest = str(item["sha256"]).lower()
        if path.startswith("/") or ".." in Path(path).parts:
            raise ValueError(f"manifest path escapes the install: {path}")
        entries.append({"path": path, "size": size, "sha256": digest})
    return {"version": version, "files": entries}


def _needed(manifest) -> list:
    """Manifest entries this install does not hold exactly: the
    file is absent, the wrong size, or hashes differently. This
    one comparison drives updates AND repairs - there is no
    separate completeness check to forget."""
    out = []
    for entry in manifest["files"]:
        target = ROOT / entry["path"]
        try:
            data = target.read_bytes()
        except OSError:
            out.append(entry)
            continue
        if len(data) != entry["size"] or sha256_of(data) != entry["sha256"]:
            out.append(entry)
    return out


LOCK_PATH = ROOT / "update.lock"


def _acquire_lock() -> bool:
    """One updater at a time: two updaters share the staging folder
    and would interleave downloads and swaps into a mixed-version
    install. A lock older than an hour, or one whose process is
    gone, is stale and taken over."""
    try:
        if LOCK_PATH.exists():
            age = time.time() - LOCK_PATH.stat().st_mtime
            pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or 0)
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
                except Exception:
                    alive = True
            if alive and age < 3600:
                return False
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return True  # the lock must never be what breaks updating


def _release_lock() -> None:
    try:
        if LOCK_PATH.exists() and \
                LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except Exception:
        pass


def _stage_and_validate(entries) -> dict:
    """Download the given manifest entries into staging and prove
    each one: the bytes must hash to the manifest's SHA-256 (and
    match its size), Python files must compile, JSON must parse.
    Returns {path: bytes}. Raises before anything is swapped."""
    shutil.rmtree(STAGING, ignore_errors=True)
    staged = {}
    for entry in entries:
        rel = entry["path"]
        data = fetch(rel)
        if len(data) != entry["size"] or sha256_of(data) != entry["sha256"]:
            raise RuntimeError(
                f"{rel} does not match the manifest "
                f"(size or SHA-256) - refusing to install it")
        dest = STAGING / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        staged[rel] = data
        print(f"  downloaded {rel} ({len(data)} bytes)")
    for entry in entries:
        rel = entry["path"]
        path = STAGING / rel
        if rel.endswith(".py"):
            py_compile.compile(str(path), doraise=True)
        elif rel.endswith(".json"):
            json.loads(path.read_text(encoding="utf-8"))
    return staged


def _swap(staged: dict, backup_dir: Path) -> None:
    """Install staged files over the live ones, backing each old
    copy up first (a .was-absent marker when there was none).
    Rolls the whole swap back if any copy fails."""
    swapped = []
    try:
        for rel in staged:
            target = ROOT / rel
            if target.exists():
                keep = backup_dir / rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(target, keep)
            else:
                marker = backup_dir / (rel + ".was-absent")
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("", encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Record the file BEFORE the copy: if the copy itself
            # fails mid-write, this is exactly the file rollback
            # must restore - appending afterwards skipped it.
            swapped.append(rel)
            shutil.copyfile(STAGING / rel, target)
    except Exception as exc:
        print(f"ERROR while installing ({exc}) - rolling back...")
        failed = []
        for rel in swapped:
            target = ROOT / rel
            keep = backup_dir / rel
            try:
                if keep.exists():
                    shutil.copyfile(keep, target)
                elif (backup_dir / (rel + ".was-absent")).exists():
                    target.unlink(missing_ok=True)
            except Exception:
                failed.append(rel)
        if failed:
            print(f"WARNING: rollback could not restore: "
                  f"{', '.join(failed)}. The intact previous copies "
                  f"are in {backup_dir} - copy them back by hand "
                  f"before launching.")
        else:
            print(f"Rolled back. Your previous files are in {backup_dir}")
        raise


def _install(entries, backup_dir: Path) -> dict:
    """Stage, validate and swap the given entries. Returns the
    staged bytes on success; raises (after any needed rollback)
    on failure, with staging cleaned up either way."""
    try:
        staged = _stage_and_validate(entries)
        print("  all files checked out")
        backup_dir.mkdir(parents=True, exist_ok=True)
        _swap(staged, backup_dir)
        return staged
    finally:
        shutil.rmtree(STAGING, ignore_errors=True)


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
    if not _acquire_lock():
        print("Another update is already running. Wait for it to "
              "finish, then run this again.")
        return 2
    try:
        return _run()
    finally:
        _release_lock()


def _run() -> int:
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
        print("Something is still answering on the console's port - "
              "normally that is the console itself. Close it first "
              "(window or tray icon), then run the update again - "
              "swapping code under a live app is how installs break. "
              "If the console is definitely closed, another program "
              "is using the port; close that instead.")
        return 2

    try:
        manifest = load_manifest()
    except Exception as exc:
        print(f"ERROR: could not read the release manifest from "
              f"GitHub ({exc}). Nothing was changed - check the "
              f"internet connection and try again.")
        return 1
    remote_version = manifest["version"]
    print(f"Latest version:    {version_str(remote_version)}")

    if remote_version < local_version:
        print()
        print("The release on GitHub is older than this install - "
              "nothing to do, and updates never run backwards.")
        return 0

    needed = _needed(manifest)
    updating = remote_version > local_version
    if not needed:
        print()
        print("Already up to date - every file matches the "
              "release manifest.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if updating:
        print()
        print(f"Updating {version_str(local_version)} -> "
              f"{version_str(remote_version)} "
              f"({len(needed)} file(s) to pull)...")
        backup_dir = BACKUP_ROOT / (
            f"v{version_str(local_version)}-to-"
            f"v{version_str(remote_version)}-{stamp}")
    else:
        print()
        print(f"Already on the latest version, but "
              f"{len(needed)} file(s) are missing or differ from "
              f"the release - repairing:")
        for entry in needed:
            print(f"  needs pull: {entry['path']}")
        backup_dir = BACKUP_ROOT / f"repair-{stamp}"

    try:
        staged = _install(needed, backup_dir)
    except Exception as exc:
        print(f"ERROR: stopped before completing ({exc}).")
        return 1

    # Verify against the manifest, not against hope: recompute
    # what the install still lacks and pull it in the same run.
    still = _needed(manifest)
    if still:
        print("Note: some files are still not matching after the "
              "swap - pulling them again now:")
        try:
            _install(still, BACKUP_ROOT / f"repair-{stamp}")
        except Exception as exc:
            print(f"WARNING: the update finished, but some files "
                  f"still do not match the manifest ({exc}). Run "
                  f"the updater again - it will pull exactly those.")
    # If the installer itself changed, the launcher refreshes the
    # app environment on its next run.
    old_setup = backup_dir / "setup.py"
    if old_setup.exists() and "setup.py" in staged and \
            old_setup.read_bytes() != staged["setup.py"]:
        try:
            (ROOT / ".needs-setup").write_text(
                "setup.py changed in this update\n", encoding="utf-8")
        except Exception as exc:
            print(f"Note: the update itself finished, but the "
                  f".needs-setup marker could not be written ({exc}). "
                  f"If the app misbehaves, delete the .venv folder "
                  f"and relaunch - setup will rebuild it.")

    print()
    if updating:
        print(f"Updated to {version_str(remote_version)}. "
              f"Old files kept in update-backup/{backup_dir.name}/")
    else:
        print("Repair complete - every file matches the release "
              "manifest.")
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
