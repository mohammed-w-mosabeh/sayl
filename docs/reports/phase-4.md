# Phase 4 report: Clipboard watcher

Date: 2026-11-03.

## Summary

The daemon watches the clipboard and offers each new link in a desktop notification with **Download** and **Ignore this site** buttons. The daemon sends the notification itself over D-Bus, so this works with the desktop app closed.

**Result:** all Phase 4 acceptance criteria are met. A copied link is offered in under 1 s on X11 and in 0.32 s on a real GNOME Wayland session. The Wayland data-control check runs in CI under headless sway (wlroots) and, since 2026-11-14, a virtual KWin 5.27 (KDE).

## Watchers

| Session | Watcher | How it learns of a change |
|---|---|---|
| X11 | `x11` | XFixes selection-owner events via python-xlib, read with `add_reader` on the event loop |
| Wayland with data-control (wlroots, KDE) | `wayland` | `wl-paste --watch echo`: each line is a change |
| GNOME on Wayland | `xwayland_poll` | `xclip -o -selection clipboard` every 700 ms |
| X11 without XFixes | `poll` | Same poll |

If a watcher is refused, the next one is tried. For example, a compositor without data-control makes `wl-paste --watch` exit, and Wayland falls back to the XWayland poll. If none works, `GET /info` reports `clipboard: unavailable`, and the desktop app's Settings say so.

Content is read only after a change, and only after checking its types. Anything a password manager marks with `x-kde-passwordManagerHint`, and anything that is not text, is never read.

## Filtering

| Rule | Detail |
|---|---|
| Links only | Exactly one `http`, `https`, `ftp`, or `magnet` (with `xt=`) link, with no other text, up to 4096 characters |
| Not at startup | What is on the clipboard when watching begins is never offered |
| Debounce | The same link is not offered twice within 10 s, even with other copies in between |
| Ignore list | `clipboard_ignore`: a host, which includes its subdomains, or a glob over the whole link (`*.pdf`) |
| Already in Sayl | A link already in the list is not offered, so the app's own "Copy link" stays quiet |

## Measurements

| Case | Budget | Measured | Where |
|---|---|---|---|
| X11, XFixes | 1 s | under 1 s (test asserts) | Xvfb, locally and in CI |
| GNOME Wayland → XWayland poll | 2 s | 0.32 s, three of three | This machine's real session: `wl-copy`, then detection by the poll |
| XWayland poll, any X server | 2 s | under 2 s (test asserts) | Xvfb |
| wlroots Wayland, data-control | 1 s | test asserts under 1 s | Headless sway in CI; sway is not installed here |
| KDE Wayland, data-control | 1 s | test asserts under 1 s | Virtual KWin 5.27 in CI (`kwin_wayland --virtual`); added 2026-11-14 |
| Copy → notification → Download → file complete | — | notification under 1 s; file correct | Xvfb, private D-Bus, a fake notification server, and a real `sayld` |

The real GNOME measurement saved the clipboard first (text only), copied three test links, then restored the original content and confirmed it matched.

## Tests

| Suite | Result |
|---|---|
| pytest | 218 passed, 2 skipped (sway, and the existing large-file test) |
| Vitest | 44 passed |
| `ui_check.py` (en, ar, ar-dark) | Pass |

Tests never touch the real desktop. `conftest.py` removes `DISPLAY`, `WAYLAND_DISPLAY`, `XDG_SESSION_TYPE`, and `DBUS_SESSION_BUS_ADDRESS`, so the daemon under test has no clipboard and no notifications. Tests that need them start Xvfb, headless sway, or a private `dbus-daemon` with no service activation, so no real notification daemon starts. In CI, `SAYL_REQUIRE_TOOLS=1` turns a missing display server into a failure instead of a skip. `ui_check.py` also hides these variables from its daemon now.

Testing found three defects, all fixed:

| Defect | Effect | Fix |
|---|---|---|
| python-xlib builds its own extension event classes | `isinstance(event, xfixes.SetSelectionOwnerNotify)` was always false, so X11 never saw a change | Match the event code (`first_event` + `XFixesSelectionNotify`) and the subtype |
| Any notification action consumed the pending link | Clicking the body, then pressing Download, did nothing | Only Download and Ignore consume it |
| A finished watcher task counted as running | After `unavailable`, turning the setting off and on never restarted watching | Restart when the task is done |

## Decisions

- **The daemon notifies, not the app.** This matches "closing the UI never stops downloads": a copied link is offered with the app closed. jeepney is pure Python, already present via keyring, and runs on the event loop.
- **The Download button queues the link with default options.** For yt-dlp links that is the best quality. Choosing a quality needs the app's Add dialog.
- **Clicking the notification body does nothing**, so a stray click never starts a download.
- **`wl-paste` and `xclip` only signal or read.** Content is read once per change, so a large clipboard is not copied every 700 ms, and a read is capped at 64 KiB.
- **Watching is on by default**, as in IDM. It can be turned off in Settings or with `sayl config clipboard_enabled false`.

## Known limitations

- **Notification text follows the app language**, but daemon error messages are still English.
- **A link copied twice more than 10 s apart is offered again**, unless it was already added to Sayl.
- **The daemon needs the session's environment.** Started from a terminal it has it. The systemd user unit (Phase 7) must import `DISPLAY`, `WAYLAND_DISPLAY`, `XDG_SESSION_TYPE`, and `DBUS_SESSION_BUS_ADDRESS`, or the watcher reports `unavailable`.
- **GNOME is polled**, at about two `xclip` runs per second. This is the documented fallback for a compositor without data-control.
