# Phase 5 report: Capture proxy

Date: 2026-11-03.

## Summary

`sayl-capture`, a mitmproxy process that sayld starts and supervises, hands browser downloads to sayld with no extension. The browser gets an empty `204`. The original response keeps streaming into the daemon with real back-pressure while Range workers accelerate the rest, and the upstream is closed once the file is complete. Videos that the proxy sees are offered in a floating bar.

**Result:** every Phase 5 acceptance criterion is met. The Google Drive run passed on a GitHub Actions runner (the `real-sites` workflow), because this machine's link had dropped to about 18 KB/s. Its first full attempt here found a real defect (see the table below), now fixed; the second was stopped at 184 MB at about 70 KB/s. One part is weaker than the rest: on the real logged-in site, the authorized request is a redirect, so sayld's own requests did not need Authorization. See [Known limitations](#known-limitations).

## Acceptance

| Criterion | Result | How |
|---|---|---|
| Google Drive, >400 MB, virus-scan page, speed limit | Pass (2026-11-14, CI runner, [run 43581630611](https://github.com/mohammed-w-mosabeh/sayl/actions/runs/43581630611)): `celeba.zip`, 1,307,393,821 bytes, ZIP header correct, hybrid engine, 3,279 s, average 398.7 KB/s under the 400 KB/s limit | `real_capture.py steps-drive.json --min-bytes 400000000 --magic 504b0304 --limit 400000`: headless Chromium clicks through the confirmation page, then the browser quits |
| Logged-in download, lab login server | Pass, Chromium HTTP/2 and Firefox HTTP/1.1 | `run_matrix.py <browser> <protocol> --target daemon`: `/private.bin` needs Basic auth and the session cookie on every request, and 3 of its 4 requests were Range requests from sayld |
| Logged-in download, one real site | Pass | A zipball of this private GitHub repo, with Basic auth from the `gh` token: 476 KB, SHA-256 identical to a direct download, `unzip -t` clean |
| 10-minute 1080p HLS, no added stalls | Pass | `hls_stalls.py --seconds 600`: 0 stalls direct, 0 through the proxy; startup 506 ms direct, 313 ms proxied; the manifest was reported to the video bar, nothing was captured as a download |
| Spool bound: over `capture_spool_max_bytes`, and too little free space | Pass | `tests/test_hybrid.py`: both end in `needs_refresh` with every byte kept |
| `sayl capture uninstall` leaves no CA and restores the proxy | Pass | `tests/test_capture_trust.py`, `test_capture_manager.py`, `test_capture_sysproxy.py`, `test_cli.py` |

The HLS run overlapped with the full pytest run and the Drive download. Load would make the proxied run worse, not better.

## Architecture

| Part | What it does |
|---|---|
| `daemon/capture/detect.py` | Navigations only: `Content-Disposition: attachment`, a known download extension, or a large binary body. Bypass list of hosts (with subdomains), globs, and paths; `DEFAULT_BYPASS` covers sign-in, payment, and browser-update hosts |
| `daemon/capture/ca.py`, `trust.py` | EC P-256 CA per machine, key `0600` in a `0700` dir. Installed into the Chrome NSS database, every Firefox profile (snap and Flatpak too), and the system store with one `pkexec` prompt; removed by name from all of them |
| `daemon/capture/sysproxy.py` | gsettings (GNOME family) and kioslaverc (KDE); the previous values are saved once as printed and written back unchanged |
| `mitm_adapter.py` | Each upstream reader sits behind a gate: above 4 MiB unsent to sayld the connection pauses, below 1 MiB it resumes, so TCP flow control holds the server. When sayld finishes early, the upload stops and the upstream is closed |
| `CaptureManager` | Runs `sayl-capture`, restarts it with backoff (gives up after five crashes in a minute), sets the system proxy only once the port answers, and restores it first on shutdown |
| Video bar | A hidden Tauri window shows the newest media page: the page itself for yt-dlp sites, otherwise its manifest with the page as Referer. Protected streams are never offered |

Measured back-pressure: with sayld limited to 2 MiB/s, once buffers filled the origin sent 6 MiB in 3 s and the proxy held exactly 4 MiB, instead of all 64 MiB at once.

## Tests

| Suite | Result |
|---|---|
| pytest | 306 passed, 3 skipped, coverage 92.0% |
| Vitest | 50 passed |
| `ui_check.py` (en, ar, ar-dark) | Pass |
| Lab matrix, `--target daemon` | Chromium HTTP/2 and Firefox HTTP/1.1 pass |

Testing found these defects, all fixed:

| Defect | Effect | Fix |
|---|---|---|
| Rate change to 0 while a transfer waited on the limiter | Division by zero; the download stalled forever | Waiters recheck the rate inside the lock and wake on change |
| mitmproxy tears down upstreams when the client disconnects | Quitting the browser killed handed-off downloads | The addon waits in `client_disconnected` until each upload has closed |
| Detached upstreams kept mitmproxy's per-host connection slot | After a few downloads from one site over one HTTP/2 connection, the next request waited forever | A detached upstream gives its slot back |
| mitmproxy buffered every response until its end | Video segments and large pages arrived late and sat in memory | Everything except media manifests streams through |
| One dropped Range connection abandoned acceleration, and a dead socket hung forever | A 1.3 GB Drive capture stopped after a network change | Range connections retry network errors (5 tries, backoff); a stream silent for 120 s counts as lost |
| A network outage longer than five retries (about 15 s) dropped every Range segment, finished ones too | On the first full Drive run, 600 MB downloaded counted as 44 MB, and the download asked for a new link | Range connections retry until 10 min pass without progress; giving up keeps every segment and ends in "the network dropped, resume to retry", which Resume continues |
| uvicorn waited for open `PUT /stream` requests | sayld could ignore SIGTERM indefinitely | 5 s grace, then cancel; the partial file is kept for recovery |
| An orphaned `sayl-capture` kept the port and a stale token | Every later sayld failed to start capture | `sayl-capture` exits when its parent is gone |
| Lab runners skipped `finally` on SIGTERM | Stopping a runner leaked sayld and aria2c | Runners turn SIGTERM into a normal exit |

## Decisions

- **No spool on the normal path.** Back-pressure holds the server instead of writing ahead. The bounded spool is used only for a paused capture on a link that cannot resume, where idling could make the server time out and lose the download.
- **Detection by headers only.** Bodies are never inspected, so the proxy cannot break a page by guessing.
- **A site that refuses the CA is bypassed automatically** (once, with a notification), so certificate pinning degrades to "not captured" instead of "site broken".
- **If sayld cannot be reached, the browser keeps the download.** A capture failure never loses a file.
- **The system proxy is set only once the proxy answers, and restored before anything else on shutdown**, so browsing never hits a dead port.

## Known limitations

- **Real-site Authorization is proven only on the first hop.** GitHub redirects the authorized `api.github.com` request to `codeload.github.com`, which uses a token in the URL and does not support Range, so the file came stream-through with no request from sayld. That sayld forwards cookies and Authorization on its own Range requests is shown by the lab login server on both browsers. GitHub serves raw files as text or images, which the browser renders instead of downloading, so they are not captured.
- **The system trust store was not changed on this machine.** It needs a `pkexec` prompt; the NSS and Firefox paths and the `pkexec` arguments are tested with fakes.
- **Chromium-based browsers that ignore the system proxy** (started with `--proxy-server` or a policy) are not captured.
- **uvicorn logs the shutdown cancel of an open capture as an ERROR traceback.** It is the intended 5 s grace.
