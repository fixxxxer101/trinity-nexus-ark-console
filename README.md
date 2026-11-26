# Trinity Nexus ARK Console

A complete dashboard for a **Nitrado ARK: Survival Evolved (Xbox / console) server** — a Windows desktop app plus a LAN web console the whole household can open in a browser. Built with the Python standard library only (no third-party packages), on top of the Nitrado API and the server's own log files.

Built for a small private, password-protected family server. Players are labelled by their **character name or Xbox gamertag**.

## Why this exists

Nitrado's panel and API answer a lot about a console ARK server — status, settings, backups — but on Xbox services they never tell you *who* is playing, when they played, what level they are, what died, or what tames you own. All of that **is** written in the server's own logs. This console reads both sources and puts them together.

One finding worth documenting: downloading the world save (`ShooterGame/Saved/SavedArks/`) through the file server/API is **not supported on console ARK services** (confirmed by Nitrado support, case LM2N7K-NPKNR). The server runs with `-AltSaveDirectoryName=SavedArks` and `-ForceS3ClusterStorage`, so saves live in the platform's cluster storage, outside the customer file area — the API lists the folder as empty and answers download attempts with HTTP 500. The console therefore runs in a "watch mode" for the save: it says so honestly on the page, and gives every new backup exactly one download attempt in case that ever changes. Everything below works without the save.

## What it does

**Web console (9 views)**

- **Overview** — live status, who is in the world, server-day counter (estimated from tribe-log day stamps, with the last confirmed stamp shown), connect address, full service facts, and start / stop / restart behind confirm dialogs
- **Live Map** — the calibrated Valguero map, ready for save data
- **Players** — sessions, playtime (including the live session), last log-on/off, and a level tracker with each player's current level and level-up history
- **Deaths** — full analytics back to day one: tallies, deaths-by-creature charts, per-day chart, search, a sortable death log, and tame losses grouped by breed (every breed, not a top-10)
- **Tames** — a tame database built from the logs: every tame claimed, frozen, released, or lost, with living / frozen / dead status — searchable, and sortable by name, breed, level, status, and dates
- **Files** — browse the server's file tree through the API and download files
- **Server Logs** — live log viewer
- **Backups** — every backup Nitrado holds (list only — there is deliberately **no Restore** anywhere in this app)
- **Settings** — the complete server configuration, read-only

**Desktop window (Windows)**

- Big ONLINE/OFFLINE status, who is in the world, squad averages (average level, average and total playtime, total combined level), server day, deaths total, and a living-tame count
- One tab per player with level-progression and deaths-by-creature charts, plus a live terminal tab streaming the app's own log
- System tray icon with hide-to-tray, a close dialog that asks tray-or-close (closing needs a second confirmation), dark title bar, and a single-instance guard so duplicate copies can't pile up and fight over the port

**Under the hood**

- Nitrado API via a long-life token (service + user_info scopes), stored **only** in a local config file on your own PC — never in this repo, never logged
- A unified log scanner that downloads every server log once, keeps a permanent local copy in `save/logs/`, and feeds five trackers: deaths, presence/sessions, levels, tames, and a (so far empty — see the notes) kill tracker
- The scanner is built to *learn formats from the logs themselves*: lines it can't parse are sampled into small candidate lists in each tracker's state file, so real-world wording gets tuned from evidence instead of guessed. That loop is how the death parser picked up three different death-line formats, including one that carries only a numeric killer ID
- Per-file scan memory: an interrupted backfill resumes where it stopped instead of starting over
- Players are resolved from gamertag → character name → optional display name via `save/player_names.json` (ships empty)

## Screenshots

Coming shortly — the console running on the author's own family server (Overview, Players, Tames, and the desktop window).

## Setup

1. Create a Nitrado long-life token: Nitrado web interface → My Account → Developer Portal → Long-life tokens, with the **service** and **user_info** scopes.
2. Run `SETUP.bat` and paste the token and your service ID when asked. Both are stored only in `~/.arkmap/config.json` on your PC.
3. Run `RUN-ARK-MAP.bat` for the desktop window, or `RUN-ARK-MAP-CONSOLE.bat` for terminal mode. The web console is served on port **8770** on your local network.

Optional: add `save/player_names.json` mapping gamertags or character names to any label you prefer, e.g. `{"xSomeGamertag": "CharacterName"}`.

## Notes and roadmap

- Settings and player management are read-only by design — this tool never writes to a live server's configuration, and Restore is intentionally unimplemented.
- Planned: an RCON menu (the server process runs RCON on port 15347) and start/stop/reboot controls in the desktop window.
- The kill tracker found zero genuine kill lines in 202 log files spanning three months — on this server build, kills simply aren't written to the logs (every candidate line turned out to be a cryopod freeze of a tame named "Killer"). The tracker stays in the backend, invisible, in case that changes.

## License

© Clint Mitchell. Shared publicly for portfolio review — please ask before reusing the code.
