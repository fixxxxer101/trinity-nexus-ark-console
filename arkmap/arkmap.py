#!/usr/bin/env python3
"""
ARK Valguero family map service.

Pulls the world save from a Nitrado ARK: Survival Evolved server and serves
a web map (players, tames, wilds, bases) on the local network.

This server is Nitrado's Xbox-class product (PC crossplay via the Microsoft
edition), which has NO FTP access and no panel file browser. The save is
pulled through Nitrado's own API instead, using a long-life API token the
owner creates in their Nitrado account. The token is a master key to the
server, so the config lives in a private folder on this PC only
(~/.arkmap/config.json) - never in this app folder, never in the log.

Run:  python arkmap.py
Stop: close the console window (or Ctrl+C).
Reset settings: delete ~/.arkmap/config.json and run again.
"""

from __future__ import annotations

import getpass
import gzip
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "2.7.1"

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".arkmap"
CONFIG_PATH = CONFIG_DIR / "config.json"
SAVE_DIR = APP_DIR / "save"
WEB_DIR = APP_DIR / "web"
DATA_PATH = WEB_DIR / "data.json"
STATUS_PATH = WEB_DIR / "status.json"
NAMES_PATH = APP_DIR / "creature_names.json"
LOG_PATH = APP_DIR / "arkmap.log"
MAP_FILE = "Valguero_P.ark"
API_BASE = "https://api.nitrado.net"
DEFAULT_SERVICE_ID = ""  # your Nitrado service ID - SETUP asks for it

# ------------------------------------------------------------------- log ----


def log(message: str) -> None:
    """Print to the console and append to arkmap.log (which syncs via Drive,
    so Vox can read exactly what Nitrado's API answered and fix problems
    from it). Secrets (the API token) are never logged."""
    line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            os.replace(LOG_PATH, APP_DIR / "arkmap.old.log")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # logging must never break the app


# ---------------------------------------------------------------- config ----


def load_or_prompt_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if cfg.get("api_token"):
                return cfg
        except Exception:
            pass
        print("The saved config looks incomplete - let's set it up again.\n")

    print("=" * 62)
    print("  ARK MAP - first-time setup")
    print("=" * 62)
    print("This Nitrado server is the Xbox / Microsoft crossplay kind,")
    print("which has no FTP. The map pulls the save through Nitrado's")
    print("own API instead, using a long-life API token from your")
    print("Nitrado account. To create one:")
    print("  1. Go to server.nitrado.net -> My Account -> Developer")
    print("     Portal -> Long-life tokens. (Nitrado will ask for your")
    print("     account password on the way - that's normal.)")
    print("  2. Description: ArkMap. Tick the 'service' and")
    print("     'user_info' scopes, then Create.")
    print("  3. Copy the token right away - Nitrado shows it only once.")
    print("The token can control your whole server, so it is stored")
    print("ONLY in a private folder on this PC:")
    print(f"  {CONFIG_PATH}")
    print("It never goes in the Drive folder and never in the log.\n")

    cfg = {}
    cfg["api_token"] = getpass.getpass("Nitrado API token (typing is hidden): ").strip()
    sid = input(f"Nitrado service ID [{DEFAULT_SERVICE_ID}]: ").strip()
    cfg["service_id"] = sid or DEFAULT_SERVICE_ID
    port = input("Web page port [8770]: ").strip()
    cfg["port"] = int(port) if port.isdigit() else 8770
    mins = input("Check the server for a fresh save every how many minutes [3]: ").strip()
    cfg["interval_min"] = int(mins) if mins.isdigit() else 3

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"\nSaved to {CONFIG_PATH}. To change these later, delete that")
    print("file and run again.\n")
    return cfg


# ------------------------------------------------------------------- api ----


class ApiError(Exception):
    pass


def api_get(cfg: dict, path: str, params: dict | None = None) -> dict:
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {cfg['api_token']}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise ApiError(f"Nitrado API {exc.code} on {path}: {body or exc.reason}")
    except Exception as exc:
        raise ApiError(f"Nitrado API call failed on {path}: {exc}")


