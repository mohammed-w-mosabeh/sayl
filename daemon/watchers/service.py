"""The clipboard watcher as a daemon task: pick a watcher, filter changes, offer links.

Settings: `clipboard_enabled` (default true) and `clipboard_ignore` (hosts or globs).
"""

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from daemon.events import EventBus
from daemon.store import Store, run
from daemon.watchers.base import Watcher, WatcherUnavailable
from daemon.watchers.filter import ClipFilter, host_of
from daemon.watchers.notify import DOWNLOAD, IGNORE, Notifier
from daemon.watchers.session import Backend, detect
from daemon.watchers.wayland import WaylandWatcher
from daemon.watchers.x11 import X11Watcher
from daemon.watchers.xwayland_poll import PollWatcher

log = logging.getLogger(__name__)

ENABLED = "clipboard_enabled"
IGNORE_LIST = "clipboard_ignore"
RETRY_SECONDS = 5.0

Factory = Callable[[Mapping[str, str]], Watcher]
TOOLS: dict[type[Watcher], str] = {
    WaylandWatcher: "wl-paste",
    X11Watcher: "xclip",
    PollWatcher: "xclip",
}
CHAINS: dict[Backend, tuple[Factory, ...]] = {
    Backend.WAYLAND: (WaylandWatcher, PollWatcher),  # Poll if the compositor refuses data-control.
    Backend.XWAYLAND_POLL: (PollWatcher,),
    Backend.X11: (X11Watcher, PollWatcher),
    Backend.POLL: (PollWatcher,),
    Backend.NONE: (),
}


def patterns(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


class ClipboardService:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        add: Callable[[str], Awaitable[None]],
        env: Mapping[str, str],
        chains: Mapping[Backend, tuple[Factory, ...]] = CHAINS,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.store = store
        self.bus = bus
        self.add = add
        self.env = dict(env)
        self.chains = chains
        self.which = which
        self.filter = ClipFilter()
        self.notifier = Notifier(self._on_action)
        self.task: asyncio.Task[None] | None = None
        self.state = "off"
        self.language: object = "en"

    async def start(self) -> None:
        await self.notifier.start(self.env)
        await self.apply(await run(self.store.settings))

    async def stop(self) -> None:
        await self._halt()
        await self.notifier.stop()

    async def apply(self, settings: Mapping[str, Any]) -> None:
        """Follow the settings: start or stop watching, and reload the ignore list."""
        self.filter.patterns = patterns(settings.get(IGNORE_LIST, []))
        ui = settings.get("ui")
        self.language = ui.get("language", "en") if isinstance(ui, dict) else "en"
        enabled = settings.get(ENABLED, True) is not False
        if enabled and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._watch())
        elif not enabled:
            await self._halt()
            self.state = "off"

    async def _halt(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _watch(self) -> None:
        backend = detect(self.env)
        chain = [factory for factory in self.chains[backend] if self._has_tool(factory)]
        if not chain:
            self.state = "unavailable"
            log.warning("clipboard watcher unavailable for %s session", backend)
            return
        while True:
            for factory in chain:
                watcher = factory(self.env)
                self.state = watcher.name
                try:
                    await self._follow(watcher)
                except WatcherUnavailable as error:
                    log.info("clipboard watcher %s unavailable: %s", watcher.name, error)
                    continue
                except Exception:
                    log.exception("clipboard watcher %s failed", watcher.name)
                break
            else:
                self.state = "unavailable"
                log.warning("no clipboard watcher works in this %s session", backend)
                return
            await asyncio.sleep(RETRY_SECONDS)

    def _has_tool(self, factory: Factory) -> bool:
        tool = TOOLS.get(factory) if isinstance(factory, type) else None
        if tool and self.which(tool) is None:
            log.warning("clipboard: %s is not installed (see sayl doctor)", tool)
            return False
        return True

    async def _follow(self, watcher: Watcher) -> None:
        self.filter.prime(await watcher.read())
        async for _ in watcher.changes():
            text = await watcher.read()
            if text is None:
                self.filter.prime(None)  # A secret or an image: forget, but offer nothing.
                continue
            link = self.filter.accept(text)
            if link is not None:
                await self._offer(link)

    async def _offer(self, link: str) -> None:
        if await run(self.store.has_url, link):
            return  # Already in Sayl, for example from the app's "Copy link".
        self.bus.publish("clipboard.detected", url=link)
        await self.notifier.offer(link, self.language)

    async def _on_action(self, action: str, link: str) -> None:
        if action == DOWNLOAD:
            await self.add(link)
        elif action == IGNORE and (host := host_of(link)):
            settings = await run(self.store.settings)
            current = list(patterns(settings.get(IGNORE_LIST, [])))
            if host not in current:
                await run(self.store.put_settings, {IGNORE_LIST: [*current, host]})
                self.filter.patterns = (*current, host)
