"""sayld entry point: wires the services and serves the API until SIGTERM or SIGINT."""

import asyncio
import signal
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from daemon.api import Services, create_app
from daemon.config import Config, Paths, ensure_token, load_config
from daemon.db import make_engine, migrate
from daemon.engines.aria2 import Aria2Engine
from daemon.engines.base import Engine
from daemon.engines.hybrid import HybridEngine
from daemon.engines.ytdlp import YtDlpEngine
from daemon.events import EventBus
from daemon.logs import setup_logging
from daemon.models import EngineName
from daemon.queue import QueueManager
from daemon.store import Store
from daemon.vault import Vault, load_key


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        """The daemon owns SIGTERM, so shutdown stops the engines as well."""
        yield


def default_download_dir(config: Config) -> str:
    if config.download_dir:
        return config.download_dir
    try:
        found = subprocess.run(
            ["xdg-user-dir", "DOWNLOAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return str(Path.home() / "Downloads")
    return found.stdout.strip() or str(Path.home() / "Downloads")


@dataclass
class Daemon:
    services: Services
    aria2: Aria2Engine
    hybrid: HybridEngine

    async def start(self) -> None:
        await self.aria2.start()
        await self.services.queue.start()

    async def stop(self) -> None:
        await self.services.queue.stop()
        await self.aria2.stop()
        await self.hybrid.client.aclose()


async def build(paths: Paths, config: Config) -> Daemon:
    paths.create()
    token = await asyncio.to_thread(ensure_token, paths)
    await asyncio.to_thread(migrate, paths.database)
    key = await asyncio.to_thread(load_key, paths.data / "headers.key")
    store = Store(make_engine(paths.database))
    vault = Vault(key)
    bus = EventBus()
    hybrid = HybridEngine(
        store, connections=config.connections, min_bytes=config.accelerate_min_bytes
    )
    aria2 = Aria2Engine(paths.data)
    engines: dict[EngineName, Engine] = {
        EngineName.ARIA2: aria2,
        EngineName.YTDLP: YtDlpEngine(),
        EngineName.HYBRID: hybrid,
    }
    queue = QueueManager(store, vault, engines, bus, config)
    default_dir = await asyncio.to_thread(default_download_dir, config)
    services = Services(store, vault, queue, bus, config, engines, token, default_dir)
    return Daemon(services, aria2, hybrid)


async def run(stop: asyncio.Event, paths: Paths | None = None) -> int:
    """Serve until the stop event, then shut everything down cleanly."""
    where = paths or Paths.from_env()
    where.create()
    setup_logging(where.log)
    config = await asyncio.to_thread(load_config, where)
    daemon = await build(where, config)
    await daemon.start()
    server = _Server(
        uvicorn.Config(
            create_app(daemon.services),
            host=config.host,
            port=config.port,
            log_config=None,
            access_log=False,
            lifespan="off",
        )
    )
    serving = asyncio.create_task(server.serve())
    await stop.wait()
    server.should_exit = True
    await serving
    await daemon.stop()
    return 0


async def _serve() -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    return await run(stop)


def main() -> None:
    raise SystemExit(asyncio.run(_serve()))
