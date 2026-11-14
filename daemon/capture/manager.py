"""sayld's side of capture: run sayl-capture, own the CA, the system proxy, and video reports.

Settings: `capture_enabled`, `capture_system_proxy` (both default false), `capture_bypass`
(extra hosts), `capture_threshold` (bytes). The system proxy only ever points at a proxy that is
listening: it is switched on after the port answers and restored when sayld stops.
"""

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daemon.capture import ca
from daemon.capture.sysproxy import SystemProxy
from daemon.capture.trust import Report, Trust
from daemon.config import Config, Paths
from daemon.events import EventBus
from daemon.i18n import language_of, t
from daemon.store import Store, run
from daemon.watchers.notify import Notifier

log = logging.getLogger(__name__)

ENABLED = "capture_enabled"
SYSTEM_PROXY = "capture_system_proxy"
BYPASS = "capture_bypass"
RESTART_LIMIT = 5  # Crashes within RESTART_WINDOW before giving up.
RESTART_WINDOW = 60.0
READY_TIMEOUT = 20.0
MEDIA_PAGES = 50  # Pages remembered for the video bar.

Spawner = Callable[..., Any]


@dataclass
class Media:
    page_url: str
    host: str
    items: list[dict[str, Any]] = field(default_factory=list)
    updated: float = field(default_factory=time.time)


