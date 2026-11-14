# Phase 6 report: IDM parity

Date: 2026-11-03.

## Summary

Phase 6 adds:

- **Scheduling:** a scheduler with speed schedules and post-completion actions, and queue start and stop.
- **Organizing downloads:** auto categories with per-category folders, and checksum verification.
- **Adding downloads:** browser cookie import for every download, batch download, and a site grabber.
- **Connections:** proper torrent and magnet handling, and an outbound HTTP or SOCKS5 proxy for every connection.

Each feature is in the daemon, the CLI, and the desktop app, in English and Arabic.

**Result:** the Phase 6 acceptance criterion is met. A scheduled queue starts on time, at the second the cron names, and stops on time. It then runs the post action once the queue is empty. This is tested against a real daemon with a real aria2 download, and the post action is a real command.

Phase 6 was started before Phase 5 was merged, at the user's request. Phase 5 was merged and tagged `v0.2.0` on 2026-11-14, and `phase/6-idm-parity` was then rebased onto `main`.

## Features

| Feature | Daemon | CLI | Desktop |
|---|---|---|---|
| Scheduler | `/schedules` (validated, `next_run`), `/schedules/{id}/run` | `sayl schedule ls\|add\|rm\|run` | Settings: time and days (incl. Sunday to Thursday) or cron, action, limit, post action |
| Queue control | `/queue`, `/queue/start`, `/queue/stop` | `sayl queue status\|start\|stop` | Header button; a banner when stopped |
| Post actions | Shutdown and suspend through logind, or a command; 60 s to cancel | `sayl queue cancel` | Banner with Cancel |
| Auto categories | Six defaults; matched on add, capture, or completion | — | Translated names, folder per category |
| Checksums | `checksum` on add, check on completion, `/verify` | `sayl add --checksum`, `sayl verify` | Field on add; state and Verify in details |
| Browser cookies | `cookies_from_browser` for any download, `/browsers` | `--cookies-from-browser` | "Use cookies from" on add |
| Batch | `/batch` with links and patterns | `sayl batch` (args, file, stdin) | Batch dialog |
| Site grabber | `/grab-site` (find, then pick) | `sayl grab [--queue]` | Grab dialog with a checklist |
| Torrents | Magnet follow-through, bounded seeding, local `.torrent` | `sayl add file.torrent` | (as any download) |
| Outbound proxy | `outbound_proxy`: HTTP, HTTPS, SOCKS5 | `sayl config outbound_proxy …` | Settings |

## Tests

| Suite | Result |
|---|---|
| pytest | 412 passed, 3 skipped, coverage 91.5% |
| Vitest | 55 passed |
| `ui_check.py` (en, ar, ar-dark) | Pass. New screenshots cover the batch and grab dialogs, the stopped queue, schedules, and the proxy |

The new tests use real components wherever that is practical:

- **Scheduler:** a real sayld with real aria2. The queue is stopped, a download waits, and a six-field cron starts the queue at a set second. The download starts within 2 s of that second, and the post-action command creates its marker file. Another schedule stops the queue at a set second and pauses a running download.
- **Torrents:** BitTorrent over loopback with a tracker written in the test and a seeding `aria2c`. Downloads come from a local `.torrent` and from a magnet, which fetches its metadata from the seeder, and they go through the queue.
- **Outbound proxy:** a SOCKS5 server written in the test, with a login. An aria2 download and a captured download's Range requests both go through it, and the proxy records every target.
- **Browser cookies:** a Firefox `cookies.sqlite` built in a temporary profile. The origin receives the imported cookie together with an explicit one.
- **Site grabber:** a fake site through httpx's `MockTransport`, with `robots.txt`, nested pages, another host, and files with and without sizes.

Testing found two defects, both fixed:

| Defect | Effect | Fix |
|---|---|---|
| crontab weekday `0-4` became APScheduler's `sun-thu`; APScheduler counts from Monday | A Sunday-to-Thursday schedule was refused | Every weekday field expands to explicit day names; a range ending on Sunday runs through the weekend |
| The Add dialog always sent the default folder | Downloads added from the app ignored their category's folder | The folder is sent only when the user changes it |

## Decisions

- **The site grabber only finds files.** The user chooses what to download. A crawl that queued everything could start thousands of downloads by surprise.
- **Post actions are careful.** They follow only a `start_queue` schedule, and never fire if the queue had nothing to do. They wait 60 s before running and can be cancelled from the CLI or the app. Shutdown and suspend are non-interactive logind calls, so logind's policy decides, and there is never a password prompt at 2 AM.
- **Default categories have no folder of their own.** Turning Phase 6 on doesn't move where files land.
- **A seeding torrent counts as completed.** The files are usable, seeding doesn't hold a download slot, and it stops at ratio 1.0 or after 60 minutes.
- **SOCKS5 for aria2 goes through a local CONNECT bridge.** aria2 has no SOCKS support, and this keeps one proxy setting for every engine.
- **Browser cookies are read once a minute at most**, so a batch of a thousand links reads the browser's store once.

## Known limitations

- **Peer traffic for torrents doesn't use the outbound proxy.** aria2 sends only HTTP and FTP through it; BitTorrent peers and DHT connect directly. Tracker announces over HTTP do use the proxy.
- **A captured download's original stream follows the browser's own proxy settings.** Only Sayl's extra Range requests use `outbound_proxy`.
- **Shutdown and suspend were tested with a fake.** The real logind call was not made, since it would power off this machine. If logind refuses (polkit), the failure is logged and nothing happens.
- **The site grabber doesn't run JavaScript.** Links that only a script builds aren't found.
- **Reading Chrome's cookies may unlock the desktop keyring once.** That's the keyring's own prompt, handled by yt-dlp's extractor.
- **The proxy password is stored in the settings database**, like other settings. It is never logged and never sent anywhere but the proxy.
- **A seeding torrent removed from the list keeps seeding** until its ratio or time limit.
