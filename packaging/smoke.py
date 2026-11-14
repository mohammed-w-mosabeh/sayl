"""Smoke-test an installed Sayl: doctor, sayld, a download, yt-dlp, sayl-capture, and shutdown.

Standard library only, so any python3 can run it against a .deb, an extracted AppImage, or a
runtime tree. Everything runs in a throwaway HOME and talks only to 127.0.0.1.

    python3 packaging/smoke.py                                    # the installed .deb
    python3 packaging/smoke.py --sayl "squashfs-root/AppRun sayl" \\
        --sayld "squashfs-root/AppRun sayld" \\
        --python "squashfs-root/usr/lib/sayl/runtime/bin/python3 -I"
"""

import argparse
import hashlib
import json
import os
import shlex
import signal
import socket
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

HIDDEN = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS")
PAGE = '<html><body><video src="clip.mp4" type="video/mp4"></video></body></html>'
PROBE = (
    "import sys, yt_dlp, yt_dlp_ejs; "
    "info = yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}).extract_info("
    "sys.argv[1], download=False); print(info.get('extractor_key'))"
)


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


class Origin(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # aria2 closes its probe connections early; that is not a failure.


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def step(text: str) -> None:
    print(f"== {text}", flush=True)


class Api:
    def __init__(self, port: int, token: str) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(  # noqa: S310 - loopback only
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
        return json.loads(raw) if raw else None


def wait(predicate: Any, what: str, seconds: float) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except OSError:
            value = None
        if value:
            return value
        time.sleep(0.2)
    raise SystemExit(f"FAIL: {what} within {seconds:.0f} s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sayl", default="sayl")
    parser.add_argument("--sayld", default="sayld")
    parser.add_argument("--python", default="/usr/lib/sayl/runtime/bin/python3 -I")
    args = parser.parse_args()
    sayl, sayld, python = (shlex.split(value) for value in (args.sayl, args.sayld, args.python))

    home = Path(tempfile.mkdtemp(prefix="sayl-smoke-"))
    site = home / "site"
    site.mkdir()
    payload = os.urandom(3 * 1024 * 1024 + 17)
    (site / "file.bin").write_bytes(payload)
    (site / "clip.mp4").write_bytes(b"\0" * 1024)
    (site / "page.html").write_text(PAGE)
    handler = partial(Quiet, directory=str(site))
    origin = Origin(("127.0.0.1", 0), handler)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    origin_url = f"http://127.0.0.1:{origin.server_address[1]}"

    port = free_port()
    config = home / ".config" / "sayl"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        f'port = {port}\ncapture_port = {free_port()}\ndownload_dir = "{home / "out"}"\n'
        'ytdlp_index = "http://127.0.0.1:9"\n'
    )
    env = {key: value for key, value in os.environ.items() if key not in HIDDEN}
    env = {key: value for key, value in env.items() if not key.startswith("XDG_")}
    env["HOME"] = str(home)

    def run(command: list[str], *extra: str) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(  # noqa: S603 - argument list
            [*command, *extra], env=env, capture_output=True, text=True, timeout=120, check=False
        )
        print(done.stdout + done.stderr, flush=True)
        return done

    step("sayl doctor")
    if run(sayl, "doctor").returncode != 0:
        raise SystemExit("FAIL: sayl doctor reported a failure")

    step("yt-dlp from the bundled runtime")
    probed = run(python, "-c", PROBE, f"{origin_url}/page.html")
    if probed.returncode != 0 or not probed.stdout.strip():
        raise SystemExit("FAIL: yt-dlp could not read a local page")

    step("start sayld")
    daemon = subprocess.Popen([*sayld], env=env)  # noqa: S603 - argument list
    try:
        token_file = config / "token"
        wait(token_file.exists, "sayld writes its token", 60)
        api = Api(port, token_file.read_text().strip())
        info = wait(lambda: api.call("GET", "/info"), "sayld answers", 60)
        print(json.dumps(info, indent=2))
        if not info.get("ytdlp", {}).get("active"):
            raise SystemExit("FAIL: /info does not report yt-dlp")
        if not (home / ".local/share/sayl/sayl.db").exists():
            raise SystemExit("FAIL: no database after the migrations")

        step("download from a local server")
        row = api.call("POST", "/downloads", {"url": f"{origin_url}/file.bin"})

        def finished() -> dict[str, Any] | None:
            current: dict[str, Any] = api.call("GET", f"/downloads/{row['id']}")
            if current["status"] == "failed":
                raise SystemExit(f"FAIL: download failed: {current.get('error')}")
            return current if current["status"] == "completed" else None

        done = wait(finished, "the download completes", 120)
        target = Path(done["dest_dir"]) / done["filename"]
        if hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(payload).hexdigest():
            raise SystemExit("FAIL: SHA-256 of the download does not match")
        if run(sayl, "ls").returncode != 0:
            raise SystemExit("FAIL: sayl ls")

        step("sayl-capture starts and stops")
        api.call("PUT", "/capture", {"enabled": True, "system_proxy": False})
        try:
            wait(
                lambda: api.call("GET", "/capture")["state"] == "running",
                "sayl-capture runs",
                60,
            )
        finally:
            state = api.call("GET", "/capture")
            print(json.dumps(state, indent=2))
            api.call("PUT", "/capture", {"enabled": False, "system_proxy": False})
    finally:
        step("SIGTERM")
        daemon.send_signal(signal.SIGTERM)
        code = daemon.wait(timeout=30)
        origin.shutdown()
    if code != 0:
        raise SystemExit(f"FAIL: sayld exited with {code}")
    print("OK: smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
