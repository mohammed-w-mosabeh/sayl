# Sayl — Specification

## Goal

Build Sayl, an open-source desktop download manager for Linux (X11 and Wayland) that matches Internet Download Manager's features and captures browser downloads WITHOUT any browser extension.

User experience target: the user clicks "Download" in any browser exactly as usual; the browser does nothing and a Sayl dialog appears with the file name, size, and save location. Playing a video on a supported site shows a small floating Sayl bar offering the available qualities.

## Non-negotiable constraints

1. No browser extension, ever. Capture works via (a) a local MITM proxy, (b) clipboard monitoring, (c) a floating drag-and-drop target, (d) a registered URL / `.desktop` handler.
2. Never implement DRM circumvention (Widevine, PlayReady, FairPlay). Detect DRM-protected streams and show "Protected content, not supported".
3. All local services bind to `127.0.0.1` only. The REST/WebSocket API requires a bearer token stored at `~/.config/sayl/token` (mode 0600).
4. Never log request/response bodies, cookies, or auth headers. Redact them in every log line.
5. The daemon owns all state. UI, CLI, proxy, and clipboard watcher are clients of the daemon API. Closing the UI never stops downloads.
6. XDG paths: config `~/.config/sayl/config.toml`, data `~/.local/share/sayl/`, cache `~/.cache/sayl/`, default download dir from `xdg-user-dir DOWNLOAD`.

## Known limits (document them in the README; do not try to "solve" them)

- Downloads assembled in the page by JavaScript (`blob:` / `data:` URLs, e.g. MEGA) never cross the network as a file and cannot be captured. The browser keeps handling them.
- DRM-protected platforms (Netflix, Shahid, Disney+, etc.) are out of scope.
- GNOME on Wayland lacks the clipboard data-control protocol; use the fallback described below.

## Architecture

- **sayld (daemon):** Python 3.12, asyncio, FastAPI + Uvicorn, SQLModel on SQLite (WAL mode), Alembic migrations, APScheduler.
- **Engine interface** `Engine`: `probe(url, headers) -> ProbeResult`, `add(job) -> engine_id`, `pause`, `resume`, `cancel`, `set_speed_limit`, `refresh_url(new_url)`, `status() -> Progress`.
  - **Aria2Engine:** spawn `aria2c` as a managed child process with `--enable-rpc --rpc-listen-all=false --rpc-secret=<random per start> --continue=true --save-session=<data>/aria2.session --input-file=<same, if it exists> --save-session-interval=30 --max-connection-per-server=16 --split=16 --min-split-size=1M --file-allocation=falloc` (fallback `none` on unsupported filesystems). JSON-RPC over WebSocket. Use `aria2.changeUri` for expired-link refresh and `aria2.changeOption` / `aria2.changeGlobalOption` for speed limits. Restart `aria2c` automatically if it dies and re-sync state from SQLite.
  - **YtDlpEngine:** use `yt_dlp.YoutubeDL` as a library in a worker thread or subprocess so it never blocks the event loop. `extract_info(download=False)` for probing; expose formats, subtitles, playlist entries. Map `progress_hooks` and `postprocessor_hooks` to `Progress`. Merge with ffmpeg. Support audio extraction, subtitle embedding, playlist item selection, rate limit, and cookies (file or `cookiesfrombrowser`). Recent yt-dlp releases need an external JavaScript runtime (e.g. Deno) for YouTube: check the yt-dlp README at build time and detect/report the runtime in `sayl doctor`.
  - **StreamThroughEngine** (see Capture): writes bytes already flowing through the proxy to disk, for single-use links.
- **Router:** if a yt-dlp extractor other than `generic` matches the URL, use YtDlpEngine; otherwise send `HEAD` (fallback ranged `GET bytes=0-0`) and use Aria2Engine. Magnet links and `.torrent` go to Aria2Engine.
- **Queue manager:** max concurrent downloads (default 3), priorities, drag reorder, retry with exponential backoff + jitter (max 5), global and per-download speed limits, speed schedules.
- **sayl-capture:** separate process running mitmproxy as a library (`DumpMaster`) with a custom addon.
- **Clipboard watcher:** separate async task in the daemon or a tiny helper process.
- **sayl (CLI):** Typer + Rich.
- **Desktop app:** Tauri 2 + Next.js (`output: 'export'`, no server features) + TypeScript strict + Tailwind + shadcn/ui + Zustand + TanStack Query. Live updates over WebSocket. Full Arabic (RTL) + English i18n, light/dark themes, system tray, native notifications, floating video-grabber bar, floating drop target.
- **Packaging:** systemd user unit `sayld.service`, AppImage and `.deb`. Detect `aria2c` and `ffmpeg`; offer the install command for the user's distro if missing.

