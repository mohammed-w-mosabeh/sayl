# Sayl — Roadmap

Claude Code updates these checkboxes at the end of each phase. Acceptance criteria live in `SPEC.md`.

## Phase 0 — Scaffold
- [x] git repo, `.gitignore`, initial commit, GitHub remote (if `gh` is authenticated)
- [x] uv project, ruff, mypy --strict, pytest, pre-commit
- [x] pnpm workspace placeholder for `desktop/`
- [x] GitHub Actions: lint + typecheck + tests
- [x] `sayl doctor` (aria2c, ffmpeg, JS runtime for yt-dlp, certutil, wl-paste/xclip, session type)

## Phase 0.5 — Proof of concept (`poc/`)
- [x] mitmproxy addon detecting downloads in `responseheaders`
- [x] Re-request handoff to aria2c via RPC + 204 to the browser
- [x] Stream-through handoff for single-use links
- [x] Results table for 8+ real sites in `poc/RESULTS.md`
- [x] Go / no-go decision recorded: GO (see `poc/RESULTS.md`)

## Phase 1 — Daemon core
- [x] Config, XDG paths, token, logging with redaction
- [x] SQLModel models + Alembic migrations
- [x] Engine interface (with `attach`), Aria2Engine, YtDlpEngine
- [x] HybridEngine: StreamThroughEngine, range workers, segment map, host policy
- [x] Stream attach API for sayl-capture
- [x] Lab acceptance: one-time, session-bound, and Range-capable links each complete with a correct hash
- [x] Router
- [x] Queue manager (concurrency, priorities, retries, speed limits)
- [x] REST API + WebSocket events + OpenAPI schema
- [x] Crash/restart recovery
- [x] Lost streams resume by Range, or wait in `needs_refresh` for a new link
- [x] Acceptance: browser lab through the daemon (Chromium + Firefox, HTTP/1.1 + HTTP/2)
- [x] Acceptance: real YouTube 1080p merge, and 5 selected items of a playlist

## Phase 2 — CLI  (tag `v0.1.0`)
- [x] `sayl add | ls | pause | resume | refresh | rm | info | watch | config | doctor`
- [x] Integration tests against a real daemon
- [x] Speed limits proven locally for all three engines
- [x] `quality_degraded` warnings for YtDlpEngine

## Phase 3 — Desktop app
- [x] Tauri 2 + Next.js static export, generated API client
- [x] Downloads list, add dialog with quality picker, details view
- [x] Settings, categories, i18n (ar RTL + en), themes
- [x] Tray, notifications, floating drop target
- [x] Acceptance: the UI renders correctly in Arabic RTL (`desktop/scripts/ui_check.py`)

## Phase 4 — Clipboard watcher
- [x] Session detection
- [x] Wayland (wl-paste), GNOME fallback (XWayland poll), X11 (XFixes)
- [x] Filtering, debounce, ignore list, actionable notification
- [x] Acceptance: a copied link is offered within 1 s on X11 and wlroots Wayland, and within 2 s on GNOME Wayland

## Phase 5 — Capture proxy  (tag `v0.2.0`)
- [x] Per-machine CA, install into system / NSS / Firefox profiles, uninstall
- [x] System proxy toggle with save/restore
- [x] Production addon: detection, stream-first handoff to HybridEngine, bypass list
- [x] Real back-pressure in `mitm_adapter.py`, and cancelling the upstream once the daemon finishes
- [x] Spool bound: 512 MB default, free-space check, `needs_refresh` when reached
- [x] Video grabber + floating bar
- [x] Acceptance: >400 MB Google Drive file with the virus-scan page completes under a speed limit
- [x] Acceptance: logged-in download forwards cookies and Authorization (lab login server + one real site)
- [x] Acceptance: no added stalls on a 10-minute 1080p HLS stream, compared with no proxy

## Phase 6 — IDM parity
- [x] Scheduler, speed schedules, post-completion actions
- [x] Auto categories, checksum verification, expired-link refresh UI
- [x] Browser cookie import
- [x] Site grabber, batch download, torrent/magnet, outbound proxy
- [x] Acceptance: a scheduled queue starts and stops on time and runs the post action

## Phase 6b — Feature list gaps
- [x] Per-site credentials, sealed with the vault key
- [x] History export and import
- [x] Statistics

## Phase 7 — Packaging  (tag `v1.0.0`)
- [x] systemd user unit
- [x] AppImage + .deb
- [x] yt-dlp auto-update
- [x] Bilingual README (English + Arabic), user guide, screenshots
