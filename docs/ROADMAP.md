# Sayl — Roadmap

Claude Code updates these checkboxes at the end of each phase. Acceptance criteria live in `SPEC.md`.

## Phase 0 — Scaffold
- [ ] git repo, `.gitignore`, initial commit, GitHub remote (if `gh` is authenticated)
- [ ] uv project, ruff, mypy --strict, pytest, pre-commit
- [ ] pnpm workspace placeholder for `desktop/`
- [ ] GitHub Actions: lint + typecheck + tests
- [ ] `sayl doctor` (aria2c, ffmpeg, JS runtime for yt-dlp, certutil, wl-paste/xclip, session type)

## Phase 0.5 — Proof of concept (`poc/`)
- [ ] mitmproxy addon detecting downloads in `responseheaders`
- [ ] Re-request handoff to aria2c via RPC + 204 to the browser
- [ ] Stream-through handoff for single-use links
- [ ] Results table for 8+ real sites in `poc/RESULTS.md`
- [ ] Go / no-go decision recorded

## Phase 1 — Daemon core
- [ ] Config, XDG paths, token, logging with redaction
- [ ] SQLModel models + Alembic migrations
- [ ] Engine interface, Aria2Engine, YtDlpEngine
- [ ] Router
- [ ] Queue manager (concurrency, priorities, retries, speed limits)
- [ ] REST API + WebSocket events + OpenAPI schema
- [ ] Crash/restart recovery

## Phase 2 — CLI  (tag `v0.1.0`)
- [ ] `sayl add | ls | pause | resume | rm | info | watch | config | doctor`
- [ ] Integration tests against a real daemon

## Phase 3 — Desktop app
- [ ] Tauri 2 + Next.js static export, generated API client
- [ ] Downloads list, add dialog with quality picker, details view
- [ ] Settings, categories, i18n (ar RTL + en), themes
- [ ] Tray, notifications, floating drop target

## Phase 4 — Clipboard watcher
- [ ] Session detection
- [ ] Wayland (wl-paste), GNOME fallback (XWayland poll), X11 (XFixes)
- [ ] Filtering, debounce, ignore list, actionable notification

## Phase 5 — Capture proxy  (tag `v0.2.0`)
- [ ] Per-machine CA, install into system / NSS / Firefox profiles, uninstall
- [ ] System proxy toggle with save/restore
- [ ] Production addon: detection, both handoff strategies, bypass list
- [ ] Video grabber + floating bar
- [ ] StreamThroughEngine

## Phase 6 — IDM parity
- [ ] Scheduler, speed schedules, post-completion actions
- [ ] Auto categories, checksum verification, expired-link refresh UI
- [ ] Browser cookie import
- [ ] Site grabber, batch download, torrent/magnet, outbound proxy

## Phase 7 — Packaging  (tag `v1.0.0`)
- [ ] systemd user unit
- [ ] AppImage + .deb
- [ ] yt-dlp auto-update
- [ ] Bilingual README (English + Arabic), user guide, screenshots