def api_post_json(cfg: dict, path: str, payload: dict) -> dict:
    url = API_BASE + path
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {cfg['api_token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_txt = ""
        try:
            body_txt = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise ApiError(f"Nitrado API {exc.code} on {path}: {body_txt or exc.reason}")
    except Exception as exc:
        raise ApiError(f"Nitrado API call failed on {path}: {exc}")


def api_list_dir(cfg: dict, directory: str = "") -> list[dict]:
    out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/file_server/list",
                  {"dir": directory} if directory else None)
    if out.get("status") != "success":
        raise ApiError(f"List failed for '{directory}': {str(out)[:300]}")
    return out.get("data", {}).get("entries", [])


def _entry_name(entry: dict) -> str:
    name = entry.get("name") or ""
    if not name and entry.get("path"):
        name = str(entry["path"]).rstrip("/").rsplit("/", 1)[-1]
    return name


def _entry_is_dir(entry: dict) -> bool:
    if _entry_name(entry).endswith("/"):
        return True
    t = str(entry.get("type") or entry.get("kind") or "").lower()
    if t:
        return "dir" in t or "folder" in t
    return bool(entry.get("is_directory") or entry.get("is_dir") or entry.get("dir"))


def _entry_size(entry: dict):
    for key in ("size", "filesize", "file_size"):
        if entry.get(key) is not None:
            return entry[key]
    return None


def _entry_stamp(entry: dict) -> str:
    for key in ("modified", "mtime", "changed", "created", "timestamp"):
        if entry.get(key):
            return str(entry[key])
    return ""


def _entry_fingerprint(entry: dict) -> str:
    return f"{_entry_size(entry)}|{_entry_stamp(entry)}"


def _already_scanned(scanned: dict, name: str, entry: dict) -> bool:
    """Has this exact version of the file been scanned before? Compares
    the stored fingerprint (size + modified stamp); records written by
    older builds stored the bare size and still count. Rotated backup
    logs never change once written, so when the listing carries no
    size/stamp at all, a backup's name on record is enough - while the
    live log, which grows, is always re-read in that case."""
    if name not in scanned:
        return False
    fp = _entry_fingerprint(entry)
    if fp == "None|":
        return "backup" in name.lower()
    if scanned[name] == fp:
        return True
    size = _entry_size(entry)
    return size is not None and str(scanned[name]) == str(size)


def _harvest_ark_names(obj) -> list[str]:
    """Collect every .ark filename mentioned anywhere in an API answer."""
    names: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and (o.lower().endswith(".ark") or o.lower().endswith(".ark.gz")):
            base = o.rsplit("/", 1)[-1]
            if base not in names:
                names.append(base)

    walk(obj)
    return names


def _backup_stamp(name: str):
    """Sortable timestamp from a game-backup name (DD.MM.YYYY_HH.MM.SS)."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})_(\d{2})\.(\d{2})\.(\d{2})", name)
    if not m:
        return (0,)
    d, mo, y, hh, mm, ss = (int(g) for g in m.groups())
    return (y, mo, d, hh, mm, ss)


def _fetch_world(cfg: dict, remote_path: str) -> int:
    """Download a world save (plain .ark or a gzipped game backup) into
    place as the local MAP_FILE. Returns the decompressed size."""
    if remote_path.lower().endswith(".gz"):
        tmp = SAVE_DIR / "_world.ark.gz"
        api_download(cfg, remote_path, tmp)
        out = SAVE_DIR / MAP_FILE
        with gzip.open(tmp, "rb") as fh_in, open(out, "wb") as fh_out:
            shutil.copyfileobj(fh_in, fh_out)
        tmp.unlink(missing_ok=True)
        return out.stat().st_size
    return api_download(cfg, remote_path, SAVE_DIR / MAP_FILE)


def _fetch_world_post(cfg: dict, remote_path: str) -> int:
    """_fetch_world via the POST download-link probe."""
    if remote_path.lower().endswith(".gz"):
        tmp = SAVE_DIR / "_world.ark.gz"
        api_download_post(cfg, remote_path, tmp)
        out = SAVE_DIR / MAP_FILE
        with gzip.open(tmp, "rb") as fh_in, open(out, "wb") as fh_out:
            shutil.copyfileobj(fh_in, fh_out)
        tmp.unlink(missing_ok=True)
        return out.stat().st_size
    return api_download_post(cfg, remote_path, SAVE_DIR / MAP_FILE)


def service_info(cfg: dict) -> tuple[str, str]:
    """Console (no-FTP) services keep their files under
    /games/<service-username>/noftp/<folder_short>/ - ask the API for the
    service username and folder code instead of guessing.
    Returns (username, file_root); either may be empty."""
    try:
        out = api_get(cfg, f"/services/{cfg['service_id']}")
        svc = out.get("data", {}).get("service", {}) or {}
        username = svc.get("username") or ""
        details = svc.get("details", {}) or {}
        folder = details.get("folder_short") or "arkxb"
        if username:
            root = f"/games/{username}/noftp/{folder}"
            log(f"Service file root: {root}")
            return username, root
        log(f"Service answer had no username (service keys: {sorted(svc.keys())})")
    except Exception as exc:
        log(f"Service lookup failed ({exc}) - falling back to the file root.")
    return "", ""


def _list_logged(cfg: dict, directory: str, raw_sample: bool = False) -> list[dict]:
    entries = api_list_dir(cfg, directory)
    names = [_entry_name(e) for e in entries]
    log(f"Folder '{directory or 'home'}' holds {len(entries)}: {', '.join(names[:40])}")
    if raw_sample and entries:
        log(f"Raw entry sample: {json.dumps(entries[:2])[:600]}")
    return entries


def _walk_for_save(cfg: dict, root: str) -> str | None:
    """Walk one root looking for the folder that holds the save file."""
    queue = [root]
    seen = set()
    for _ in range(80):  # bounded walk
        if not queue:
            break
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            entries = _list_logged(cfg, current)
        except Exception as exc:
            log(f"Could not list '{current or 'home'}': {exc}")
            continue
        for e in entries:
            if _entry_name(e).lower() == MAP_FILE.lower():
                p = str(e.get("path") or f"{current.rstrip('/')}/{_entry_name(e)}")
                log(f"Found the save at {p}")
                return p.rsplit("/", 1)[0]
        for e in entries:
            if _entry_is_dir(e):
                queue.append(str(e.get("path") or f"{current.rstrip('/')}/{_entry_name(e)}"))
    return None


def find_save_dir(cfg: dict, state: dict | None = None) -> str:
    """Locate the SavedArks folder on the file server.
    Returns the path of the folder that holds Valguero_P.ark."""
    # Fast path: the list endpoint's search may find the file directly.
    try:
        out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/file_server/list",
                      {"search": MAP_FILE})
        entries = out.get("data", {}).get("entries", [])
        for e in entries:
            if _entry_name(e).lower() == MAP_FILE.lower() and e.get("path"):
                p = str(e["path"])
                log(f"Search found the save at {p}")
                return p.rsplit("/", 1)[0]
    except Exception as exc:
        log(f"Save search failed ({exc}) - probing the folders instead.")

    # What does the gameserver itself say its file path is?
    try:
        out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers")
        gs = out.get("data", {}).get("gameserver", {}) or {}
        spec = gs.get("game_specific", {}) or {}
        safe = {k: v for k, v in spec.items()
                if "pass" not in str(k).lower() and "token" not in str(k).lower()}
        if safe:
            log(f"Gameserver game_specific: {safe}")
    except Exception as exc:
        log(f"Gameserver lookup failed ({exc})")

    username, root = service_info(cfg)
    probes = []
    if root:
        probes += [root, root + "/", root.lstrip("/"),
                   f"/games/{username}/noftp", f"/games/{username}", "/games",
                   root + "/ShooterGame", root + "/ShooterGame/Saved",
                   root + "/ShooterGame/Saved/SavedArks",
                   root + "/ShooterGame/Saved/SavedArks/"]
    walk_roots = []
    for i, probe in enumerate(probes):
        try:
            entries = _list_logged(cfg, probe, raw_sample=(i == 0))
        except Exception as exc:
            log(f"Could not list '{probe}': {exc}")
            continue
        for e in entries:
            if _entry_name(e).lower() == MAP_FILE.lower():
                p = str(e.get("path") or f"{probe.rstrip('/')}/{_entry_name(e)}")
                log(f"Found the save at {p}")
                return p.rsplit("/", 1)[0]
        if entries:
            walk_roots.append(probe)
    for wr in walk_roots:
        found = _walk_for_save(cfg, wr)
        if found:
            return found

    walled = False  # set when Nitrado 500s on a SavedArks download
    # Round 2: interrogate the endpoints that can settle whether the save
    # exists and whether downloads work at all on this service.
    try:
        out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/backups")
        log(f"Backups answer: {json.dumps(out)[:2500]}")
    except Exception as exc:
        log(f"Backups lookup failed ({exc})")
    probe_backup_download(cfg, state)
    game_backups: list[str] = []
    try:
        out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/games/arkse/backups")
        log(f"ARK game backups answer: {json.dumps(out)[:1500]}")
        game_backups = [n for n in _harvest_ark_names(out)
                        if n.lower() != MAP_FILE.lower()]
        log(f"Game-internal save backups seen: {game_backups[:10]}")
    except Exception as exc:
        log(f"ARK game backups lookup failed ({exc})")
    stem = MAP_FILE.rsplit(".", 1)[0]  # "Valguero_P"
    game_backups = [n for n in game_backups if n.startswith(stem + "_")]
    game_backups.sort(key=_backup_stamp, reverse=True)
    if root:
        savedarks = f"{root}/ShooterGame/Saved/SavedArks"
        dir_variants = [savedarks, root, f"{root}/ShooterGame/Saved", f"{root}/backups"]
        for i, name in enumerate(game_backups[:3]):
            for d in (dir_variants if i == 0 else [savedarks]):
                try:
                    size = _fetch_world(cfg, f"{d}/{name}")
                    log(f"Downloaded game backup {name} as the world save ({size} bytes)")
                    if size > 0:
                        if state is not None:
                            state["direct_ok"] = True
                            state["source_name"] = name
                        return d
                except Exception as exc:
                    log(f"Game backup download failed for {d}/{name}: {exc}")
                    if d == savedarks and "500" in str(exc):
                        walled = True
        if game_backups:
            name = game_backups[0]
            try:
                size = _fetch_world_post(cfg, f"{savedarks}/{name}")
                log(f"POST-variant download worked for {name} ({size} bytes)")
                if size > 0:
                    if state is not None:
                        state["direct_ok"] = True
                        state["source_name"] = name
                    return savedarks
            except Exception as exc:
                log(f"POST-variant download failed for {name}: {exc}")
                if "500" in str(exc):
                    walled = True
        log_path = f"{root}/ShooterGame/Saved/Logs/ShooterGame.log"
        try:
            size = api_download(cfg, log_path, SAVE_DIR / "_probe.log")
            log(f"Probe download of a server log worked ({size} bytes) - downloads DO work on this service.")
            (SAVE_DIR / "_probe.log").unlink(missing_ok=True)
        except Exception as exc:
            log(f"Probe download of a server log failed: {exc}")
        if game_backups and probe_link_variants(cfg, state, game_backups[0]):
            return savedarks
        try:
            out = api_post_json(cfg, f"/services/{cfg['service_id']}/gameservers/file_server/stats",
                                {"files": [f"{root}/ShooterGame/Saved/SavedArks/{MAP_FILE}", log_path]})
            log(f"File stats answer: {json.dumps(out)[:800]}")
        except Exception as exc:
            log(f"File stats lookup failed ({exc})")

    # Last resort: on console services the list endpoint may never show the
    # tree while direct downloads by absolute path still work (seen in the
    # wild on console servers). Probe the standard ARK save paths.
    if root:
        for cand in (f"{root}/ShooterGame/Saved/SavedArks/{MAP_FILE}",
                     f"{root}/{MAP_FILE}",
                     f"{root.lstrip('/')}/ShooterGame/Saved/SavedArks/{MAP_FILE}"):
            try:
                size = api_download(cfg, cand, SAVE_DIR / MAP_FILE)
                log(f"Direct download worked: {cand} ({size} bytes)")
                if size > 0:
                    if state is not None:
                        state["direct_ok"] = True
                    return cand.rsplit("/", 1)[0]
            except Exception as exc:
                log(f"Direct download failed for {cand}: {exc}")
                if "SavedArks" in cand and "500" in str(exc):
                    walled = True

    found = _walk_for_save(cfg, "")
    if found:
        return found
    if walled:
        log("Nitrado is walling off the SavedArks folder on this Xbox service: "
            "its listings come back empty and downloads error out on their "
            "side (HTTP 500). Switching to watch mode - each new game backup "
            "gets one download attempt, so the map lights up by itself if "
            "Nitrado opens the door.")
        if state is not None:
            state["walled"] = True
            state["file_root"] = root
            (SAVE_DIR / "pull_state.json").write_text(
                json.dumps(state, indent=2), encoding="utf-8")
        return ""
    raise ApiError("Could not find Valguero_P.ark anywhere on the file server. "
                   "The folder listings above show what the server exposes.")


def api_download(cfg: dict, remote_path: str, local: Path) -> int:
    out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/file_server/download",
                  {"file": remote_path})
    return _fetch_via_answer(out, remote_path, local)


def api_download_post(cfg: dict, remote_path: str, local: Path) -> int:
    """Probe variant: ask for the download link with a POST instead of a
    GET (JSON body first, form body second) and fetch from any link issued."""
    path = f"/services/{cfg['service_id']}/gameservers/file_server/download"
    errors: list[str] = []
    out = None
    try:
        out = api_post_json(cfg, path, {"file": remote_path})
        if out.get("status") != "success":
            errors.append(f"JSON POST answer: {str(out)[:200]}")
            out = None
    except Exception as exc:
        errors.append(f"JSON POST: {exc}")
    if out is None:
        try:
            body = urllib.parse.urlencode({"file": remote_path}).encode("utf-8")
            req = urllib.request.Request(API_BASE + path, data=body, method="POST", headers={
                "Authorization": f"Bearer {cfg['api_token']}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            if out.get("status") != "success":
                errors.append(f"form POST answer: {str(out)[:200]}")
                out = None
        except Exception as exc:
            errors.append(f"form POST: {exc}")
    if out is None:
        raise ApiError("POST download request failed - " + " | ".join(errors))
    return _fetch_via_answer(out, remote_path, local)


def _fetch_via_answer(out: dict, remote_path: str, local: Path) -> int:
    if out.get("status") != "success":
        raise ApiError(f"Download request failed for {remote_path}: {str(out)[:300]}")
    data = out.get("data", {})
    url = data.get("url")
    token = data.get("token")
    if isinstance(token, dict):  # the fileserver answer nests url+token here
        url = url or token.get("url")
        token = token.get("token")
    if not url:
        raise ApiError(f"Download answer had no URL (keys: {sorted(data.keys())})")
    return _fetch_via_link(url, token, local)


LAST_LINK: dict = {}


def _note_link(url: str, token) -> None:
    """Record a fileserver link for the variant probe and log only its
    shape - the token value is a credential and is never logged."""
    parts = urllib.parse.urlsplit(url)
    query_keys = sorted(urllib.parse.parse_qs(parts.query).keys())
    LAST_LINK.clear()
    LAST_LINK.update({"url": url, "token": token})
    log(f"Fileserver link shape: host={parts.netloc} path={parts.path} "
        f"query_keys={query_keys} token_in_query={'token' in query_keys} "
        f"token_len={len(str(token)) if token else 0}")


def _fetch_via_link(url: str, token, local: Path) -> int:
    _note_link(url, token)
    headers = {}
    if token:
        headers["token"] = str(token)
        if "token=" not in url:
            url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(str(token))
    req = urllib.request.Request(url, headers=headers)
    tmp = local.with_suffix(local.suffix + ".tmp")
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    os.replace(tmp, local)
    return local.stat().st_size


def probe_link_variants(cfg: dict, state: dict | None, target_name: str) -> bool:
    """Long-shot probe: if the fileserver link Nitrado just issued encodes
    the file path, re-aim the same link shape at a save backup (same token)
    and see whether the fileserver honors it."""
    url = LAST_LINK.get("url")
    token = LAST_LINK.get("token")
    if not url:
        log("Link probe: no fileserver link captured - skipping.")
        return False
    marker = "ShooterGame/Saved/Logs/ShooterGame.log"
    decoded = urllib.parse.unquote(url)
    if marker not in decoded:
        log("Link probe: the fileserver link does not encode the file path "
            "(the token alone decides which file) - nothing to re-aim.")
        return False
    variant = decoded.replace(marker, f"ShooterGame/Saved/SavedArks/{target_name}")
    log(f"Link probe: re-aiming the link at {target_name}...")
    raw = SAVE_DIR / "_probe.bin"
    try:
        _fetch_via_link(variant, token, raw)
        with open(raw, "rb") as fh:
            magic = fh.read(2)
        if magic == b"\x1f\x8b":
            with gzip.open(raw, "rb") as fh_in, open(SAVE_DIR / MAP_FILE, "wb") as fh_out:
                shutil.copyfileobj(fh_in, fh_out)
            raw.unlink(missing_ok=True)
        else:
            os.replace(raw, SAVE_DIR / MAP_FILE)
        size = (SAVE_DIR / MAP_FILE).stat().st_size
    except Exception as exc:
        log(f"Link probe: the re-aimed link was refused ({exc}).")
        raw.unlink(missing_ok=True)
        return False
    log(f"Link probe WORKED: {target_name} downloaded via the re-aimed link ({size} bytes).")
    if state is not None:
        state["direct_ok"] = True
        state["source_name"] = target_name
    return True


def probe_backup_download(cfg: dict, state: dict | None) -> None:
    """Probe (once per backup) whether Nitrado's API will hand over a
    gameserver backup file through any plausible download route. The
    backups list gives numbers and sizes only - no filenames, no links -
    so this tries the REST-shaped candidates and logs every answer."""
    try:
        out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/backups")
        chains = (out.get("data", {}).get("backups", {}) or {}).get("gameserver", {}) or {}
        numbers = [e.get("backup_number") for entries in chains.values()
                   for e in entries if isinstance(e, dict) and e.get("backup_number")]
        if not numbers:
            log("Backup probe: the backups answer held no gameserver backup numbers.")
            return
        number = max(numbers)
    except Exception as exc:
        log(f"Backup probe: backups lookup failed ({exc})")
        return
    if state is not None and state.get("backup_probed") == number:
        return
    log(f"Backup probe: trying download routes for gameserver backup #{number}...")
    base = f"/services/{cfg['service_id']}/gameservers/backups"
    got = None
    for path, params in [(f"{base}/download", {"backup_number": number}),
                         (f"{base}/{number}/download", None),
                         (f"{base}/{number}", None)]:
        try:
            ans = api_get(cfg, path, params)
            log(f"Backup probe: GET {path} answered: {json.dumps(ans)[:300]}")
            if ans.get("status") == "success":
                got = ans
                break
        except Exception as exc:
            log(f"Backup probe: GET {path} failed: {str(exc)[:220]}")
    if got is None:
        try:
            ans = api_post_json(cfg, f"{base}/download", {"backup_number": number})
            log(f"Backup probe: POST {base}/download answered: {json.dumps(ans)[:300]}")
            if ans.get("status") == "success":
                got = ans
        except Exception as exc:
            log(f"Backup probe: POST {base}/download failed: {str(exc)[:220]}")
    if state is not None:
        state["backup_probed"] = number
        (SAVE_DIR / "pull_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    if got is None:
        log(f"Backup probe: no route handed over backup #{number} - restore-only, as far as the API shows.")
        return
    local = SAVE_DIR / f"gameserver_backup_{number}.bin"
    try:
        size = _fetch_via_answer(got, f"gameserver backup #{number}", local)
        with open(local, "rb") as fh:
            head = fh.read(300)
        kind = "unknown format"
        if head[:2] == b"\x1f\x8b":
            kind = "gzip"
        elif head[:2] == b"PK":
            kind = "zip"
        elif head[257:262] == b"ustar":
            kind = "tar"
        log(f"Backup probe WORKED: backup #{number} downloaded ({size} bytes, looks like {kind}) "
            f"as {local.name} - ready to examine.")
        if state is not None:
            state["backup_file"] = local.name
            (SAVE_DIR / "pull_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        log(f"Backup probe: a route answered success but the fetch failed: {exc}")


def watch_backups(cfg: dict, state: dict) -> bool:
    """Walled-garden watch mode: Nitrado's file server will not hand over
    anything inside SavedArks on this Xbox service, so watch the game's own
    backup list and give each NEW backup exactly one download attempt. If
    Nitrado ever opens the door, the map starts by itself."""
    probe_backup_download(cfg, state)
    try:
        out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/games/arkse/backups")
    except Exception as exc:
        log(f"Watch mode: backup list lookup failed ({exc})")
        return False
    stem = MAP_FILE.rsplit(".", 1)[0]
    backups = [n for n in _harvest_ark_names(out) if n.startswith(stem + "_")]
    if not backups:
        return False
    newest = max(backups, key=_backup_stamp)
    if state.get("watch_tried") == newest:
        return False
    state["watch_tried"] = newest
    (SAVE_DIR / "pull_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    root = state.get("file_root") or ""
    savedarks = f"{root}/ShooterGame/Saved/SavedArks"
    log(f"Watch mode: new game backup {newest} - trying its download once...")
    try:
        size = _fetch_world(cfg, f"{savedarks}/{newest}")
    except Exception as exc:
        log(f"Watch mode: Nitrado refused the GET download ({exc}) - trying the POST variant once...")
        try:
            size = _fetch_world_post(cfg, f"{savedarks}/{newest}")
        except Exception as exc2:
            log(f"Watch mode: POST variant refused too ({exc2}). Staying in watch mode.")
            write_status(False, "Nitrado is not releasing this Xbox server's world save - "
                                "their download service errors on the save folder. The map "
                                "is watching for new backups and starts by itself if that changes.")
            return False
    log(f"Watch mode: IT WORKED - {newest} downloaded ({size} bytes). The door is open!")
    state["walled"] = False
    state["direct_ok"] = True
    state["source_name"] = newest
    state["save_dir"] = savedarks
    (SAVE_DIR / "pull_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    return True


def pull_direct(cfg: dict, state: dict) -> bool:
    """Direct-download mode for console services whose file server will not
    list the SavedArks folder: pull the newest game-internal savegame
    backup (or the save itself) by absolute path. Returns True when a new
    world file was downloaded."""
    save_dir = state.get("save_dir") or ""
    source = state.get("source_name", MAP_FILE)
    try:
        out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/games/arkse/backups")
        stem = MAP_FILE.rsplit(".", 1)[0]
        backups = [n for n in _harvest_ark_names(out) if n.startswith(stem + "_")]
        log(f"Game-internal save backups seen: {backups[:10]}")
        if backups:
            source = max(backups, key=_backup_stamp)
    except Exception as exc:
        log(f"ARK game backups lookup failed ({exc}) - using the last known source.")
    local = SAVE_DIR / MAP_FILE
    if state.get("source_name") == source and local.exists():
        return False
    size = _fetch_world(cfg, f"{save_dir}/{source}")
    state["source_name"] = source
    (SAVE_DIR / "pull_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    log(f"Downloaded {source} as the world save ({size} bytes).")
    return True


def pull_save(cfg: dict) -> bool:
    """Download save files that changed on the server (via the Nitrado API).
    Returns True if the world .ark file was (re)downloaded."""
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = SAVE_DIR / "pull_state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    if state.get("walled") and not state.get("direct_ok"):
        return watch_backups(cfg, state)

    save_dir = state.get("save_dir")
    if not save_dir:
        save_dir = find_save_dir(cfg, state)
        if not save_dir:
            return watch_backups(cfg, state)
        state["save_dir"] = save_dir

    if state.get("direct_ok"):
        return pull_direct(cfg, state)

    try:
        entries = api_list_dir(cfg, save_dir)
    except ApiError:
        log("Saved folder stopped answering - rediscovering it...")
        save_dir = find_save_dir(cfg, state)
        if not save_dir:
            state.pop("save_dir", None)
            return watch_backups(cfg, state)
        state["save_dir"] = save_dir
        if state.get("direct_ok"):
            return pull_direct(cfg, state)
        entries = api_list_dir(cfg, save_dir)

    wanted = [e for e in entries
              if _entry_name(e).lower() == MAP_FILE.lower()
              or _entry_name(e).lower().endswith(".arkprofile")
              or _entry_name(e).lower().endswith(".arktribe")]
    log(f"Save folder holds {len(entries)} files; {len(wanted)} are save files.")
    if MAP_FILE.lower() not in [_entry_name(e).lower() for e in entries]:
        log(f"WARNING: {MAP_FILE} is not in '{save_dir}' - rediscovering next time.")
        state.pop("save_dir", None)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        raise ApiError(f"{MAP_FILE} missing from the folder that had it before.")

    ark_changed = False
    for e in wanted:
        name = _entry_name(e)
        sig = f"{_entry_size(e)}:{_entry_stamp(e)}"
        local = SAVE_DIR / name
        if state.get(name) == sig and local.exists():
            continue
        size = api_download(cfg, str(e.get("path") or f"{save_dir}/{name}"), local)
        state[name] = sig
        log(f"Downloaded {name} ({size} bytes).")
        if name.lower() == MAP_FILE.lower():
            ark_changed = True
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return ark_changed


# ----------------------------------------------------------------- parse ----

_CREATURE_NAMES: dict | None = None


def creature_name(class_name: str) -> str:
    global _CREATURE_NAMES
    if _CREATURE_NAMES is None:
        try:
            _CREATURE_NAMES = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
        except Exception:
            _CREATURE_NAMES = {}
    if class_name in _CREATURE_NAMES:
        return _CREATURE_NAMES[class_name]
    n = class_name
    for suf in ("_Character_BP_C", "_Character_C", "_BP_C", "_C"):
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    return n.replace("_", " ").strip() or class_name


def structure_name(class_name: str) -> str:
    n = class_name
    for suf in ("_C",):
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    return n.replace("_", " ").strip() or class_name


def _latlon(rec: dict):
    try:
        lat = float(rec.get("lat") or 0.0)
        lon = float(rec.get("lon") or 0.0)
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    return round(lat, 2), round(lon, 2)


def cluster_bases(structures: list[dict]) -> list[dict]:
    """Group structures into bases: same owner, pieces within ~35 m."""
    by_owner: dict[str, list[dict]] = {}
    for s in structures:
        ll = _latlon(s)
        if ll is None:
            continue
        owner = (s.get("tribe") or "").strip() or "Unclaimed"
        by_owner.setdefault(owner, []).append(
            {"lat": ll[0], "lon": ll[1], "kind": structure_name(str(s.get("struct") or ""))}
        )

    bases = []
    RADIUS = 0.45  # lat/lon units; 1 unit ~ 81 m on Valguero, so ~36 m
    for owner, pieces in by_owner.items():
        clusters: list[dict] = []
        for p in pieces:
            placed = None
            for c in clusters:
                if abs(p["lat"] - c["lat"]) <= RADIUS and abs(p["lon"] - c["lon"]) <= RADIUS:
                    placed = c
                    break
            if placed is None:
                placed = {"lat": p["lat"], "lon": p["lon"], "n": 0, "kinds": {}}
                clusters.append(placed)
            n = placed["n"]
            placed["lat"] = (placed["lat"] * n + p["lat"]) / (n + 1)
            placed["lon"] = (placed["lon"] * n + p["lon"]) / (n + 1)
            placed["n"] = n + 1
            placed["kinds"][p["kind"]] = placed["kinds"].get(p["kind"], 0) + 1
        for c in clusters:
            top = sorted(c["kinds"].items(), key=lambda kv: -kv[1])[:4]
            bases.append({
                "t": owner,
                "n": c["n"],
                "lat": round(c["lat"], 2),
                "lon": round(c["lon"], 2),
                "kinds": [f"{k} x{v}" for k, v in top],
            })
    bases.sort(key=lambda b: -b["n"])
    return bases


def build_data() -> dict:
    from arkparser import (
        WorldSave,
        export_players,
        export_structures,
        export_tamed,
        export_wild,
        get_map_config_by_name,
    )

    ark_path = SAVE_DIR / MAP_FILE
    save = WorldSave.load(str(ark_path))
    try:
        cfg_map = get_map_config_by_name("Valguero")
    except Exception:
        cfg_map = None

    players = []
    for r in export_players(save, cfg_map):
        ll = _latlon(r)
        if ll is None:
            continue
        players.append({
            "n": r.get("name") or r.get("steam") or "Survivor",
            "s": r.get("steam") or "",
            "t": r.get("tribe") or "",
            "l": r.get("lvl") or 0,
            "lat": ll[0], "lon": ll[1],
            "ccc": r.get("ccc") or "",
        })

    tames, cryo_count = [], 0
    for r in export_tamed(save, cfg_map):
        if r.get("cryo"):
            cryo_count += 1
            continue
        ll = _latlon(r)
        if ll is None:
            continue
        cname = creature_name(str(r.get("creature") or ""))
        tames.append({
            "n": (r.get("name") or "").strip() or cname,
            "c": cname,
            "t": r.get("tribe") or "",
            "l": r.get("lvl") or 0,
            "x": (r.get("sex") or "")[:1],
            "lat": ll[0], "lon": ll[1],
            "ccc": r.get("ccc") or "",
        })

    wilds = []
    for r in export_wild(save, cfg_map):
        ll = _latlon(r)
        if ll is None:
            continue
        wilds.append({
            "c": creature_name(str(r.get("creature") or "")),
            "l": r.get("lvl") or 0,
            "x": (r.get("sex") or "")[:1],
            "lat": ll[0], "lon": ll[1],
        })

    structures = export_structures(save, cfg_map)
    bases = cluster_bases(structures)

    gt = float(getattr(save, "game_time", 0.0) or 0.0)
    day = int(gt // 86400)
    rem = int(gt % 86400)
    game_time = f"Day {day} - {rem // 3600:02d}:{(rem % 3600) // 60:02d}"

    return {
        "meta": {
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "save_mtime": datetime.fromtimestamp(ark_path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            "game_time": game_time,
            "counts": {
                "players": len(players),
                "tames": len(tames),
                "cryo": cryo_count,
                "wilds": len(wilds),
                "bases": len(bases),
                "structures": len(structures),
            },
        },
        "players": players,
        "tames": tames,
        "wilds": wilds,
        "bases": bases,
    }


def write_status(ok: bool, message: str) -> None:
    payload = {
        "ok": ok,
        "message": message,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, STATUS_PATH)


def refresh_once(cfg: dict, force: bool = False) -> None:
    try:
        changed = pull_save(cfg)
        if (changed or force or not DATA_PATH.exists()) and not (SAVE_DIR / MAP_FILE).exists():
            log("No world save has been downloaded yet - nothing to build. "
                "The map page shows the waiting status.")
            return
        if changed or force or not DATA_PATH.exists():
            log("Fresh save downloaded - rebuilding the map...")
            data = build_data()
            tmp = DATA_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, DATA_PATH)
            c = data["meta"]["counts"]
            log(f"Map updated: {c['players']} players, {c['tames']} tames "
                f"(+{c['cryo']} in cryo), {c['wilds']} wild, {c['bases']} bases "
                f"from {c['structures']} structures.")
            write_status(True, "OK")
        else:
            log("Save hasn't changed on the server yet - nothing to do.")
    except Exception as exc:  # keep serving the last good map
        log(f"Refresh failed: {type(exc).__name__}: {exc}")
        log("Traceback: " + traceback.format_exc(limit=4).replace("\n", " | "))
        write_status(False, str(exc))


# ------------------------------------------------------------------ serve ----


# ------------------------------------------------------------- console ----
# The web console's JSON API: everything the Nitrado API exposes for this
# service, served to the local network. Restore is deliberately NOT
# implemented anywhere in this section - there is no code path for it.

_CFG_HOLDER: dict = {}
_API_CACHE: dict = {}
_API_CACHE_LOCK = threading.Lock()
_SHAPES_LOGGED: dict = {}


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with _API_CACHE_LOCK:
        hit = _API_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _API_CACHE_LOCK:
        _API_CACHE[key] = (time.time(), val)
    return val


def _gs(cfg: dict) -> dict:
    out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers")
    return out.get("data", {}).get("gameserver", {}) or {}


def _svc(cfg: dict) -> dict:
    out = api_get(cfg, f"/services/{cfg['service_id']}")
    return out.get("data", {}).get("service", {}) or {}


def _read_state() -> dict:
    try:
        return json.loads((SAVE_DIR / "pull_state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _file_root(cfg: dict) -> str:
    root = _CFG_HOLDER.get("file_root") or _read_state().get("file_root") or ""
    if not root:
        _, root = service_info(cfg)
    if root:
        _CFG_HOLDER["file_root"] = root
    return root


def console_overview(cfg: dict) -> dict:
    gs = _cached("gameserver", 20, lambda: _gs(cfg))
    svc = _cached("service", 60, lambda: _svc(cfg))
    if not _SHAPES_LOGGED.get("overview"):
        _SHAPES_LOGGED["overview"] = True
        q = gs.get("query")
        st = gs.get("settings")
        log(f"Console: gameserver keys: {sorted(gs.keys())}")
        log(f"Console: query keys: {sorted(q.keys()) if isinstance(q, dict) else type(q).__name__}")
        log(f"Console: settings keys: {sorted(st.keys())[:60] if isinstance(st, dict) else type(st).__name__}")
    return {
        "ok": True,
        "version": VERSION,
        "service": svc,
        "gameserver": gs,
        "file_root": _file_root(cfg),
        "state": _read_state(),
        "status": _read_status(),
        "day": console_day(),
        "config": {"service_id": cfg.get("service_id"),
                   "interval_min": cfg.get("interval_min"),
                   "port": cfg.get("port")},
    }


def console_backups(cfg: dict) -> dict:
    def fetch():
        out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/backups")
        b = (out.get("data", {}) or {}).get("backups", {}) or {}
        out2 = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/games/arkse/backups")
        saves = (out2.get("data", {}) or {}).get("backups", []) or []
        return {"gameserver": b.get("gameserver", {}),
                "database": b.get("database", {}),
                "savegames": saves}

    return {"ok": True, **_cached("backups", 60, fetch)}


def console_settings(cfg: dict) -> dict:
    gs = _cached("gameserver", 20, lambda: _gs(cfg))
    result = {"ok": True, "from_gameserver": gs.get("settings")}
    for key, path in [("sets", "settings/sets"), ("current", "settings")]:
        try:
            out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/{path}")
            result[key] = out.get("data")
            if not _SHAPES_LOGGED.get(key):
                _SHAPES_LOGGED[key] = True
                d = result[key]
                log(f"Console: {path} keys: {sorted(d.keys())[:60] if isinstance(d, dict) else type(d).__name__}")
        except Exception as exc:
            result[key] = {"_error": str(exc)}
    return result


def console_logs(cfg: dict) -> dict:
    root = _file_root(cfg)
    directory = f"{root}/ShooterGame/Saved/Logs"
    return {"ok": True, "dir": directory, "entries": api_list_dir(cfg, directory)}


def console_logfile(cfg: dict, name: str) -> str:
    if name != Path(name).name or not name.endswith(".log"):
        raise ApiError("Bad log name.")
    root = _file_root(cfg)
    directory = f"{root}/ShooterGame/Saved/Logs"
    names = {_entry_name(e) for e in api_list_dir(cfg, directory)}
    if name not in names:
        raise ApiError("No such log file.")
    tmp = SAVE_DIR / "_logview.tmp"
    api_download(cfg, f"{directory}/{name}", tmp)
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return data[-200_000:].decode("utf-8", "replace")


def console_download(cfg: dict, remote: str) -> tuple[Path, str]:
    dl_dir = SAVE_DIR / "_dl"
    dl_dir.mkdir(parents=True, exist_ok=True)
    name = remote.rsplit("/", 1)[-1] or "download.bin"
    tmp = dl_dir / f"{threading.get_ident()}.bin"
    api_download(cfg, remote, tmp)
    return tmp, name


_CONTROL_ACTIONS = ("restart", "stop", "start")


def console_control(cfg: dict, action: str) -> dict:
    if action not in _CONTROL_ACTIONS:
        raise ApiError(f"Unknown control action: {action}")
    path = f"/services/{cfg['service_id']}/gameservers/{action}"
    log(f"Console: server control requested from the web console: {action}")
    out = api_post_json(cfg, path, {})
    log(f"Console: control {action} answer: {json.dumps(out)[:300]}")
    with _API_CACHE_LOCK:
        _API_CACHE.pop("gameserver", None)
    return {"ok": True, "action": action, "answer": out}


# -------------------------------------------------------------- deaths ----
DEATHS_PATH = SAVE_DIR / "deaths.json"
_TS_RE = re.compile(r"\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})[:.](\d{3})\]")
_LVL_RE = re.compile(r"^(.*?)(?:\s+-\s+Lvl\s+(\d+))?$")


def _split_level(text: str) -> tuple[str, int | None]:
    m = _LVL_RE.match(text.strip())
    if not m:
        return text.strip(), None
    return m.group(1).strip(), (int(m.group(2)) if m.group(2) else None)


_TRIBE_DEATH_RE = re.compile(r"Tribemember\s+(.+?)\s+-\s+Lvl\s+(\d+)\s+was killed(?:\s+by\s+(.+))?$")
_TAME_DEATH_RE = re.compile(r"Your\s+(.+?)\s+-\s+Lvl\s+(\d+)\s+\(([^)]*)\)\s+was killed(?:\s+by\s+(.+))?$")
_PLAIN_DEATH_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+-\s+Lvl\s+(\d+)((?:\s+\([^)]*\))*)\s+was killed(?:\s+by\s+(.+))?$")
_SID_DEATH_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+-\s+Lvl\s+(\d+)((?:\s+\([^)]*\))*)\s+was killed!?\s*\[KillerSID:\s*(\d+)\]")
PARSER_VERSION = 3
# Candidate samplers (kill lines and 'was killed' lines that match no
# known format) get one full re-read of every log file per sampler
# version, tracked per file in each tracker's sampler_scanned map, so
# a new sampler still sees the whole history exactly once.
SAMPLER_V = 3


def _species_names() -> set:
    try:
        raw = json.loads((APP_DIR / "creature_names.json").read_text(encoding="utf-8"))
        return {str(v).strip().lower() for v in raw.values()}
    except Exception:
        return set()


_SPECIES = _species_names()


def _scrub_tail(s: str) -> str:
    """Remove the trailing junk the log wrappers append after the killer
    text ('!', a stray ')', an empty '()')."""
    s = (s or "").strip()
    changed = True
    while changed:
        changed = False
        for suf in ("!", ")", "("):
            if s.endswith(suf):
                s = s[:-1].rstrip()
                changed = True
        if re.search(r"\(\s*\)$", s):
            s = re.sub(r"\s*\(\s*\)$", "", s).rstrip()
            changed = True
    return s


def parse_deaths(text: str, source: str) -> list[dict]:
    """Extract death events from ShooterGame log text (parser v3, tuned
    to this server's real formats, verified against its logs):
      tribe line:  'Tribemember <Name> - Lvl n was killed by <K> - Lvl n'
      tribe tame:  'Your <Tame> - Lvl n (<Species>) was killed by ...'
      plain line:  '<Name> - Lvl n [(<Species>)] [(<Tribe>)] was killed...'
      SID line:    '<Name> - Lvl n [(<Species>)] [(<Tribe>)] was killed!
                   [KillerSID: <id>]' - no killer name, only the
                   killer's numeric SID (found by the sampler in the
                   full 202-log pass, v2.6.2); recorded as by_sid.
    Every death is written in two of these forms; the scanner dedupes
    them on (timestamp, victim, killer). Bracket timestamps are UTC.
    The raw line is kept on every event for auditing."""
    events = []
    for line in text.splitlines():
        if "was killed" not in line:
            continue
        ts = None
        tm = _TS_RE.search(line)
        if tm:
            y, mo, d, h, mi, s = (int(tm.group(i)) for i in range(1, 7))
            ts = int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())
        clean = _scrub_tail(re.sub(r"<[^>]*>", "", line))
        who = kind = species = None
        who_lvl = None
        by, by_lvl = "", None
        by_sid = None
        m = _TRIBE_DEATH_RE.search(clean)
        if m:
            who, who_lvl, kind = m.group(1).strip(), int(m.group(2)), "player"
            if m.group(3):
                by, by_lvl = _split_level(m.group(3))
        else:
            m = _TAME_DEATH_RE.search(clean)
            if m:
                who, who_lvl, kind = m.group(1).strip(), int(m.group(2)), "tame"
                species = m.group(3)
                if m.group(4):
                    by, by_lvl = _split_level(m.group(4))
            else:
                m = _PLAIN_DEATH_RE.search(clean)
                if m and not m.group(1).strip().lower().startswith(("tribemember", "your ")):
                    who, who_lvl = m.group(1).strip(), int(m.group(2))
                    parens = [p.strip() for p in re.findall(r"\(([^)]*)\)", m.group(3)) if p.strip()]
                    species = next((p for p in parens if p.lower() in _SPECIES), None)
                    if species is None and len(parens) >= 2:
                        species = parens[0]
                    kind = "tame" if species else "player"
                    if m.group(4):
                        by, by_lvl = _split_level(m.group(4))
                if not who:
                    m = _SID_DEATH_RE.search(clean)
                    if m and not m.group(1).strip().lower().startswith(("tribemember", "your ")):
                        who, who_lvl = m.group(1).strip(), int(m.group(2))
                        parens = [p.strip() for p in re.findall(r"\(([^)]*)\)", m.group(3)) if p.strip()]
                        species = next((p for p in parens if p.lower() in _SPECIES), None)
                        if species is None and len(parens) >= 2:
                            species = parens[0]
                        kind = "tame" if species else "player"
                        by_sid = int(m.group(4))
        if not who:
            continue
        events.append({"ts": ts, "who": who, "who_lvl": who_lvl,
                       "by": by, "by_lvl": by_lvl, "by_sid": by_sid,
                       "kind": kind,
                       "species": species,
                       "source": source, "raw": line.strip()[:300]})
    return events


def parse_death_candidates(text: str, source: str) -> list[dict]:
    """'was killed' lines that match no known death format. Every known
    format requires the victim to carry a '- Lvl n' tag, so a wild
    creature's death - if the server writes those at all - would be
    dropped silently without this sampler. Samples land in
    save/deaths.json so the real wording (and whether it names what
    killed the creature) can be read from the server's own logs."""
    out = []
    for line in text.splitlines():
        if "was killed" not in line.lower():
            continue
        if parse_deaths(line, source):
            continue
        out.append({"raw": line.strip()[:300], "source": source})
    return out


def _death_key(e: dict) -> tuple:
    return (e.get("ts"), (e.get("who") or "").lower(), (e.get("by") or "").lower())


def _load_deaths() -> dict:
    try:
        data = json.loads(DEATHS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            if data.get("parser", 1) < PARSER_VERSION:
                data = _repair_deaths(data)
                _save_deaths(data)
            return data
    except Exception:
        pass
    return {"version": 1, "parser": PARSER_VERSION, "events": [], "scanned": {}}


def _repair_deaths(data: dict) -> dict:
    """Rebuild stored events with the current parser. Older builds
    captured this server's real log formats imperfectly (names polluted
    with prefixes, killer levels lost, each death stored twice). Every
    event kept its raw line, so history rebuilds exactly - no
    re-download needed."""
    rebuilt: list[dict] = []
    seen = set()
    for old in data.get("events", []):
        for ev in parse_deaths(old.get("raw", ""), old.get("source", "")):
            key = _death_key(ev)
            if key in seen:
                continue
            seen.add(key)
            rebuilt.append(ev)
    rebuilt.sort(key=lambda e: (e.get("ts") or 0))
    data["events"] = rebuilt
    data["parser"] = PARSER_VERSION
    log(f"Deaths: rebuilt history with parser v{PARSER_VERSION} - "
        f"{len(rebuilt)} deaths ({sum(1 for e in rebuilt if e['kind'] == 'player')} player, "
        f"{sum(1 for e in rebuilt if e['kind'] == 'tame')} tame).")
    return data

def _save_deaths(data: dict) -> None:
    tmp = DEATHS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, DEATHS_PATH)


def _absorb_deaths(data: dict, text: str, name: str) -> list[dict]:
    seen = {_death_key(e) for e in data["events"]}
    new = []
    for ev in parse_deaths(text, name):
        key = _death_key(ev)
        if key in seen:
            continue
        seen.add(key)
        data["events"].append(ev)
        new.append(ev)
    cands = data.setdefault("candidates", [])
    seen_raw = {c.get("raw") for c in cands}
    for c in parse_death_candidates(text, name):
        data["candidate_count"] = data.get("candidate_count", 0) + 1
        if c["raw"] not in seen_raw and len(cands) < 12:
            seen_raw.add(c["raw"])
            cands.append(c)
    return new


# --------------------------------------------------------------- kills ----
KILLS_PATH = SAVE_DIR / "kills.json"
_TRIBE_KILL_RE = re.compile(r"Tribemember\s+(.+?)\s+-\s+Lvl\s+(\d+)\s+killed\s+(.+?)\s*$")
_TAME_KILL_RE = re.compile(r"Your\s+(.+?)\s+-\s+Lvl\s+(\d+)\s+\(([^)]*)\)\s+killed\s+(.+?)\s*$")
_PLAIN_KILL_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+-\s+Lvl\s+(\d+)((?:\s+\([^)]*\))*)\s+killed\s+(.+?)\s*$")
_TRIBE_KILL_NL_RE = re.compile(r"Tribemember\s+(.+?)\s+killed\s+(.+?)\s*$")
_TAME_KILL_NL_RE = re.compile(r"Your\s+(.+?)\s+\(([^)]*)\)\s+killed\s+(.+?)\s*$")


