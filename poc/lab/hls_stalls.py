"""Does sayl-capture add stalls to video playback? A local 1080p HLS stream, direct vs proxied.

ffmpeg makes a 10-minute 1080p HLS stream once (cached in ~/.cache/sayl-lab), a local TLS origin
serves it, and headless Chromium plays it with hls.js at normal speed, first directly, then
through a real sayld and its own sayl-capture. The proxied run must not stall more, must report
the manifest for the video bar, and must not capture anything as a download.

Usage: uv run python poc/lab/hls_stalls.py [--seconds 600] [--state DIR]
Needs ffmpeg, node, PLAYWRIGHT_CORE and CHROME (see chromium.mjs); internet once, for hls.js.
"""

import argparse
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from run_matrix import _certificate  # type: ignore[import-not-found]

LAB = Path(__file__).parent
CACHE = Path.home() / ".cache/sayl-lab"
HLS_JS = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.20/hls.min.js"
ORIGIN_PORT = 8445
DAEMON_PORT = 6993
PROXY_PORT = 8997
ALLOWED_EXTRA_STALL_MS = 1000

PAGE = """<!doctype html><title>hls</title>
<video id="v" muted playsinline></video>
<script src="hls.min.js"></script>
<script>
const video = document.getElementById('v');
const started = performance.now();
window.report = { stalls: 0, stalled_ms: 0, startup_ms: null, played: 0, height: 0, done: false };
let waitingSince = null;
const finish = (error) => {
  if (window.report.done) return;
  if (waitingSince !== null) window.report.stalled_ms += performance.now() - waitingSince;
  const { currentTime: played, videoHeight: height } = video;
  Object.assign(window.report, { played, height, done: true });
  if (error) window.report.error = error;
};
video.addEventListener('playing', () => {
  if (window.report.startup_ms === null) window.report.startup_ms = performance.now() - started;
  if (waitingSince !== null) {
    window.report.stalled_ms += performance.now() - waitingSince;
    waitingSince = null;
  }
});
video.addEventListener('waiting', () => {
  if (window.report.startup_ms === null || waitingSince !== null) return;
  window.report.stalls += 1;
  waitingSince = performance.now();
});
video.addEventListener('ended', () => finish());
const hls = new Hls();
hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) finish(data.details); });
hls.loadSource('index.m3u8');
hls.attachMedia(video);
hls.on(Hls.Events.MANIFEST_PARSED, () => video.play());
</script>"""


def stream(seconds: int) -> Path:
    """The 1080p stream and its player page, made once."""
    folder = CACHE / f"hls-1080p-{seconds}s"
    if not (folder / "index.m3u8").exists():
        folder.mkdir(parents=True, exist_ok=True)
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SystemExit("ffmpeg not found")
        subprocess.run(  # noqa: S603 - argument list
            [
                ffmpeg, "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "high",
                "-b:v", "5M", "-maxrate", "5M", "-bufsize", "10M", "-g", "60",
                "-c:a", "aac", "-b:a", "128k",
                "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(folder / "seg%04d.ts"),
                str(folder / "index.m3u8"),
            ],
            check=True,
        )  # fmt: skip
    script = folder / "hls.min.js"
    if not script.exists():
        with urllib.request.urlopen(HLS_JS, timeout=60) as response:
            script.write_bytes(response.read())
    (folder / "index.html").write_text(PAGE)
    return folder


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


class Server(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """hls.js aborts requests it no longer needs; that is not a lab failure."""


def serve(folder: Path, key: Path, cert: Path) -> ThreadingHTTPServer:
    server = Server(("127.0.0.1", ORIGIN_PORT), partial(Quiet, directory=folder))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def play(proxy: str, seconds: int) -> dict[str, Any]:
    node = shutil.which("node") or "node"
    result = subprocess.run(  # noqa: S603 - argument list
        [
            node,
            str(LAB / "hls_play.mjs"),
            proxy,
            f"https://127.0.0.1:{ORIGIN_PORT}/index.html",
            str(seconds),
        ],
        capture_output=True,
        text=True,
        timeout=seconds + 240,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"player failed: {result.stderr[-2000:]}")
    report: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return report


def call(token: str, path: str, body: dict[str, Any] | None = None, method: str = "GET") -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{DAEMON_PORT}{path}",
        json.dumps(body).encode() if body is not None else None,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        raw = response.read()
    return json.loads(raw) if raw else None


def proxied(state: Path, cert: Path, seconds: int) -> tuple[dict[str, Any], Any, Any]:
    home = state / "home"
    config = home / ".config/sayl"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(
        f'port = {DAEMON_PORT}\ncapture_port = {PROXY_PORT}\ndownload_dir = "{state / "out"}"\n'
    )
    hidden = ("XDG_", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")
    env = {key: value for key, value in os.environ.items() if not key.startswith(hidden)}
    env |= {
        "HOME": str(home),
        "SAYL_CAPTURE_UPSTREAM_CA": str(cert),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "GSETTINGS_BACKEND": "memory",
    }
    with (state / "daemon.log").open("wb") as log:
        daemon = subprocess.Popen(
            [sys.executable, "-c", "from daemon.main import main; main()"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 40
        while True:
            try:
                token = (config / "token").read_text()
                call(token, "/settings")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise SystemExit("sayld did not start") from None
                time.sleep(0.2)
        status = call(token, "/capture", {"enabled": True, "system_proxy": False}, "PUT")
        ready = time.monotonic() + 60  # A busy machine can take a while to start mitmproxy.
        while status["state"] == "starting" and time.monotonic() < ready:
            time.sleep(0.5)
            status = call(token, "/capture")
        if status["state"] != "running":
            raise SystemExit(f"sayl-capture did not start: {status}")
        report = play(str(PROXY_PORT), seconds)
        return report, call(token, "/capture/media"), call(token, "/downloads")
    finally:
        os.killpg(daemon.pid, signal.SIGTERM)
        daemon.wait(timeout=30)


def run(state: Path, seconds: int) -> int:
    folder = stream(seconds)
    key, cert = _certificate(state)
    server = serve(folder, key, cert)
    try:
        direct = play("direct", seconds)
        through, media, downloads = proxied(state, cert, seconds)
    finally:
        server.shutdown()
    problems = []
    for name, report in (("direct", direct), ("proxy", through)):
        if report.get("error") or report["played"] < seconds - 2:
            problems.append(f"{name} played {report['played']:.0f}s: {report.get('error')}")
        if report["height"] != 1080:
            problems.append(f"{name} played at {report['height']}p")
    if through["stalls"] > direct["stalls"]:
        problems.append(f"proxy stalled {through['stalls']} times, direct {direct['stalls']}")
    if through["stalled_ms"] > direct["stalled_ms"] + ALLOWED_EXTRA_STALL_MS:
        problems.append(f"proxy stalled {through['stalled_ms']:.0f} ms more than direct")
    if not any(page["items"] for page in media):
        problems.append("the manifest was not reported for the video bar")
    if downloads:
        problems.append(f"{len(downloads)} downloads captured from video playback")
    print(json.dumps({"seconds": seconds, "direct": direct, "proxy": through, "ok": not problems}))
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    # Run the finally blocks that stop sayld and the browsers when the runner is stopped.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.state is not None:
        args.state.mkdir(parents=True, exist_ok=True)
        return run(args.state.resolve(), args.seconds)
    with tempfile.TemporaryDirectory(prefix="sayl-hls-") as state:
        return run(Path(state), args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
