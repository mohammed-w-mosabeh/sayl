"""Run the single-use lab matrix for one browser and protocol, and check what the origin saw.

Usage: uv run python poc/lab/run_matrix.py <chromium|firefox> <http1|http2>
           [--target poc|daemon|capture] [--state DIR]
poc checks the addon's own handoff. daemon runs a real sayld and checks the attach API with the
PoC addon. capture is production: sayld starts its own sayl-capture, with back-pressure.
Exits non-zero only when a check fails. Chromium needs PLAYWRIGHT_CORE; see chromium.mjs.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

LAB = Path(__file__).parent
POC = LAB.parent
ORIGIN_PORT = 8443
PROXY_PORT = 8899
DAEMON_PORT = 6981
ONE_TIME_KEY = "GET /one/k9Xq2vLw7Rt4Zp1Bn6Ys/once.bin"
EXPECTED_FILES = {"noranges.bin", "once.bin", "post.bin", "range.bin"}
DAEMON_FILES = {"accel.bin", "once.bin", "private.bin", "session.bin"}
FINISH_TIMEOUT = 60.0


def _spawn(
    command: list[str], log: Path, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Each service gets its own process group, so stopping it also stops its children."""
    with log.open("wb") as stream:
        return subprocess.Popen(  # noqa: S603 - argument list built in this file
            command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True, env=env
        )


