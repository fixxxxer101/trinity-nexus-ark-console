"""Trinity Nexus ARK Console - desktop GUI (v2.7.0).

A small tek-styled window that runs the same backend as the terminal
mode (web console server + Nitrado refresh loop). Tabs across the top:
"All Players" (the squad view: big server status, players in world,
connect address, world-save state, and the squad aggregates - average
level, average playtime, total playtime, total combined level) and one
tab per player with their own charts (level progression from every
sighting the logs caught, deaths by creature) and
their stats, plus a Terminal tab that streams the app log live. It
can hide to the Windows system tray (double-click the tray icon to
bring it back; right-click it for the menu). Started by
arkmap.py --gui, which the normal RUN-ARK-MAP.bat uses via pythonw
(no terminal window).

Stdlib only (tkinter + ctypes), same rule as the rest of the app.
On non-Windows systems the tray is unavailable and the window behaves
like a normal window (used for testing on the build VM).
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import arkmap

BG = "#0a1512"
PANEL = "#0e1f1a"
BORDER = "#1e3a32"
TEXT = "#dff7ef"
MUTED = "#7fa89c"
ACCENT = "#3fe8c4"
AMBER = "#ffb347"
RED = "#ff7a7a"
TAB_ON = "#14402f"

HERE = Path(__file__).resolve().parent
ICO_PATH = HERE.parent / "ArkMap.ico"
EMBLEM_PATH = HERE / "emblem.png"

# The window's own remembered settings (size/position, last tab,
# map-polling toggle) - private per-PC state in the app's save/
# folder, never part of the shipped software, never touched by the
# updater (which leaves save/ alone by design).
GUI_SETTINGS_PATH = arkmap.SAVE_DIR / "gui_settings.json"

# GUI refresh timer choices: (seconds, menu label). 0 is Off - the
# stats poller then never re-renders on its own (Refresh Now still
# works). This window only; the web console refreshes itself.
POLL_CHOICES = (0, 4, 15, 30, 60, 300)
POLL_LABELS = {0: "Off", 4: "4 sec", 15: "15 sec", 30: "30 sec",
               60: "1 min", 300: "5 min"}


def _load_gui_settings() -> dict:
    try:
        data = arkmap.json.loads(
            GUI_SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_gui_settings(data: dict) -> None:
    try:
        GUI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        arkmap._write_json_retry(GUI_SETTINGS_PATH, data)
    except Exception as exc:
        arkmap.log(f"GUI: could not save window settings ({exc})")


def _fix_stdio() -> None:
    """pythonw has no console; give print() somewhere harmless to go."""
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w")
    except Exception:
        pass


def _fmt_dur(sec) -> str:
    if sec is None:
        return "—"
    sec = max(0, int(sec))
    d, h, m = sec // 86400, (sec % 86400) // 3600, (sec % 3600) // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _fmt_when(ts) -> str:
    if not ts:
        return "—"
    return time.strftime("%b %d, %I:%M %p", time.localtime(ts))


def _creature(e) -> str:
    """The killer label for a death event, matching the web console:
    the killer's name when the game wrote one; 'Unnamed creature'
    when it recorded only the killer's numeric KillerSID (same SID,
    same label, so one animal's repeat kills group together); and
    'Environmental (no killer)' when it recorded neither - a fall,
    drowning, starvation and the like, not a creature at all."""
    if not isinstance(e, dict):
        e = {"by": e}
    s = re.sub(r"^(a|an|the)\s+", "", str(e.get("by") or ""), flags=re.I).strip()
    if s:
        return s
    if e.get("by_sid"):
        return f"Unnamed creature (…{str(e['by_sid'])[-3:]})"
    return "Environmental (no killer)"


def _dark_titlebar(widget) -> None:
    """Ask Windows DWM for the immersive dark title bar, matching the
    system dark theme Clint uses. Attribute 20 on current Windows,
    19 on older Windows 10 - try both. Silent no-op anywhere else."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        if not hwnd:
            hwnd = widget.winfo_id()
        value = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd), attr, ctypes.byref(value),
                    ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


