"""sayld entry point. Phase 0 placeholder: idles until SIGTERM/SIGINT."""

import asyncio
import signal


async def run(stop: asyncio.Event) -> int:
    """Wait for the stop event, then shut down cleanly."""
    await stop.wait()
    return 0


async def _serve() -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    return await run(stop)


def main() -> None:
    raise SystemExit(asyncio.run(_serve()))