def _stop(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _leftovers(services: list[subprocess.Popen[bytes]]) -> list[int]:
    """Return process groups still alive. Finding none is the normal result, not an error."""
    alive = []
    for process in services:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            continue
        alive.append(process.pid)
    return alive


def _certificate(state: Path) -> tuple[Path, Path]:
    key, cert = state / "lab.key", state / "lab.crt"
    openssl = shutil.which("openssl")
    if openssl is None:
        raise SystemExit("openssl not found")
    subprocess.run(  # noqa: S603 - argument list, path from shutil.which
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return key, cert


def _driver(browser: str, steps_file: str) -> list[str]:
    steps = str(LAB / steps_file)
    if browser == "chromium":
        node = shutil.which("node")
        if node is None:
            raise SystemExit("node not found")
        return [node, str(LAB / "chromium.mjs"), str(PROXY_PORT), steps]
    return [sys.executable, str(LAB / "firefox.py"), str(PROXY_PORT), steps]


def _stats() -> dict[str, Any]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Lab origin on loopback, self-signed.
    url = f"https://127.0.0.1:{ORIGIN_PORT}/stats"
    with urllib.request.urlopen(url, context=context, timeout=10) as response:
        result: dict[str, Any] = json.load(response)
    return result


def _strip_prefix(path: Path) -> str:
    """Stream-through files are saved as <8 hex chars>-<name>."""
    return path.name.split("-", 1)[-1]


def _finished(downloads: Path) -> list[Path]:
    files = [path for path in downloads.iterdir() if path.suffix != ".aria2"]
    busy = any(path.suffix == ".aria2" for path in downloads.iterdir())
    return [] if busy else files


def _events(state: Path) -> list[dict[str, Any]]:
    log = state / "events.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _wait_for_files(state: Path) -> list[Path]:
    downloads = state / "downloads"
    deadline = time.monotonic() + FINISH_TIMEOUT
    while time.monotonic() < deadline:
        closed = sum(event["kind"] == "stream-through-closed" for event in _events(state))
        files = _finished(downloads) if downloads.exists() else []
        if (
            closed >= len(EXPECTED_FILES) - 1
            and {_strip_prefix(p) for p in files} >= EXPECTED_FILES
        ):
            return files
        time.sleep(0.25)
    return _finished(downloads)


def _browser_downloads(log: Path) -> list[str] | None:
    for line in log.read_text().splitlines():
        if line.startswith("{") and "browserDownloads" in line:
            names: list[str] = json.loads(line)["browserDownloads"]
            return names
    return None


def _check(state: Path, driver_log: Path, stats: dict[str, Any], files: list[Path]) -> list[str]:
    problems = []
    hits = stats["hits"].get("GET /once.bin")
    if hits != 1:
        problems.append(f"/once.bin was requested {hits} times, expected 1")
    names = {_strip_prefix(path) for path in files}
    if names != EXPECTED_FILES:
        problems.append(f"saved files {sorted(names)}, expected {sorted(EXPECTED_FILES)}")
    for path in files:
        if hashlib.sha256(path.read_bytes()).hexdigest() != stats["sha256"]:
            problems.append(f"{path.name} does not match the origin hash")
    browser_downloads = _browser_downloads(driver_log)
    if browser_downloads != []:
        problems.append(f"browser downloads: {browser_downloads}, expected none")
    if not _events(state):
        problems.append("the addon recorded no events")
    return problems


def _daemon_env(state: Path, cert: Path, protocol: str = "http2") -> dict[str, str]:
    home = state / "home"
    config = home / ".config/sayl"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(
        f"port = {DAEMON_PORT}\ncapture_port = {PROXY_PORT}\nconnections = 4\n"
        f'accelerate_min_bytes = 1048576\ndownload_dir = "{state / "out"}"\n'
    )
    hidden = ("XDG_", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")
    env = {key: value for key, value in os.environ.items() if not key.startswith(hidden)}
    return {
        **env,
        "HOME": str(home),
        "SSL_CERT_FILE": str(cert),
        "SAYL_CAPTURE_UPSTREAM_CA": str(cert),
        "SAYL_CAPTURE_HTTP2": "0" if protocol == "http1" else "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "GSETTINGS_BACKEND": "memory",
    }


def _enable_capture(token: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{DAEMON_PORT}/capture",
        json.dumps({"enabled": True, "system_proxy": False}).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        status = json.load(response)
    if status["state"] != "running":
        raise SystemExit(f"sayl-capture did not start: {status}")


def _daemon_get(token: str, path: str) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{DAEMON_PORT}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.load(response)


def _wait_for_daemon(state: Path) -> str:
    token_file = state / "home/.config/sayl/token"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            token = token_file.read_text()
            _daemon_get(token, "/settings")
        except OSError:
            time.sleep(0.1)
        else:
            return token
    raise SystemExit("sayld did not start")


def _wait_for_rows(token: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + FINISH_TIMEOUT
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = _daemon_get(token, "/downloads")
        finished = ("completed", "failed", "needs_refresh")
        if len(rows) == len(DAEMON_FILES) and all(r["status"] in finished for r in rows):
            return rows
        time.sleep(0.25)
    return rows


def _check_daemon(
    state: Path, driver_log: Path, stats: dict[str, Any], rows: list[dict[str, Any]]
) -> list[str]:
    problems = []
    if {row["filename"] for row in rows} != DAEMON_FILES:
        problems.append(f"downloads {sorted(row['filename'] for row in rows)}")
    for row in rows:
        if row["status"] != "completed":
            problems.append(f"{row['filename']}: {row['status']} {row['error']}")
            continue
        data = (state / "out" / row["filename"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != stats["sha256"]:
            problems.append(f"{row['filename']} does not match the origin hash")
    if stats["hits"].get(ONE_TIME_KEY) != 1:
        problems.append(f"one-time link hits: {stats['hits'].get(ONE_TIME_KEY)}, expected 1")
    if not stats["ranged"].get("GET /accel.bin"):
        problems.append("the Range-capable link was not accelerated")
    if stats["ranged"].get("GET /session.bin") != 1:
        problems.append(f"session Range requests: {stats['ranged'].get('GET /session.bin')}")
    if stats["hits"].get("GET /session.bin", 0) - stats["ranged"].get("GET /session.bin", 0) != 1:
        problems.append("the session link was not fetched exactly once with its cookie")
    # Logged in: the Range workers must carry the cookie and Authorization the browser sent.
    if not stats["ranged"].get("GET /private.bin"):
        problems.append("the logged-in link was not accelerated")
    if stats["denied"].get("GET /private.bin"):
        problems.append(f"logged-in Range requests refused: {stats['denied']['GET /private.bin']}")
    browser_downloads = _browser_downloads(driver_log)
    if browser_downloads != []:
        problems.append(f"browser downloads: {browser_downloads}, expected none")
    return problems


def run(browser: str, protocol: str, state: Path, target: str = "poc") -> int:
    state.mkdir(parents=True, exist_ok=True)
    key, cert = _certificate(state)
    node = shutil.which("node")
    if node is None:
        raise SystemExit("node not found")
    proxy = [sys.executable, str(POC / "run.py"), f"--port={PROXY_PORT}", f"--state={state}"]
    proxy += ["--insecure-upstream", *(["--http1"] if protocol == "http1" else [])]
    origin_args = [node, str(LAB / "origin.mjs"), str(ORIGIN_PORT), str(key), str(cert)]
    services = [_spawn(origin_args, state / "origin.log")]
    driver_log = state / "driver.log"
    steps = "steps-matrix.json" if target == "poc" else "steps-daemon.json"
    token = ""
    try:
        proxy_env = None
        if target in ("daemon", "capture"):
            daemon = [sys.executable, "-c", "from daemon.main import main; main()"]
            env = _daemon_env(state, cert, protocol)
            services.append(_spawn(daemon, state / "daemon.log", env))
            token = _wait_for_daemon(state)
            proxy_env = {
                **os.environ,
                "SAYL_POC_DAEMON_URL": f"http://127.0.0.1:{DAEMON_PORT}",
                "SAYL_POC_DAEMON_TOKEN": token,
            }
        if target == "capture":
            _enable_capture(token)  # sayld starts sayl-capture; no PoC process.
        else:
            services.append(_spawn(proxy, state / "proxy.log", proxy_env))
            time.sleep(3)
        with driver_log.open("wb") as stream:
            subprocess.run(  # noqa: S603 - argument list built in this file
                _driver(browser, steps),
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=True,
            )
        if target in ("daemon", "capture"):
            rows = _wait_for_rows(token)
            stats = _stats()
            problems = _check_daemon(state, driver_log, stats, rows)
        else:
            files = _wait_for_files(state)
            stats = _stats()
            problems = _check(state, driver_log, stats, files)
    finally:
        for service in reversed(services):
            _stop(service)
    problems += [f"process group {pid} survived shutdown" for pid in _leftovers(services)]
    print(
        json.dumps(
            {
                "browser": browser,
                "protocol": protocol,
                "target": target,
                "hits": stats["hits"],
                "ranged": stats["ranged"],
                "ok": not problems,
            }
        )
    )
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    # Run the finally blocks that stop sayld and the browsers when the runner is stopped.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    parser = argparse.ArgumentParser()
    parser.add_argument("browser", choices=["chromium", "firefox"])
    parser.add_argument("protocol", choices=["http1", "http2"])
    parser.add_argument("--target", choices=["poc", "daemon", "capture"], default="poc")
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.state is not None:
        return run(args.browser, args.protocol, args.state.resolve(), args.target)
    with tempfile.TemporaryDirectory(prefix="sayl-lab-") as state:
        return run(args.browser, args.protocol, Path(state), args.target)


if __name__ == "__main__":
    raise SystemExit(main())
