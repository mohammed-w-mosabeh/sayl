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

## Documented deviations

- **Header key without a keyring.** The key for `headers_enc` belongs in the system keyring. When no keyring is available, such as in a headless session, it falls back to a mode 0600 file in the data directory. This is weaker: anyone who can read the file can read saved cookies. `sayl doctor` warns about it, and the daemon logs it once at startup.

## Known limits (document them in the README; do not try to "solve" them)

- Downloads assembled in the page by JavaScript (`blob:` / `data:` URLs, e.g. MEGA) never cross the network as a file and cannot be captured. The browser keeps handling them.
- DRM-protected platforms (Netflix, Shahid, Disney+, etc.) are out of scope.
- GNOME on Wayland lacks the clipboard data-control protocol; use the fallback described below.

## Architecture

- **sayld (daemon):** Python 3.12, asyncio, FastAPI + Uvicorn, SQLModel on SQLite (WAL mode), Alembic migrations, APScheduler.
- **Engine interface** `Engine`: `probe(url, headers) -> ProbeResult`, `add(job) -> engine_id`, `attach(job, stream) -> engine_id`, `pause`, `resume`, `cancel`, `set_speed_limit`, `refresh_url(new_url)`, `status() -> Progress`. `attach` takes ownership of a byte stream already open at offset 0, and is what makes a captured download possible without requesting its URL again. Only HybridEngine implements it; the other engines raise `AttachNotSupported`.
  - **Aria2Engine:** spawn `aria2c` as a managed child process with `--enable-rpc --rpc-listen-all=false --rpc-secret=<random per start> --continue=true --save-session=<data>/aria2.session --input-file=<same, if it exists> --save-session-interval=30 --max-connection-per-server=16 --split=16 --min-split-size=1M --file-allocation=falloc` (fallback `none` on unsupported filesystems). JSON-RPC over WebSocket. Use `aria2.changeUri` for expired-link refresh and `aria2.changeOption` / `aria2.changeGlobalOption` for speed limits. Restart `aria2c` automatically if it dies and re-sync state from SQLite.
  - **YtDlpEngine:** use `yt_dlp.YoutubeDL` as a library in a worker thread or subprocess so it never blocks the event loop. `extract_info(download=False)` for probing; expose formats, subtitles, playlist entries. Map `progress_hooks` and `postprocessor_hooks` to `Progress`. Merge with ffmpeg. Support audio extraction, subtitle embedding, playlist item selection, rate limit, and cookies (file or `cookiesfrombrowser`). After a download, compare what was requested with the final file as read by `ffprobe` (height, video codec, audio codec, whether audio exists). A height cap such as `height<=1080` counts as the lower of the cap and the best height available, so a source that is simply smaller is not a loss; format ids are compared strictly. A shortfall, or a failed signature or n-challenge solve even when yt-dlp exits 0, leaves the download `completed` with a `quality_degraded` warning holding requested and delivered values, sent as `download.quality_degraded` and shown by `sayl info`. Recent yt-dlp releases need an external JavaScript runtime (e.g. Deno) for YouTube: check the yt-dlp README at build time and detect/report the runtime in `sayl doctor`.
  - **HybridEngine** (see Capture): the engine for every captured download. **StreamThroughEngine** writes the attached stream to disk; **range workers** fetch the remaining segments when acceleration is allowed; a persisted **segment map** records which byte ranges are on disk.
- **Router:** a download attached by sayl-capture always goes to HybridEngine, with no probe. Downloads that start from the CLI, clipboard, or UI: if a yt-dlp extractor other than `generic` matches the URL, use YtDlpEngine; otherwise send `HEAD` (fallback ranged `GET bytes=0-0`) and use Aria2Engine. Magnet links and `.torrent` go to Aria2Engine.
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
    engines/       # base.py, aria2.py, ytdlp.py, streamthrough.py, hybrid.py
    capture/       # mitmproxy addon, CA management, system proxy toggles
                   # mitm_adapter.py is the only file allowed to touch mitmproxy internals
    watchers/      # clipboard: x11.py, wayland.py, xwayland_poll.py
  cli/             # sayl: Typer
  desktop/         # Tauri 2 + Next.js
  packaging/       # systemd unit, AppImage, deb
  poc/             # Phase 0.5 proof of concept (kept for reference)
  tests/
  docs/
