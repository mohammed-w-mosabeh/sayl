# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: [SemVer](https://semver.org/).

## [Unreleased]

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
