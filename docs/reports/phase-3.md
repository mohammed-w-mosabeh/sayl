# Phase 3 report: Desktop app

Date: 2026-11-03.

## Summary

The desktop app is a Tauri 2 shell around a Next.js static export. It holds no download state: the webview talks to `sayld` over HTTP and WebSocket on `127.0.0.1`, as the CLI does. Rust handles only what a webview cannot: reading the token, the tray, notifications, the floating drop target, and opening folders.

**Result:** the UI renders correctly in Arabic (RTL) and English, in light and dark themes, against a real `sayld`. This was checked by `desktop/scripts/ui_check.py` and by Vitest. The Rust shell is compiled, linted, and tested in CI only (see Known limitations).

## What the app does

| Area | Behaviour |
|---|---|
| Downloads list | Status filters with counts, categories, search, newest first. Progress, size, speed, and time left update live. |
| Add dialog | Ctrl+N, or paste a link anywhere outside a field. The link is probed as you type. Shows size, engine, and whether it can resume. |
| Quality picker | For yt-dlp links: one option per height, with estimated size, plus audio only, subtitles to embed, and playlist items. A link whose formats all need DRM shows "Protected content, not supported", and Add is disabled. |
| Details | Mode ("single connection" or "accelerated (N connections)"), link, folder, times, error, the `quality_degraded` warning (requested vs delivered), per-download speed limit and priority. |
| Expired links | A `needs_refresh` download shows a field for a fresh link (`refresh-url`). |
| Settings | Language, theme, global speed limit, notifications, drop target, clearing the host policy, categories, about. |
| Tray | Show, toggle the drop target, quit. Labels follow the UI language. Closing the window hides it, and downloads keep running in `sayld`. |
| Notifications | Completed, failed, link expired, lower quality, captured. Only the main window sends them. |
| Drop target | A small always-on-top window. Native drag-and-drop is off, so links dragged from a browser arrive as `text/uri-list`. |

## Daemon changes

- **CORS for the app only.** The webview runs on `tauri://localhost`, so its requests are cross-origin. Only the Tauri origins get CORS. `dev_origins` in `config.toml` adds loopback origins for `pnpm tauri dev`. Anything that is not `http://localhost:<port>` or `http://127.0.0.1:<port>` is refused at startup.
- **Typed responses.** `DownloadOut`, `ProbeOut`, and `InfoOut` make the OpenAPI schema precise, and the TypeScript client is generated from it. CI fails if the generated client is out of date. A test fails if `DownloadOut` drifts from the `Download` table.
- **`GET /info`** returns the version, the default download folder, and the concurrency limit.
- **New events:** `download.updated` (pause, resume, patch, refresh) and `download.removed`. Before this, a pause from the CLI never reached an open app.
- **Probe output is trimmed.** yt-dlp formats carry per-format `http_headers` and cookies. Only a fixed list of fields now leaves the daemon. The probe also lists playlist entries (flat, so a long playlist probes quickly) and subtitle languages.

## Tests

| Suite | Result |
|---|---|
| pytest (daemon and CLI) | 179 passed, 1 skipped |
| Vitest (desktop) | 43 passed |
| `ui_check.py`, en-light, ar-light, ar-dark | Pass: WebSocket connected, `dir` and theme correct, the add dialog probed a link, no console errors |
| CI `tauri` job | rustfmt, clippy `-D warnings`, and `cargo test` (3 tests), built with `--locked` |

The Vitest suites check that `ar.json` has exactly the keys of `en.json`, with the same placeholders. They also render the list and details in Arabic, and check that file names use `dir="auto"` and links stay `ltr` inside the RTL layout.

The first screenshots found three layout defects, all fixed:

| Defect | Fix |
|---|---|
| With details open, file names were cut to five characters | Time left moved under the progress bar, and that column was removed |
| A paused download of unknown size showed the pulsing "in progress" bar | The pulse shows only while bytes are moving |
| The Play icon was mirrored in Arabic | Media controls are not mirrored, so the flip was removed |

## Decisions

- **Plain `fetch` and `WebSocket`, not Tauri's HTTP plugin.** The same code runs in the app and in a browser during development, and the daemon keeps one origin policy.
- **The token never enters the page source.** In the app, Rust reads it from `~/.config/sayl/token` on request. In a plain browser, it is pasted once and kept in `sessionStorage` for that tab only.
- **Appearance is stored in the daemon's `ui` setting**, and cached in each webview so the first frame is already in the right language and theme.
- **Latin digits in Arabic.** Sizes and speeds then read the same as the file names and URLs beside them. Units and time words are translated.
- **System fonts only.** The app works offline; Noto Sans Arabic covers Arabic on most distributions.
- **TypeScript 5.9 and ESLint 9.** TypeScript 7 (the native compiler) and ESLint 10 are newer than the Next.js lint plugins support.
- **`unrs-resolver` may not run its install script.** pnpm 11 asks for each package. Its native binary already arrives as an optional dependency.

## Known limitations

- **Rust is not compiled on the development machine.** `Cargo.lock` comes from the first CI build. Its link could not download the toolchain, and the WebKit `-dev` headers need root. CI compiles and tests the shell on every pull request. The tray, notifications, folder picker, and drop target window have not been seen running on a desktop. To run the app locally: `sudo apt install libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev libssl-dev`, install Rust with rustup, then `cd desktop && pnpm tauri dev`.
- **Always-on-top is a request on Wayland.** GNOME may ignore it for the drop target window. Phase 4 covers the GNOME clipboard limits; this one belongs in the README's known limits (Phase 7).
- **Error messages come from the daemon in English**, even in the Arabic UI.
- **Categories are stored but not yet applied** to new downloads. Auto categories are Phase 6.
- **The app does not start `sayld`.** It shows how to start it. The systemd user unit is Phase 7.