### Repository layout

```
sayl/
  daemon/          # sayld: FastAPI app, engines, router, queue, scheduler, db
    engines/       # base.py, aria2.py, ytdlp.py, streamthrough.py
    capture/       # mitmproxy addon, CA management, system proxy toggles
    watchers/      # clipboard: x11.py, wayland.py, xwayland_poll.py
  cli/             # sayl: Typer
  desktop/         # Tauri 2 + Next.js
  packaging/       # systemd unit, AppImage, deb
  poc/             # Phase 0.5 proof of concept (kept for reference)
  tests/
  docs/
```

## Data model (minimum)

- `Download(id, url, final_url, page_url, filename, dest_dir, category_id, engine, engine_ref, status[queued|probing|active|paused|completed|failed|canceled], total_bytes, done_bytes, speed, eta, connections, priority, headers_enc, checksum_algo, checksum_expected, checksum_ok, error, created_at, started_at, finished_at)` — `headers_enc` is encrypted at rest with a key held in the system keyring.
- `Category(id, name, extensions, mime_prefixes, dest_dir, icon)`
- `Schedule(id, name, cron, action[start_queue|stop_queue|speed_limit], params_json, post_action[none|shutdown|suspend|command])`
- `Setting(key, value_json)`

## API (FastAPI; every route requires the bearer token)

`POST /downloads` (url, headers?, cookies?, referer?, page_url?, filename?, dest_dir?, category?, format_id?, start_paused?) · `GET /downloads` · `GET|PATCH|DELETE /downloads/{id}` · `POST /downloads/{id}/(pause|resume|refresh-url|verify)` · `POST /probe` · `GET|PUT /settings` · CRUD `/categories`, `/schedules` · `POST /batch` (list, or pattern like `file[001-100].jpg`) · `POST /grab-site` · `WS /ws` (events: `download.added|progress|completed|failed`, `capture.detected`, `clipboard.detected`). Publish the OpenAPI schema and generate the TypeScript client from it. Reject requests whose `Origin` header is a web origin.

## Capture (no extension)

### Certificate authority
- On first enable, generate a per-machine CA (EC P-256). Private key mode 0600 in `~/.local/share/sayl/ca/`. Never ship a CA in the package.
- Install into: the system trust store (`update-ca-certificates` / `update-ca-trust`, via `pkexec`), the Chrome/Chromium NSS DB (`~/.pki/nssdb`, `certutil` from libnss3-tools), and every Firefox profile (`certutil` on `cert9.db`).
- `sayl capture uninstall` removes the CA from every store and restores the previous proxy settings.

### System proxy toggle
GNOME via `gsettings org.gnome.system.proxy`, KDE via `kwriteconfig5/6`, plus printed env vars for other desktops. Always save and restore the previous values.

### Addon logic
- Decide in the `responseheaders` hook, NEVER in `response` (that would buffer whole files in memory).
- Only intercept top-level navigations (`Sec-Fetch-Mode: navigate`) or requests with no `Sec-Fetch-*` headers. For every other request (XHR, fetch, media) only observe and report media manifests; never replace their response.
- It is a download when any of: `Content-Disposition: attachment`; a known download extension; a binary MIME type (not `text/*`, not `application/json|javascript|xml`, not fonts, not images under the threshold) with `Content-Length` above the configurable threshold (default 2 MB).
- **Handoff strategy** (try in order):
  1. **Re-request:** if the server supports `Range` (`Accept-Ranges: bytes`) and the URL does not look single-use, POST to the daemon (final URL, method, `Cookie`, `Referer`, `User-Agent`, `Authorization`, page URL), then replace the response with HTTP 204 so the browser does nothing. aria2 downloads it with segmentation.
  2. **Stream-through:** otherwise (single-use or signed links, no Range support, POST downloads), keep the existing upstream response: set `flow.response.stream` to a callable that tees each chunk to the daemon's StreamThroughEngine (writes to the file) and returns empty bytes to the browser, then finish the browser side with an empty body. The download continues on the original connection, so one-time tokens are never reused. No segmentation in this mode; show "single connection" in the UI.