def _line_ts(line: str):
    tm = _TS_RE.search(line)
    if not tm:
        return None
    y, mo, d, h, mi, s = (int(tm.group(i)) for i in range(1, 7))
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def _scrub_target(s: str) -> str:
    s = (s or "").strip().rstrip("!").strip()
    s = re.sub(r"\s*\(\s*\)$", "", s).strip()
    if s.endswith(")") and s.count("(") != s.count(")"):
        s = s[:-1].rstrip("!").strip()
    return s


def _clean_target(raw: str):
    name, lvl = _split_level(_scrub_target(raw))
    species = None
    m = re.search(r"\(([^)]*)\)\s*$", name)
    if m and m.group(1).strip().lower() in _SPECIES:
        species = m.group(1).strip()
        name = name[:m.start()].strip()
    return name, lvl, species


def parse_kills(text: str, source: str) -> list[dict]:
    """Extract kill events (the mirror of the death formats): a tribe
    member or a tame killing something, in tribe-log and plain forms.
    Death lines ('was killed') are excluded - the death parser owns
    those. The killer's level and the target's level are both kept."""
    events = []
    for line in text.splitlines():
        if "killed" not in line or "was killed" in line:
            continue
        ts = _line_ts(line)
        clean = re.sub(r"<[^>]*>", "", line)
        who = kind = species = None
        who_lvl = None
        target = ""
        target_lvl = target_species = None
        m = _TRIBE_KILL_RE.search(clean)
        if m:
            who, who_lvl, kind = m.group(1).strip(), int(m.group(2)), "player"
            target, target_lvl, target_species = _clean_target(m.group(3))
        else:
            m = _TAME_KILL_RE.search(clean)
            if m:
                who, who_lvl, kind = m.group(1).strip(), int(m.group(2)), "tame"
                species = m.group(3)
                target, target_lvl, target_species = _clean_target(m.group(4))
            else:
                m = _PLAIN_KILL_RE.search(clean)
                if m and not m.group(1).strip().lower().startswith(("tribemember", "your ")):
                    who, who_lvl = m.group(1).strip(), int(m.group(2))
                    parens = [p.strip() for p in re.findall(r"\(([^)]*)\)", m.group(3)) if p.strip()]
                    species = next((p for p in parens if p.lower() in _SPECIES), None)
                    if species is None and len(parens) >= 2:
                        species = parens[0]
                    kind = "tame" if species else "player"
                    target, target_lvl, target_species = _clean_target(m.group(4))
                else:
                    m = _TRIBE_KILL_NL_RE.search(clean)
                    if m:
                        who, kind = m.group(1).strip(), "player"
                        target, target_lvl, target_species = _clean_target(m.group(2))
                    else:
                        m = _TAME_KILL_NL_RE.search(clean)
                        if m:
                            who, kind = m.group(1).strip(), "tame"
                            species = m.group(2)
                            target, target_lvl, target_species = _clean_target(m.group(3))
        if not who or not target:
            continue
        events.append({"ts": ts, "who": who, "who_lvl": who_lvl,
                       "target": target, "target_lvl": target_lvl,
                       "target_species": target_species,
                       "kind": kind, "species": species,
                       "source": source, "raw": line.strip()[:300]})
    return events


