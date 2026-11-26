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
import struct
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

VERSION = "2.15.0"

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".arkmap"
CONFIG_PATH = CONFIG_DIR / "config.json"
SAVE_DIR = APP_DIR / "save"
WEB_DIR = APP_DIR / "web"
NAMES_PATH = APP_DIR / "creature_names.json"
LOG_PATH = APP_DIR / "arkmap.log"
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


_SECRET_KEY_WORDS = ("token", "pass", "secret")


def _walk_redact(obj, replacement: str):
    """Deep-copy an API payload with credential values replaced:
    any dict key mentioning token/pass/secret loses its value, and
    a token= parameter inside a URL string is masked."""
    if isinstance(obj, dict):
        return {k: (replacement if any(w in str(k).lower()
                                       for w in _SECRET_KEY_WORDS)
                    else _walk_redact(v, replacement))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_redact(v, replacement) for v in obj]
    if isinstance(obj, str) and "token=" in obj:
        return re.sub(r"token=[^&]+", "token=" + replacement, obj)
    return obj


def _redacted_dump(obj, limit: int = 300) -> str:
    """json.dumps safe for the log: download answers carry a
    short-lived fileserver token, which must never reach arkmap.log
    (it syncs off this PC)."""
    try:
        return json.dumps(_walk_redact(obj, "<redacted>"))[:limit]
    except Exception:
        return f"<{type(obj).__name__}>"


def _scrub_secrets(obj):
    """_walk_redact for API responses served to the LAN: the Settings
    and Overview payloads are Nitrado's verbatim answers, and ARK
    server settings normally include the admin password (which is
    also the RCON password) - it is masked, never served."""
    return _walk_redact(obj, "••••••")


def api_get(cfg: dict, path: str, params: dict | None = None) -> dict:
    """GET with a small retry ladder: transient 429/5xx answers and
    network hiccups are retried twice (1 s, then 3 s) before the call
    is given up as an ApiError, so one Nitrado wobble no longer costs
    a whole pull/scan cycle. Other 4xx answers fail at once."""
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, 1.0, 3.0)):
        if delay:
            time.sleep(delay)
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
            last_error = ApiError(
                f"Nitrado API {exc.code} on {path}: {body or exc.reason}")
            if exc.code not in (429, 500, 502, 503, 504):
                raise last_error
        except Exception as exc:
            last_error = ApiError(f"Nitrado API call failed on {path}: {exc}")
    raise last_error or ApiError(f"Nitrado API call failed on {path}")


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
        raise ApiError(f"List failed for '{directory}': {_redacted_dump(out)}")
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


# ------------------------------------------------------------ downloads ----
# The fileserver download route, shared by the Files view
# and the log scanner. (Its world-save callers are gone
# with the live map; the route itself is general.)

def api_download(cfg: dict, remote_path: str, local: Path) -> int:
    out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/file_server/download",
                  {"file": remote_path})
    return _fetch_via_answer(out, remote_path, local)


def _fetch_via_answer(out: dict, remote_path: str, local: Path) -> int:
    if out.get("status") != "success":
        raise ApiError(f"Download request failed for {remote_path}: {_redacted_dump(out)}")
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


def _file_root(cfg: dict) -> str:
    root = _CFG_HOLDER.get("file_root") or ""
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
        "service": _scrub_secrets(svc),
        "gameserver": _scrub_secrets(gs),
        "file_root": _file_root(cfg),
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
    result = {"ok": True, "from_gameserver": _scrub_secrets(gs.get("settings"))}
    for key, path in [("sets", "settings/sets"), ("current", "settings")]:
        try:
            out = api_get(cfg, f"/services/{cfg['service_id']}/gameservers/{path}")
            result[key] = _scrub_secrets(out.get("data"))
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
    # Per-thread staging file: the server is threaded, and two people
    # viewing logs at once must not interleave on one shared tmp path.
    tmp = SAVE_DIR / f"_logview.{threading.get_ident()}.tmp"
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


def _ts_from_match(tm) -> int | None:
    """Epoch seconds from a _TS_RE match, or None when the captured
    fields are not a real date (month 13, hour 99): one malformed
    stamp must yield no timestamp, never an exception that stalls
    the scanner on that file forever."""
    if not tm:
        return None
    try:
        y, mo, d, h, mi, s = (int(tm.group(i)) for i in range(1, 7))
        return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())
    except (ValueError, OverflowError):
        return None
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
SAMPLER_V = 7


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
            ts = _ts_from_match(tm)
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
                # The SID form must be tried BEFORE the plain form:
                # a KillerSID line also matches the plain pattern
                # (with an empty killer), so plain-first silently
                # dropped the SID - the five real KillerSID deaths in
                # this server's history all lost theirs that way.
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
    # Deliberately NOT by_sid/kind: every death is written in two
    # forms (tribe + plain, sometimes SID) and the forms disagree on
    # those fields - the plain form can even classify a player death
    # as a tame. Including them splits verified twin pairs and
    # double-counts (the 61-death corpus verdict rests on this key).
    # The theoretical cost - two same-named victims killed by the
    # same killer within one second merging - is far rarer.
    return (e.get("ts"), (e.get("who") or "").lower(), (e.get("by") or "").lower())


_LOAD_WARNED: set = set()


def _note_load_failure(name: str, exc: Exception) -> None:
    """Tracker loaders fall back to an empty state when their file
    cannot be read - but silently, a torn/locked file made the web
    console report 'no deaths' as if that were true. Log it (once
    per tracker per run) so the fallback is at least visible."""
    if name in _LOAD_WARNED or isinstance(exc, FileNotFoundError):
        return  # a missing file is just a fresh install, not a failure
    _LOAD_WARNED.add(name)
    log(f"Tracker load: {name} could not be read ({type(exc).__name__}: {exc}) "
        f"- treating it as empty for now; the next scan rebuilds it.")