- **Video grabber:** report `.m3u8` / `.mpd` manifests and media MIME types without breaking playback; group them per page via `Referer`. For sites yt-dlp supports (YouTube etc.), do not rely on traffic: report the page URL and let YtDlpEngine list the formats. Emit `capture.detected` for the floating bar.
- **Bypass list** (defaults + user editable): banking and login domains, OS update mirrors, and any host that fails the TLS handshake through the proxy (auto-add after a failure, notify once).

## Clipboard

- Detect the session via `XDG_SESSION_TYPE`.
- Wayland with data-control (wlroots, KDE): `wl-paste --watch`.
- GNOME on Wayland: poll the XWayland clipboard (`xclip -o -selection clipboard` every 700 ms) and document the limitation.
- X11: XFixes selection events via python-xlib; polling only as fallback.
- Accept only `http(s)`, `ftp`, `magnet`; debounce duplicates for 10 s; respect an ignore list; show an actionable notification.

## Feature list (IDM parity and beyond)

Segmented download up to 16 connections · resume after crash/reboot · expired-link refresh · queue with priorities · video/playlist/audio download with quality picker · proxy capture + clipboard + floating drop target · auto categories with per-category folders · global and per-download speed limits + speed schedules · scheduler · post-completion action (shutdown/suspend/command) · tray + notifications · browser cookie import · checksum verification · auto retry · site grabber (crawl one site, filter by extension/depth/size) · batch download · torrent/magnet · outbound HTTP/SOCKS5 proxy · per-site credentials in the keyring · yt-dlp auto-update · history export/import · statistics · Arabic RTL + English UI.

## Phases (stop after each one, run all tests, report, open the PR)

0. **Scaffold:** uv, pnpm, ruff, mypy --strict, pytest, pre-commit, GitHub Actions, `sayl doctor`.
0.5. **Proof of concept (one day, in `poc/`):** a minimal mitmproxy addon that detects a download in Chrome and Firefox and hands it to a bare aria2c via RPC, plus the stream-through path for one single-use-link site. Test on at least 8 real sites (GitHub release, SourceForge, a Google Drive file, a Mediafire file, a Linux ISO mirror, a POST-based download, a site with signed URLs, a YouTube page for grabber detection). Report a table of site → result → notes. If capture fails on more than 2 of them, stop and report before Phase 1.
1. **Daemon core:** both engines, router, queue, API, WebSocket, persistence.
2. **CLI.**
3. **Desktop app.**
4. **Clipboard watcher.**
5. **Capture proxy** (production version of the PoC) and CA management.
6. **IDM parity:** scheduler, post actions, categories, checksums, cookie import, site grabber, batch.
7. **Packaging:** systemd unit, AppImage/.deb, bilingual README, yt-dlp auto-update.

## Acceptance tests (automate where possible)

- 1 GB file from a local test server with Range support: 16 connections, network cut mid-way, resume completes, SHA-256 matches.
- Kill `sayld` during 5 active downloads; restart; all resume.
- A YouTube 1080p video downloads merged with audio; selecting 5 items of a 20-item playlist downloads exactly those 5.
- With capture on, clicking a download link in Chrome and Firefox puts the file in Sayl with the session cookies; the browser shows nothing; normal browsing is unaffected.
- A single-use link is completed via stream-through without a second request to the server.
- `sayl capture uninstall` leaves no Sayl CA in any store and restores the previous proxy settings.
- Copying a supported URL triggers the notification within 1 s on X11 and KDE Wayland, within 2 s on GNOME Wayland.
- The UI renders correctly in Arabic RTL.
- A scheduled queue starts and stops on time and runs the post action.
- A server sending `filename="../../.bashrc"` results in a safe file inside `dest_dir`.

## Quality bar

- Type hints everywhere, mypy strict, ruff clean, >80% coverage on daemon logic.
- No blocking calls on the event loop. Graceful shutdown on SIGTERM.
- Structured JSON logs with redaction and rotation.
- Subprocess calls use argument lists, never `shell=True`.
- Sanitize filenames (strip path separators, control chars, reserved names; cap at 255 bytes).