def parse_kill_candidates(text: str, source: str) -> list[dict]:
    """Lines that mention killing but match no known kill format (and
    are not death lines). Collected as samples so the real kill format
    can be identified from the server's own logs instead of guessed -
    the first full scan (v2.3.0) matched zero kills in 202 files, so
    either kills are phrased differently or they are not in the log
    stream at all; these samples settle which.

    Two traps are filtered out on Clint's word (2026-11-25): cryopod
    lines ("... froze ...") are not kills, and the tribe has a Tek
    tame NAMED "Killer" - a bare substring match on "kill" would flag
    every line about that dino, so a real kill verb ("killed", or a
    standalone kill/kills/killing) is required."""
    out = []
    for line in text.splitlines():
        low = line.lower()
        if "kill" not in low or "was killed" in low or "froze" in low:
            continue
        if "killed" not in low and not re.search(r"\bkill(s|ing)?\b", low):
            continue
        if parse_kills(line, source):
            continue
        out.append({"raw": line.strip()[:300], "source": source})
    return out


def _kill_key(e: dict) -> tuple:
    return (e.get("ts"), (e.get("who") or "").lower(), (e.get("target") or "").lower())


def _load_kills() -> dict:
    try:
        data = json.loads(KILLS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
    except Exception:
        pass
    return {"version": 1, "events": [], "scanned": {}}


def _save_kills(data: dict) -> None:
    tmp = KILLS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, KILLS_PATH)


