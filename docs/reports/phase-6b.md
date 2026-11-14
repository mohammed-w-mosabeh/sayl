# Phase 6b — Feature list gaps

The SPEC feature list names three features that no phase covered: per-site credentials, history export and import, and statistics. They were built before Phase 7 so packaging, the guide, and the screenshots describe the finished feature set.

**Result:** all three work through the API, the CLI, and the desktop app, in English and Arabic. The SPEC now describes each one ("Site credentials", "History export and import", "Statistics").

## Decisions

| Decision | Why |
|---|---|
| Passwords are sealed with the vault key (keyring, file fallback), in a `credential` table | Same protection as saved headers, and it works headless. One keyring entry per site would fail without a keyring |
| The API and CLI never return a password | A login can be replaced or removed, never read back |
| aria2 downloads with a login follow redirects in sayld first (one HEAD) | aria2 resends custom headers on every redirect. httpx drops `Authorization` when a redirect leaves the origin, so only the final same-host URL gets the login |
| yt-dlp gets the login as `username`/`password` | yt-dlp sends `http_headers` to CDNs too; its site login is what video sites need |
| `sayl creds set` prompts for the password, or reads `--password-stdin` | A password on the command line ends up in shell history and process lists |
| Import never starts anything | Unfinished aria2 and yt-dlp entries arrive paused; captures arrive in `needs_refresh`, since their stream is gone |
| `POST /history/save` writes the export to a path from the save dialog | The webview has no file access. sayld runs as the same user and already writes downloads where it is told |

## Tests

| Check | Result |
|---|---|
| A download behind HTTP Basic auth, through real aria2, with the stored login | Completes, SHA-256 matches |
| A redirect to another host name | The other host receives no `Authorization`. With the redirect step removed, the test fails |
| Export of a row with sealed cookies | The file contains neither the cookie nor `headers_enc` |
| Import of an active, a queued capture, and a `../../.bashrc` name | Paused, `needs_refresh`, `bashrc`; nothing starts |
| pytest | 440 passed, 4 skipped, coverage 91.7% |
| Vitest | 57 passed |
| `ui_check.py` (en, ar, ar-dark) | No problems; new screenshots of Site logins and Statistics |

## Known limitations

- **A login matches one exact host.** `example.com` does not cover `files.example.com`; add each host.
- **aria2 logins cost one HEAD request** per start, to resolve redirects.
- **Statistics count completed downloads** by their finish time in UTC, so a day boundary follows UTC, not local time.