```

## Data model (minimum)

- `Download(id, url, final_url, page_url, filename, dest_dir, category_id, engine, engine_ref, status[queued|probing|active|paused|completed|failed|canceled|needs_refresh], total_bytes, done_bytes, speed, eta, connections, priority, headers_enc, checksum_algo, checksum_expected, checksum_ok, error, created_at, started_at, finished_at)` — `headers_enc` is encrypted at rest with a key held in the system keyring.
- `Category(id, name, extensions, mime_prefixes, dest_dir, icon)`
- `Schedule(id, name, cron, action[start_queue|stop_queue|speed_limit], params_json, post_action[none|shutdown|suspend|command])`
- `Setting(key, value_json)`
- `Segment(id, download_id, start, end, done_bytes, source[stream|range], status)` — the segment map of a HybridEngine download.
- `HostPolicy(host, accelerate, reason[auth|gone|validator_changed|not_206], updated_at)` — a host recorded here skips acceleration. Users can clear it in settings.

## API (FastAPI; every route requires the bearer token)

`POST /downloads` (url, headers?, cookies?, referer?, page_url?, filename?, dest_dir?, category?, format_id?, start_paused?, speed_limit?, audio_only?, subtitles?, playlist_items?, cookies_file?, cookies_from_browser?; the last five are YtDlpEngine options: extract audio only, languages to embed, items such as `1,3,5-7`, a cookies.txt path, a browser to read cookies from) · `POST /downloads/attach` (url, method, request_headers, response_headers, status, page_url?, filename?; creates a HybridEngine download and returns its id, with no request to the origin) · `PUT /downloads/{id}/stream` (raw response body from offset 0, chunked, with back-pressure; used once, and an early close fails the stream) · `GET /downloads` · `GET|PATCH|DELETE /downloads/{id}` · `POST /downloads/{id}/(pause|resume|refresh-url|verify)` (`refresh-url` takes url and optional headers) · `POST /probe` · `GET|PUT /settings` · CRUD `/categories`, `/schedules` · `POST /batch` (list, or pattern like `file[001-100].jpg`) · `POST /grab-site` · `WS /ws` (events: `download.added|progress|updated|removed|completed|failed|needs_refresh|quality_degraded`, `capture.detected`, `clipboard.detected`). Publish the OpenAPI schema and generate the TypeScript client from it. Reject requests whose `Origin` header is a web origin.

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
- **Handoff: stream first, accelerate if allowed.** The URL is never guessed at to decide how to start.
  1. **Stream first.** Every captured download starts as stream-through on the original connection. Never request the original URL again to begin a download. Replace the response with an empty HTTP 204 so the browser does nothing, keep the upstream open, and tee each chunk to the daemon (`POST /downloads/attach`, then `PUT /downloads/{id}/stream`). `flow.response.stream` returns empty bytes to the browser. POST downloads and responses with no `Content-Length` stay in this mode for their whole life.
  2. **Accelerate if allowed.** Only when all of these hold: the response has `Accept-Ranges: bytes`, a known `Content-Length`, a strong validator (`ETag` that is not weak, or `Last-Modified`), no `Content-Encoding`, the method is `GET`, the remaining bytes exceed `accelerate_min_bytes` (default 20 MB), the host is not recorded in `HostPolicy`, and the single-use hint did not fire. The daemon then opens extra `Range` connections for the remaining segments with the original `Cookie`, `Authorization`, `Referer`, and `User-Agent`, working back from the end of the file so the original stream keeps the bytes it is already receiving. It opens one connection first and adds the others only after that one succeeds, to keep a mistaken request to a single.
  3. **Validate every extra connection.** Each response must be `206` with a matching `Content-Range` and total size, and the same `ETag`/`Last-Modified` as the original response.
  4. **Fall back, never fail.** On `401`, `403`, `410`, a changed validator, or any non-206 response: cancel all range workers, discard their unverified bytes, and finish on the original stream. The original stream stays open until the segment map shows the file is complete, so this is always possible. Record the host in `HostPolicy` so later downloads from it skip acceleration.
  5. **Single-use hint.** `looks_single_use` checks signed query parameters and high-entropy path segments. It only skips acceleration. It never decides correctness, and never picks a different way to start. A link it misses may receive one rejected Range request, then falls back under rule 4 with a correct file.
  - **Back-pressure (Phase 5):** the daemon pauses the stream at the boundary, but mitmproxy's stream callback cannot pause the upstream. The PoC bridge spools those bytes to disk and keeps reading the upstream after the daemon finishes. The production addon must do neither: `mitm_adapter.py` has to pause reading from the upstream, and cancel it once the file is complete.
  - **Spool bound (Phase 5):** any spool has a maximum size, `capture_spool_max_bytes` (default 512 MB). Before spooling, check free space on the spool's filesystem and refuse to start if it cannot hold the smaller of that maximum and the remaining bytes. When the limit is reached or space runs out, stop spooling and take the `needs_refresh` path, keeping what is already on disk. Never fail the download for this.
  - **Show the mode in the UI:** "single connection" or "accelerated (N connections)".
  - **Lost streams:** a stream cannot survive a dropped connection or a crash, and the original URL is never requested whole again. The partial file and segment map are kept. If the original response proved Range support (same conditions as acceleration, except size and the single-use hint) and the host is not in `HostPolicy`, the missing segments resume with `Range` requests. Otherwise, or if those are refused, the download moves to `needs_refresh` with "link expired, refresh it". `POST /downloads/{id}/refresh-url` then continues from the same offset, with the same validators; a link that serves a different file is refused and the download stays in `needs_refresh`.
- **Video grabber:** report `.m3u8` / `.mpd` manifests and media MIME types without breaking playback; group them per page via `Referer`. For sites yt-dlp supports (YouTube etc.), do not rely on traffic: report the page URL and let YtDlpEngine list the formats. Emit `capture.detected` for the floating bar.
- **mitmproxy isolation.** The handoff needs behaviour stock mitmproxy cannot provide: ending the browser side while the upstream keeps running. All code that touches non-public mitmproxy APIs lives in `daemon/capture/mitm_adapter.py`, and nowhere else. It refuses to install on any version other than the pinned one.
- **Upgrading mitmproxy** (always by hand, in its own `fix/` branch; excluded from automated updates):
  1. Read the upstream changes to `mitmproxy/proxy/layers/http/`, which `mitm_adapter.py` patches.
  2. Change the exact pin in `pyproject.toml` and `SUPPORTED_VERSION` in `mitm_adapter.py` together, then run `uv lock`.
  3. Run the single-use lab matrix locally: `uv run python poc/lab/run_matrix.py` for each browser and protocol.
  4. Merge only when every CI matrix cell is green. If it cannot go green, stay on the pinned version and open an issue.
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
1. **Daemon core:** all three engines including HybridEngine, stream attach API, router, queue, API, WebSocket, persistence.
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
- **Phase 1, hybrid engine (lab, with a test client attaching streams the way sayl-capture will):** each of these completes with a correct SHA-256:
  - a one-time link with its token in the path, advertising `Accept-Ranges` and an `ETag`, requested exactly once because the hint skips acceleration;
  - a session-bound link that rejects Range connections with 403, which finishes on the original stream and records the host in `HostPolicy`;
  - a normal Range-capable link, which is accelerated.
- **Phase 5, spool bound:** a capture that exceeds `capture_spool_max_bytes`, and one started with too little free space, each end in `needs_refresh` with no data loss.
- **Phase 5, Google Drive:** a file over 400 MB behind the virus-scan confirmation page completes, with a speed limit applied so it does not saturate the link.
- **Phase 5, logged-in download:** cookies and `Authorization` are forwarded and the file is correct, on a lab login server and one real site.
- **Phase 5, video playback:** a 10-minute 1080p HLS stream through the proxy has no added stalls compared with no proxy.
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
