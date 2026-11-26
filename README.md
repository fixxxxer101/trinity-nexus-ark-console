# Trinity Nexus ARK Console

A complete dashboard for a **Nitrado ARK: Survival Evolved (Xbox / console) server** — a Windows desktop app plus a LAN web console the whole household can open in a browser. Built on the Python standard library (one small third-party package, `arkparser`, is used only to parse world saves on services where a save can actually be downloaded), on top of the Nitrado API and the server's own log files.

Built for a small private, password-protected family server. Players are labelled by their **character name or Xbox gamertag**.

## Why this exists

Nitrado's panel and API answer a lot about a console ARK server — status, settings, backups — but on Xbox services they never tell you *who* is playing, when they played, what level they are, what died, or what tames you own. All of that **is** written in the server's own logs. This console reads both sources and puts them together.

One finding worth documenting: downloading the world save (`ShooterGame/Saved/SavedArks/`) through the file server/API is **not supported on console ARK services** (confirmed by Nitrado support, case LM2N7K-NPKNR). The server runs with `-AltSaveDirectoryName=SavedArks` and `-ForceS3ClusterStorage`, so saves live in the platform's cluster storage, outside the customer file area — the API lists the folder as empty and answers download attempts with HTTP 500. The console therefore runs in a "watch mode" for the save: it says so honestly on the page, and gives every new backup exactly one download attempt in case that ever changes. Everything below works without the save.

## What it does

**Web console (11 views)**

- **Overview** — live status, who is in the world, server-day counter (estimated from tribe-log day stamps, with the last confirmed stamp shown), connect address, full service facts, and start / stop / restart behind confirm dialogs
- **Live Map** — the calibrated Valguero map, ready for save data
- **Players** — sessions, playtime (including the live session), last log-on/off, a level tracker with each player's current level and level-up history, and squad aggregates including total player deaths and average deaths per player
- **Names** — every username the log scanner has detected (Xbox gamertags and character names, grouped per person): give an account a nickname, and choose which players get a dedicated player tab. Everyone stays tracked either way, and All Players always includes everyone
- **Deaths** — full analytics back to day one: tallies, deaths-by-creature charts, per-day chart, search, a sortable death log, tame losses grouped by breed (every breed, not a top-10), and a Repeat Offenders section tracking the unnamed killers by their game ID until you identify one and hang it as a trophy
- **Tames** — a tame database built from the logs: every tame claimed, frozen, released, unclaimed, or lost, with living / frozen / dead / unclaimed status (unclaimed tames show who unclaimed them) — searchable, sortable by name, breed, level, status, and dates, with an analytics block (counts per breed, killers, average and highest level) that follows the status filter
- **Files** — browse the server's file tree through the API and download files
- **Server Logs** — live log viewer
- **Backups** — every backup Nitrado holds (list only — there is deliberately **no Restore** anywhere in this app)
- **RCON** — a button-driven server-admin menu (Source RCON): save, broadcast, time/day/speed, player kick/ban/chat/give-item/give-XP by picker, tribe actions, wild-creature resets by kind, Valguero weather, and a save-first shutdown — behind a password prompt, with confirms on everything destructive. The password lives only in the app's memory. (Commands that act at a player's own position — spawning, teleporting, taming — have no RCON form and stay in-game.) **Note: Nitrado exposes RCON only on PC servers — on an Xbox/console service the view states that RCON is disabled there and stays locked.**
- **Settings** — the complete server configuration, read-only

**Desktop window (Windows)**

- Big ONLINE/OFFLINE status, who is in the world, squad averages (average level, average and total playtime, total combined level, total player deaths, average deaths), server day, deaths total, and a living-tame count
- One tab per player with level-progression and deaths-by-creature charts, plus a live terminal tab streaming the app's own log
- A Names tab mirroring the web console's: detected usernames with nickname editing and per-player tab toggles
- A **Map polling** toggle: the world-save pull behind the Live Map. On Xbox/console services Nitrado never releases the save, so the pull is pure retry noise — switch it off and the deaths / tames / players trackers keep running on the log scan alone
- A **Refresh** timer for the window itself: Off, 4 sec, 15 sec, 30 sec, 1 min, or 5 min between automatic updates. Off stops the window re-drawing itself entirely (Refresh Now still works), and a refresh no longer throws you back to the top of a player tab — your scroll position survives it
- The window **remembers itself**: size and position, the last tab, the Map polling toggle, and the Refresh timer are restored on the next launch (stored in `arkmap/save/gui_settings.json`, local data the updater never touches). It opens wide rather than tall by default (up to 1120×720, never more than the screen)
- The layout **scales with the window**: squad stats flow three-across, the tab bar wraps onto extra rows instead of running off the edge, player pages scroll, and charts and wrapped text follow the live width — maximize it and nothing is clipped or lost
- System tray icon with hide-to-tray, a close dialog that asks tray-or-close (closing needs a second confirmation), dark title bar, and a single-instance guard so duplicate copies can't pile up and fight over the port

