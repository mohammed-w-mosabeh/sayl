"""Render the built desktop UI against a real sayld, in English and Arabic, and save screenshots.

Checks that the WebSocket connects, the document direction follows the language, the add
dialog probes a link, the video bar lists an HLS stream's qualities, and the console stays
clean. No internet: files come from a local server.

Usage:
  cd desktop && pnpm build && cd ..
  PLAYWRIGHT_CORE=... CHROME=... uv run python desktop/scripts/ui_check.py [--out DIR]
"""

import argparse
import json
import os
import shutil
import signal
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

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "desktop" / "out"
DAEMON_PORT = 6983
FILES_PORT = 3297
WEB_PORT = 3299  # The page's origin, allowed through dev_origins.
FILES = {
    "ubuntu-24.04-desktop-amd64.iso": 300_000_000,
    "تقرير-سنوي.pdf": 80_000_000,
    "archive.tar.gz": 20_000_000,
}
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
720.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10,
segment.ts
#EXT-X-ENDLIST
"""


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return None


class Server(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """sayld drops its connection at shutdown, mid-file; that is expected here."""


def serve(directory: Path, port: int) -> Server:
    server = Server(("127.0.0.1", port), partial(Quiet, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def call(token: str, path: str, body: dict[str, Any] | None = None, method: str = "") -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{DAEMON_PORT}{path}",
        json.dumps(body).encode() if body is not None else None,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method or None,
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        raw = response.read()
    return json.loads(raw) if raw else None


def wait_for_token(file: Path) -> str:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if file.exists():
            token = file.read_text().strip()
            try:
                call(token, "/info")
                return token
            except OSError:
                pass
        time.sleep(0.2)
    raise SystemExit("sayld did not start")


def seed(token: str) -> None:
    """One download in each state the list shows."""
    base = f"http://127.0.0.1:{FILES_PORT}"
    call(token, "/downloads", {"url": f"{base}/archive.tar.gz"})
    call(token, "/downloads", {"url": "http://127.0.0.1:9/missing.zip"})
    call(
        token,
        "/downloads",
        {"url": f"{base}/{urllib.request.quote('تقرير-سنوي.pdf')}", "start_paused": True},
    )
    call(
        token,
        "/downloads",
        {"url": f"{base}/ubuntu-24.04-desktop-amd64.iso", "speed_limit": 2_000_000},
    )
    call(
        token,
        "/schedules",
        {
            "name": "Night",
            "cron": "0 2 * * 0-4",
            "action": "start_queue",
            "post_action": "shutdown",
        },
    )
    call(
        token,
        "/schedules",
        {"name": "Day", "cron": "0 8 * * *", "action": "speed_limit", "params": {"limit": 512_000}},
    )
    call(token, "/credentials/files.example.com", {"username": "me", "password": "x"}, "PUT")
    call(
        token,
        "/capture/media",
        {"kind": "manifest", "page_url": f"{base}/watch", "url": f"{base}/stream.m3u8"},
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        states = {row["status"] for row in call(token, "/downloads")}
        if {"completed", "failed", "paused", "active"} <= states:
            return
        time.sleep(0.5)
    raise SystemExit(f"downloads did not reach every state: {states}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "desktop" / "screenshots")
    args = parser.parse_args()
    if not (EXPORT / "index.html").exists():
        raise SystemExit("build the app first: cd desktop && pnpm build")
    for name in ("PLAYWRIGHT_CORE", "CHROME"):
        if not os.environ.get(name):
            raise SystemExit(f"set {name}: a playwright-core directory and a Chromium binary")

    state = Path(tempfile.mkdtemp(prefix="sayl-ui-"))
    files = state / "files"
    files.mkdir()
    for name, size in FILES.items():
        with (files / name).open("wb") as stream:
            stream.truncate(size)
    (files / "stream.m3u8").write_text(MASTER)
    for height in (1080, 720):
        (files / f"{height}.m3u8").write_text(MEDIA)
    config = state / "config" / "sayl"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        f'port = {DAEMON_PORT}\ndev_origins = ["http://localhost:{WEB_PORT}"]\n'
        f'download_dir = "{state / "out"}"\n'
    )
    # No display or session bus: the daemon must not watch the real clipboard or notify.
    hidden = {"DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS"}
    env = {
        **{key: value for key, value in os.environ.items() if key not in hidden},
        "XDG_CONFIG_HOME": str(state / "config"),
        "XDG_DATA_HOME": str(state / "data"),
        "XDG_CACHE_HOME": str(state / "cache"),
    }
    servers = [serve(files, FILES_PORT), serve(EXPORT, WEB_PORT)]
    daemon = subprocess.Popen(
        [sys.executable, "-c", "from daemon.main import main; main()"],
        env=env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
    )
    try:
        token = wait_for_token(config / "token")
        seed(token)
        args.out.mkdir(parents=True, exist_ok=True)
        node = shutil.which("node") or "node"
        result = subprocess.run(  # noqa: S603 - argument list
            [node, str(Path(__file__).with_name("screenshots.mjs"))],
            env={
                **env,
                "SAYL_URL": f"http://localhost:{WEB_PORT}/",
                "SAYL_API": f"http://127.0.0.1:{DAEMON_PORT}",
                "SAYL_TOKEN": token,
                "SAYL_OUT": str(args.out),
            },
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        sys.stderr.write(result.stderr)
        report = json.loads(result.stdout or "{}")
    finally:
        daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=20)
        for server in servers:
            server.shutdown()
        shutil.rmtree(state, ignore_errors=True)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    failures = [
        f"{run['name']}: {problem}" for run in report.get("runs", []) for problem in run["problems"]
    ]
    if result.returncode or not report.get("runs") or failures:
        print("\n".join(failures) or "screenshot run failed", file=sys.stderr)
        return 1
    print(f"OK: screenshots in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
