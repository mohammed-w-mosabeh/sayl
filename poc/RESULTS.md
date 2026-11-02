# Phase 0.5 results

**Decision: GO for Phase 1.** No site failed because of capture. The SPEC's stop rule (more than
2 capture failures) was not triggered.

Tested 2026-11-02 on Debian 13 with mitmproxy 12.3.1, Chromium 1243 (Playwright, headless) and
Firefox 153.5 ESR (Marionette, headless), over a roughly 3 Mbit/s Wi-Fi link. Reproduce with
`poc/lab/` and `poc/run.py`.

## Real sites

"Pass" means the proxy detected the download, handed it off, and the browser recorded no download
of its own.

| # | SPEC target | Site | Chromium | Firefox | Handoff | Evidence |
|---|---|---|---|---|---|---|
| 1 | GitHub release | ripgrep 14.1.1 asset | Pass | Pass | Stream-through (signed URL) | SHA-256 matches the published `.sha256`, all 3 runs |
| 2 | SourceForge | 7-Zip 24.08 via `netix.dl.sourceforge.net` | **Blocked** | Pass | Re-request | `.xz` CRC valid, 1,556,956 bytes |
| 3 | Google Drive | 499 MB file behind the virus-scan page | Pass, partial | Pass, partial | Re-request | 16 connections, ZIP magic bytes; **not run to completion** |
| 4 | Mediafire | `ID_Testing.zip` | Pass | Pass | Re-request | ZIP integrity test passes |
| 5 | Linux ISO | TinyCore 16.2, 25 MB | Pass | Pass | Re-request | MD5 matches the published value, up to 16 connections |
| 6 | POST download | httpbin form POST | Pass | Pass | Stream-through | 134 of 134 bytes; synthetic endpoint |
| 7 | Signed URL | GitHub `release-assets` | Pass | Pass | Stream-through | Same transfer as row 1 |
| 8 | YouTube grabber | Big Buck Bunny page | Pass | Pass | `video-page` event only | Page URL reported; no file captured, as designed |
| + | HLS manifests | hls.js demo | Pass | Pass | Observed only | Master and variant `.m3u8` reported with the page host |

Normal browsing kept working after each capture: Wikipedia loaded before and after every matrix.

### Row 2, Chromium: not a capture failure

SourceForge served a Cloudflare 403 to headless Chromium **with no proxy at all**, so Sayl never
got as far as a download. The same step passes in Firefox.

### Other things that looked like failures

- **7-zip.org** could not be reached from this network, directly or through the proxy. SourceForge's
  copy of the same project stands in for it.
- **`libpng/.../zlib-1.3.1.tar.gz/download`** redirected Firefox to the file listing with no
  download. That file was replaced by the row 2 URL; the cause was not investigated further.
- **Slow pages and timeouts** on GitHub, YouTube and the httpbin root page also happen without the
  proxy. GitHub took 41 s direct and 28 s proxied, and YouTube 30 s direct and 41 s proxied. The
  httpbin root page timed out at 60 s both ways, so the POST test starts from `/robots.txt`.
- **A 211 s GitHub transfer** was the CDN, not the proxy: a direct curl of the same file ran at
  21 KB/s. In that first Chromium run the page also ended on `chrome-error://` after
  `ERR_NETWORK_CHANGED`. A clean retest stayed on the page, and the matrix was not affected.

## Single-use links (local TLS lab)

The lab origin returns 410 for a second request to `/once.bin`, and counts every hit. Each file is
8 MiB and every saved file matched the origin's SHA-256.

| Run | `/once.bin` hits | once | post | no-range | range (aria2) | Browser downloads |
|---|---|---|---|---|---|---|
| Chromium, HTTP/2 | 1 | Match | Match | Match | Match | Not captured by this run's output |
| Firefox, HTTP/2 | 1 | Match | Match | Match | Match | 0 |
| Chromium, HTTP/1.1 | 1 | Match | Match | Match | Match | Not captured by this run's output |

The matrix also navigates away during each transfer and closes the browser during the last one.
The downloads still finished, so stream-through survives the browser leaving.

## Limitations of this evidence

- **Drive was not completed.** The transfer was stopped to free the link, so the bytes after the
  first segment are unverified.
- **Cookie forwarding has not been proven on a login session.** Every site was public, in a
  throwaway profile. The SPEC acceptance test needs a real authenticated download.
- **No CA was installed.** The browsers ran with certificate errors ignored, so the CA and system
  proxy flow is still untested. That work belongs to Phase 5.
- **Everything ran headless.** "The browser shows nothing" rests on the drivers' download reports,
  not on a look at a real browser window.
- **A true single-use link was tested only in the lab.** The GitHub URL is signed but reusable.
- **Video playback through the proxy was not measured.** Manifests were reported and the pages
  loaded, but nobody confirmed that playback stayed smooth.

## What Phase 5 must carry forward

- **`detach.py` patches mitmproxy internals** and refuses any version but 12.3.1. This is the main
  risk: the handoff cannot be built on stock mitmproxy. Pin the version and keep the lab matrix as
  a regression test for every upgrade.
- **`looks_single_use` only reads query parameters.** Mediafire's path-embedded token went to
  re-request. That worked here, but a true one-time path token would fail on aria2's second request.
- **Re-request costs a second upstream request.** The original is cancelled once headers arrive.
- **Plain-HTTP navigation detection needs a wider test.** Browsers omit `Sec-Fetch-*` there, so
  `is_interceptable` falls back to the `Accept` header. Only TinyCore exercised that path.
