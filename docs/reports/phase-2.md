# Phase 2 report: CLI

Sayl v0.1.0. Date: 2026-11-02.

## Summary

The CLI is built and tested against a real `sayld`. Before that work, the speed limits were measured locally, YtDlpEngine gained quality-loss warnings, and the spool bound was added to the SPEC.

**Result:** all Phase 2 acceptance criteria are met. Test totals and CI status are in the pull request for this phase.

## Speed limits

Local origin at full speed, no internet. Pass means elapsed time is within ±15% of size / limit. Global is 1 MB/s (1,000,000 B/s) and per-download is 512 KiB/s.

| Engine | Limit | Size | Expected | Measured | Ratio |
|---|---|---|---|---|---|
| Aria2Engine | Global | 12.0 MiB | 12.58 s | 14.15 s | 1.125 |
| Aria2Engine | Per-download | 12.0 MiB | 24.00 s | 26.10 s | 1.087 |
| Aria2Engine | Global, after a slow period | 12.0 MiB | 12.58 s | 14.12 s | 1.122 |
| HybridEngine | Global, 3 Range connections | 4.0 MiB | 4.19 s | 4.18 s | 0.996 |
| HybridEngine | Per-download | 4.0 MiB | 8.00 s | 8.00 s | 1.000 |
| YtDlpEngine | Per-download, local HLS | 6.0 MiB | 11.99 s | 12.55 s | 1.046 |
| YtDlpEngine | Global, local HLS | 6.0 MiB | 6.29 s | 6.72 s | 1.069 |

The unlimited Aria2Engine and HybridEngine baselines finish in under a third of the limited time, so the limit is what slows these transfers down.

Testing found three real defects, all fixed:

| Defect | Effect | Fix |
|---|---|---|
| `config.speed_limit` was never applied | The configured limit did nothing until changed through the API | Applied at startup; a saved setting wins over the file |
| YtDlpEngine ignored the global limit | Video downloads ran unlimited | Added. Running downloads split it equally |
| aria2's overall limit is a moving average | After a slow period, 12 MB passed in 1 s, six times the limit | The global limit is also applied as a per-download cap |

Test design choices:

- aria2 finishes on a 1 s tick, which is 20% of a 4 s test, so its tests transfer 12 MiB. Its ratios sit near 1.12 for that reason.
- These tests run alone, without coverage. Tracing added about 0.1 s per HLS segment and failed the global case at a ratio of 1.24.
- The HLS fixture uses 512 KiB segments, as real streams do. With 128 KiB segments, fixed per-segment overhead pushed the ratio past the tolerance. The limit can run slightly slow on streams with very small segments.

## Quality warnings

| Case | Result |
|---|---|
| Delivered file matches the request | No warning |
| Height, codec, or audio falls short | `completed`, with `quality_degraded` |
| Signature or n-challenge solve fails, yt-dlp exits 0 | `completed`, with `quality_degraded` |
| Cap of `height<=1080`, source only has 720p | No warning: nothing was lost |
| `height=1080`, source only has 720p | `quality_degraded` |
| Metadata says 1080p, file is 240p | `quality_degraded`: `ffprobe` on the file wins |
| Playlist | Each item is checked |

The warning is stored on the download, sent as `download.quality_degraded`, and shown by `sayl info`. `sayl ls` marks it as `completed (warning)`.

These cases use a fake downloader and real `ffprobe`. No live YouTube download was run in this phase, so the `sayl info` output has not been seen with a real solver failure.

## CLI

| Command | Purpose |
|---|---|
| `sayl add` | Add a download, with format, audio, subtitle, playlist, and speed options |
| `sayl ls` | List downloads, optionally by status |
| `sayl pause`, `sayl resume` | Pause and resume |
| `sayl refresh` | Give a `needs_refresh` download a new link |
| `sayl rm` | Cancel and forget a download; finished files stay on disk |
| `sayl info` | Show one download and any warning |
| `sayl watch` | Follow live events, or one download until it ends |
| `sayl config` | Show or set settings |
| `sayl doctor` | Check the environment |

Commands accept any unique start of an id, and `ls`, `info`, and `add` accept `--json`.

## Decisions

- `sayl refresh` is not in the SPEC list. Without it, `needs_refresh` could not be resolved from the CLI.
- `sayl add` has no cookie option. Cookies on a command line end up in shell history and process lists. The API still accepts them.
- `sayl rm` never deletes finished files.

## Known limitations

- yt-dlp splits the global limit between running downloads when each starts, so shares are not rebalanced when one finishes.
- `sayl config` sets daemon settings, not `config.toml`. Only `speed_limit` currently takes effect.

## Spec changes

- The spool bound is in `docs/SPEC.md`: 512 MB default, a free-space check, and `needs_refresh` when reached. It is implemented in Phase 5.
