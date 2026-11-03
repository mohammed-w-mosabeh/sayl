"""GNOME on Wayland has no data-control protocol, so poll the XWayland clipboard.

Mutter mirrors the Wayland clipboard into XWayland, so xclip sees what Wayland apps copy. The
poll compares raw content only; TARGETS, and so the secret check, are read only on a change.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping

from daemon.watchers.base import Watcher, WatcherUnavailable, capture
from daemon.watchers.xclip import SELECTION, read_x11

INTERVAL = 0.7


class PollWatcher(Watcher):
    name = "xwayland_poll"

    def __init__(self, env: Mapping[str, str], interval: float = INTERVAL) -> None:
        super().__init__(env)
        self.interval = interval

    async def read(self) -> str | None:
        return await read_x11(self.env)

    async def _raw(self) -> bytes | None:
        return await capture(["xclip", "-o", *SELECTION], self.env)

    async def changes(self) -> AsyncIterator[None]:
        if not self.env.get("DISPLAY"):
            raise WatcherUnavailable("no X display to poll")
        last = await self._raw()
        while True:
            await asyncio.sleep(self.interval)
            current = await self._raw()
            if current != last:
                last = current
                yield None