def _load_deaths() -> dict:
    try:
        data = json.loads(DEATHS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            if data.get("parser", 1) < PARSER_VERSION:
                data = _repair_deaths(data)
                _save_deaths(data)
            return data
    except Exception as exc:
        _note_load_failure("deaths.json", exc)
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
    tmp = DEATHS_PATH.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, DEATHS_PATH)


def _absorb_deaths(data: dict, text: str, name: str) -> list[dict]:
    seen = {_death_key(e) for e in data["events"]}
    stored = {_death_key(e): e for e in data["events"]}
    new = []
    for ev in parse_deaths(text, name):
        key = _death_key(ev)
        if key in seen:
            # A twin form of an already-recorded death: not a new
            # death, but it may carry killer detail the stored form
            # lacked (the tribe line names nobody while the server
            # line carries the KillerSID). Adopt any field the
            # stored record is missing so re-reads backfill it.
            old = stored.get(key)
            if old is not None:
                if not old.get("by") and ev.get("by"):
                    old["by"] = ev["by"]
                    old["by_lvl"] = ev.get("by_lvl")
                if not old.get("by_sid") and ev.get("by_sid"):
                    old["by_sid"] = ev["by_sid"]
                if not old.get("species") and ev.get("species"):
                    old["species"] = ev["species"]
            continue
        seen.add(key)
        data["events"].append(ev)
        new.append(ev)
    cands = data.setdefault("candidates", [])
    seen_raw = {c.get("raw") for c in cands}
    for c in parse_death_candidates(text, name):
        if c["raw"] not in seen_raw:
            # Count each distinct sampled line once: re-absorbing the
            # same log (it grows, or a sampler re-read) must not
            # inflate the counter away from the stored samples.
            data["candidate_count"] = data.get("candidate_count", 0) + 1
            seen_raw.add(c["raw"])
            if len(cands) < 12:
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
    return _ts_from_match(_TS_RE.search(line))


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
    except Exception as exc:
        _note_load_failure("kills.json", exc)
    return {"version": 1, "events": [], "scanned": {}}


def _save_kills(data: dict) -> None:
    tmp = KILLS_PATH.with_suffix(f".{threading.get_ident()}.tmp")
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
        if c["raw"] not in seen_raw:
            # Count each distinct sampled line once: re-absorbing the
            # same log (it grows, or a sampler re-read) must not
            # inflate the counter away from the stored samples.
            data["candidate_count"] = data.get("candidate_count", 0) + 1
            seen_raw.add(c["raw"])
            if len(cands) < 12:
                cands.append(c)
    return new


# --------------------------------------------------------------- tames ----
TAMES_PATH = SAVE_DIR / "tames.json"
_FROZE_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+froze\s+(.+?)\s+-\s+Lvl\s+(\d+)\s*\(([^)]*)\)")
_RELEASE_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+(?:released|unfroze)\s+(.+?)\s+-\s+Lvl\s+(\d+)\s*\(([^)]*)\)")
_TAMED_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+[Tt]amed\s+(?:a\s+|an\s+)?(.+?)\s+-\s+Lvl\s+(\d+)(?:\s*\(([^)]*)\))?")
_CLAIMED_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+[Cc]laimed\s+(?:a\s+|an\s+)?(.+?)\s+-\s+Lvl\s+(\d+)(?:\s*\(([^)]*)\))?")
_UNCLAIM_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+unclaimed\s+'?(.+?)\s+-\s+Lvl\s+(\d+)\s*\(([^)]*)\)")
_TAMED_NOLVL_RE = re.compile(r"([A-Za-z0-9_][^:<>]*?)\s+[Tt]amed\s+(?:a\s+|an\s+)?(.+?)\s*\(([^)]*)\)")
_TAME_WORDS = ("tamed", "froze", "unfroze", "released", "cryo", "claimed")


def _tame_actor(raw: str) -> str:
    """The actor capture can start inside the log line's bracketed
    timestamp prefix ('045][123] KayDence') when the line carries no
    colon before the name; keep only the segment after the last ']'."""
    return (raw or "").rsplit("]", 1)[-1].strip()


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
                         (_TAMED_RE, "claim"), (_CLAIMED_RE, "claim"),
                         (_UNCLAIM_RE, "unclaim")):
            m = rx.search(clean)
            if not m:
                continue
            name = m.group(2).strip()
            species = (m.group(4) or "").strip()
            if kind == "claim" and not species:
                species = name
            out.append({"ts": ts, "kind": kind,
                        "actor": _tame_actor(m.group(1)), "name": name,
                        "lvl": int(m.group(3)) if m.group(3) else None,
                        "species": species,
                        "source": source, "raw": line.strip()[:300]})
            break
        else:
            # Level-less tame form ('Tribe X Tamed a Raft (Raft)!'):
            # vehicles carry no level. Groups: actor, name, species.
            m = _TAMED_NOLVL_RE.search(clean)
            if m:
                out.append({"ts": ts, "kind": "claim",
                            "actor": _tame_actor(m.group(1)),
                            "name": m.group(2).strip(), "lvl": None,
                            "species": m.group(3).strip(),
                            "source": source, "raw": line.strip()[:300]})
    return out


def parse_tame_candidates(text: str, source: str) -> list[dict]:
    """Tame-ish lines (claim/cryo wording) that match no known tame
    format and are not death lines. Collected in save/tames.json so
    the real claim/release wording can be tuned from the server's own
    logs instead of guessed."""
    out = []
    for line in text.splitlines():
        low = line.lower()
        if "commandline:" in low:
            continue  # the boot config line mentions cryo settings; never an event
        if "demolished" in low or "frozen by id" in low:
            continue  # structure demolitions (Cryofridge) and ID-only freeze
            # notices are known formats that name no tame record
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
            # One-time cleanup: boot Commandline samples were collected
            # as tame candidates before they were excluded at the source.
            cands = data.get("candidates")
            if isinstance(cands, list):
                junk = [c for c in cands
                        if "commandline:" in (c.get("raw") or "").lower()]
                if junk:
                    data["candidates"] = [c for c in cands if c not in junk]
                    data["candidate_count"] = max(
                        0, data.get("candidate_count", 0) - len(junk))
            # One-time cleanup: re-scans before v2.10.0 appended tame
            # events unconditionally, so stored histories can hold
            # exact duplicates; drop them, keeping first occurrence.
            for r in data["records"].values():
                evs = r.get("events")
                if isinstance(evs, list) and len(evs) > 1:
                    seen_ev: list = []
                    for ev in evs:
                        if ev not in seen_ev:
                            seen_ev.append(ev)
                    r["events"] = seen_ev
            return data
    except Exception as exc:
        _note_load_failure("tames.json", exc)
    return {"version": 1, "records": {}, "scanned": {}}


