"""Run the single-use lab matrix for one browser and protocol, and check what the origin saw.

Usage: uv run python poc/lab/run_matrix.py <chromium|firefox> <http1|http2> [--state DIR]
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
EXPECTED_FILES = {"noranges.bin", "once.bin", "post.bin", "range.bin"}
FINISH_TIMEOUT = 60.0


def _spawn(command: list[str], log: Path) -> subprocess.Popen[bytes]:
    """Each service gets its own process group, so stopping it also stops its children."""
    with log.open("wb") as stream:
        return subprocess.Popen(  # noqa: S603 - argument list built in this file
            command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True
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


def _driver(browser: str) -> list[str]:
    steps = str(LAB / "steps-matrix.json")
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


def run(browser: str, protocol: str, state: Path) -> int:
    state.mkdir(parents=True, exist_ok=True)
    key, cert = _certificate(state)
    node = shutil.which("node")
    if node is None:
        raise SystemExit("node not found")
    proxy = [sys.executable, str(POC / "run.py"), f"--port={PROXY_PORT}", f"--state={state}"]
    proxy += ["--insecure-upstream", *(["--http1"] if protocol == "http1" else [])]
    origin_args = [node, str(LAB / "origin.mjs"), str(ORIGIN_PORT), str(key), str(cert)]
    services = [_spawn(origin_args, state / "origin.log"), _spawn(proxy, state / "proxy.log")]
    driver_log = state / "driver.log"
    try:
        time.sleep(3)
        with driver_log.open("wb") as stream:
            subprocess.run(  # noqa: S603 - argument list built in this file
                _driver(browser), stdout=stream, stderr=subprocess.STDOUT, timeout=120, check=True
            )
        files = _wait_for_files(state)
        stats = _stats()
        problems = _check(state, driver_log, stats, files)
    finally:
        for service in reversed(services):
            _stop(service)
    problems += [f"process group {pid} survived shutdown" for pid in _leftovers(services)]
    print(json.dumps({"browser": browser, "protocol": protocol, "hits": stats["hits"]}))
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("browser", choices=["chromium", "firefox"])
    parser.add_argument("protocol", choices=["http1", "http2"])
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.state is not None:
        return run(args.browser, args.protocol, args.state.resolve())
    with tempfile.TemporaryDirectory(prefix="sayl-lab-") as state:
        return run(args.browser, args.protocol, Path(state))


if __name__ == "__main__":
    raise SystemExit(main())