def _absorb_kills(data: dict, text: str, name: str) -> list[dict]:
    seen = {_kill_key(e) for e in data["events"]}
    new = []
    for ev in parse_kills(text, name):
        key = _kill_key(ev)
        if key in seen:
            continue
        seen.add(key)
        data["events"].append(ev)
        new.append(ev)
    cands = data.setdefault("candidates", [])
    seen_raw = {c.get("raw") for c in cands}
    for c in parse_kill_candidates(text, name):
        data["candidate_count"] = data.get("candidate_count", 0) + 1
        if c["raw"] not in seen_raw and len(cands) < 12:
            seen_raw.add(c["raw"])
            cands.append(c)
    return new


# --------------------------------------------------------------- tames ----
TAMES_PATH = SAVE_DIR / "tames.json"
_FROZE_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+froze\s+(.+?)\s+-\s+Lvl\s+(\d+)\s*\(([^)]*)\)")
_RELEASE_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+(?:released|unfroze)\s+(.+?)\s+-\s+Lvl\s+(\d+)\s*\(([^)]*)\)")
_TAMED_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+[Tt]amed\s+(?:a\s+|an\s+)?(.+?)\s+-\s+Lvl\s+(\d+)(?:\s*\(([^)]*)\))?")
_CLAIMED_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+[Cc]laimed\s+(?:a\s+|an\s+)?(.+?)\s+-\s+Lvl\s+(\d+)(?:\s*\(([^)]*)\))?")
_TAME_WORDS = ("tamed", "froze", "unfroze", "released", "cryo", "claimed")


