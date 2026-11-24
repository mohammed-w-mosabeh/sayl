# Sayl user guide

This guide covers installing Sayl, catching downloads from your browser, and every feature of
the app and the `sayl` command line. For a short overview, see the [README](../README.md).

## How Sayl fits together

- **sayld** is the background service. It owns every download, the queue, and all settings.
  It keeps running when you close the window, and it starts again with your desktop session.
- **The Sayl window** and the **`sayl` command** are two ways to control sayld. Anything one of
  them does shows up in the other at once.
- **sayl-capture** is a small proxy that sayld runs on 127.0.0.1 when capture is on. It hands
  browser downloads to sayld and lets everything else pass through untouched.

Where things live:

| What | Where |
| --- | --- |
| Settings file | `~/.config/sayl/config.toml` |
| API token (mode 0600) | `~/.config/sayl/token` |
| Database, logs, capture certificate | `~/.local/share/sayl/` |
| Downloads | your Downloads folder (`xdg-user-dir DOWNLOAD`), or a category's folder |

## Installing

See [Install](../README.md#install) in the README for the `.deb`, the AppImage, and running from
source. Then run:

```bash
sayl doctor
```

It checks `aria2c`, `ffmpeg`, a JavaScript runtime for YouTube, the tools capture needs, the
keyring, and your session type. Each line it marks as missing comes with the install command for
your distribution. With the AppImage, put `./Sayl_*_amd64.AppImage` in front of every `sayl`
command in this guide, for example `./Sayl_*_amd64.AppImage sayl doctor`.

### Running sayld

Opening the Sayl window starts sayld if it is not running. Behind the scenes it runs:

```bash
sayl service install     # enable sayld at login and start it now
sayl service status      # enabled? running? which sayld?
sayl service restart     # restart it, for example after a yt-dlp update
sayl service uninstall   # stop it and remove it from your session
```

`sayl service install` is safe to run again. If you moved the AppImage, it points the service at
the new location. sayld starts with your graphical session (GNOME and KDE handle this). On sway
and other wlroots compositors, add the two lines from the README to your configuration.

You can also run `sayld` in a terminal yourself. Stop it with Ctrl+C.

## Adding downloads

### From the window

Click **Add**, paste a link, and Sayl probes it: you see the size, which engine will handle it,
and whether it can resume. For a video page you also get the list of qualities. Choose a file
name, a folder, a speed limit, an optional checksum, and whether to send the cookies of a
browser you are logged in with.

### From the command line

```bash
sayl add https://example.com/file.iso
sayl add https://example.com/file.iso --dest ~/ISOs --limit 2000000 --checksum sha256:4f1c...
sayl add "magnet:?xt=urn:btih:..."
sayl add ~/Downloads/ubuntu.torrent
```

Useful options: `--filename`, `--paused`, `--referer`, and `--cookies-from-browser firefox`
(or `chrome`, `chromium`, `brave`, `edge`, `opera`, `vivaldi`). `sayl add --help` lists them all.

### Copying a link

With the clipboard watcher on (it is on by default), copying a link shows a notification with
**Download** and **Ignore this site**. Only `http`, `https`, `ftp`, and `magnet` links are offered,
the same link is not offered twice in 10 seconds, and links already in Sayl are skipped. Text a
password manager marks as secret is never read.

On GNOME under Wayland, Sayl checks the clipboard every 0.7 seconds, so the notification can
take up to 2 seconds. Settings → Clipboard shows which watcher is in use, and holds the list of
ignored sites.

### The drop target

Settings → Downloads → **Show the floating drop target** opens a small window you can drop links onto
from any application.

## Catching browser downloads (capture)

Capture works without a browser extension: your browser talks to a proxy on your own machine,
and when a response is a file download, the proxy hands it to sayld instead of the browser.
Normal browsing, video playback, and logins go through unchanged.

```bash
sayl capture install-ca   # create this machine's certificate and trust it
sayl capture enable       # start the proxy and point the desktop proxy setting at it
sayl capture status       # running? which stores trust the certificate?
sayl capture disable      # stop the proxy and restore your previous proxy setting
sayl capture uninstall    # all of the above, and remove the certificate everywhere
```

- `install-ca` trusts the certificate in Chrome and Firefox (through their certificate
  databases, which needs `certutil` from `libnss3-tools`), and in the system store, which asks
  for your password. Pass `--no-system` to skip the system store.
- The certificate is generated on your machine, its private key is readable only by you, and it
  never leaves the machine.
- `enable --no-system-proxy` starts the proxy without touching the desktop setting, if you
  prefer to point a browser at `127.0.0.1:6972` yourself.

**Only browsers use it.** On GNOME, Sayl sets the desktop proxy to *automatic* with a small
proxy auto-config (PAC) file that sayld serves. Browsers follow it; terminals, `curl`, `git`,
package managers, and API clients do not, because GNOME Terminal exports `https_proxy` only in
*manual* mode. If the proxy stops, the PAC file sends browsers direct, so browsing never breaks.
Sayl adds no firewall (nftables/iptables) rules and no hosts entries. When sayld stops, crashes, or
is killed, the previous setting comes back (on stop, on the next start, or from the systemd unit).
If a terminal opened while an older Sayl used manual mode still has `https_proxy` set, open a new
terminal.

**Passthrough hosts.** A passthrough host is never decrypted: the proxy reads the host name from
the TLS hello and forwards the encrypted connection to the real server unchanged, so clients see
the real certificate. Built in: `anthropic.com` and `claude.ai`, with their subdomains. Nothing
else, so downloads from GitHub, PyPI, npm, Docker and the rest are still captured. Add more in
`capture_passthrough` in `config.toml` (an entry covers its subdomains; `*` globs), or in
Settings. A site that refuses Sayl's certificate with an "unknown CA" error is added for you,
once, with a notification. `sayl doctor` checks the proxy, the certificate, and that
`api.anthropic.com` passes through untouched.

A captured download keeps the browser's own connection, so links that work only once still
complete. When a server allows it, Sayl adds more connections to speed the rest up.

**What cannot be captured:** downloads a page assembles with JavaScript (`blob:` and `data:`
links, as on MEGA) and DRM-protected streams. The browser keeps handling those.

### Videos while you browse

When capture is on and you play a video on a supported site, a small bar offers the available
qualities. Turn it off in Settings → Downloads → **Offer videos I play in a floating bar**.

## Videos, audio, and playlists

Sayl downloads from video sites with yt-dlp.

```bash
sayl add "https://www.youtube.com/watch?v=..." --format "bestvideo[height<=1080]+bestaudio"
sayl add "https://www.youtube.com/playlist?list=..." --items 1-3,7
sayl add "https://..." --audio-only
sayl add "https://..." --subtitles en,ar
```

If the file Sayl gets is worse than what you asked for (a lower height, a missing audio track),
the download still completes but carries a warning, which `sayl info <id>` and the window both
show.

YouTube needs a JavaScript runtime such as Deno; `sayl doctor` tells you whether one is found.

### yt-dlp updates

Sites change often, and yt-dlp follows them. Sayl checks PyPI for a newer yt-dlp ten minutes
after it starts and once a day. A new version is verified against PyPI's SHA-256 hashes, tested
in a separate process, and used the next time sayld starts.

```bash
sayl ytdlp status    # the version in use, the bundled one, and any update waiting
sayl ytdlp update    # check now
sayl service restart # start using a downloaded update
```

Turn automatic updates off in Settings → yt-dlp, or with `sayl config ytdlp_auto_update false`.
If an update ever fails to load, Sayl falls back to the version it shipped with.

## Managing downloads

```bash
sayl ls                     # all downloads
sayl ls --status downloading
sayl info 4c67              # any unique start of an id works
sayl pause 4c67
sayl resume 4c67
sayl rm 4c67                # forget it; a finished file stays on disk
sayl watch                  # live events
sayl watch 4c67             # until that download finishes
```

### Expired links

When a link expires mid-download, the download waits in **Link expired**. Get a fresh link from
the site and give it to Sayl; it continues from the same byte:

```bash
sayl refresh 4c67 "https://new-link..."
```

### Checksums

Give a checksum when you add a download (`--checksum sha256:...`, or the hex alone) and Sayl
checks it when the download ends. You can also check a finished file later:

```bash
sayl verify 4c67 sha256:4f1c...
```

md5, sha1, sha224, sha256, sha384, and sha512 are supported. A mismatch keeps the file and marks
it.

## The queue and the scheduler

By default up to three downloads run at once (`max_concurrent` in `config.toml`). Failed
downloads are retried up to five times with growing delays.

```bash
sayl queue status
sayl queue stop     # start nothing new and pause running downloads
sayl queue start    # resume what `stop` paused and continue the queue
```

Captured browser downloads keep going when you stop the queue: the browser connection cannot
wait.

### Speed limits

Set a global limit in Settings → Downloads or with `sayl config speed_limit 2000000` (bytes
per second, 0 for none), and a per-download limit in the details panel or with `--limit`.

### Schedules

A schedule uses five crontab fields and does one of three things:

```bash
sayl schedule add night "0 2 * * *" start_queue
sayl schedule add morning "0 8 * * 1-5" stop_queue
sayl schedule add slow-days "0 9 * * 1-5" speed_limit --limit 500000
sayl schedule add overnight "0 1 * * *" start_queue --after shutdown
sayl schedule ls
sayl schedule run night     # run one now
sayl schedule rm night
```

`--after shutdown`, `suspend`, or `command --command "notify-send done"` runs once the queue
that schedule started is empty. You get 60 seconds to cancel it with `sayl queue cancel`.

## Categories

Six categories come ready: Compressed, Documents, Music, Video, Programs, and Disk images. A
download is matched by its file extension (then its type) when it is added, and goes to the
category's folder unless you chose another. Add, edit, reorder, or delete categories in
Settings → Categories. A category without a folder uses your default download folder.

## Batch downloads and the site grabber

```bash
sayl batch "https://example.com/photos/img[001-120].jpg"
sayl batch "https://example.com/part[a-f].zip" "https://example.com/vol[1-9:2].pdf"
sayl batch --file links.txt --dest ~/Batch
```

Number ranges keep their zero padding, letter ranges keep their case, and `:step` skips. Several
ranges in one link multiply out (up to 10,000 links).

The grabber crawls one site and lists the files it finds, without downloading anything until you
choose:

```bash
sayl grab https://example.com/files/ --ext pdf,zip --depth 2
sayl grab https://example.com/files/ --ext pdf --min-size 1000000 --queue --dest ~/Papers
```

It stays on the starting host, honours `robots.txt`, and reads at most 2,000 pages.

## Torrents

Magnet links and `.torrent` files go to aria2. After a torrent completes, it seeds in the
background until a share ratio of 1.0 or 60 minutes, whichever comes first (`bt_seed_ratio` and
`bt_seed_minutes` in `config.toml`).

## Network settings

### Outbound proxy

To send Sayl's own connections through a proxy, set it in Settings → Outbound proxy or:

```bash
sayl config outbound_proxy "socks5://user:password@host:1080"
sayl config outbound_proxy ""     # connect directly
```

`http`, `https`, `socks5`, and `socks5h` proxies are supported. The password is never logged.

### Site logins

For sites that ask for an HTTP username and password:

```bash
sayl creds set files.example.com me --password-stdin < password.txt
sayl creds ls
sayl creds rm files.example.com
```

The password is stored encrypted and never leaves sayld: neither the window nor `sayl creds ls`
can show it. It is sent only to that host, never to a site a link redirects to.

## History and statistics

```bash
sayl history export history.json
sayl history import history.json   # never starts anything; known downloads are skipped
sayl stats
```

The export has links, names, folders, sizes, and statuses, but never cookies, headers,
passwords, or settings. The window's statistics page shows the last 30 days, the busiest hosts,
and totals per category.

## Settings reference

Settings you change from the window or with `sayl config <key> <value>`:

| Key | Meaning |
| --- | --- |
| `speed_limit` | Global limit in bytes per second, 0 for none |
| `clipboard_enabled` | Watch the clipboard for links (`true`/`false`) |
| `clipboard_ignore` | Hosts or globs never offered from the clipboard |
| `outbound_proxy` | Proxy for Sayl's own connections, `""` for direct |
| `ytdlp_auto_update` | Check for yt-dlp updates (`true`/`false`) |

`sayl config` alone prints them all.

`~/.config/sayl/config.toml` holds what sayld reads at start (restart it after a change):

| Key | Default | Meaning |
| --- | --- | --- |
| `port` | `6971` | API port on 127.0.0.1 |
| `capture_port` | `6972` | Capture proxy port on 127.0.0.1 |
| `capture_passthrough` | `[]` | Extra hosts never intercepted, e.g. `["github.com", "*.corp.test"]` |
| `download_dir` | Downloads folder | Default folder for new downloads |
| `max_concurrent` | `3` | Downloads running at once |
| `max_retries` | `5` | Retries for a failed download |
| `connections` | `16` | Connections per download |
| `bt_seed_ratio` | `1.0` | Seed a finished torrent up to this ratio |
| `bt_seed_minutes` | `60` | or for this long |

## Troubleshooting

- **The window says sayld is not running.** Run `sayl service status`, then
  `journalctl --user -u sayld` and `~/.local/share/sayl/sayld.log` for the reason.
- **Copied links are not offered.** Check Settings → Clipboard: "not available in this session"
  means sayld cannot see your display. On sway, add the two lines from the README and run
  `sayl service restart`.
- **Browser downloads are not captured.** Run `sayl capture status`. The certificate must be
  trusted by the browser you use; restart the browser after `install-ca`. Some browsers use
  their own proxy setting instead of the desktop's.
- **A YouTube download fails with "n challenge solving failed".** Install a JavaScript runtime
  (`sayl doctor` shows the command) and run `sayl ytdlp update`.
- **Something else.** `sayl doctor` first; the log is at `~/.local/share/sayl/sayld.log`. It
  never contains cookies, passwords, or the contents of what you download.