def _save_tames(data: dict) -> None:
    tmp = TAMES_PATH.with_suffix(f".{threading.get_ident()}.tmp")
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
                    "actor": e.get("actor") or "",
                    "raw": e["raw"]})
    for e in death_events:
        if e.get("kind") == "tame":
            evs.append({"ts": e["ts"], "kind": "death", "name": e["who"],
                        "species": e.get("species") or "",
                        "lvl": e.get("who_lvl"),
                        "by": e.get("by") or "", "by_sid": e.get("by_sid"),
                        "raw": e["raw"]})
    evs.sort(key=lambda x: (x["ts"] or 0))
    touched = []
    for e in evs:
        key = _tame_key(e["name"], e["species"])
        r = records.get(key)
        if r is None:
            r = {"name": e["name"],
                 "species": e["species"] or "Unknown type",
                 "level": None, "first_seen": e["ts"], "claimed": None,
                 "frozen": False, "unclaimed": False, "unclaimed_by": None,
                 "death": None,
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
            if r.get("unclaimed") and (r["last_ts"] is None
                                       or ts is None
                                       or ts >= r["last_ts"]):
                r["unclaimed"] = False
                r["unclaimed_by"] = None
        elif e["kind"] == "unclaim":
            if r["death"] is None and (r["last_ts"] is None
                                       or ts is None
                                       or ts >= r["last_ts"]):
                r["unclaimed"] = True
                r["unclaimed_by"] = e.get("actor") or None
        elif e["kind"] == "death":
            if r["death"] is None or (ts is not None and r["death"]["ts"]
                                      is not None
                                      and ts < r["death"]["ts"]):
                r["death"] = {"ts": ts, "by": e.get("by", ""),
                              "by_sid": e.get("by_sid")}
            elif (ts is not None and r["death"].get("ts") == ts
                    and not r["death"].get("by_sid") and e.get("by_sid")):
                # Same death re-read (sampler pass): backfill the
                # KillerSID the original absorb could not capture.
                r["death"]["by_sid"] = e["by_sid"]
        elif e["kind"] in ("froze", "release"):
            if r["death"] is None and (r["last_ts"] is None
                                       or ts is None
                                       or ts >= r["last_ts"]):
                r["frozen"] = e["kind"] == "froze"
                r["unclaimed"] = False
                r["unclaimed_by"] = None
        if ts is not None and (r["last_ts"] is None or ts >= r["last_ts"]):
            r["last_ts"] = ts
            r["last_kind"] = e["kind"]
        ev_rec = {"ts": ts, "kind": e["kind"]}
        if e.get("actor"):
            ev_rec["actor"] = e["actor"]
        # Re-scans (the growing live log, SAMPLER_V re-reads) absorb
        # the same lines again; deaths/kills dedupe theirs, and the
        # history list must too, or duplicates evict genuine events.
        if ev_rec not in r["events"]:
            r["events"].append(ev_rec)
        r["events"] = r["events"][-40:]
    cands = data.setdefault("candidates", [])
    seen_raw = {c.get("raw") for c in cands}
    for c in parse_tame_candidates(text, name):
        if c["raw"] not in seen_raw:
            # Count each distinct sampled line once: re-absorbing the
            # same log (it grows, or a sampler re-read) must not
            # inflate the counter away from the stored samples.
            data["candidate_count"] = data.get("candidate_count", 0) + 1
            seen_raw.add(c["raw"])
            if len(cands) < 12:
                cands.append(c)
    return touched


def console_tames() -> dict:
    data = _load_tames()
    recs = []
    for r in data["records"].values():
        f = dict(r)
        f["status"] = "dead" if r.get("death") else (
            "unclaimed" if r.get("unclaimed") else (
                "frozen" if r.get("frozen") else "living"))
        recs.append(f)
    recs.sort(key=lambda r: (r.get("name") or "").lower())
    dead = sum(1 for r in recs if r["status"] == "dead")
    frozen = sum(1 for r in recs if r["status"] == "frozen")
    unclaimed = sum(1 for r in recs if r["status"] == "unclaimed")
    return {"ok": True, "tames": recs,
            "counts": {"total": len(recs),
                       # One meaning of "living" everywhere: the
                       # record status - out, not frozen, not dead,
                       # not unclaimed. (The old formula included
                       # frozen, so the four counts summed past the
                       # total and disagreed with the Living filter.)
                       "living": len(recs) - dead - frozen - unclaimed,
                       "frozen": frozen, "dead": dead,
                       "unclaimed": unclaimed},
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
    except Exception as exc:
        _note_load_failure("levels.json", exc)
    return {"version": 1, "observations": [], "scanned": {}}


def _save_levels(data: dict) -> None:
    tmp = LEVELS_PATH.with_suffix(f".{threading.get_ident()}.tmp")
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
                    "sightings": len(obs), "show_tab": _key_shown(char)})
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
        ts = _ts_from_match(tm)
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
    except Exception as exc:
        _note_load_failure("presence.json", exc)
    return {"version": 1, "events": [], "scanned": {}}


