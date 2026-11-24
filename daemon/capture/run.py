"""sayl-capture: mitmproxy on 127.0.0.1 with the Sayl addon. sayld starts and stops it.

Reads the token and ports the way every client does. Exits cleanly on SIGTERM.
"""

import argparse
import asyncio
import logging
import os
import signal
from collections.abc import Callable

import httpx
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from daemon.capture import detect, mitm_adapter
from daemon.capture.addon import Capture
from daemon.capture.ca import TRUSTED_ENV, ca_dir
from daemon.config import Paths, load_config
from daemon.engines.ytdlp import supported_extractor
from daemon.logs import setup_logging

log = logging.getLogger(__name__)
# Lab only; unset means production behaviour. Verify upstreams against this CA file instead of
# the default bundle (the lab origin is self-signed), and turn HTTP/2 off to test HTTP/1.1.
UPSTREAM_CA_ENV = "SAYL_CAPTURE_UPSTREAM_CA"
HTTP2_ENV = "SAYL_CAPTURE_HTTP2"


def options(port: int, confdir: str, bypass: tuple[str, ...]) -> Options:
    result = Options(
        listen_host="127.0.0.1",
        listen_port=port,
        confdir=confdir,
        ignore_hosts=detect.ignore_hosts(bypass),
        ssl_insecure=False,  # Upstream certificates are verified as a browser would.
        http2=os.environ.get(HTTP2_ENV, "1") != "0",
    )
    if extra := os.environ.get(UPSTREAM_CA_ENV):
        result.update(ssl_verify_upstream_trusted_ca=extra)  # type: ignore[no-untyped-call]
    return result  # block_global, an addon option, defaults to True: only local clients are served.


async def watch_parent(shutdown: Callable[[], None], interval: float = 1.0) -> None:
    """Exit with sayld. If it is killed outright, an orphan would keep the port and its token."""
    parent = os.getppid()
    while os.getppid() == parent:  # noqa: ASYNC110 - nothing signals a new parent
        await asyncio.sleep(interval)
    log.warning("sayld is gone; stopping")
    shutdown()


async def serve(daemon_port: int | None = None, port: int | None = None) -> int:
    """sayld passes both ports, so a daemon started with overrides is still found."""
    mitm_adapter.install()
    os.umask(0o077)  # Whatever mitmproxy writes into the CA directory stays private.
    paths = Paths.from_env()
    setup_logging(paths.data / "capture.log")
    config = load_config(paths)
    daemon_port = daemon_port or config.port
    port = port or config.capture_port
    token = paths.token.read_text().strip()
    passthrough = config.capture_passthrough
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{daemon_port}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        trust_env=False,  # An http_proxy variable must never route us through ourselves.
    )
    master = DumpMaster(
        options(port, str(ca_dir(paths.data)), (*detect.DEFAULT_BYPASS, *passthrough)),
        with_termlog=False,
        with_dumper=False,
    )
    addon = Capture(
        client,
        f"ws://127.0.0.1:{daemon_port}/ws",
        token,
        extractor=supported_extractor,
        ca_trusted=os.environ.get(TRUSTED_ENV) == "1",
        passthrough=passthrough,
    )
    addon.on_settings = lambda _: master.options.update(  # type: ignore[no-untyped-call]
        ignore_hosts=detect.ignore_hosts(addon.bypass)
    )
    master.addons.add(addon)  # type: ignore[no-untyped-call]
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, master.shutdown)
    log.info("sayl-capture listening on 127.0.0.1:%s", port)
    orphan = asyncio.create_task(watch_parent(master.shutdown))
    try:
        await master.run()
    finally:
        orphan.cancel()
        await client.aclose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="sayl-capture")
    parser.add_argument("--daemon-port", type=int)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(serve(args.daemon_port, args.port)))


if __name__ == "__main__":
    main()