**Under the hood**

- Nitrado API via a long-life token (service + user_info scopes), stored **only** in a local config file on your own PC — never in this repo, never logged
- A unified log scanner that downloads every server log once, keeps a permanent local copy in `arkmap/save/logs/`, and feeds five trackers: deaths, presence/sessions, levels, tames, and a (so far empty — see the notes) kill tracker
- The scanner is built to *learn formats from the logs themselves*: lines it can't parse are sampled into small candidate lists in each tracker's state file, so real-world wording gets tuned from evidence instead of guessed. That loop is how the death parser picked up three different death-line formats, including one that carries only a numeric killer ID
- Per-file scan memory: an interrupted backfill resumes where it stopped instead of starting over
- Scanner resilience: a log line with a malformed timestamp (or any other single poisonous line) is skipped and logged, never stalls the scan — and transient Nitrado errors are retried before a cycle is given up
- Credential hygiene beyond the token: fileserver download tokens are redacted out of the app log, and the Settings/Overview API answers are served with password-shaped fields masked (ARK's server settings carry the admin password, which is also the RCON password — it never reaches the LAN in clear text)
- Players are resolved from gamertag → character name → optional display name via `arkmap/save/player_names.json` (ships empty); the Names tabs edit those nicknames plus the per-player tab flags in `arkmap/save/player_show.json`
- The RCON menu's creature and item picker tables (`arkmap/creatures.json`, ~375 creatures by class name; `arkmap/items.json`, ~220 items by number/blueprint path) are built from the ARK wiki's Creature IDs / Item IDs pages — creatures have no numeric IDs in ARK, and items whose number sources disagree on are flagged in the picker

## Screenshots

Coming shortly — the console running on the author's own family server (Overview, Players, Tames, and the desktop window).

## Install

You need:

- A Windows 10/11 PC on the same network as the devices that will open the console
- **Python 3.10 or newer** — from [python.org/downloads](https://www.python.org/downloads/), and tick **"Add python.exe to PATH"** during the install
- A Nitrado ARK (Xbox / console) server, and a Nitrado long-life API token (below)

### 1. Get the code

Download this repo as a ZIP (green **Code** button → **Download ZIP**) and unzip it anywhere, or `git clone` it. There is exactly one file you ever double-click: **`RUN-ARK-MAP.bat`**.

### 2. Create the API token

1. Nitrado web interface → **My Account → Developer Portal → Long-life tokens** (Nitrado asks for your account password on the way — that's its normal security gate).
2. Description: `ArkMap`. Tick the **service** and **user_info** scopes, then **Create**.
3. Copy the token right away — Nitrado shows it only once.

The token can control your whole server. The app stores it **only** in `C:\Users\<your name>\.arkmap\config.json` on your PC — never in this folder, never in the log. You can revoke it any time on the same Developer Portal page (changing your Nitrado password also revokes tokens).

### 3. First run — just double-click

Double-click **`RUN-ARK-MAP.bat`**. The first run sets itself up and keeps going:

1. It builds the app's own Python environment (`.venv`) and installs `arkparser` — this needs the internet once, and is skipped on every later run.
2. It opens a console window and asks four questions: your **API token** (typing stays hidden), your **service ID** (shown in the Nitrado panel), the **web port** (default 8770), and how often to **check the server** in minutes (default 3). The answers are saved to `~/.arkmap/config.json`.
3. The console starts right there in that window. The first log scan backfills your server's whole history, which can take a few minutes on a long-running server.

Every run after that, the same double-click opens the desktop window directly — no prompts, no waiting. (Terminal mode is still there if you want it: `RUN-ARK-MAP.bat console`.)

Open the web console at `http://localhost:8770` on the PC itself, or `http://<the PC's LAN address>:8770` from any phone, tablet, or PC on your home network.

Optional: add `arkmap/save/player_names.json` mapping gamertags or character names to any label you prefer, e.g. `{"xSomeGamertag": "CharacterName"}`.

## Updating

This repo is the software's single home, and every install updates from here:

- Click **Update** in the desktop window. It checks GitHub and tells you if you're current; if a new version is out, it asks once, closes the console, and runs the updater. (From the command line instead: `RUN-ARK-MAP.bat update`, or `python update.py`.)
- The updater compares your version with the one here, downloads the software files, and **checks them before swapping anything** (Python files must compile, the data table must parse). The files it replaces are backed up to `update-backup/` first, so an update can be rolled back by hand.
- The updater waits for the console to close before swapping files, and refuses if it's still running after a short wait. Only one updater can run at a time, and if a file swap fails halfway, the rollback restores **every** file it touched — including the one that failed — and says so honestly if a restore itself fails.
- One effect of the v2.10.0 setup fix: the launcher now skips setup only when setup **finished** (it writes a marker at the end), so the first launch after this update runs setup once more even on a healthy install — a minute, one time, and failed first-time setups can finally recover by just double-clicking again.
- New in v2.12.2: fixes a build error in v2.12.1 that left the desktop window's All Players page half-built (the server-detail rows never appeared and no stats could load). The width-flow tab bar and squad tiles from v2.12.1 are unchanged and now sit on a complete page.
- New in v2.12.1: the desktop window's tab bar and squad stats now flow by width — the tab buttons sit in a single row whenever they fit (wrapping only in a narrow window) instead of stacking four-per-row, and the squad averages spread as many across as the window is wide (six across at the default size) instead of a fixed three-by-three stack.
- New in v2.12.0: deaths are classified the way the game actually recorded them. A creature kill carries the killer's name, or — when the name could not be written — its numeric KillerSID, shown as **Unnamed creature (…123)** so one animal's repeat kills group together (this server has one: the same creature killed a player and, 19 days later, an Ice Wyvern). A death with neither name nor SID had no killer at all and is labelled **Environmental (no killer)** — a fall, drowning, starvation and the like — instead of being lumped in as "unknown". The first scan after updating re-reads the kept logs once and backfills the KillerSIDs onto the deaths already on record; totals do not change. The Deaths view also gains a **Repeat Offenders** section: every unnamed creature is tracked by its game ID with its kill list, and when you kill one you can record what it was — who killed it, when, a note — which hangs it as a **trophy** and puts its name everywhere its kills are shown (identifications live in `arkmap/save/offenders.json`, your data, never shipped). The desktop window also gains its own Refresh timer (Off included) and opens wider and shorter by default.
- It **never** touches your data: not `~/.arkmap/config.json`, not `arkmap/save/` (scan state, player names, kept log copies), not the app log, not `.venv`. If an update changes the installer, the launcher refreshes the app environment by itself on the next run.

If you installed with `git clone`, `git pull` works too — the built-in updater is there for ZIP installs.

## Troubleshooting

- **"Python 3.10 or newer was not found"** — install Python from python.org and tick "Add python.exe to PATH", then double-click again. Setup resumes by itself.
- **The updater says the console is running** — it is, probably in the system tray. Quit it from the tray icon, then update.
- **A second copy says the console is already running** — that's the single-instance guard working: one copy serves the port and the web page; extras close themselves.
- **Start over with settings** — delete `C:\Users\<your name>\.arkmap\config.json` and run again; you'll be asked the four questions again. Your scan history in `arkmap/save/` is kept.
- **Everything the app does is logged** to `arkmap/arkmap.log` — connections, what Nitrado answered, downloads, errors. The token is never written there.

## Notes and roadmap

- Settings and player management are read-only by design — this tool never writes to a live server's configuration, and Restore is intentionally unimplemented.
- Planned: an RCON menu (the server process runs RCON on port 15347) and start/stop/reboot controls in the desktop window.
- The kill tracker found zero genuine kill lines in 202 log files spanning three months — on this server build, kills simply aren't written to the logs (every candidate line turned out to be a cryopod freeze of a tame named "Killer"). The tracker stays in the backend, invisible, in case that changes.

## License

© Clint Mitchell. Shared publicly for portfolio review — please ask before reusing the code.
