"""sayld entry point: wires the services and serves the API until SIGTERM or SIGINT."""

import asyncio
import logging
import os
import signal
import subprocess
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from daemon import categories
from daemon.api import AddDownload, Services, create_app, create_download
from daemon.capture.manager import CaptureManager
from daemon.config import Config, Paths, ensure_token, load_config
from daemon.db import make_engine, migrate
from daemon.engines.aria2 import Aria2Engine, Seeding
from daemon.engines.base import Engine
from daemon.engines.hybrid import HybridEngine
from daemon.engines.ytdlp import YtDlpEngine
from daemon.events import EventBus
from daemon.grabber import Grabber
from daemon.logs import setup_logging
from daemon.models import EngineName
from daemon.outbound import OutboundProxy
from daemon.proxy import ProxyError
from daemon.queue import QueueManager
from daemon.scheduler import Scheduler
from daemon.store import Store
from daemon.store import run as in_thread
from daemon.vault import Vault, load_key
from daemon.watchers.service import ClipboardService

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 5


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
        if self.services.outbound is not None:
            saved = (await in_thread(self.services.store.settings)).get("outbound_proxy", "")
            try:
                await self.services.outbound.apply(str(saved or ""))
            except ProxyError as error:
                log.warning("outbound proxy ignored: %s", error)
        await self.services.queue.start()
        if self.services.scheduler is not None:
            await self.services.scheduler.start()
        if self.services.clipboard is not None:
            await self.services.clipboard.start()
        if self.services.capture is not None:
            await self.services.capture.start()

    async def stop(self) -> None:
        if self.services.capture is not None:
            await self.services.capture.stop()  # First: it restores the system proxy.
        if self.services.clipboard is not None:
            await self.services.clipboard.stop()
        if self.services.scheduler is not None:
            await self.services.scheduler.stop()
        if self.services.grabber is not None:
            await self.services.grabber.stop()
        await self.services.queue.stop()
        await self.aria2.stop()
        await self.hybrid.client.aclose()
        if self.services.outbound is not None:
            await self.services.outbound.stop()


async def build(paths: Paths, config: Config, env: Mapping[str, str] | None = None) -> Daemon:
    """`env` is the environment sayld runs in, passed on to its watchers and children."""
    environment = dict(os.environ if env is None else env)
    paths.create()
    token = await asyncio.to_thread(ensure_token, paths)
    await asyncio.to_thread(migrate, paths.database)
    key = await asyncio.to_thread(load_key, paths.data / "headers.key")
    store = Store(make_engine(paths.database))
    await asyncio.to_thread(store.seed_categories, categories.defaults())
    vault = Vault(key)
    bus = EventBus()
    hybrid = HybridEngine(
        store,
        connections=config.connections,
        min_bytes=config.accelerate_min_bytes,
        spool_max=config.capture_spool_max_bytes,
    )
    aria2 = Aria2Engine(paths.data, seeding=Seeding(config.bt_seed_ratio, config.bt_seed_minutes))
    engines: dict[EngineName, Engine] = {
        EngineName.ARIA2: aria2,
        EngineName.YTDLP: YtDlpEngine(),
        EngineName.HYBRID: hybrid,
    }
    queue = QueueManager(store, vault, engines, bus, config)
    default_dir = await asyncio.to_thread(default_download_dir, config)
    queue.default_dir = default_dir
    services = Services(store, vault, queue, bus, config, engines, token, default_dir)

    async def add(link: str) -> None:
        await create_download(services, AddDownload(url=link))

    services.clipboard = ClipboardService(store, bus, add, environment)
    services.capture = CaptureManager(paths, config, store, bus, environment)
    services.scheduler = Scheduler(store, queue, bus)
    services.grabber = Grabber()
    ytdlp = engines[EngineName.YTDLP]
    assert isinstance(ytdlp, YtDlpEngine)  # noqa: S101 - built just above
    services.outbound = OutboundProxy(aria2, ytdlp, hybrid, services.grabber)
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
            # A captured download's PUT /stream lasts as long as its body. Past this, uvicorn
            # cancels it, and HybridEngine keeps the partial file for crash recovery.
            timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
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