# ------------------------------------------------------------ tray icon ----
class TrayIcon:
    """Minimal Win32 notification-area icon on its own thread.

    Commands are delivered as strings on the shared queue:
    "show" (double-click / Open), "browser", "quit". Any failure to
    set up leaves .active False and the app runs windowed-only.
    """

    WM_TRAY = 0x0400 + 1          # WM_USER + 1
    WM_TRAYQUIT = 0x0400 + 2
    ID_OPEN = 1001
    ID_BROWSER = 1002
    ID_QUIT = 1003

    def __init__(self, tip: str, commands: "queue.Queue"):
        self.tip = tip
        self.commands = commands
        self.active = False
        self.hwnd = None
        self._thread = None
        self._nid = None

    def start(self) -> None:
        if sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="tray")
        self._thread.start()
        for _ in range(30):
            if self.active or not self._thread.is_alive():
                break
            time.sleep(0.05)

    def remove(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
            if self.hwnd:
                ctypes.windll.user32.PostMessageW(
                    wintypes.HWND(self.hwnd), self.WM_TRAYQUIT, 0, 0)
        except Exception:
            pass

    # -- everything below runs on the tray thread --
    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes
        try:
            self._run_inner(ctypes, wintypes)
        except Exception:
            arkmap.log("GUI: tray icon unavailable "
                       f"({traceback.format_exc(limit=2).strip()})")
            self.active = False

    def _run_inner(self, ctypes, wintypes) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT),
                        ("lpfnWndProc", ctypes.c_void_p),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT),
                        ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", ctypes.c_byte * 16),
                        ("hBalloonIcon", wintypes.HICON)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        user32.LoadImageW.restype = ctypes.c_void_p
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.restype = ctypes.c_ssize_t

        if ICO_PATH.exists():
            hicon = user32.LoadImageW(None, str(ICO_PATH), 1, 0, 0,
                                      0x00000010 | 0x00000040)
        else:
            hicon = None
        if not hicon:
            hicon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))

        commands = self.commands
        WM_TRAY, WM_TRAYQUIT = self.WM_TRAY, self.WM_TRAYQUIT
        ID_OPEN, ID_BROWSER, ID_QUIT = self.ID_OPEN, self.ID_BROWSER, self.ID_QUIT
        nid_holder = {}

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam == 0x0203:            # WM_LBUTTONDBLCLK
                    commands.put(("cmd", "show"))
                elif lparam == 0x0205:          # WM_RBUTTONUP
                    menu = user32.CreatePopupMenu()
                    user32.AppendMenuW(menu, 0, ID_OPEN, "Open Console Window")
                    user32.AppendMenuW(menu, 0, ID_BROWSER, "Open in Browser")
                    user32.AppendMenuW(menu, 0x0800, 0, None)   # separator
                    user32.AppendMenuW(menu, 0, ID_QUIT, "Quit")
                    pt = POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    user32.SetForegroundWindow(hwnd)
                    picked = user32.TrackPopupMenu(
                        menu, 0x0100, pt.x, pt.y, 0, hwnd, None)
                    user32.DestroyMenu(menu)
                    if picked == ID_OPEN:
                        commands.put(("cmd", "show"))
                    elif picked == ID_BROWSER:
                        commands.put(("cmd", "browser"))
                    elif picked == ID_QUIT:
                        commands.put(("cmd", "quit"))
                return 0
            if msg == WM_TRAYQUIT:
                nid = nid_holder.get("nid")
                if nid is not None:
                    shell32.Shell_NotifyIconW(2, ctypes.byref(nid))  # NIM_DELETE
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:                   # WM_DESTROY
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        cb = WNDPROC(wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(cb, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = "TrinityNexusArkTray"
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "TrinityNexusArkTray", 0,
            0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            raise RuntimeError("CreateWindowExW failed")
        self.hwnd = hwnd

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = wintypes.HWND(hwnd)
        nid.uID = 1
        nid.uFlags = 0x1 | 0x2 | 0x4            # MESSAGE | ICON | TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = wintypes.HICON(hicon)
        nid.szTip = self.tip[:127]
        nid_holder["nid"] = nid
        self._nid = nid
        if not shell32.Shell_NotifyIconW(0, ctypes.byref(nid)):  # NIM_ADD
            raise RuntimeError("Shell_NotifyIconW failed")
        self.active = True
        self._cb_keepalive = cb

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.active = False


# ----------------------------------------------------------------- GUI ----
REPO_RAW = ("https://raw.githubusercontent.com/fixxxxer101/"
            "trinity-nexus-ark-console/main")


def _latest_version():
    """The newest console version on GitHub, as an (x, y, z) tuple.
    Raises on any network or format problem."""
    req = urllib.request.Request(
        REPO_RAW + "/arkmap/arkmap.py",
        headers={"User-Agent": "TrinityNexusArkConsole"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    m = re.search(rb'VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', data)
    if not m:
        raise ValueError("no version found in the GitHub copy")
    return tuple(int(g) for g in m.groups())


def _version_str(v) -> str:
    return ".".join(str(n) for n in v)


class ConsoleGui:
    def __init__(self, cfg: dict):
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self.port = int(cfg.get("port") or 8770)
        self.interval = max(1, int(cfg.get("interval_min") or 3)) * 60
        self.started_at = time.monotonic()
        self.q: "queue.Queue" = queue.Queue()
        self.stop = threading.Event()
        self.server = None
        self.tray = TrayIcon(f"Trinity Nexus ARK Console v{arkmap.VERSION}",
                             self.q)
        self._hidden = False
        self._refreshing = False
        self._checking_update = False
        self._log_pending = ""
        self._terminal_frame = None
        self._terminal_text = None
        self._tab = "all"
        self._tab_names: list = []
        self._tab_buttons: dict = {}
        self._player_frames: dict = {}
        self._names_frame = None
        self._names_inner = None
        self._names_status_label = None
        self._last_stats: dict = {}
        # Remembered window settings: applied before the build so
        # the map-polling flag is set before the backend starts.
        self.settings = _load_gui_settings()
        arkmap.set_map_polling(bool(self.settings.get("map_polling", True)))
        # GUI refresh timer (this window only - the web console has
        # its own): how often the stats poller re-renders the tabs.
        # 0 is Off. Anything unrecognised falls back to the 4-second
        # cadence the GUI originally shipped with.
        try:
            _poll = int(self.settings.get("poll_seconds", 4))
        except Exception:
            _poll = 4
        self._poll_seconds = _poll if _poll in POLL_CHOICES else 4
        self.poll_var = None
        saved_tab = self.settings.get("tab")
        if isinstance(saved_tab, str) and saved_tab:
            self._tab = saved_tab
        self._wrap_labels: list = []
        self._player_inners: dict = {}
        self._player_canvases: dict = {}
        self.map_poll_var = None
        self._build()

    # ---------------------------------------------------------- build --
    def _build(self) -> None:
        tk = self.tk
        root = tk.Tk()
        self.root = root
        root.title(f"Trinity Nexus ARK Console v{arkmap.VERSION}")
        root.configure(bg=BG)
        # Remembered size/position when there is one; otherwise a
        # default that never exceeds the screen it opens on.
        geo = self.settings.get("geometry")
        if isinstance(geo, str) and re.fullmatch(
                r"\d+x\d+([+-]\d+){2}", geo):
            root.geometry(geo)
        else:
            # Default: wide, not tall - the old 640x800 opened as a
            # narrow tower; the content is tables and charts that
            # want width, and 800px was already crowding the screen.
            w = min(1120, root.winfo_screenwidth() - 40)
            h = min(720, root.winfo_screenheight() - 60)
            root.geometry(f"{w}x{h}")
        root.minsize(560, 540)
        root.bind("<Configure>", self._on_root_configure)
        if sys.platform == "win32" and ICO_PATH.exists():
            try:
                root.iconbitmap(default=str(ICO_PATH))
            except Exception:
                pass
        _dark_titlebar(root)

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 4))
        if EMBLEM_PATH.exists():
            try:
                self._emblem = tk.PhotoImage(file=str(EMBLEM_PATH))
                tk.Label(head, image=self._emblem, bg=BG).pack(side="left")
            except Exception:
                pass
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left", padx=12)
        tk.Label(titles, text="TRINITY NEXUS", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(titles, text="ARK CONSOLE", bg=BG, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(titles, text=f"desktop window · v{arkmap.VERSION}",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")

        self.tabbar = tk.Frame(root, bg=BG)
        self.tabbar.pack(fill="x", padx=16, pady=(8, 0))
        self.content = tk.Frame(root, bg=BG)
        self.content.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        self.all_frame = tk.Frame(self.content, bg=BG)
        self._build_all_tab(self.all_frame)
        self.all_frame.pack(fill="both", expand=True)
        self._rebuild_tabbar([])

        btns = tk.Frame(root, bg=BG)
        btns.pack(fill="x", padx=16, pady=(10, 4))
        def btn(text, cmd, accent=False):
            b = tk.Button(btns, text=text, command=cmd,
                          bg=TAB_ON if accent else PANEL, fg=TEXT,
                          activebackground="#1e3a32", activeforeground=TEXT,
                          relief="flat", font=("Segoe UI", 10, "bold"),
                          padx=12, pady=7, cursor="hand2",
                          highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 8))
            return b
        btn("Open in Browser", self.open_browser, accent=True)
        self.refresh_btn = btn("Refresh Now", self.refresh_now)
        self.update_btn = btn("Update", self.check_for_updates)
        btn("Hide to Tray", self.hide)
        # Map polling toggle: on Xbox services Nitrado never hands
        # over the world save, so the save pull is pure retry noise -
        # this stops it (the log scan behind the trackers keeps
        # running). Remembered across launches.
        self.map_poll_var = tk.BooleanVar(
            value=arkmap.map_polling_enabled())
        tk.Checkbutton(btns, text="Map polling", variable=self.map_poll_var,
                       command=self._on_map_poll_toggle,
                       bg=BG, fg=TEXT, activebackground=BG,
                       activeforeground=TEXT, selectcolor=PANEL,
                       font=("Segoe UI", 10), cursor="hand2").pack(
                           side="left", padx=(4, 0))
        # GUI refresh timer: how often this window re-reads and
        # re-renders its tabs. Every re-render used to land every
        # 4 seconds and throw the reader back to the top of the tab;
        # the timer is now the user's choice (Off included), it is
        # remembered, and a re-render keeps the scroll position.
        tk.Label(btns, text="Refresh", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="left", padx=(14, 4))
        self.poll_var = tk.StringVar(
            value=POLL_LABELS[self._poll_seconds])
        poll_menu = tk.OptionMenu(
            btns, self.poll_var,
            *[POLL_LABELS[s] for s in POLL_CHOICES],
            command=self._on_poll_change)
        poll_menu.configure(bg=PANEL, fg=TEXT, activebackground=TAB_ON,
                            activeforeground=TEXT, relief="flat",
                            font=("Segoe UI", 10), cursor="hand2",
                            highlightbackground=BORDER,
                            highlightthickness=1)
        try:
            poll_menu["menu"].configure(bg=PANEL, fg=TEXT,
                                        activebackground=TAB_ON,
                                        activeforeground=TEXT)
        except Exception:
            pass
        poll_menu.pack(side="left")

        help_label = tk.Label(
            root,
            text="Minimizing or closing this window sends the console "
                 "to the system tray - it keeps running. Double-click "
                 "the tray icon to bring it back; right-click it to quit. "
                 "Map polling pulls the world save for the map; on Xbox "
                 "Nitrado never releases it, so it can be switched off - "
                 "deaths, tames and players keep tracking either way. "
                 "Refresh is this window's own update timer: set it to "
                 "Off and the window stops re-drawing itself (Refresh "
                 "Now still works) and never throws you back to the top "
                 "of a tab while you are reading it.",
            bg=BG, fg=MUTED, font=("Segoe UI", 9), wraplength=540,
            justify="left")
        help_label.pack(fill="x", padx=16, pady=(8, 12))
        self._track_wrap(help_label, 32)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Unmap>", self._on_unmap)

    # ------------------------------------------- remembered state --
    def _track_wrap(self, label, padding: int) -> None:
        """Labels with a fixed wraplength clip or strand text when
        the window resizes; track them and re-wrap to the live width."""
        self._wrap_labels.append((label, padding))

    def _on_root_configure(self, event=None) -> None:
        if event is not None and event.widget is not self.root:
            return
        try:
            w = self.root.winfo_width()
        except Exception:
            return
        if w <= 1:
            return
        for label, padding in self._wrap_labels:
            try:
                label.configure(wraplength=max(220, w - padding))
            except Exception:
                pass
        # The tab bar and the squad tiles flow by width; both layout
        # helpers no-op when the arrangement already fits this width.
        try:
            self._layout_tabbar()
            self._layout_agg()
        except Exception:
            pass

    def _persist_settings(self) -> None:
        try:
            if self.root.state() != "withdrawn":
                self.settings["geometry"] = self.root.geometry()
        except Exception:
            pass
        self.settings["tab"] = self._tab
        self.settings["map_polling"] = arkmap.map_polling_enabled()
        self.settings["poll_seconds"] = self._poll_seconds
        _save_gui_settings(self.settings)

    def _on_poll_change(self, label: str) -> None:
        for secs, text in POLL_LABELS.items():
            if text == label:
                self._poll_seconds = secs
                break
        self._persist_settings()

    def _on_map_poll_toggle(self) -> None:
        arkmap.set_map_polling(bool(self.map_poll_var.get()))
        self._persist_settings()
        if arkmap.map_polling_enabled():
            # Turning it back on: check for a save right away rather
            # than waiting for the next timed cycle.
            self.refresh_now()

    def _build_all_tab(self, parent) -> None:
        tk = self.tk
        panel = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                         highlightthickness=1)
        panel.pack(fill="x")
        self.v_status = tk.StringVar(value="● STARTING…")
        self.status_label = tk.Label(panel, textvariable=self.v_status,
                                     bg=PANEL, fg=AMBER,
                                     font=("Segoe UI", 16, "bold"))
        self.status_label.pack(anchor="w", padx=14, pady=(12, 2))
        self.v_names = tk.StringVar(value="")
        names_label = tk.Label(panel, textvariable=self.v_names, bg=PANEL,
                               fg=MUTED, font=("Segoe UI", 9),
                               wraplength=520, justify="left")
        names_label.pack(anchor="w", padx=14, pady=(0, 10))
        self._track_wrap(names_label, 60)

        agg = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                       highlightthickness=1)
        agg.pack(fill="x", pady=(10, 0))
        self._agg_frame = agg
        self._agg_header = tk.Label(agg, text="SQUAD AVERAGES & TOTALS",
                                    bg=PANEL, fg=MUTED,
                                    font=("Segoe UI", 9, "bold"))
        self._agg_header.grid(row=0, column=0, columnspan=3, sticky="w",
                              padx=14, pady=(10, 2))
        self.agg = {}
        self._agg_tiles = []
        specs = [("avg_level", "Average level"),
                 ("avg_play", "Average playtime"),
                 ("total_play", "Total playtime"),
                 ("combined_level", "Total combined level"),
                 ("total_deaths", "Total player deaths"),
                 ("avg_deaths", "Average deaths"),
                 ("total_sessions", "Total sessions"),
                 ("avg_session", "Average session"),
                 ("deaths_per_hour", "Deaths per hour")]
        # The tiles are placed by _layout_agg, which fits as many
        # across as the window is wide and wraps the rest - a fixed
        # three-across stacked them three rows high with the rest of
        # the wide panel sitting empty.
        for i, (key, label) in enumerate(specs):
            row, col = 1 + (i // 3) * 2, i % 3
            var = tk.StringVar(value="—")
            self.agg[key] = var
            value = tk.Label(agg, textvariable=var, bg=PANEL, fg=TEXT,
                             font=("Segoe UI", 14, "bold"))
            value.grid(row=row, column=col, sticky="w", padx=14)
            caption = tk.Label(agg, text=label, bg=PANEL, fg=MUTED,
                               font=("Segoe UI", 9))
            caption.grid(row=row + 1, column=col, sticky="w", padx=14,
                         pady=(0, 8))
            self._agg_tiles.append((value, caption))
        for c in range(len(specs)):
            agg.columnconfigure(c, weight=1 if c < 3 else 0)
        self._agg_cols = None
        self.root.after(60, self._layout_agg)

        rows = tk.Frame(parent, bg=BG)
        rows.pack(fill="x", pady=(12, 0))
        self.fields = {}
        specs = [("server", "Server"), ("day", "Server day"),
                 ("players", "Players in world"),
                 ("map", "Map · Version"), ("connect", "Connect"),
                 ("save", "World save"), ("trackers", "Deaths on record"),
                 ("tames", "Living tames"),
                 ("checked", "Last check"), ("uptime", "Console uptime")]
        for i, (key, label) in enumerate(specs):
            tk.Label(rows, text=label, bg=BG, fg=MUTED,
                     font=("Segoe UI", 10)).grid(row=i, column=0,
                                                  sticky="w", pady=2)
            var = tk.StringVar(value="—")
            self.fields[key] = var
            tk.Label(rows, textvariable=var, bg=BG, fg=TEXT,
                     font=("Segoe UI", 10, "bold")).grid(
                         row=i, column=1, sticky="w", padx=12, pady=2)
        rows.columnconfigure(1, weight=1)

    def _layout_agg(self) -> None:
        # Reflow the squad tiles to the width on hand: ~175 px per
        # tile, so the wide default window fits six across (two rows
        # for the nine stats) and only a narrow window drops to three.
        frame = getattr(self, "_agg_frame", None)
        if frame is None or not getattr(self, "_agg_tiles", None):
            return
        avail = frame.winfo_width()
        if avail < 100:
            avail = max(self.root.winfo_width() - 32, 100)
        cols = max(1, min(len(self._agg_tiles), avail // 175))
        if cols == self._agg_cols:
            return
        self._agg_cols = cols
        self._agg_header.grid_configure(columnspan=cols)
        for i, (value, caption) in enumerate(self._agg_tiles):
            row, col = 1 + (i // cols) * 2, i % cols
            value.grid_configure(row=row, column=col)
            caption.grid_configure(row=row + 1, column=col)
        for c in range(len(self._agg_tiles)):
            frame.columnconfigure(c, weight=1 if c < cols else 0)

    # ----------------------------------------------------------- tabs --
    def _rebuild_tabbar(self, names: list) -> None:
        tk = self.tk
        for child in self.tabbar.winfo_children():
            child.destroy()
        self._tab_buttons = {}
        self._tab_names = list(names)
        self._tab_keys = ["all"] + list(names) + ["names", "terminal"]
        if self._tab not in self._tab_keys:
            self._tab = "all"
        for key in self._tab_keys:
            label = {"all": "All Players", "names": "Names", "terminal": "Terminal"}.get(key, key)
            b = tk.Button(self.tabbar, text=label,
                          command=lambda k=key: self._select_tab(k),
                          bg=TAB_ON if key == self._tab else PANEL,
                          fg=TEXT, activebackground="#1e3a32",
                          activeforeground=TEXT, relief="flat",
                          font=("Segoe UI", 10, "bold"), padx=12, pady=6,
                          cursor="hand2", highlightbackground=BORDER,
                          highlightthickness=1)
            self._tab_buttons[key] = b
        self._tab_rows = None
        self._layout_tabbar()

    def _layout_tabbar(self) -> None:
        # Flow the tab buttons by the width actually available: one
        # row whenever they fit (the normal case in the wide window),
        # wrapping onto a second row only when the window is too
        # narrow. A fixed count per row stacked them even with empty
        # space stretching to the right.
        if not getattr(self, "_tab_buttons", None):
            return
        avail = self.tabbar.winfo_width()
        if avail < 100:
            avail = max(self.root.winfo_width() - 32, 100)
        rows, row, x = [], [], 0
        for key in self._tab_keys:
            w = self._tab_buttons[key].winfo_reqwidth() + 6
            if row and x + w > avail:
                rows.append(row)
                row, x = [], 0
            row.append(key)
            x += w
        if row:
            rows.append(row)
        signature = tuple(len(r) for r in rows)
        if signature == getattr(self, "_tab_rows", None):
            return
        self._tab_rows = signature
        for r, keys in enumerate(rows):
            for c, key in enumerate(keys):
                self._tab_buttons[key].grid(row=r, column=c, sticky="w",
                                            padx=(0, 6), pady=(0, 6))

    def _select_tab(self, key: str) -> None:
        self._tab = key
        self.settings["tab"] = key
        _save_gui_settings(self.settings)
        for k, b in self._tab_buttons.items():
            b.configure(bg=TAB_ON if k == key else PANEL)
        self.all_frame.pack_forget()
        for f in self._player_frames.values():
            f.pack_forget()
        if self._terminal_frame is not None:
            self._terminal_frame.pack_forget()
        if self._names_frame is not None:
            self._names_frame.pack_forget()
        if key == "all":
            self.all_frame.pack(fill="both", expand=True)
        elif key == "terminal":
            self._build_terminal()
            self._terminal_frame.pack(fill="both", expand=True)
        elif key == "names":
            self._build_names()
            self._names_frame.pack(fill="both", expand=True)
            self._request_names()
        else:
            frame = self._player_frames.get(key)
            if frame is None:
                frame, inner, canvas = self._make_scroll_tab()
                self._player_frames[key] = frame
                self._player_inners[key] = inner
                self._player_canvases[key] = canvas
            frame.pack(fill="both", expand=True)
            self._render_player_tab(key)

    def _make_scroll_tab(self):
        """A tab page that scrolls vertically (same pattern as the
        Names tab): a player page is taller than the content area on
        smaller windows, and without scrolling its bottom sections
        are simply unreachable."""
        tk = self.tk
        frame = tk.Frame(self.content, bg=BG)
        canvas = tk.Canvas(frame, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(frame, orient="vertical",
                              command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(
                       scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        return frame, inner, canvas

    # ---------------------------------------------------------- names --
    def _build_names(self) -> None:
        if self._names_frame is not None:
            return
        tk = self.tk
        frame = tk.Frame(self.content, bg=BG)
        intro = tk.Label(frame,
                         text="Every username the console has detected in the "
                              "server logs. Give an account a nickname, and "
                              "choose who gets their own player tab. Everyone "
                              "stays tracked either way, and All Players always "
                              "includes everyone.",
                         bg=BG, fg=MUTED, font=("Segoe UI", 9),
                         wraplength=540, justify="left")
        intro.pack(fill="x", pady=(0, 6))
        self._track_wrap(intro, 32)
        self._names_status_label = tk.Label(frame, text="", bg=BG,
                                            fg=ACCENT,
                                            font=("Segoe UI", 9))
        self._names_status_label.pack(fill="x", pady=(0, 4))
        canvas = tk.Canvas(frame, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(frame, orient="vertical",
                              command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(
                       scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        tk.Label(inner, text="Reading the detected names...",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        self._names_frame = frame
        self._names_inner = inner

    def _request_names(self) -> None:
        def work():
            try:
                data = arkmap.console_names()
                self.q.put(("names_data", data.get("groups") or []))
            except Exception as exc:
                arkmap.log(f"GUI: names load failed ({exc})")
        threading.Thread(target=work, daemon=True).start()

    def _render_names(self, groups) -> None:
        tk = self.tk
        if self._names_inner is None:
            return
        for w in self._names_inner.winfo_children():
            w.destroy()
        if not groups:
            tk.Label(self._names_inner,
                     text="No usernames detected yet - they appear "
                          "here as the log scanner reads joins, deaths "
                          "and level sightings.",
                     bg=BG, fg=MUTED, font=("Segoe UI", 9),
                     wraplength=520, justify="left").pack(anchor="w",
                                                          pady=8)
            return
        for g in groups:
            row = tk.Frame(self._names_inner, bg=PANEL,
                           highlightbackground=BORDER,
                           highlightthickness=1)
            row.pack(fill="x", pady=(0, 8))
            top = tk.Frame(row, bg=PANEL)
            top.pack(fill="x", padx=10, pady=(8, 0))
            tk.Label(top, text=g.get("display") or "", bg=PANEL,
                     fg=TEXT, font=("Segoe UI", 11, "bold")).pack(
                         side="left")
            if g.get("show"):
                tk.Label(top, text="own tab", bg=PANEL, fg=ACCENT,
                         font=("Segoe UI", 8)).pack(side="left",
                                                    padx=(8, 0))
            kinds = ", ".join(
                f"{k.get('key')} ({'/'.join(k.get('kinds') or [])})"
                for k in g.get("keys") or [])
            tk.Label(row, text=kinds, bg=PANEL, fg=MUTED,
                     font=("Segoe UI", 8)).pack(anchor="w", padx=10)
            line = tk.Frame(row, bg=PANEL)
            line.pack(fill="x", padx=10, pady=(4, 8))
            tk.Label(line, text="Nickname", bg=PANEL, fg=MUTED,
                     font=("Segoe UI", 9)).pack(side="left")
            nick = tk.StringVar(value=g.get("nickname") or "")
            tk.Entry(line, textvariable=nick, bg=BG, fg=TEXT,
                     relief="flat", highlightbackground=BORDER,
                     highlightthickness=1, font=("Segoe UI", 10),
                     width=18, insertbackground=TEXT).pack(
                         side="left", padx=(6, 10))
            show = tk.BooleanVar(value=bool(g.get("show")))
            tk.Checkbutton(line, text="Own tab", variable=show,
                           bg=PANEL, fg=TEXT, activebackground=PANEL,
                           activeforeground=TEXT, selectcolor=BG,
                           font=("Segoe UI", 9)).pack(side="left",
                                                       padx=(0, 10))
            keys = [k.get("key") for k in g.get("keys") or []]
            tk.Button(line, text="Save",
                      command=lambda k=keys, n=nick, s=show:
                      self._save_name_group(k, n, s),
                      bg=PANEL, fg=TEXT, activebackground="#1e3a32",
                      activeforeground=TEXT, relief="flat",
                      font=("Segoe UI", 9, "bold"), padx=10, pady=3,
                      cursor="hand2", highlightbackground=BORDER,
                      highlightthickness=1).pack(side="left")

    def _save_name_group(self, keys, nick_var, show_var) -> None:
        nickname = nick_var.get()
        show = bool(show_var.get())

        def work():
            try:
                data = arkmap.save_name_group(keys, nickname, show)
                self.q.put(("names_data", data.get("groups") or []))
                self.q.put(("names_saved", True))
            except Exception as exc:
                arkmap.log(f"GUI: name save failed ({exc})")
                self.q.put(("names_saved", False))
        threading.Thread(target=work, daemon=True).start()

    def _build_terminal(self) -> None:
        if self._terminal_frame is not None:
            return
        tk = self.tk
        frame = tk.Frame(self.content, bg=BG)
        text = tk.Text(frame, bg=PANEL, fg=TEXT, font=("Consolas", 9),
                       relief="flat", highlightbackground=BORDER,
                       highlightthickness=1, wrap="none",
                       insertbackground=TEXT)
        scroll = tk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        if self._log_pending:
            text.insert("1.0", self._log_pending)
            text.see("end")
        self._terminal_frame = frame
        self._terminal_text = text

    def _append_log(self, chunk: str) -> None:
        self._log_pending = (self._log_pending + chunk)[-120000:]
        text = self._terminal_text
        if text is None:
            return
        try:
            at_bottom = text.yview()[1] >= 0.999
            text.insert("end", chunk)
            # Trim the widget itself, not just the pending buffer:
            # this is an always-on tray app, and an untrimmed Text
            # grows for days until the tab turns sluggish.
            lines = int(text.index("end-1c").split(".")[0])
            if lines > 1600:
                text.delete("1.0", f"{lines - 1200}.0")
            if at_bottom:
                text.see("end")
        except Exception:
            pass

    def _log_poller(self) -> None:
        """Tail arkmap.log into the Terminal tab via the queue. Seeds
        with the last ~60 KB so the tab opens on recent history, then
        follows new lines; a size drop means rotation - start over."""
        path = arkmap.LOG_PATH
        pos = 0
        try:
            pos = max(0, path.stat().st_size - 60000)
        except Exception:
            pos = 0
        while not self.stop.is_set():
            try:
                size = path.stat().st_size
                if size < pos:
                    pos = 0
                if size > pos:
                    with path.open("rb") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                    pos += len(chunk)
                    self.q.put(("log", chunk.decode("utf-8", "replace")))
            except Exception:
                pass
            self.stop.wait(0.7)

    def _render_player_tab(self, name: str) -> None:
        tk = self.tk
        frame = self._player_inners.get(name)
        if frame is None:
            return
        # The stats refresh rebuilds this page; without this, every
        # refresh threw the reader back to the top of the tab. Keep
        # the scroll fraction and put it back after the rebuild.
        canvas = self._player_canvases.get(name)
        frac = None
        if canvas is not None:
            try:
                frac = canvas.yview()[0]
            except Exception:
                frac = None
        for child in frame.winfo_children():
            child.destroy()
        s = self._last_stats or {}
        detail = (s.get("detail") or {}).get(name) or {}
        pres = detail.get("presence") or {}
        lvl = detail.get("level") or {}
        online = bool(pres.get("online"))
        title = tk.Frame(frame, bg=BG)
        title.pack(fill="x")
        tk.Label(title, text=name, bg=BG, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(title,
                 text="  ● ONLINE" if online else "  ● offline",
                 bg=BG, fg=ACCENT if online else MUTED,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        if lvl.get("current"):
            tk.Label(title, text=f"   Level {lvl['current']}", bg=BG,
                     fg=AMBER, font=("Segoe UI", 12, "bold")).pack(side="left")

        now = time.time()
        total = (pres.get("total_seconds") or 0) + \
            (now - pres["since"] if online and pres.get("since") else 0)
        rows = [("Total playtime", _fmt_dur(total)),
                ("Sessions", str(pres.get("sessions") or 0)),
                ("Last log-on", _fmt_when(pres.get("last_on"))),
                ("Last log-off", "still on" if online else _fmt_when(pres.get("last_off"))),
                ("Deaths", str(detail.get("deaths_total", 0)))]
        sess = pres.get("sessions") or 0
        if sess:
            rows.append(("Average session", _fmt_dur(total / sess)))
        dt = detail.get("deaths_total", 0)
        if dt:
            rows.append(("Playtime per death", _fmt_dur(total / dt)))
            if total:
                rows.append(("Deaths per hour", f"{dt / (total / 3600):.2f}"))
        dby = detail.get("deaths_by") or []
        if dby:
            rows.append(("Top killer", f"{dby[0][0]} ({dby[0][1]})"))
        grid = tk.Frame(frame, bg=BG)
        grid.pack(fill="x", pady=(8, 2))
        for i, (label, value) in enumerate(rows):
            tk.Label(grid, text=label, bg=BG, fg=MUTED,
                     font=("Segoe UI", 10)).grid(row=i, column=0,
                                                  sticky="w", pady=1)
            tk.Label(grid, text=value, bg=BG, fg=TEXT,
                     font=("Segoe UI", 10, "bold")).grid(
                         row=i, column=1, sticky="w", padx=12, pady=1)

        tk.Label(frame, text="LEVEL PROGRESSION", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(12, 2))
        series = lvl.get("series") or []
        chart = tk.Canvas(frame, bg=PANEL, height=150,
                          highlightbackground=BORDER, highlightthickness=1)
        chart.pack(fill="x")
        frame.update_idletasks()
        self._draw_step_chart(chart, series)
        sight = lvl.get("sightings") or 0
        cur = lvl.get("current") or 0
        tk.Label(frame,
                 text=(f"Caught at {sight} point{'s' if sight != 1 else ''} "
                       f"in the logs - {len(lvl.get('history') or [])} of "
                       f"{cur} levels sighted. The logs only record a level "
                       "when a death or level-up line catches it."),
                 bg=BG, fg=MUTED, font=("Segoe UI", 9),
                 wraplength=max(220, self.root.winfo_width() - 70),
                 justify="left").pack(anchor="w", pady=(4, 0))

        for heading, rows_data, color in [
                ("THEIR DEATHS BY CREATURE", detail.get("deaths_by") or [], RED)]:
            tk.Label(frame, text=heading, bg=BG, fg=MUTED,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w",
                                                        pady=(12, 2))
            bars = tk.Canvas(frame, bg=PANEL,
                             height=max(28, 22 * min(8, len(rows_data)) + 8),
                             highlightbackground=BORDER,
                             highlightthickness=1)
            bars.pack(fill="x")
            frame.update_idletasks()
            self._draw_bars(bars, rows_data, color)

        if canvas is not None and frac:
            try:
                frame.update_idletasks()
                canvas.yview_moveto(frac)
            except Exception:
                pass

    def _draw_step_chart(self, canvas, series: list) -> None:
        canvas.delete("all")
        w = max(300, canvas.winfo_width())
        h = 150
        pts = [p for p in series if p.get("ts")]
        if len(pts) < 2:
            canvas.create_text(12, 14, anchor="w", fill=MUTED,
                               font=("Segoe UI", 9),
                               text="Not enough level sightings to chart yet.")
            return
        t0, t1 = pts[0]["ts"], pts[-1]["ts"]
        lv = [p["level"] for p in pts]
        lo, hi = min(lv), max(lv)
        pl, pr, pt, pb = 30, 10, 10, 18
        iw, ih = w - pl - pr, h - pt - pb

        def x(ts):
            return pl + (ts - t0) / max(1, t1 - t0) * iw

        def y(level):
            return pt + ih - (level - lo) / max(1, hi - lo) * ih

        coords = []
        for i, p in enumerate(pts):
            if i:
                coords += [x(p["ts"]), y(pts[i - 1]["level"])]
            coords += [x(p["ts"]), y(p["level"])]
        coords += [x(t1) + (iw - (x(t1) - pl)), y(pts[-1]["level"])]
        canvas.create_line(coords, fill=ACCENT, width=2)
        for p in pts:
            canvas.create_oval(x(p["ts"]) - 2, y(p["level"]) - 2,
                               x(p["ts"]) + 2, y(p["level"]) + 2,
                               fill=ACCENT, outline="")
        canvas.create_text(2, y(hi) + 6, anchor="w", fill=MUTED,
                           font=("Segoe UI", 8), text=str(hi))
        canvas.create_text(2, y(lo) + 6, anchor="w", fill=MUTED,
                           font=("Segoe UI", 8), text=str(lo))
        canvas.create_text(pl, h - 4, anchor="w", fill=MUTED,
                           font=("Segoe UI", 8),
                           text=time.strftime("%b %d", time.localtime(t0)))
        canvas.create_text(w - pr, h - 4, anchor="e", fill=MUTED,
                           font=("Segoe UI", 8),
                           text=time.strftime("%b %d", time.localtime(t1)))

    def _draw_bars(self, canvas, rows: list, color: str) -> None:
        canvas.delete("all")
        w = max(300, canvas.winfo_width())
        if not rows:
            canvas.create_text(12, 14, anchor="w", fill=MUTED,
                               font=("Segoe UI", 9),
                               text="Nothing recorded yet.")
            return
        top = rows[:8]
        mx = max(v for _, v in top) or 1
        lab_w = 150
        for i, (label, value) in enumerate(top):
            y0 = 6 + i * 22
            canvas.create_text(8, y0 + 7, anchor="w", fill=TEXT,
                               font=("Segoe UI", 9),
                               text=str(label)[:24])
            bw = max(3, (w - lab_w - 46) * value / mx)
            canvas.create_rectangle(lab_w, y0, lab_w + bw, y0 + 14,
                                    fill=color, outline="")
            canvas.create_text(lab_w + bw + 6, y0 + 7, anchor="w",
                               fill=TEXT, font=("Segoe UI", 9, "bold"),
                               text=str(value))

    # -------------------------------------------------------- actions --
    def open_browser(self) -> None:
        webbrowser.open(f"http://localhost:{self.port}")

    def refresh_now(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.refresh_btn.configure(state="disabled", text="Refreshing…")

        def work():
            try:
                arkmap.backend_cycle(self.cfg)
            except Exception as exc:
                arkmap.log(f"GUI: manual refresh failed ({exc})")
            self.q.put(("refresh_done", None))
            self.q.put(("stats", _collect_stats(self.cfg, self.port)))

        threading.Thread(target=work, daemon=True).start()

    def check_for_updates(self) -> None:
        if self._checking_update:
            return
        self._checking_update = True
        self.update_btn.configure(state="disabled", text="Checking...")

        def work():
            try:
                self.q.put(("update_check", (True, _latest_version())))
            except Exception as exc:
                self.q.put(("update_check", (False, str(exc))))

        threading.Thread(target=work, daemon=True).start()

    def _update_check_done(self, ok, payload) -> None:
        from tkinter import messagebox
        self._checking_update = False
        self.update_btn.configure(state="normal", text="Update")
        if self._hidden:
            # The check can take ~30 s; if the window went to the
            # tray meanwhile, a dialog parented to the withdrawn root
            # can open invisibly and the result is never seen.
            self.show()
        if not ok:
            messagebox.showerror(
                "Update check failed",
                "Could not reach GitHub to check for updates.\n\n"
                f"{payload}",
                parent=self.root)
            return
        local = tuple(int(n) for n in arkmap.VERSION.split("."))
        if payload <= local:
            messagebox.showinfo(
                "Up to date",
                f"You're on the latest version (v{arkmap.VERSION}).",
                parent=self.root)
            return
        if messagebox.askyesno(
                "Update available",
                f"Version {_version_str(payload)} is available - "
                f"you have v{arkmap.VERSION}.\n\n"
                "Update now? The console will close, update itself "
                "from GitHub, and you relaunch RUN-ARK-MAP.bat to "
                "start the new version.",
                parent=self.root):
            self._start_update_and_quit()

    def _start_update_and_quit(self) -> None:
        from tkinter import messagebox
        update_py = arkmap.APP_DIR.parent / "update.py"
        if not update_py.exists():
            messagebox.showerror(
                "Updater missing",
                "update.py was not found next to the app folder, "
                "so the update cannot start. The console stays open.",
                parent=self.root)
            return
        pyexe = sys.executable
        if pyexe.lower().endswith("pythonw.exe"):
            pyexe = pyexe[:-len("pythonw.exe")] + "python.exe"
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_CONSOLE
        try:
            subprocess.Popen([pyexe, str(update_py), "--pause"],
                             cwd=str(update_py.parent),
                             creationflags=flags)
        except Exception as exc:
            messagebox.showerror(
                "Update could not start",
                f"{exc}\n\nThe console stays open.",
                parent=self.root)
            return
        arkmap.log("GUI: closing to update from GitHub.")
        self.quit()

    def hide(self) -> None:
        if self.tray.active:
            self._hidden = True
            self.root.withdraw()
            arkmap.log("GUI: hidden to tray.")
        else:
            self.root.iconify()

    def show(self) -> None:
        self._hidden = False
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _on_close(self) -> None:
        """The X button asks first (Clint's rule, v2.6.3 - X used to
        hide to the tray silently, and he once found four instances
        running at once): Hide to Tray, or Close App - and Close asks
        for a confirmation before anything actually stops."""
        if not self.tray.active:
            self._confirm_quit()
            return
        dlg = self.tk.Toplevel(self.root)
        dlg.title("Close the ARK Console?")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        self.tk.Label(dlg, text="Hide to the tray, or close the console?",
                 bg=BG, fg=TEXT, font=("Segoe UI", 10)).pack(
                     padx=18, pady=(14, 4))
        self.tk.Label(dlg, text="In the tray it keeps checking Nitrado and "
                           "serving the web page.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(
                     padx=18, pady=(0, 10))
        row = self.tk.Frame(dlg, bg=BG)
        row.pack(padx=14, pady=(0, 14))

        def to_tray():
            dlg.destroy()
            self.hide()

        def to_close():
            dlg.destroy()
            self._confirm_quit()

        for text, cmd in (("Hide to Tray", to_tray),
                          ("Close App", to_close),
                          ("Cancel", dlg.destroy)):
            self.tk.Button(row, text=text, command=cmd, bg=PANEL, fg=TEXT,
                      activebackground=ACCENT, activeforeground="#06231b",
                      relief="flat", padx=12, pady=5,
                      font=("Segoe UI", 9)).pack(side="left", padx=4)
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + \
            (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + \
            (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dlg.grab_set()
        dlg.focus_force()

    def _confirm_quit(self) -> None:
        from tkinter import messagebox
        if messagebox.askyesno(
                "Really close the ARK Console?",
                "This stops the web console and the Nitrado checks "
                "until you relaunch the app.",
                parent=self.root):
            self.quit()

    def _on_unmap(self, _event=None) -> None:
        if self.tray.active and not self._hidden:
            try:
                if self.root.state() == "iconic":
                    self.root.after(60, self.hide)
            except Exception:
                pass

    def quit(self) -> None:
        arkmap.log("GUI: quitting.")
        self._persist_settings()
        self.stop.set()
        try:
            self.tray.remove()
            # Give the tray thread a moment to actually remove the
            # icon before the process dies, or it lingers in the
            # notification area as a ghost until hovered over.
            if self.tray._thread is not None:
                self.tray._thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self.server is not None:
                threading.Thread(target=self.server.shutdown,
                                 daemon=True).start()
        except Exception:
            pass
        self.root.destroy()

    # ----------------------------------------------------------- loop --
    def run(self) -> None:
        arkmap.log(f"=== TRINITY NEXUS ARK CONSOLE v{arkmap.VERSION} "
                   "starting (GUI) ===")
        if not arkmap.DATA_PATH.exists():
            arkmap.write_status(False, "Waiting for the first world save "
                                       "from Nitrado...")
        try:
            self.server = arkmap.start_server(self.cfg)
        except (arkmap.AlreadyRunning, OSError):
            # AlreadyRunning is the friendly path; an OSError here is
            # the same situation arriving by the race (two launches at
            # once, the probe timed out and the bind lost) - it must
            # get the same dialog, not a silent pythonw exit.
            from tkinter import messagebox
            arkmap.log("GUI: another instance is already running - "
                       "this copy is closing.")
            messagebox.showwarning(
                "ARK Console already running",
                "The Trinity Nexus ARK Console is already running - "
                "check the system tray (bottom-right, by the clock).\n\n"
                "This second copy will now close.",
                parent=self.root)
            self.root.destroy()
            return
        threading.Thread(target=arkmap.run_backend,
                         args=(self.cfg, self.stop, self.interval),
                         daemon=True, name="backend").start()
        self.tray.start()
        threading.Thread(target=self._poller, daemon=True,
                         name="gui-poller").start()
        threading.Thread(target=self._log_poller, daemon=True,
                         name="gui-logtail").start()
        self.root.after(250, self._drain)
        self.root.mainloop()

    def _poller(self) -> None:
        # One collection at startup whatever the timer says, so the
        # window never opens empty; after that the GUI refresh timer
        # rules (0 = Off: no automatic re-renders, Refresh Now still
        # works). The wait runs in half-second slices against the
        # live setting, so changing the timer - including to Off -
        # bites at once instead of when the old interval expires.
        first = True
        while not self.stop.is_set():
            if first or self._poll_seconds > 0:
                try:
                    self.q.put(("stats",
                                _collect_stats(self.cfg, self.port)))
                except Exception:
                    # A dead poller used to be invisible: stale stats
                    # forever, nothing logged (stderr is devnull under
                    # pythonw). Log and keep polling.
                    arkmap.log("GUI: stats collection failed:\n"
                               + traceback.format_exc(limit=3))
                first = False
            waited = 0.0
            while not self.stop.is_set():
                if self._poll_seconds > 0 and waited >= self._poll_seconds:
                    break
                self.stop.wait(0.5)
                waited += 0.5

    def _drain(self) -> None:
        # Every handler is wrapped: one bad payload must never kill
        # the drain loop. Before this, an exception skipped the
        # after() reschedule below and the whole window froze -
        # stats, tray commands, buttons - silently, forever.
        try:
            while True:
                kind, payload = self.q.get_nowait()
                try:
                    if kind == "stats":
                        self._apply_stats(payload)
                    elif kind == "log":
                        self._append_log(payload)
                    elif kind == "refresh_done":
                        self._refreshing = False
                        self.refresh_btn.configure(state="normal",
                                                   text="Refresh Now")
                    elif kind == "update_check":
                        self._update_check_done(*payload)
                    elif kind == "names_data":
                        self._render_names(payload)
                    elif kind == "names_saved":
                        if self._names_status_label is not None:
                            self._names_status_label.configure(
                                text="Saved." if payload
                                else "Save failed - see the Terminal tab log.")
                    elif kind == "cmd":
                        if payload == "show":
                            self.show()
                        elif payload == "browser":
                            self.open_browser()
                        elif payload == "quit":
                            self.quit()
                            return
                except Exception:
                    arkmap.log(f"GUI: handler for '{kind}' failed:\n"
                               + traceback.format_exc(limit=3))
        except queue.Empty:
            pass
        try:
            self.root.after(250, self._drain)
        except Exception:
            pass

    def _apply_stats(self, s: dict) -> None:
        if not s:
            return
        self._last_stats = s
        if s.get("ok"):
            status = str(s.get("status") or "")
            if status == "started":
                self.v_status.set("● ONLINE")
                self._status_color(ACCENT)
            elif status == "stopped":
                self.v_status.set("● OFFLINE")
                self._status_color(RED)
            else:
                self.v_status.set("● " + (status.upper() or "UNKNOWN"))
                self._status_color(AMBER)
            self.fields["server"].set(s.get("name") or "—")
            cur, mx = s.get("cur"), s.get("max")
            self.fields["players"].set(
                f"{cur if cur is not None else '—'} / {mx if mx is not None else '—'}")
            names = s.get("online_names") or []
            self.v_names.set("In the world now: " + ", ".join(names)
                             if names else "")
            mv = " · ".join(x for x in [s.get("map"), s.get("version")] if x)
            self.fields["map"].set(mv or "—")
            self.fields["connect"].set(s.get("connect") or "—")
            if arkmap.DATA_PATH.exists():
                self.fields["save"].set("Live — a world save has been received")
            elif (s.get("state") or {}).get("walled"):
                self.fields["save"].set("Waiting — Nitrado holds the save "
                                         "(watch mode)")
            else:
                self.fields["save"].set("Waiting for the first save")
            d = s.get("deaths")
            self.fields["trackers"].set(str(d) if d is not None else "—")
            tv = s.get("tames_alive")
            self.fields["tames"].set(str(tv) if tv is not None else "—")
            dy = s.get("day") or {}
            if dy.get("ok"):
                self.fields["day"].set(
                    f"Day {dy.get('est_day') or dy.get('day')}")
            else:
                self.fields["day"].set("—")
        else:
            self.v_status.set("● UNREACHABLE")
            self._status_color(RED)
            self.v_names.set(str(s.get("error") or "")[:160])
        agg = s.get("agg") or {}
        self.agg["avg_level"].set(
            f"{agg['avg_level']:.1f}" if agg.get("avg_level") is not None else "—")
        self.agg["avg_play"].set(_fmt_dur(agg.get("avg_play")))
        self.agg["total_play"].set(_fmt_dur(agg.get("total_play")))
        self.agg["combined_level"].set(
            str(agg["combined_level"]) if agg.get("combined_level") is not None else "—")
        self.agg["total_deaths"].set(
            str(agg["total_deaths"]) if agg.get("total_deaths") is not None else "—")
        self.agg["avg_deaths"].set(
            f"{agg['avg_deaths']:.1f}" if agg.get("avg_deaths") is not None else "—")
        self.agg["total_sessions"].set(
            str(agg["total_sessions"]) if agg.get("total_sessions") is not None else "—")
        self.agg["avg_session"].set(_fmt_dur(agg.get("avg_session")))
        self.agg["deaths_per_hour"].set(
            f"{agg['deaths_per_hour']:.2f}" if agg.get("deaths_per_hour") is not None else "—")
        self.fields["checked"].set(
            time.strftime("%I:%M:%S %p", time.localtime(s.get("at", time.time()))))
        up = int(time.monotonic() - self.started_at)
        self.fields["uptime"].set(f"{up // 3600}h {(up % 3600) // 60}m"
                                  if up >= 3600 else f"{up // 60}m {up % 60}s")
        all_names = [p["name"] for p in (s.get("presence_players") or [])]
        shown = s.get("shown_names")
        if shown is None:
            names = all_names
        else:
            names = [n for n in all_names if n in shown]
        if names != self._tab_names:
            current = self._tab
            self._rebuild_tabbar(names)
            valid = ["all", "names", "terminal"] + names
            self._select_tab(current if current in valid else "all")
        elif self._tab != "all":
            self._render_player_tab(self._tab)

    def _status_color(self, color: str) -> None:
        try:
            self.status_label.configure(fg=color)
        except Exception:
            pass


def _collect_stats(cfg: dict, port: int) -> dict:
    out: dict = {"ok": False, "port": port}
    try:
        ov = arkmap.console_overview(cfg)
        gs = ov.get("gameserver") or {}
        q = gs.get("query") or {}
        out.update(ok=True, status=gs.get("status"),
                   name=q.get("server_name"), cur=q.get("player_current"),
                   max=q.get("player_max"), map=q.get("map"),
                   version=q.get("version"), connect=q.get("connect_ip"),
                   state=ov.get("state") or {})
    except Exception as exc:
        out["error"] = str(exc)
    presence_players = []
    try:
        pr = arkmap.console_presence()
        # Records without a name are unusable everywhere below;
        # drop them here rather than KeyError-ing on p["name"].
        presence_players = [p for p in pr.get("players", [])
                            if p.get("name")]
        out["online_names"] = [p["name"] for p in presence_players
                               if p.get("online")]
    except Exception:
        out["online_names"] = []
    out["presence_players"] = presence_players
    try:
        out["shown_names"] = sorted(arkmap.shown_display_names())
    except Exception:
        out["shown_names"] = None
    try:
        out["deaths"] = len(arkmap._load_deaths()["events"])
    except Exception:
        out["deaths"] = None
    try:
        out["day"] = arkmap.console_day()
    except Exception:
        out["day"] = {}
    try:
        out["tames_alive"] = arkmap.console_tames()["counts"]["living"]
    except Exception:
        out["tames_alive"] = None

    # ---- per-player detail + squad aggregates ----
    now = time.time()
    detail: dict = {}

    def slot(name):
        return detail.setdefault(name, {"presence": {}, "level": {},
                                        "deaths_by": [],
                                        "deaths_total": 0})

    for p in presence_players:
        slot(p["name"])["presence"] = p
    levels = []
    try:
        levels = arkmap.console_levels().get("players", [])
    except Exception:
        levels = []
    for p in levels:
        if p.get("name"):
            slot(p["name"])["level"] = p
    try:
        from collections import Counter
        dc: dict = {}
        for e in arkmap.console_deaths().get("events", []):
            if e.get("kind") == "player":
                who = e.get("player") or e.get("who") or ""
                dc.setdefault(who, Counter())[_creature(e)] += 1
        for who, counter in dc.items():
            s = slot(who)
            s["deaths_by"] = counter.most_common(8)
            s["deaths_total"] = sum(counter.values())
    except Exception:
        pass
    out["detail"] = detail

    currents = [p["current"] for p in levels
                if p.get("current") and p.get("name")]
    totals = []
    for p in presence_players:
        t = p.get("total_seconds") or 0
        if p.get("online") and p.get("since"):
            t += now - p["since"]
        totals.append(t)
    tot_deaths = sum(s.get("deaths_total", 0) for s in detail.values())
    tot_sessions = sum((p.get("sessions") or 0) for p in presence_players)
    out["agg"] = {
        "avg_level": (sum(currents) / len(currents)) if currents else None,
        "combined_level": sum(currents) if currents else None,
        "avg_play": (sum(totals) / len(totals)) if totals else None,
        "total_play": sum(totals) if totals else None,
        "total_deaths": tot_deaths,
        "avg_deaths": (tot_deaths / len(presence_players)) if presence_players else None,
        "total_sessions": tot_sessions,
        "avg_session": (sum(totals) / tot_sessions) if tot_sessions else None,
        "deaths_per_hour": (tot_deaths / (sum(totals) / 3600)) if sum(totals) else None,
    }
    out["at"] = time.time()
    return out


def run() -> int:
    _fix_stdio()
    try:
        import tkinter as tk
        from tkinter import messagebox
        if not arkmap.CONFIG_PATH.exists():
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Trinity Nexus ARK Console",
                "No saved configuration yet.\n\nRun "
                "RUN-ARK-MAP.bat once and complete the one-time "
                "setup in the console window it opens, then use "
                "the normal launcher.")
            root.destroy()
            return 1
        cfg = arkmap.load_or_prompt_config()
        ConsoleGui(cfg).run()
        return 0
    except Exception:
        try:
            arkmap.log("GUI crashed:\n" + traceback.format_exc())
        except Exception:
            pass
        # pythonw shows nothing on a raise - a double-click would
        # just do nothing. Put the failure on screen.
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Trinity Nexus ARK Console",
                "The console window crashed while starting.\n\n"
                f"The details are in the log:\n{arkmap.LOG_PATH}",
                parent=root)
            root.destroy()
        except Exception:
            pass
        return 1