def parse_tames(text: str, source: str) -> list[dict]:
    """Tame lifecycle events from log text: claims (taming), cryopod
    freezes and releases. The froze form is verified against this
    server's real tribe lines ('Dakota froze Killer Tech - Lvl 131
    (Tek Rex)'); the claim/release wordings follow the same tribe-log
    shape, and whatever they miss is collected by
    parse_tame_candidates so the real wording can be read from the
    samples - the same learn-from-the-logs approach as kills/deaths."""
    out = []
    for line in text.splitlines():
        low = line.lower()
        if not any(w in low for w in _TAME_WORDS):
            continue
        ts = _line_ts(line)
        # No _scrub_tail here: it eats trailing ')' chars, which would
        # remove the species' own closing paren on froze lines. These
        # patterns anchor on their keywords, so wrapper junk after the
        # match is harmless.
        clean = re.sub(r"<[^>]*>", "", line)
        for rx, kind in ((_FROZE_RE, "froze"), (_RELEASE_RE, "release"),
                         (_TAMED_RE, "claim"), (_CLAIMED_RE, "claim")):
            m = rx.search(clean)
            if not m:
                continue
            name = m.group(2).strip()
            species = (m.group(4) or "").strip()
            if kind == "claim" and not species:
                species = name
            out.append({"ts": ts, "kind": kind,
                        "actor": m.group(1).strip(), "name": name,
                        "lvl": int(m.group(3)), "species": species,
                        "source": source, "raw": line.strip()[:300]})
            break
    return out


def parse_tame_candidates(text: str, source: str) -> list[dict]:
    """Tame-ish lines (claim/cryo wording) that match no known tame
    format and are not death lines. Collected in save/tames.json so
    the real claim/release wording can be tuned from the server's own
    logs instead of guessed."""
    out = []
    for line in text.splitlines():
        low = line.lower()
        if not any(w in low for w in _TAME_WORDS):
            continue
        if "was killed" in low:
            continue
        if parse_tames(line, source):
            continue
        out.append({"raw": line.strip()[:300], "source": source})
    return out


def _load_tames() -> dict:
    try:
        data = json.loads(TAMES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("records"), dict):
            return data
    except Exception:
        pass
    return {"version": 1, "records": {}, "scanned": {}}


def _save_tames(data: dict) -> None:
    tmp = TAMES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, TAMES_PATH)


def _tame_key(name: str, species: str) -> str:
    return f"{(name or '').lower()}|{(species or '').lower()}"


def _absorb_tames(data: dict, text: str, name: str,
                  death_events: list) -> list:
    """Fold one log file's tame events (claims, freezes, releases from
    parse_tames; deaths from parse_deaths) into the tame records. A
    record is one tame, keyed by name + breed: first/last seen, the
    highest level sighted, claim time, current cryo state, and its
    death if it has one. A death is never undone by a later event,
    and an out-of-order file cannot rewind the cryo state."""
    records = data["records"]
    evs = []
    for e in parse_tames(text, name):
        evs.append({"ts": e["ts"], "kind": e["kind"], "name": e["name"],
                    "species": e["species"], "lvl": e["lvl"],
                    "raw": e["raw"]})
    for e in death_events:
        if e.get("kind") == "tame":
            evs.append({"ts": e["ts"], "kind": "death", "name": e["who"],
                        "species": e.get("species") or "",
                        "lvl": e.get("who_lvl"),
                        "by": e.get("by") or "", "raw": e["raw"]})
    evs.sort(key=lambda x: (x["ts"] or 0))
    touched = []
    for e in evs:
        key = _tame_key(e["name"], e["species"])
        r = records.get(key)
        if r is None:
            r = {"name": e["name"],
                 "species": e["species"] or "Unknown type",
                 "level": None, "first_seen": e["ts"], "claimed": None,
                 "frozen": False, "death": None,
                 "last_ts": None, "last_kind": "", "events": []}
            records[key] = r
            touched.append(key)
        ts = e["ts"]
        if ts is not None and (r["first_seen"] is None
                               or ts < r["first_seen"]):
            r["first_seen"] = ts
        if e["lvl"] and (r["level"] is None or e["lvl"] > r["level"]):
            r["level"] = e["lvl"]
        if e["kind"] == "claim":
            if r["claimed"] is None or (ts is not None
                                        and ts < r["claimed"]):
                r["claimed"] = ts
        elif e["kind"] == "death":
            if r["death"] is None or (ts is not None and r["death"]["ts"]
                                      is not None
                                      and ts < r["death"]["ts"]):
                r["death"] = {"ts": ts, "by": e.get("by", "")}
        elif e["kind"] in ("froze", "release"):
            if r["death"] is None and (r["last_ts"] is None
                                       or ts is None
                                       or ts >= r["last_ts"]):
                r["frozen"] = e["kind"] == "froze"
        if ts is not None and (r["last_ts"] is None or ts >= r["last_ts"]):
            r["last_ts"] = ts
            r["last_kind"] = e["kind"]
        r["events"].append({"ts": ts, "kind": e["kind"]})
        r["events"] = r["events"][-40:]
    cands = data.setdefault("candidates", [])
    seen_raw = {c.get("raw") for c in cands}
    for c in parse_tame_candidates(text, name):
        data["candidate_count"] = data.get("candidate_count", 0) + 1
        if c["raw"] not in seen_raw and len(cands) < 12:
            seen_raw.add(c["raw"])
            cands.append(c)
    return touched


def console_tames() -> dict:
    data = _load_tames()
    recs = []
    for r in data["records"].values():
        f = dict(r)
        f["status"] = "dead" if r.get("death") else (
            "frozen" if r.get("frozen") else "living")
        recs.append(f)
    recs.sort(key=lambda r: (r.get("name") or "").lower())
    dead = sum(1 for r in recs if r["status"] == "dead")
    frozen = sum(1 for r in recs if r["status"] == "frozen")
    return {"ok": True, "tames": recs,
            "counts": {"total": len(recs), "living": len(recs) - dead,
                       "frozen": frozen, "dead": dead},
            "candidates": data.get("candidates", []),
            "candidate_count": data.get("candidate_count", 0)}


# -------------------------------------------------------------- levels ----
LEVELS_PATH = SAVE_DIR / "levels.json"
_LEVELUP_RES = [
    re.compile(r"Tribemember\s+(.+?)\s+(?:leveled|levelled)\s+up\s+to\s+(?:level\s+)?(\d+)", re.I),
    re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+(?:leveled|levelled)\s+up\s+to\s+(?:level\s+)?(\d+)", re.I),
    re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+has\s+reached\s+level\s+(\d+)", re.I),
]


def parse_levelups(text: str, source: str) -> list[dict]:
    """Explicit level-up lines, if the server writes any. Most level
    sightings come from death/kill lines, which carry the player's
    current level - those are folded in by _absorb_levels."""
    out = []
    for line in text.splitlines():
        if "level" not in line.lower():
            continue
        clean = re.sub(r"<[^>]*>", "", line)
        for rx in _LEVELUP_RES:
            m = rx.search(clean)
            if m:
                who = m.group(1).strip().strip('"')
                if who and ":" not in who and len(who) <= 40:
                    out.append({"ts": _line_ts(line), "who": who,
                                "level": int(m.group(2)), "via": "levelup",
                                "source": source, "raw": line.strip()[:300]})
                break
    return out


def _load_levels() -> dict:
    try:
        data = json.loads(LEVELS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("observations"), list):
            return data
    except Exception:
        pass
    return {"version": 1, "observations": [], "scanned": {}}


def _save_levels(data: dict) -> None:
    tmp = LEVELS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, LEVELS_PATH)


def _level_key(o: dict) -> tuple:
    return (o.get("ts"), (o.get("who") or "").lower(), o.get("level"), o.get("via"))


def _absorb_levels(data: dict, deaths: list, kills: list, levelups: list) -> list[dict]:
    seen = {_level_key(o) for o in data["observations"]}
    new = []
    cands = []
    for e in deaths:
        if e.get("kind") == "player" and e.get("who_lvl"):
            cands.append({"ts": e["ts"], "who": e["who"], "level": e["who_lvl"],
                          "via": "death", "source": e["source"], "raw": e["raw"]})
    for e in kills:
        if e.get("kind") == "player" and e.get("who_lvl"):
            cands.append({"ts": e["ts"], "who": e["who"], "level": e["who_lvl"],
                          "via": "kill", "source": e["source"], "raw": e["raw"]})
    cands.extend(levelups)
    for o in cands:
        key = _level_key(o)
        if key in seen:
            continue
        seen.add(key)
        data["observations"].append(o)
        new.append(o)
    return new


PLAYER_NAMES_PATH = SAVE_DIR / "player_names.json"


def _resolve_name(name: str, names: dict) -> str:
    """Display name for a character name OR an Xbox gamertag (the logs
    use characters for deaths/kills and gamertags for joins/leaves);
    matching is case-insensitive."""
    if not name:
        return name
    if name in names:
        return names[name]
    low = name.lower()
    for k, v in names.items():
        if k.lower() == low:
            return v
    return name


def _player_names() -> dict:
    """Character name / Xbox gamertag -> display name, from
    save/player_names.json. Ships empty: players are labelled by
    their character name or gamertag until you add your own
    mappings (for example {"xSomeGamertag": "CharacterName"})."""
    try:
        raw = json.loads(PLAYER_NAMES_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}


_DAY_RE = re.compile(r"Day\s+(\d+)\s*,")
_DAY_CACHE: dict = {}