def _save_presence(data: dict) -> None:
    tmp = PRESENCE_PATH.with_suffix(f".{threading.get_ident()}.tmp")
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
                # The per-file scanned map belongs to the old
                # generation too, so it starts empty with the new
                # one: leaving the old map in place marks every file
                # as already sampler-scanned, the re-read list comes
                # up empty, and the new generation silently re-reads
                # nothing at all (the v7 KillerSID backfill did
                # exactly that on Clint's PC - sampler_v sat at 6
                # while every pass found no work). Cleared here, in
                # the same once-per-generation block as the samples,
                # so an interrupted pass resumes from the stamps it
                # managed to save instead of starting over.
                ddata["sampler_scanned"] = {}
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
        try:
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
                # Always a fresh full parse, not the deaths absorb's
                # new-only list: on a sampler re-read every death is
                # already recorded, so the new-only list is empty and
                # tame death records would never pick up backfilled
                # killer detail (the KillerSID). _absorb_tames is
                # built to re-absorb the same lines safely.
                _absorb_tames(tames, text, name, parse_deaths(text, name))
        except Exception as exc:
            # One poisonous file must not stall the scanner: the file
            # is still stamped scanned below (its kept copy in
            # save/logs stays available for research), the failure is
            # logged loudly, and the pass moves on to the next file.
            log(f"Logs: absorb failed for {name} ({type(exc).__name__}: {exc}) "
                f"- skipping its remaining trackers this pass.")
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
        q["show_tab"] = _key_shown(p["name"])
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


# -------------------------------------------------------------- names ----
PLAYER_SHOW_PATH = SAVE_DIR / "player_show.json"


def _write_json_retry(path: Path, obj) -> None:
    """Write JSON via tmp + replace, retrying through the brief
    file locks a sync client puts on brand-new files (the Third Eye
    I-001 lesson); falls back to writing in place rather than
    losing the save."""
    text = json.dumps(obj, indent=1)
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    last = None
    for delay in (0.0, 0.15, 0.4, 1.0):
        try:
            if delay:
                time.sleep(delay)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return
        except Exception as exc:
            last = exc
    try:
        path.write_text(text, encoding="utf-8")
    except Exception:
        raise ApiError(f"Could not save {path.name} - the file stayed "
                       f"locked through every retry (last error: {last}).")


def _player_show() -> dict:
    """Raw identity key -> 'gets its own player tab' flag, from
    save/player_show.json. Absent key = default (see _key_shown)."""
    try:
        raw = json.loads(PLAYER_SHOW_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): bool(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}


def _identities() -> dict:
    """Every username the trackers have ever detected, keyed by
    lower-cased raw name: {key (original casing), kinds}. Presence
    lines carry Xbox gamertags; death and level lines carry
    character names; the nickname file may add aliases of its own."""
    found: dict = {}

    def note(key, kind):
        key = (key or "").strip()
        if not key:
            return
        slot = found.setdefault(key.lower(), {"key": key, "kinds": set()})
        slot["kinds"].add(kind)

    try:
        for e in _load_presence()["events"]:
            note(e.get("who"), "gamertag")
    except Exception:
        pass
    try:
        for e in _load_deaths()["events"]:
            if e.get("kind") == "player":
                note(e.get("who"), "character")
    except Exception:
        pass
    try:
        for o in _load_levels()["observations"]:
            note(o.get("who"), "character")
    except Exception:
        pass
    for k in _player_names():
        note(k, "alias")
    return found


def console_names() -> dict:
    """The Names tab data: detected identities grouped by person
    (identities that resolve to the same nickname form one group),
    each group with its current nickname and its 'own tab' flag.
    Default flag: a group with a nickname shows; an unnamed stray
    stays tab-less until someone ticks it."""
    names = _player_names()
    flags = _player_show()
    groups: dict = {}
    for low, slot in sorted(_identities().items()):
        display = _resolve_name(slot["key"], names)
        g = groups.setdefault(display.lower(),
                              {"display": display, "nickname": "", "keys": []})
        g["keys"].append({"key": slot["key"], "kinds": sorted(slot["kinds"])})
        nick = ""
        for k, v in names.items():
            if k.lower() == low:
                nick = v
                break
        if nick:
            g["nickname"] = nick
    out = []
    for g in groups.values():
        show = None
        for kk in g["keys"]:
            for fk, fv in flags.items():
                if fk.lower() == kk["key"].lower():
                    show = bool(fv)
                    break
            if show is not None:
                break
        if show is None:
            show = bool(g["nickname"])
        g["show"] = show
        out.append(g)
    out.sort(key=lambda g: (not g["show"], g["display"].lower()))
    return {"ok": True, "groups": out}


# ------------------------------------------------------------ offenders --
OFFENDERS_PATH = SAVE_DIR / "offenders.json"
_OFFENDERS_LOCK = threading.Lock()


def _load_offenders() -> dict:
    """Identifications the family has recorded for unnamed killers:
    {KillerSID-as-string: {species, killed_by, killed_date, note}}.
    Lives in save/offenders.json - the console's own per-server data,
    never shipped and never touched by the updater."""
    try:
        raw = json.loads(OFFENDERS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("identified"), dict):
            return raw
    except Exception:
        pass
    return {"version": 1, "identified": {}}


def console_offenders() -> dict:
    """Every unnamed creature that has killed on this server, grouped
    by its KillerSID: how many kills, who the victims were, and
    whether the family has identified it and hung it as a trophy.
    Sorted worst-first; 'repeat' marks the ones with 2+ kills."""
    data = _load_deaths()
    names = _player_names()
    identified = _load_offenders().get("identified", {})
    by_sid: dict = {}
    for e in data["events"]:
        if not e.get("by_sid"):
            continue
        sid = int(e["by_sid"])
        slot = by_sid.setdefault(sid, {"sid": sid, "kills": 0,
                                       "victims": []})
        slot["kills"] += 1
        victim = {"who": e.get("who", ""), "kind": e.get("kind"),
                  "ts": e.get("ts"), "who_lvl": e.get("who_lvl")}
        if e.get("kind") == "player":
            victim["player"] = _resolve_name(e.get("who", ""), names)
        if e.get("species"):
            victim["species"] = e["species"]
        slot["victims"].append(victim)
    out = []
    for sid, slot in by_sid.items():
        slot["victims"].sort(key=lambda v: v.get("ts") or 0)
        slot["first_ts"] = slot["victims"][0]["ts"] if slot["victims"] else None
        slot["last_ts"] = slot["victims"][-1]["ts"] if slot["victims"] else None
        slot["repeat"] = slot["kills"] >= 2
        slot["identified"] = identified.get(str(sid)) or None
        out.append(slot)
    out.sort(key=lambda s: (-s["kills"], s["first_ts"] or 0))
    return {"ok": True, "offenders": out}


