"""Start aria2c and mitmdump for the PoC, both bound to 127.0.0.1. Ctrl-C stops both.

Usage: uv run --group poc python poc/run.py [--port 8899] [--http1] [--insecure-upstream]
"""

import argparse
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path

POC = Path(__file__).parent
ARIA2_PORT = 6899


def _raise_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    signal.signal(signal.SIGTERM, _raise_interrupt)
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--state", type=Path, default=POC / ".state")
    parser.add_argument("--http1", action="store_true", help="disable HTTP/2 in the proxy")
    parser.add_argument("--insecure-upstream", action="store_true", help="lab self-signed origin")
    args = parser.parse_args()

    state = args.state.resolve()
    (state / "downloads").mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(16)
    aria2_bin = shutil.which("aria2c")
    mitmdump = shutil.which("mitmdump")
    if aria2_bin is None or mitmdump is None:
        print("aria2c or mitmdump is missing; run with: uv run --group poc", file=sys.stderr)
        return 1

    aria2 = subprocess.Popen(  # noqa: S603 - argument list, path from shutil.which
        [
            aria2_bin,
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={ARIA2_PORT}",
            f"--rpc-secret={secret}",
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--auto-file-renaming=true",
            "--quiet=true",
            *(["--check-certificate=false"] if args.insecure_upstream else []),
        ]
    )
    env = {
        **os.environ,
        "SAYL_POC_STATE": str(state),
        "SAYL_POC_ARIA2_SECRET": secret,
        "SAYL_POC_ARIA2_URL": f"http://127.0.0.1:{ARIA2_PORT}/jsonrpc",
    }
    command = [
        mitmdump,
        "--listen-host=127.0.0.1",
        f"--listen-port={args.port}",
        "--quiet",
        "--scripts",
        str(POC / "addon.py"),
        "--set",
        f"confdir={state / 'mitm'}",
    ]
    if args.http1:
        command += ["--set", "http2=false"]
    if args.insecure_upstream:
        command += ["--set", "ssl_insecure=true"]
    proxy = subprocess.Popen(command, env=env)  # noqa: S603 - argument list
    print(
        f"proxy 127.0.0.1:{args.port}  aria2 rpc 127.0.0.1:{ARIA2_PORT}  state {state}", flush=True
    )
    try:
        return proxy.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        for child in (proxy, aria2):
            child.terminate()
        for child in (proxy, aria2):
            child.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