def console_day() -> dict:
    """The server's current in-game day, from tribe-log day stamps.

    Every tribe-log line ARK writes carries 'Day <n>,' - the death
    raws keep those lines, and the cached server logs in save/logs
    carry even more of them (cryo, demolish, claim entries). The
    newest stamp found is the last CONFIRMED day; the counter advances
    at a steady rate (measured across all stamps), so the current day
    is estimated from that rate and labelled an estimate. Returns
    {"ok": False} until any stamp has been seen."""
    try:
        sig_parts = []
        try:
            sig_parts.append(str(DEATHS_PATH.stat().st_mtime))
        except Exception:
            sig_parts.append("0")
        cache_files = []
        try:
            cache_files = sorted(LOGCACHE_DIR.glob("*.log"),
                                 key=lambda p: p.stat().st_mtime,
                                 reverse=True)[:8]
            for p in cache_files:
                st = p.stat()
                sig_parts.append(f"{p.name}:{st.st_size}:{st.st_mtime}")
        except Exception:
            cache_files = []
        sig = "|".join(sig_parts)
        if _DAY_CACHE.get("sig") == sig and _DAY_CACHE.get("val"):
            return _DAY_CACHE["val"]
        stamps = []
        try:
            for e in _load_deaths()["events"]:
                m = _DAY_RE.search(e.get("raw") or "")
                if m and e.get("ts"):
                    stamps.append((e["ts"], int(m.group(1))))
        except Exception:
            pass
        for p in cache_files:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for line in text.splitlines():
                if "Day" not in line:
                    continue
                m = _DAY_RE.search(line)
                if not m:
                    continue
                ts = _line_ts(line)
                if ts:
                    stamps.append((ts, int(m.group(1))))
        if not stamps:
            val = {"ok": False}
        else:
            stamps.sort()
            first_ts, first_day = stamps[0]
            last_ts, last_day = stamps[-1]
            rate = None
            if last_ts - first_ts > 7 * 86400 and last_day > first_day:
                rate = (last_day - first_day) / (last_ts - first_ts)
            est = last_day
            if rate:
                est = last_day + int(rate * (time.time() - last_ts))
            val = {"ok": True, "day": last_day, "ts": last_ts,
                   "est_day": est,
                   "per_real_day": round(rate * 86400, 1) if rate else None,
                   "stamps": len(stamps)}
        _DAY_CACHE["sig"] = sig
        _DAY_CACHE["val"] = val
        return val
    except Exception:
        return {"ok": False}


def console_deaths() -> dict:
    data = _load_deaths()
    names = _player_names()
    events = []
    for e in data["events"]:
        f = dict(e)
        if f.get("kind") == "player":
            f["player"] = _resolve_name(f.get("who", ""), names)
        events.append(f)
    return {"ok": True, "events": events,
            "candidates": data.get("candidates", []),
            "candidate_count": data.get("candidate_count", 0)}


def console_kills() -> dict:
    data = _load_kills()
    names = _player_names()
    events = []
    for e in data["events"]:
        f = dict(e)
        if f.get("kind") == "player":
            f["player"] = _resolve_name(f.get("who", ""), names)
        events.append(f)
    return {"ok": True, "events": events,
            "candidates": data.get("candidates", []),
            "candidate_count": data.get("candidate_count", 0)}


def console_levels() -> dict:
    """Per-player level picture from every sighting the logs give us
    (death lines, kill lines, explicit level-up lines): the current
    level is the highest ever sighted, and the history is the first
    time each new high was seen - the level-up history the logs can
    prove."""
    data = _load_levels()
    names = _player_names()
    by_player: dict = {}
    for o in sorted(data["observations"], key=lambda x: (x.get("ts") or 0)):
        by_player.setdefault((o.get("who") or "").lower(), []).append(o)
    out = []
    for obs in by_player.values():
        history = []
        best = 0
        for o in obs:
            if (o.get("level") or 0) > best:
                best = o["level"]
                history.append({"ts": o.get("ts"), "level": o["level"],
                                "via": o.get("via")})
        series = []
        prev = None
        for o in obs:
            if prev is None or o.get("level") != prev:
                series.append({"ts": o.get("ts"), "level": o.get("level")})
                prev = o.get("level")
        if series and obs and series[-1]["ts"] != obs[-1].get("ts"):
            series.append({"ts": obs[-1].get("ts"),
                           "level": obs[-1].get("level")})
        char = obs[0].get("who", "")
        out.append({"name": _resolve_name(char, names), "character": char,
                    "current": best, "history": history, "series": series,
                    "sightings": len(obs)})
    out.sort(key=lambda p: -p["current"])
    return {"ok": True, "players": out}


# ------------------------------------------------------------ presence ----
PRESENCE_PATH = SAVE_DIR / "presence.json"
_JOIN_RES = [
    re.compile(r"(.+?)\s+joined this ARK!"),
    re.compile(r"Join succeeded:\s*(.+?)\s*$"),
    re.compile(r"(.+?)\s+joined the game\b"),
]
_LEAVE_RES = [
    re.compile(r"(.+?)\s+left this ARK!"),
    re.compile(r"(.+?)\s+left the game\b"),
]


def _clean_who(raw: str) -> str:
    who = raw.strip().strip('"').strip()
    who = re.sub(r"(?i)^player\s+", "", who).strip()
    who, _ = _split_level(who)
    if not who or len(who) > 40 or "/" in who or "\\" in who or ":" in who:
        return ""
    if re.fullmatch(r"[\d.:]+", who):  # an IP or a number, not a name
        return ""
    return who


def parse_presence(text: str, source: str) -> list[dict]:
    """Extract join / leave / server-boot events from ShooterGame log text.
    A boot closes every open session (a restart logs everyone off), which
    keeps the roster honest even if a leave line is phrased unexpectedly."""
    events = []
    for line in text.splitlines():
        tm = _TS_RE.search(line)
        ts = None
        if tm:
            y, mo, d, h, mi, s = (int(tm.group(i)) for i in range(1, 7))
            ts = int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())
        if "Log file open" in line:
            events.append({"ts": ts, "who": "", "action": "boot",
                           "source": source, "raw": line.strip()[:200]})
            continue
        if "joined" not in line and "left" not in line and "Join succeeded" not in line:
            continue
        msg = line[tm.end():] if tm else line
        msg = re.sub(r"^\s*\[\s*\d+\]\s*", "", msg).strip()
        msg = re.sub(r"^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}:\s*", "", msg)
        for rxs, action in ((_JOIN_RES, "on"), (_LEAVE_RES, "off")):
            hit = None
            for rx in rxs:
                m = rx.search(msg)
                if m:
                    hit = _clean_who(m.group(1))
                    break
            if hit:
                events.append({"ts": ts, "who": hit, "action": action,
                               "source": source, "raw": line.strip()[:300]})
                break
    return events


def _repair_presence(data: dict) -> dict:
    """Rebuild presence events from their stored raw lines with the
    current parser. The first backfill (v2.2.0) left the secondary
    timestamp glued to every gamertag ('2026.08.23_21.38.57:
    SomeGamertag'), so every join looked like a different player; the
    fixed parser strips it, and the raw lines carry everything needed
    to rebuild the history cleanly."""
    rebuilt = []
    seen = set()
    for e in data.get("events", []):
        for f in parse_presence(e.get("raw", ""), e.get("source", "")):
            if f.get("ts") is None:
                f["ts"] = e.get("ts")
            key = (f.get("ts"), (f.get("who") or "").lower(), f.get("action"))
            if key in seen:
                continue
            seen.add(key)
            rebuilt.append(f)
    rebuilt.sort(key=lambda e: (e.get("ts") or 0))
    data["events"] = rebuilt
    data["parser"] = 2
    return data


def _load_presence() -> dict:
    try:
        data = json.loads(PRESENCE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            if data.get("parser") != 2:
                data = _repair_presence(data)
                _save_presence(data)
            return data
    except Exception:
        pass
    return {"version": 1, "events": [], "scanned": {}}


def _save_presence(data: dict) -> None:
    tmp = PRESENCE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, PRESENCE_PATH)


def _absorb_presence(data: dict, text: str, name: str) -> list[dict]:
    seen = {(e.get("ts"), (e.get("who") or "").lower(), e.get("action"))
            for e in data["events"]}
    new = []
    for ev in parse_presence(text, name):
        key = (ev.get("ts"), (ev.get("who") or "").lower(), ev.get("action"))
        if key in seen:
            continue
        seen.add(key)
        data["events"].append(ev)
        new.append(ev)
    return new


LOGCACHE_DIR = SAVE_DIR / "logs"