def save_offender(sid, species, killed_by, killed_date, note,
                  level="") -> dict:
    """Record the family's identification of one unnamed killer -
    what it turned out to be, who killed it, and when - which hangs
    it as a trophy. A blank species clears the identification (the
    creature goes back to unidentified). Returns the fresh
    console_offenders()."""
    try:
        sid = int(sid)
    except Exception:
        raise ApiError("No creature ID given.")
    species = (species or "").strip()
    with _OFFENDERS_LOCK:
        data = _load_offenders()
        if not species:
            data["identified"].pop(str(sid), None)
        else:
            try:
                lvl = int(str(level).strip())
                lvl = lvl if 1 <= lvl <= 2000 else ""
            except Exception:
                lvl = ""
            data["identified"][str(sid)] = {
                "species": species[:80],
                "level": lvl,
                "killed_by": (killed_by or "").strip()[:60],
                "killed_date": (killed_date or "").strip()[:10],
                "note": (note or "").strip()[:200],
            }
        _write_json_retry(OFFENDERS_PATH, data)
    return console_offenders()


_NAMES_LOCK = threading.Lock()


def save_name_group(keys: list, nickname: str, show: bool) -> dict:
    """Set one group's nickname (blank clears it) and tab flag for
    every raw key in the group. Nicknames live in the same
    player_names.json the console has always used; flags in
    player_show.json. Returns the fresh console_names().
    The whole read-modify-write runs under a lock: the web console
    and the desktop window can save at the same moment, and unlocked
    they would silently lose one of the two updates."""
    with _NAMES_LOCK:
        return _save_name_group_locked(keys, nickname, show)


def _save_name_group_locked(keys: list, nickname: str, show: bool) -> dict:
    keys = [str(k) for k in (keys or []) if str(k).strip()]
    if not keys:
        raise ApiError("No identity keys given.")
    nickname = (nickname or "").strip()
    names = _player_names()
    for k in keys:
        hit = next((ek for ek in names if ek.lower() == k.lower()), None)
        if nickname:
            names[hit or k] = nickname
        elif hit:
            names.pop(hit, None)
    _write_json_retry(PLAYER_NAMES_PATH, names)
    flags = _player_show()
    for k in keys:
        hit = next((ek for ek in flags if ek.lower() == k.lower()), None)
        flags[hit or k] = bool(show)
    _write_json_retry(PLAYER_SHOW_PATH, flags)
    return console_names()


def _key_shown(key: str) -> bool:
    """Whether the person behind one raw identity key gets their
    own player tab: explicit flag if one was saved, else the
    default - nicknamed players show, unnamed strays don't."""
    flags = _player_show()
    low = (key or "").lower()
    for k, v in flags.items():
        if k.lower() == low:
            return bool(v)
    return _resolve_name(key or "", _player_names()) != (key or "")


def shown_display_names() -> set:
    """Display names whose group gets its own player tab (the GUI
    filters its per-player tabs by this set)."""
    return {g["display"] for g in console_names()["groups"] if g["show"]}


# --------------------------------------------------------------- rcon ----
CREATURES_PATH = APP_DIR / "creatures.json"
ITEMS_PATH = APP_DIR / "items.json"
_RCON_DATA_CACHE: dict = {}

RCON_DEFAULT_PORT = 15347
# The RCON password lives ONLY here, in this process's memory,
# from the moment the webpage's password prompt unlocks until the
# app restarts or Lock is pressed. It is never written to disk,
# never put in the config, and never logged.
_RCON_STATE: dict = {"password": None}


class RconError(Exception):
    """An RCON problem the webpage should show as-is."""


def rcon_data() -> dict:
    """The creature and item tables the RCON menu's pickers use,
    from the data files beside this module (built from the ARK
    wiki's Creature IDs / Item IDs research, 2026-11-25)."""
    out = {"ok": True, "creatures": [], "items": []}
    for key, path in (("creatures", CREATURES_PATH), ("items", ITEMS_PATH)):
        try:
            sig = path.stat().st_mtime
            if _RCON_DATA_CACHE.get(key, (None,))[0] != sig:
                raw = json.loads(path.read_text(encoding="utf-8"))
                _RCON_DATA_CACHE[key] = (sig, raw.get(key) or [])
            out[key] = _RCON_DATA_CACHE[key][1]
        except Exception:
            out[key] = []
    return out


def _rcon_host(cfg: dict) -> str:
    if cfg.get("rcon_host"):
        return str(cfg["rcon_host"]).strip()
    gs = _cached("gameserver", 20, lambda: _gs(cfg))
    q = gs.get("query") or {}
    ci = str(q.get("connect_ip") or "").strip()
    host = ci.rsplit(":", 1)[0].strip() if ":" in ci else ci
    if host:
        return host
    raise RconError("The server's address isn't known yet - try again "
                    "once the Overview has loaded.")


def _rcon_port(cfg: dict) -> int:
    try:
        return int(cfg.get("rcon_port") or RCON_DEFAULT_PORT)
    except Exception:
        return RCON_DEFAULT_PORT