class CaptureManager:
    def __init__(
        self,
        paths: Paths,
        config: Config,
        store: Store,
        bus: EventBus,
        env: Mapping[str, str],
        spawn: Spawner = asyncio.create_subprocess_exec,
    ) -> None:
        self.paths = paths
        self.directory = ca.ca_dir(paths.data)
        self.state_dir = paths.data / "capture"
        self.config = config
        self.store = store
        self.bus = bus
        self.env = dict(env)
        self.spawn = spawn
        self.trust = Trust(Path(self.env.get("HOME") or Path.home()))
        self.proxy = SystemProxy(self.state_dir, self.env)
        self.notifier = Notifier(self._no_action)
        self.process: Any = None
        self.supervisor: asyncio.Task[None] | None = None
        self.state = "off"
        self.error = ""
        self.crashes: list[float] = []
        self.media: dict[str, Media] = {}
        self.lock = asyncio.Lock()

    @staticmethod
    async def _no_action(action: str, link: str) -> None:
        return None

    # Lifecycle

    async def start(self) -> None:
        await self.notifier.start(self.env)
        settings = await run(self.store.settings)
        if settings.get(ENABLED) is True:
            await self.apply(enabled=True, system_proxy=settings.get(SYSTEM_PROXY) is True)

    async def stop(self) -> None:
        """sayld is stopping: never leave the system proxy pointing at a dead port."""
        await asyncio.to_thread(self.proxy.disable)
        await self._halt()
        await self.notifier.stop()

    async def apply(self, *, enabled: bool, system_proxy: bool) -> dict[str, Any]:
        async with self.lock:
            await run(
                self.store.put_settings, {ENABLED: enabled, SYSTEM_PROXY: enabled and system_proxy}
            )
            if not enabled:
                await asyncio.to_thread(self.proxy.disable)
                await self._halt()
                self.state = "off"
                return self.status()
            await asyncio.to_thread(ca.ensure, self.directory)
            if self.supervisor is None or self.supervisor.done():
                self.crashes = []
                self.supervisor = asyncio.create_task(self._supervise())
            if not await self._wait_ready():
                return self.status()
            if system_proxy:
                result = await asyncio.to_thread(self.proxy.enable, self.config.capture_port)
                self.error = "" if result.applied else result.detail
            else:
                await asyncio.to_thread(self.proxy.disable)
            return self.status()

    async def _halt(self) -> None:
        if self.supervisor is not None:
            self.supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.supervisor
            self.supervisor = None

    async def _supervise(self) -> None:
        while True:
            trusted = await asyncio.to_thread(self._trusted)
            env = {
                **self.env,
                # The child must find this daemon's files, whatever its own defaults are.
                "XDG_CONFIG_HOME": str(self.paths.config.parent),
                "XDG_DATA_HOME": str(self.paths.data.parent),
                "XDG_CACHE_HOME": str(self.paths.cache.parent),
                ca.TRUSTED_ENV: "1" if trusted else "0",
            }
            self.state = "starting"
            self.process = await self.spawn(
                sys.executable, "-m", "daemon.capture.run",
                "--daemon-port", str(self.config.port), "--port", str(self.config.capture_port),
                env=env, stdin=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            try:
                code = await self.process.wait()
            except asyncio.CancelledError:
                await self._terminate()
                raise
            now = time.monotonic()
            self.crashes = [at for at in self.crashes if now - at < RESTART_WINDOW] + [now]
            log.warning("sayl-capture exited with %s", code)
            if len(self.crashes) >= RESTART_LIMIT:
                self.state = "failed"
                self.error = f"sayl-capture keeps exiting (last code {code}); see capture.log"
                await asyncio.to_thread(self.proxy.disable)
                return
            await asyncio.sleep(min(10.0, 0.5 * 2 ** len(self.crashes)))

    async def _terminate(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def _wait_ready(self) -> bool:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.supervisor is None or self.supervisor.done():
                break
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.config.capture_port)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            writer.close()
            self.state = "running"
            self.error = ""
            return True
        self.state = "failed"
        self.error = self.error or f"sayl-capture did not listen on {self.config.capture_port}"
        return False

    # Certificate authority

    def _trusted(self) -> bool:
        authority = ca.load(self.directory)
        return bool(authority and self.trust.status(authority)["stores"])

    async def install_ca(self, system: bool) -> Report:
        authority = await asyncio.to_thread(ca.ensure, self.directory)
        report = await asyncio.to_thread(self.trust.install, authority, system=system)
        if self.supervisor is not None and not self.supervisor.done() and self.process:
            await self._terminate()  # The supervisor restarts it, now knowing the CA is trusted.
        return report

    async def uninstall(self) -> Report:
        """Restore the proxy settings, stop capturing, and remove the CA from everywhere."""
        await self.apply(enabled=False, system_proxy=False)
        authority = await asyncio.to_thread(ca.load, self.directory)
        if authority is None:
            return Report()
        report = await asyncio.to_thread(self.trust.uninstall, authority)
        if report.ok:
            await asyncio.to_thread(ca.remove, self.directory)
        return report

    def status(self) -> dict[str, Any]:
        authority = ca.load(self.directory)
        trust = self.trust.status(authority) if authority else None
        return {
            "state": self.state,
            "error": self.error,
            "port": self.config.capture_port,
            "system_proxy": self.proxy.active(),
            "ca": trust,
        }

    # Reports from sayl-capture

    def report_media(self, report: Mapping[str, Any]) -> None:
        page = str(report.get("page_url") or report.get("url") or "")
        if not page:
            return
        host = page.split("/")[2] if "://" in page else page
        entry = self.media.get(page) or Media(page, host)
        item = {key: report[key] for key in ("kind", "url", "drm", "extractor") if key in report}
        if item in entry.items:
            return
        entry.items.append(item)
        entry.updated = time.time()
        self.media[page] = entry
        if len(self.media) > MEDIA_PAGES:
            oldest = min(self.media.values(), key=lambda media: media.updated)
            self.media.pop(oldest.page_url)
        self.bus.publish("capture.detected", page_url=page, host=host, **item)

    def media_list(self) -> list[dict[str, Any]]:
        pages = sorted(self.media.values(), key=lambda media: media.updated, reverse=True)
        return [
            {"page_url": m.page_url, "host": m.host, "items": m.items, "updated": m.updated}
            for m in pages
        ]

    async def add_bypass(self, host: str) -> bool:
        """A site refused the CA: stop intercepting it, and say so once."""
        host = host.strip().lower()
        settings = await run(self.store.settings)
        current = settings.get(BYPASS)
        entries = [str(item) for item in current] if isinstance(current, list) else []
        if not host or host in entries:
            return False
        await run(self.store.put_settings, {BYPASS: [*entries, host]})
        self.bus.publish("settings.updated", keys=[BYPASS])
        language = language_of(settings)
        await self.notifier.inform(
            t(language, "capture.bypass.summary", host=host), t(language, "capture.bypass.body")
        )
        return True