def scan_logs(cfg: dict) -> None:
    """One pass over the log folder feeding every tracker (deaths,
    kills, presence, levels). Each tracker keeps its own scanned-file
    fingerprints in its own state file; a log is downloaded once if any
    tracker has not seen its current version, every tracker that needed
    it absorbs it, and state is saved after each file so an interrupted
    backfill resumes where it stopped. Downloaded logs are KEPT in
    save/logs (Clint's call, v2.5.4): a local research copy of every
    log the scanner pulls, so formats can be studied - and new
    features built against the real lines - without re-downloading."""
    root = _file_root(cfg)
    if not root:
        return
    directory = f"{root}/ShooterGame/Saved/Logs"
    entries = api_list_dir(cfg, directory)
    deaths = _load_deaths()
    kills = _load_kills()
    presence = _load_presence()
    levels = _load_levels()
    tames = _load_tames()
    domains = [("deaths", deaths, _save_deaths),
               ("kills", kills, _save_kills),
               ("presence", presence, _save_presence),
               ("levels", levels, _save_levels),
               ("tames", tames, _save_tames)]
    sampler = {}
    for dname, ddata, _dsave in domains:
        if dname in ("deaths", "kills", "tames"):
            ddata.setdefault("sampler_scanned", {})
            sampler[dname] = ddata.get("sampler_v", 0) < SAMPLER_V
            if sampler[dname] and ddata.get("candidates_v") != SAMPLER_V:
                # a new sampler generation re-collects its samples
                # under the current parser/filter - old samples are
                # replaced, not merged (v1's samples were cryo lines
                # about the tame named Killer; they would otherwise
                # linger forever). Stamped so a mid-pass restart does
                # not wipe the new generation's early samples.
                ddata["candidates"] = []
                ddata["candidate_count"] = 0
                ddata["candidates_v"] = SAMPLER_V
    todo = []
    skipped = 0
    all_log_names = []
    for e in entries:
        name = _entry_name(e)
        if not name.endswith(".log"):
            continue
        all_log_names.append(name)
        need = [d for d in domains
                if not _already_scanned(d[1].setdefault("scanned", {}), name, e)
                or (sampler.get(d[0])
                    and name not in d[1]["sampler_scanned"])]
        if not need:
            skipped += 1
            continue
        todo.append((name, _entry_fingerprint(e), need))
    if not todo:
        return
    new_deaths: list = []
    new_kills: list = []
    new_presence: list = []
    for i, (name, fp, need) in enumerate(todo, 1):
        dest = LOGCACHE_DIR / Path(name).name
        try:
            LOGCACHE_DIR.mkdir(parents=True, exist_ok=True)
            api_download(cfg, f"{directory}/{name}", dest)
            text = dest.read_bytes().decode("utf-8", "replace")
        except Exception as exc:
            log(f"Logs: could not download {name} ({exc})")
            continue
        need_names = {d[0] for d in need}
        d_events = k_events = None
        if "deaths" in need_names:
            d_events = _absorb_deaths(deaths, text, name)
            new_deaths.extend(d_events)
        if "kills" in need_names:
            k_events = _absorb_kills(kills, text, name)
            new_kills.extend(k_events)
        if "presence" in need_names:
            new_presence.extend(_absorb_presence(presence, text, name))
        if "levels" in need_names:
            if d_events is None:
                d_events = parse_deaths(text, name)
            if k_events is None:
                k_events = parse_kills(text, name)
            _absorb_levels(levels, d_events, k_events, parse_levelups(text, name))
        if "tames" in need_names:
            if d_events is None:
                d_events = parse_deaths(text, name)
            _absorb_tames(tames, text, name, d_events)
        for dname, ddata, dsave in need:
            ddata["scanned"][name] = fp
            if sampler.get(dname):
                ddata["sampler_scanned"][name] = fp
            key = "observations" if dname == "levels" else "events"
            if isinstance(ddata.get(key), list):
                ddata[key].sort(key=lambda e: (e.get("ts") or 0))
            dsave(ddata)
        if len(todo) > 10 and (i % 25 == 0 or i == len(todo)):
            log(f"Logs: scanned {i}/{len(todo)} log files...")
    if todo:
        log(f"Logs: downloaded log files are kept in save/logs "
            f"({len(todo)} file(s) this pass) as the research copy.")
    for dname, ddata, dsave in domains:
        if sampler.get(dname) and all(
                n in ddata["sampler_scanned"] for n in all_log_names):
            ddata["sampler_v"] = SAMPLER_V
            dsave(ddata)
    big = len(todo) > 10
    if new_deaths:
        for ev in new_deaths[:3]:
            log(f"Deaths sample: {ev['raw']}")
    if big or new_deaths:
        log(f"Deaths: pass complete - {len(deaths['events'])} death(s) on record"
            + (f", {len(new_deaths)} new." if new_deaths else "."))
    if new_kills:
        for ev in new_kills[:3]:
            log(f"Kills sample: {ev['raw']}")
        log(f"Kills: pass complete - {len(kills['events'])} kill(s) on record, "
            f"{len(new_kills)} new.")
    elif big and not kills["events"]:
        if kills.get("candidates"):
            for c in kills["candidates"][:3]:
                log(f"Kills candidate: {c['raw']}")
            log(f"Kills: no kills matched yet, but "
                f"{kills.get('candidate_count', 0)} kill-like line(s) were "
                f"collected as samples (see save/kills.json).")
        else:
            log("Kills: no kill lines matched in the logs scanned so far - "
                "the kill wording needs the real lines to tune against.")
    if big and deaths.get("candidates"):
        for c in deaths["candidates"][:3]:
            log(f"Deaths candidate: {c['raw']}")
        log(f"Deaths: {deaths.get('candidate_count', 0)} 'was killed' line(s) "
            "matched no known death format and were sampled in "
            "save/deaths.json - wild-creature deaths would land here.")
    if big:
        n_dead = sum(1 for r in tames["records"].values() if r.get("death"))
        log(f"Tames: {len(tames['records'])} tame(s) on record, "
            f"{len(tames['records']) - n_dead} living, {n_dead} dead"
            + (f"; {tames.get('candidate_count', 0)} tame line(s) sampled "
               "for wording (see save/tames.json)."
               if tames.get("candidates") else "."))
    joins = [e for e in new_presence if e["action"] == "on"]
    if joins:
        log(f"Presence: {len(joins)} new log-on event(s); "
            f"{sum(1 for e in presence['events'] if e['action'] == 'on')} total.")
        for ev in joins[:3]:
            log(f"Presence sample: {ev['raw']}")
    elif big and not any(e["action"] == "on" for e in presence["events"]):
        log("Presence: no log-on lines matched in the logs scanned so far - "
            "the join wording needs the real lines to tune against.")
    if big:
        log(f"Logs: pass complete - {len(todo)} new/changed file(s), "
            f"{skipped} already scanned.")


def _scan_logs_quietly(cfg: dict) -> None:
    try:
        scan_logs(cfg)
    except Exception as exc:
        log(f"Log scan failed ({exc})")


def console_presence() -> dict:
    data = _load_presence()
    players: dict = {}
    activity: list[dict] = []
    for e in sorted(data["events"], key=lambda x: (x.get("ts") or 0)):
        if e["action"] == "boot":
            for p in players.values():
                if p["online"] and p["since"]:
                    p["total_seconds"] += max(0, (e.get("ts") or 0) - p["since"])
                if p["online"]:
                    p["last_off"] = e.get("ts")
                p["online"] = False
                p["since"] = None
            continue
        key = (e.get("who") or "").lower()
        if not key:
            continue
        p = players.setdefault(key, {"name": e["who"], "online": False, "since": None,
                                     "last_on": None, "last_off": None,
                                     "sessions": 0, "total_seconds": 0})
        p["name"] = e["who"]
        if e["action"] == "on":
            if not p["online"]:
                p["online"] = True
                p["since"] = e.get("ts")
                p["sessions"] += 1
            p["last_on"] = e.get("ts")
        elif e["action"] == "off":
            if p["online"] and p["since"]:
                p["total_seconds"] += max(0, (e.get("ts") or 0) - p["since"])
            p["online"] = False
            p["since"] = None
            p["last_off"] = e.get("ts")
        activity.append(e)
    names = _player_names()
    out_players = []
    for p in players.values():
        q = dict(p)
        q["character"] = p["name"]
        q["name"] = _resolve_name(p["name"], names)
        out_players.append(q)
    out_activity = []
    for e in list(reversed(activity))[:200]:
        f = dict(e)
        f["who"] = _resolve_name(e.get("who", ""), names)
        out_activity.append(f)
    return {"ok": True,
            "players": sorted(out_players,
                              key=lambda p: (not p["online"], -(p["last_on"] or 0))),
            "activity": out_activity}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        # no-store on the pages too, not just the data: without it the
        # browser heuristically caches index.html and page updates
        # look like they never shipped (bit us on the v2.6.0 kills
        # removal - Clint hard-refreshed and still saw the old page).
        if (self.path.startswith(("/data.json", "/status.json", "/api/"))
                or self.path in ("/", "/index.html", "/map.html")
                or self.path.endswith(".html")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):  # quiet console
        pass

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_error(self, exc: Exception) -> None:
        self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 502)

    def do_GET(self):
        if not self.path.startswith("/api/"):
            return super().do_GET()
        cfg = _CFG_HOLDER.get("cfg")
        if cfg is None:
            return self._send_json({"ok": False, "error": "Console not ready yet."}, 503)
        parts = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parts.query)
        try:
            if parts.path == "/api/overview":
                self._send_json(console_overview(cfg))
            elif parts.path == "/api/files":
                directory = (qs.get("dir") or [""])[0]
                self._send_json({"ok": True, "dir": directory,
                                 "entries": api_list_dir(cfg, directory)})
            elif parts.path == "/api/logs":
                self._send_json(console_logs(cfg))
            elif parts.path == "/api/logfile":
                text = console_logfile(cfg, (qs.get("name") or [""])[0])
                body = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parts.path == "/api/backups":
                self._send_json(console_backups(cfg))
            elif parts.path == "/api/settings":
                self._send_json(console_settings(cfg))
            elif parts.path == "/api/deaths":
                self._send_json(console_deaths())
            elif parts.path == "/api/presence":
                self._send_json(console_presence())
            elif parts.path == "/api/kills":
                self._send_json(console_kills())
            elif parts.path == "/api/levels":
                self._send_json(console_levels())
            elif parts.path == "/api/tames":
                self._send_json(console_tames())
            elif parts.path == "/api/download":
                remote = (qs.get("file") or [""])[0]
                if not remote:
                    raise ApiError("No file given.")
                tmp, name = console_download(cfg, remote)
                data = tmp.read_bytes()
                tmp.unlink(missing_ok=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json({"ok": False, "error": "Unknown API route."}, 404)
        except Exception as exc:
            self._api_error(exc)

    def do_POST(self):
        if not self.path.startswith("/api/control"):
            return self._send_json({"ok": False, "error": "Unknown API route."}, 404)
        cfg = _CFG_HOLDER.get("cfg")
        if cfg is None:
            return self._send_json({"ok": False, "error": "Console not ready yet."}, 503)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            self._send_json(console_control(cfg, str(payload.get("action") or "")))
        except Exception as exc:
            self._api_error(exc)


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


class AlreadyRunning(Exception):
    """Raised by start_server when another console instance already
    serves this port (single-instance guard, v2.6.4)."""


def instance_already_running(port: int) -> bool:
    """True when a live ARK Console answers on this port. Probing the
    API itself - not a lock file - means the guard also catches
    instances started by older builds, and a crashed instance never
    leaves a stale lock behind."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/overview", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok") and "gameserver" in data)
    except Exception:
        return False


def start_server(cfg: dict) -> ThreadingHTTPServer:
    port = int(cfg.get("port") or 8770)
    if instance_already_running(port):
        raise AlreadyRunning(
            f"port {port} already serves an ARK Console")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    _CFG_HOLDER["cfg"] = cfg
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


_BACKEND_LOCK = threading.Lock()


def backend_cycle(cfg: dict) -> None:
    """One refresh + log scan, serialized so the GUI's Refresh Now
    button and the timed loop never run at the same time."""
    with _BACKEND_LOCK:
        refresh_once(cfg)
        _scan_logs_quietly(cfg)


def run_backend(cfg: dict, stop: threading.Event, interval: int) -> None:
    """The refresh loop: one forced cycle up front, then a cycle every
    interval until the stop event is set (the terminal path passes an
    event that never fires and relies on Ctrl+C)."""
    with _BACKEND_LOCK:
        refresh_once(cfg, force=True)
        _scan_logs_quietly(cfg)
    while not stop.wait(interval):
        backend_cycle(cfg)


def main() -> int:
    cfg = load_or_prompt_config()
    port = int(cfg.get("port") or 8770)
    interval = max(1, int(cfg.get("interval_min") or 3)) * 60

    log(f"=== TRINITY NEXUS ARK CONSOLE v{VERSION} starting ===")
    log(f"Config: service_id={cfg.get('service_id')} port={port} "
        f"interval={interval // 60} min (API token never logged)")

    if not DATA_PATH.exists():
        write_status(False, "Waiting for the first world save from Nitrado...")

    try:
        start_server(cfg)
    except AlreadyRunning:
        print("The ARK Console is already running - check the system "
              "tray. This second copy is closing.")
        log("Startup stopped: another instance is already running.")
        return 0

    print("=" * 62)
    print("  TRINITY NEXUS ARK CONSOLE is running")
    print(f"  On this PC:      http://localhost:{port}")
    print(f"  For the family:  http://{lan_ip()}:{port}")
    print(f"  Checking Nitrado every {interval // 60} minute(s) for a fresh save.")
    print("  Leave this window open; closing it stops the map.")
    print("=" * 62)

    try:
        run_backend(cfg, threading.Event(), interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    if "--gui" in sys.argv:
        # The GUI module does `import arkmap`; make that resolve to
        # this running module instead of loading a second copy.
        sys.modules.setdefault("arkmap", sys.modules["__main__"])
        import arkgui
        sys.exit(arkgui.run())
    sys.exit(main())