def _rcon_roundtrip(host: str, port: int, password: str,
                    command: str, timeout: float = 12.0) -> str:
    """One Source RCON session: authenticate, send ONE command,
    collect its response. ARK answers many successful commands
    with an empty response - that is success, not failure - and
    often never answers the end-marker packet, so collection ends
    on the marker OR on a short read timeout after data arrived."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except Exception as exc:
        raise RconError(
            f"Could not connect to RCON at {host}:{port} ({exc}). "
            "If the port is unreachable, the host may not expose "
            "RCON on this server product.")
    try:
        sock.settimeout(timeout)
        buf = b""

        def send(rid: int, ptype: int, payload: bytes) -> None:
            body = struct.pack("<ii", rid, ptype) + payload + b"\x00\x00"
            sock.sendall(struct.pack("<i", len(body)) + body)

        def read_packet():
            nonlocal buf
            while len(buf) < 4:
                chunk = sock.recv(65536)
                if not chunk:
                    raise RconError("The server closed the RCON connection.")
                buf += chunk
            (length,) = struct.unpack("<i", buf[:4])
            if length < 10 or length > 1024 * 1024:
                raise RconError("The server sent a malformed RCON packet.")
            while len(buf) < 4 + length:
                chunk = sock.recv(65536)
                if not chunk:
                    raise RconError("The server closed the RCON connection.")
                buf += chunk
            body = buf[4:4 + length]
            buf = buf[4 + length:]
            rid, ptype = struct.unpack("<ii", body[:8])
            return rid, ptype, body[8:-2]

        send(1, 3, password.encode("utf-8"))
        authed = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            rid, ptype, _payload = read_packet()
            if rid == -1:
                raise RconError("The server rejected that RCON password.")
            if ptype == 2 and rid == 1:
                authed = True
                break
        if not authed:
            raise RconError("The server never answered the RCON login.")
        send(2, 2, command.encode("utf-8"))
        send(3, 0, b"")
        out: list = []
        sock.settimeout(2.5)
        try:
            while True:
                rid, _ptype, payload = read_packet()
                if rid == 3:
                    break
                if rid == 2:
                    out.append(payload.decode("utf-8", "replace"))
        except (socket.timeout, TimeoutError):
            pass
        return "".join(out)
    except RconError:
        raise
    except Exception as exc:
        raise RconError(f"RCON failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def rcon_exec(cfg: dict, command: str) -> str:
    pw = _RCON_STATE.get("password")
    if not pw:
        raise RconError("RCON is locked - enter the RCON password first.")
    return _rcon_roundtrip(_rcon_host(cfg), _rcon_port(cfg), pw, command)


def parse_list_players(text: str) -> list:
    """Parse ListPlayers output defensively: the wiki documents no
    exact layout, so accept 'N. Name, ID', 'Name, ID' and
    'Name ID' lines and keep the raw line for display."""
    rows = []
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        line = re.sub(r"^\d+[\.\)]\s*", "", raw)
        name, ident = line, ""
        if "," in line:
            name, ident = (p.strip() for p in line.rsplit(",", 1))
        else:
            parts = line.rsplit(None, 1)
            if len(parts) == 2 and re.search(r"\d", parts[1]):
                name, ident = parts[0].strip(), parts[1].strip()
        rows.append({"name": name, "id": ident, "raw": raw})
    return rows


def _rcon_block_reason(cfg: dict):
    """Why RCON cannot work on this service, or None when it can.
    Nitrado exposes RCON only on PC servers - on console (Xbox)
    services the game process listens internally but the port is
    never routed, and the service's own feature flags say so. The
    menu stays in the app for PC services; here it must say plainly
    that it is disabled rather than fail at the password prompt."""
    try:
        svc = _cached("service", 60, lambda: _svc(cfg))
        gs = _cached("gameserver", 20, lambda: _gs(cfg))

        def find(d, key):
            if isinstance(d, dict):
                if key in d:
                    return d[key]
                for v in d.values():
                    r = find(v, key)
                    if r is not None:
                        return r
            elif isinstance(d, list):
                for v in d:
                    r = find(v, key)
                    if r is not None:
                        return r
            return None

        hr = find(svc, "has_rcon")
        if hr is None:
            hr = find(gs, "has_rcon")
        if hr is False:
            return ("Nitrado flags RCON off for this service - RCON is "
                    "only available on PC servers, and this is a console "
                    "(Xbox) service.")
        blob = (json.dumps(svc) + json.dumps(gs)).lower()
        if "xbox" in blob:
            return ("This is an Xbox / console service, and Nitrado only "
                    "exposes RCON on PC servers.")
    except Exception:
        pass
    return None


def rcon_status(cfg: dict) -> dict:
    host = None
    try:
        host = _rcon_host(cfg)
    except Exception:
        host = None
    return {"ok": True, "unlocked": bool(_RCON_STATE.get("password")),
            "host": host, "port": _rcon_port(cfg),
            "blocked": _rcon_block_reason(cfg)}


def rcon_unlock(cfg: dict, password: str) -> dict:
    """Test the password with a real ListPlayers and, only on
    success, keep it - in memory, for this process only."""
    reason = _rcon_block_reason(cfg)
    if reason:
        raise RconError("RCON is disabled on this server. " + reason)
    password = password or ""
    if not password:
        raise RconError("Enter the RCON password.")
    out = _rcon_roundtrip(_rcon_host(cfg), _rcon_port(cfg),
                          password, "ListPlayers")
    _RCON_STATE["password"] = password
    return {"ok": True, "players": parse_list_players(out), "raw": out}


def rcon_lock() -> dict:
    _RCON_STATE["password"] = None
    return {"ok": True}


# --- RCON action catalogue -------------------------------------------
# Only commands the wiki documents as working in the remote console
# (no Self / crosshair targets: there is no player character behind
# an RCON session, so spawning, teleporting, taming and the like
# are impossible here by design - see the 2026-11-25 research).

def _p_str(p: dict, key: str, maxlen: int = 300, required: bool = True) -> str:
    v = str(p.get(key) or "").strip()
    if required and not v:
        raise RconError(f"Missing value: {key}.")
    if len(v) > maxlen or re.search(r"[\r\n]", v):
        raise RconError(f"Invalid value: {key}.")
    return v


def _p_id(p: dict, key: str = "account_id") -> str:
    v = _p_str(p, key, maxlen=80)
    if '"' in v:
        raise RconError("Invalid player identifier.")
    return v


def _p_qname(p: dict, key: str) -> str:
    """A player/tribe name as a quoted RCON argument: character names
    with spaces are normal in ARK, and unquoted the server reads only
    the first word as the name and the rest as something else."""
    v = _p_str(p, key, maxlen=80)
    if '"' in v:
        raise RconError(f"Invalid value: {key}.")
    return f'"{v}"'


def _p_pid(p: dict, key: str = "player_id") -> str:
    v = _p_str(p, key, maxlen=12)
    if not re.fullmatch(r"\d{1,12}", v):
        raise RconError("That action needs the player's numeric "
                        "PlayerID (the in-game UE4 ID).")
    return v


def _p_int(p: dict, key: str, lo: int, hi: int) -> int:
    try:
        v = int(p.get(key))
    except Exception:
        raise RconError(f"Invalid number: {key}.")
    if v < lo or v > hi:
        raise RconError(f"Out of range: {key} (allowed {lo}..{hi}).")
    return v


def _p_float(p: dict, key: str, lo: float, hi: float) -> float:
    try:
        v = float(p.get(key))
    except Exception:
        raise RconError(f"Invalid number: {key}.")
    if v < lo or v > hi:
        raise RconError(f"Out of range: {key} (allowed {lo}..{hi}).")
    return v


def _fmt_num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


def _p_bool(p: dict, key: str) -> str:
    return "1" if p.get(key) else "0"


def rcon_action(cfg: dict, p: dict) -> dict:
    """Build ONE server command from a fixed menu action and run
    it. The webpage sends an action name plus fields; the command
    string is assembled here, from validated pieces, and returned
    so the page can show exactly what was sent."""
    action = str(p.get("action") or "")
    commands: list = []

    if action == "save_world":
        commands = ["SaveWorld"]
    elif action == "broadcast":
        commands = [f"Broadcast {_p_str(p, 'message', 400)}"]
    elif action == "server_chat":
        commands = [f"ServerChat {_p_str(p, 'message', 400)}"]
    elif action == "motd":
        commands = [f"SetMessageOfTheDay {_p_str(p, 'message', 400)}"]
    elif action == "set_time":
        v = _p_str(p, "time", 8)
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", v)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59 \
                or int(m.group(3) or 0) > 59:
            raise RconError("Time must be HH:MM or HH:MM:SS (24-hour).")
        commands = [f"SetTimeOfDay {int(m.group(1)):02d}:{m.group(2)}:"
                    f"{m.group(3) or '00'}"]
    elif action == "set_day":
        commands = [f"SetDay {_p_int(p, 'day', 0, 2147483647)}"]
    elif action == "slomo":
        commands = [f"Slomo {_fmt_num(_p_float(p, 'factor', 0, 16))}"]
    elif action == "players_only":
        commands = ["PlayersOnly"]
    elif action == "get_chat":
        commands = ["GetChat"]
    elif action == "list_players":
        commands = ["ListPlayers"]
    elif action == "destroy_wild_dinos":
        commands = ["DestroyWildDinos"]
    elif action == "destroy_all_enemies":
        commands = ["DestroyAllEnemies"]
    elif action == "destroy_structures":
        commands = ["DestroyStructures"]
    elif action == "restart_level":
        commands = ["DoRestartLevel"]
    elif action == "shutdown":
        commands = ["SaveWorld", "DoExit"]
    elif action == "weather_rainbow":
        commands = ["CE SpawnRainbow"]
    elif action == "weather_aurora":
        commands = ["CE TestNorthernLights"]
    # ---- player, account-ID family (identifier ListPlayers prints)
    elif action == "kick":
        commands = [f"KickPlayer {_p_id(p)}"]
    elif action == "ban":
        commands = [f"BanPlayer {_p_id(p)}"]
    elif action == "unban":
        commands = [f"UnbanPlayer {_p_id(p)}"]
    elif action == "chat_to_id":
        commands = [f'ServerChatTo "{_p_id(p)}" {_p_str(p, "message", 400)}']
    elif action == "chat_to_name":
        commands = [f"ServerChatToPlayer {_p_qname(p, 'name')} "
                    f"{_p_str(p, 'message', 400)}"]
    elif action == "rename_player":
        commands = [f"RenamePlayer {_p_qname(p, 'name')} "
                    f"{_p_qname(p, 'new_name')}"]
    # ---- player, PlayerID family (in-game UE4 ID)
    elif action == "give_item":
        pid = _p_pid(p)
        qty = _p_int(p, "quantity", 1, 10000)
        quality = _fmt_num(_p_float(p, "quality", 0, 100))
        force = _p_bool(p, "as_blueprint")
        mode = str(p.get("mode") or "")
        if mode == "path":
            path = _p_str(p, "item_path", 300)
            if not (path.startswith("Blueprint'") and path.endswith("'")):
                raise RconError("Invalid blueprint path.")
            commands = [f"GiveItemToPlayer {pid} {path} {qty} {quality} {force}"]
        elif mode == "num":
            num = _p_int(p, "item_id", 1, 100000)
            commands = [f"GiveItemNumToPlayer {pid} {num} {qty} {quality} {force}"]
        else:
            raise RconError("Pick an item the menu can give (it needs a "
                            "verified blueprint path or item number).")
    elif action == "give_xp":
        pid = _p_pid(p)
        amt = _fmt_num(_p_float(p, "amount", 0, 100000000))
        prevent = "0" if p.get("share_tribe", True) else "1"
        commands = [f"GiveExpToPlayer {pid} {amt} 0 {prevent}"]
    elif action == "chibi_xp":
        commands = [f"AddChibiExpToPlayer {_p_pid(p)} "
                    f"{_fmt_num(_p_float(p, 'amount', 0, 100000000))}"]
    elif action == "kill_player":
        commands = [f"KillPlayer {_p_pid(p)}"]
    elif action == "clear_inventory":
        pid = _p_pid(p)
        commands = [f"ClearPlayerInventory {pid} {_p_bool(p, 'inv')} "
                    f"{_p_bool(p, 'slots')} {_p_bool(p, 'equipped')}"]
    elif action == "creative_mode":
        commands = [f"GiveCreativeModeToPlayer {_p_pid(p)}"]
    elif action == "tekengrams":
        part = _p_str(p, "partial", 60, required=False) or "tek"
        commands = [f"GiveTekengramsTo {_p_pid(p)} {part}"]
    elif action == "max_ascend":
        commands = [f"MaxAscend {_p_pid(p)}"]
    elif action == "defeat_all_bosses":
        commands = [f"DefeatAllBosses {_p_pid(p)}"]
    elif action == "rename_player_id":
        commands = [f"RenamePlayerId {_p_pid(p)} {_p_str(p, 'new_name', 80)}"]
    # ---- tribe (TribeID; this server's tribe defaults client-side)
    elif action == "tribe_message":
        commands = [f"TribeMessage {_p_pid(p, 'tribe_id')} "
                    f"{_p_str(p, 'message', 400)}"]
    elif action == "tribe_players":
        commands = [f"GetTribeIdPlayerList {_p_pid(p, 'tribe_id')}"]
    elif action == "tribe_audit_structures":
        commands = [f"TribeStructureAudit {_p_pid(p, 'tribe_id')}"]
    elif action == "tribe_audit_dinos":
        commands = [f"TribeDinoAudit {_p_pid(p, 'tribe_id')}"]
    elif action == "rename_tribe_id":
        commands = [f"RenameTribeID {_p_pid(p, 'tribe_id')} "
                    f"{_p_str(p, 'new_name', 80)}"]
    elif action == "destroy_tribe_dinos":
        commands = [f"DestroyTribeIdDinos {_p_pid(p, 'tribe_id')}"]
    elif action == "destroy_tribe_structures":
        commands = [f"DestroyTribeIdStructures {_p_pid(p, 'tribe_id')}"]
    elif action == "destroy_tribe_players":
        commands = [f"DestroyTribeIdPlayers {_p_pid(p, 'tribe_id')}"]
    elif action == "destroy_tribe":
        commands = [f"DestroyTribeId {_p_pid(p, 'tribe_id')}"]
    # ---- world, by creature class
    elif action == "destroy_all_class":
        cls = _p_str(p, "class", 120)
        if not re.fullmatch(r"[A-Za-z0-9_]+", cls):
            raise RconError("Invalid creature class.")
        commands = [f"DestroyAll {cls} 1"]
    elif action == "destroy_wild_class":
        cls = _p_str(p, "class", 120)
        if not re.fullmatch(r"[A-Za-z0-9_]+", cls):
            raise RconError("Invalid creature class.")
        commands = [f'DestroyWildDinoClasses "{cls}" 1']
    else:
        raise RconError(f"Unknown RCON action: {action or '(none)'}")

    outputs = []
    for cmd in commands:
        outputs.append(rcon_exec(cfg, cmd))
    return {"ok": True, "commands": commands,
            "output": "\n".join(o for o in outputs if o)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        # no-store on the pages too, not just the data: without it the
        # browser heuristically caches index.html and page updates
        # look like they never shipped (bit us on the v2.6.0 kills
        # removal - Clint hard-refreshed and still saw the old page).
        if (self.path.startswith("/api/")
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
            elif parts.path == "/api/names":
                self._send_json(console_names())
            elif parts.path == "/api/offenders":
                self._send_json(console_offenders())
            elif parts.path == "/api/rcon/status":
                self._send_json(rcon_status(cfg))
            elif parts.path == "/api/rcon/data":
                self._send_json(rcon_data())
            elif parts.path == "/api/rcon/players":
                try:
                    raw = rcon_exec(cfg, "ListPlayers")
                    self._send_json({"ok": True,
                                     "players": parse_list_players(raw),
                                     "raw": raw})
                except RconError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, 400)
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
        parts = urllib.parse.urlsplit(self.path)
        if not parts.path.startswith("/api/"):
            return self._send_json({"ok": False, "error": "Unknown API route."}, 404)
        cfg = _CFG_HOLDER.get("cfg")
        if cfg is None:
            return self._send_json({"ok": False, "error": "Console not ready yet."}, 503)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if parts.path == "/api/control":
                self._send_json(console_control(cfg, str(payload.get("action") or "")))
            elif parts.path == "/api/rcon/unlock":
                self._send_json(rcon_unlock(cfg, str(payload.get("password") or "")))
            elif parts.path == "/api/rcon/lock":
                self._send_json(rcon_lock())
            elif parts.path == "/api/rcon/exec":
                self._send_json(rcon_action(cfg, payload))
            elif parts.path == "/api/names":
                self._send_json(save_name_group(payload.get("keys") or [],
                                                str(payload.get("nickname") or ""),
                                                bool(payload.get("show"))))
            elif parts.path == "/api/offenders":
                self._send_json(save_offender(
                    payload.get("sid"),
                    str(payload.get("species") or ""),
                    str(payload.get("killed_by") or ""),
                    str(payload.get("killed_date") or ""),
                    str(payload.get("note") or ""),
                    payload.get("level")))
            else:
                self._send_json({"ok": False, "error": "Unknown API route."}, 404)
        except RconError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
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
    """One log scan, serialized so the GUI's Refresh Now button and
    the timed loop never run at the same time. (The world-save pull
    that used to run here fed only the old live map; the Resource
    Map is a static atlas and needs nothing from the save, so the
    pull and its polling toggle were removed in v2.14.0. The scan -
    deaths, tames, players - is untouched.)"""
    with _BACKEND_LOCK:
        _scan_logs_quietly(cfg)


def run_backend(cfg: dict, stop: threading.Event, interval: int) -> None:
    """The refresh loop: one scan up front, then a scan every
    interval until the stop event is set (the terminal path passes
    an event that never fires and relies on Ctrl+C)."""
    with _BACKEND_LOCK:
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
