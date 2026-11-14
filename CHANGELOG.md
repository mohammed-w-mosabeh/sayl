# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: [SemVer](https://semver.org/).

## [Unreleased]
- Fixed: a captured download could be reported as active after it had finished, when the queue's poll and the stream handler both saw the end. Progress is written only to unfinished rows, and each download's progress is recorded one update at a time.
- Phase 6 scheduler: cron schedules start or stop the queue, or set the speed limit (five crontab fields, Sunday as 0 or 7, or six with seconds). Stopping the queue pauses what runs and starts nothing new; captured streams keep going. A start_queue schedule can shut down, suspend (through logind), or run a command once its queue is empty, after 60 s in which it can be cancelled; an empty queue never triggers it.
- Auto categories: six defaults, matched by extension or MIME type when a download is added, captured, or (for a name only known at the end) completed. A category's folder applies unless the user chose one.
- Checksums: `checksum` on add (`sha256:HEX`, aria2's `sha-256=HEX`, or bare hex), checked when the download completes and on demand (`POST /downloads/{id}/verify`, `sayl verify`).
- Browser cookie import for every download, not only yt-dlp: Firefox and the Chromium family, read once a minute at most. `GET /browsers` lists the browsers found.
- Batch download (`POST /batch`, `sayl batch`): links and patterns such as `file[001-100].jpg`, up to 10,000 at once.
- Site grabber (`POST /grab-site`, `sayl grab`): crawls one host by depth, type, and size, honours robots.txt, and lists the files; the user picks which to download.
- Torrents and magnets: a magnet follows its metadata to its files; a finished torrent counts as completed and seeds in the background until ratio 1.0 or 60 minutes; local `.torrent` files.
- Outbound HTTP or SOCKS5 proxy for every connection, aria2 included through a local CONNECT bridge.
- Desktop app: schedules, queue control and post-action banner, batch and grab dialogs, checksum and browser cookies on add, Verify, proxy, and per-category folders.

## [0.2.0] - 2026-11-14
- CI: a `real-sites` workflow runs the Google Drive acceptance test (1.3 GB behind the virus-scan page, 400 KB/s limit) on demand.
- Phase 5 capture proxy: `sayl-capture` (mitmproxy, run and supervised by sayld) hands browser downloads to sayld with no extension. The browser gets an empty 204 and the original response keeps streaming into the daemon, with real back-pressure, and is cut off once Range workers finish the file.
- Per-machine CA installed into the system store, NSS, and Firefox profiles, and removed again; system proxy toggle for GNOME and KDE that restores the exact previous values. `sayl capture status|enable|disable|install-ca|uninstall`.
- Detection by headers only, a bypass list (sign-in, payment, browser updates), and automatic bypass of a site that refuses the CA.
- Floating video bar: videos sayl-capture sees (yt-dlp sites and HLS/DASH manifests) are offered with their qualities. Protected streams are never offered.
- A paused capture whose link cannot resume keeps writing ahead, bounded by `capture_spool_max_bytes` (512 MB) and free space, then waits in `needs_refresh`.
- Robustness: downloads survive the browser quitting; several held downloads no longer stall later requests to the same site; uncaptured responses stream through instead of being buffered; dropped Range connections retry; a silent stream counts as lost after 120 s; SIGTERM no longer waits for open captures; sayl-capture exits if sayld is killed. A network outage no longer discards finished Range segments; such a download waits with "the network dropped, resume to retry" and Resume continues it.
- Phase 4 clipboard watcher: copied links are offered in a desktop notification with Download and Ignore this site buttons, even with the app closed. Works on X11 (XFixes), Wayland with data-control (`wl-paste --watch`), and GNOME on Wayland (XWayland poll).
- Links only (http, https, ftp, magnet), debounced for 10 s, with an ignore list. Content a password manager marks secret is never read. Settings and the watcher in use are shown in the desktop app.
- Phase 3 desktop app: Tauri 2 shell around a Next.js static export, with a live downloads list, add dialog with a yt-dlp quality, subtitle, and playlist picker, details view, expired-link refresh, settings, and categories.
- Arabic (RTL) and English UI, light and dark themes, tray, notifications, and a floating drop target for links.
- API: typed responses and a generated TypeScript client, `GET /info`, `download.updated` and `download.removed` events, and CORS for the app's own origin only (`dev_origins` for development).
- The yt-dlp probe no longer returns per-format headers or cookies, and lists playlist entries and subtitle languages.

## [0.1.0] - 2026-11-02
- Phase 2 CLI: `sayl add`, `ls`, `pause`, `resume`, `refresh`, `rm`, `info`, `watch`, `config`, and `doctor`.
- YtDlpEngine warns with `quality_degraded` when the delivered file is below the request or a solver fails.
- Speed limits measured locally for every engine. The configured limit now applies at startup, reaches yt-dlp, and holds on aria2 after a slow period.
- Earlier work:
- Project specification, roadmap, and Claude Code instructions.
- Phase 0 scaffold: uv project, ruff, mypy strict, pytest, pre-commit, CI, pnpm workspace placeholder, and `sayl doctor`.
- Phase 0.5 proof of concept: mitmproxy capture addon with re-request and stream-through handoffs, a TLS test lab, and real-site results in `poc/RESULTS.md`. Decision: go.
- Stream-first capture spec, isolated mitmproxy adapter with a pinned version, and a CI lab matrix for Chromium and Firefox on HTTP/1.1 and HTTP/2.
- Phase 1 daemon core: config, redacted logs, database, HybridEngine, Aria2Engine, YtDlpEngine, router, queue, REST API, WebSocket events, OpenAPI schema, and crash recovery.
- Captured downloads survive a lost stream: missing segments resume by Range, or the download waits in `needs_refresh` for `refresh-url`.
- PoC addon can hand captures to the daemon; the browser lab runs through a real `sayld` in CI.
- `sayl doctor` warns about a missing keyring and a missing yt-dlp challenge solver; yt-dlp-ejs is now a dependency.
