"""A real site through a real sayld and its own sayl-capture, under a speed limit.

Headless Chromium clicks through the steps, the browser quits, and the download must finish in
sayld with at least --min-bytes and the expected leading bytes. Needs internet, PLAYWRIGHT_CORE,
and CHROME (see chromium.mjs).

Usage: uv run python poc/lab/real_capture.py steps-drive.json --min-bytes 400000000 --magic 504b0304
           [--magic 504b0304] [--limit 400000] [--timeout 3600] [--state DIR]
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

LAB = Path(__file__).parent
DAEMON_PORT = 6991
PROXY_PORT = 8999  # Away from run_matrix.py, so both can run at once.


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


def wait_for_token(file: Path) -> str:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            token = file.read_text()
            call(token, "/settings")
        except OSError:
            time.sleep(0.2)
        else:
            return token
    raise SystemExit("sayld did not start")


def run(args: argparse.Namespace, state: Path) -> int:
    home = state / "home"
    config = home / ".config/sayl"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(
        f"port = {DAEMON_PORT}\ncapture_port = {PROXY_PORT}\nspeed_limit = {args.limit}\n"
        f'download_dir = "{state / "out"}"\n'
    )
    hidden = ("XDG_", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")
    env = {key: value for key, value in os.environ.items() if not key.startswith(hidden)}
    env |= {
        "HOME": str(home),
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
    started = time.monotonic()
    row: dict[str, Any] = {}
    problems: list[str] = []
    try:
        token = wait_for_token(config / "token")
        status = call(token, "/capture", {"enabled": True, "system_proxy": False}, "PUT")
        ready = time.monotonic() + 60  # A busy machine can take a while to start mitmproxy.
        while status["state"] == "starting" and time.monotonic() < ready:
            time.sleep(0.5)
            status = call(token, "/capture")
        if status["state"] != "running":
            raise SystemExit(f"sayl-capture did not start: {status}")
        node = shutil.which("node") or "node"
        with (state / "driver.log").open("wb") as log:
            subprocess.run(  # noqa: S603 - argument list
                [node, str(LAB / "chromium.mjs"), str(PROXY_PORT), str(LAB / args.steps)],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=300,
                check=True,
            )
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            rows = call(token, "/downloads")
            row = rows[0] if rows else {}
            if row.get("status") in ("completed", "failed", "needs_refresh"):
                break
            if not rows and time.monotonic() - started > 120:
                break  # Nothing was captured.
            time.sleep(2)
    finally:
        os.killpg(daemon.pid, signal.SIGTERM)
        daemon.wait(timeout=30)

    elapsed = time.monotonic() - started
    if row.get("status") != "completed":
        problems.append(f"download {row.get('status', 'never captured')}: {row.get('error', '')}")
    else:
        file = Path(row["dest_dir"]) / row["filename"]
        size = file.stat().st_size
        if size < args.min_bytes:
            problems.append(f"{size} bytes, expected at least {args.min_bytes}")
        if args.magic and file.read_bytes()[: len(args.magic) // 2].hex() != args.magic:
            problems.append("the file does not start with the expected bytes")
        rate = size / elapsed
        if args.limit and rate > args.limit * 1.15:
            problems.append(f"average {rate:.0f} B/s is above the {args.limit} B/s limit")
    report = {
        "steps": args.steps,
        "filename": row.get("filename"),
        "bytes": row.get("done_bytes"),
        "engine": row.get("engine"),
        "seconds": round(elapsed),
        "limit": args.limit,
        "ok": not problems,
    }
    print(json.dumps(report))
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    # Run the finally blocks that stop sayld and the browsers when the runner is stopped.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    parser = argparse.ArgumentParser()
    parser.add_argument("steps")
    parser.add_argument("--min-bytes", type=int, default=1)
    parser.add_argument("--magic", default="", help="leading bytes as hex, e.g. 504b0304 (ZIP)")
    parser.add_argument("--limit", type=int, default=400_000, help="speed limit in B/s")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.state is not None:
        args.state.mkdir(parents=True, exist_ok=True)
        return run(args, args.state.resolve())
    with tempfile.TemporaryDirectory(prefix="sayl-real-") as state:
        return run(args, Path(state))


if __name__ == "__main__":
    raise SystemExit(main())
