"""X11: XFixes tells us when the clipboard changes owner, so there is nothing to poll.

The X connection is read from the event loop with add_reader, never with a blocking call.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from Xlib import display as xdisplay
from Xlib import error as xerror
from Xlib.ext import xfixes

from daemon.watchers.base import Watcher, WatcherUnavailable
from daemon.watchers.xclip import read_x11


class X11Watcher(Watcher):
    name = "x11"

    async def read(self) -> str | None:
        return await read_x11(self.env)

    def _connect(self) -> tuple[xdisplay.Display, int]:
        """Connect and subscribe; returns the connection and the XFixes selection event code."""
        try:
            connection = xdisplay.Display(self.env.get("DISPLAY"))
        except (xerror.DisplayError, OSError) as error:
            raise WatcherUnavailable(f"cannot open the X display: {error}") from error
        extension = connection.query_extension("XFIXES")
        if extension is None or not extension.present:
            connection.close()
            raise WatcherUnavailable("the X server has no XFIXES")
        connection.xfixes_query_version()
        clipboard = connection.intern_atom("CLIPBOARD")
        connection.xfixes_select_selection_input(
            connection.screen().root, clipboard, xfixes.XFixesSetSelectionOwnerNotifyMask
        )
        connection.sync()
        return connection, extension.first_event + xfixes.XFixesSelectionNotify

    async def changes(self) -> AsyncIterator[None]:
        connection, selection_event = await asyncio.to_thread(self._connect)
        signals: asyncio.Queue[None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def readable() -> None:
            try:
                while connection.pending_events():
                    # Match on the protocol codes: python-xlib builds its own event classes,
                    # so isinstance against xfixes.SetSelectionOwnerNotify is always false.
                    event = connection.next_event()
                    if (
                        event.type == selection_event
                        and getattr(event, "sub_code", None) == xfixes.XFixesSetSelectionOwnerNotify
                    ):
                        signals.put_nowait(None)
            except (xerror.ConnectionClosedError, OSError):
                loop.remove_reader(connection.fileno())
                signals.put_nowait(None)  # Wake the loop below so it can report the loss.
                closed.set()

        closed = asyncio.Event()
        loop.add_reader(connection.fileno(), readable)
        try:
            while True:
                await signals.get()
                if closed.is_set():
                    raise WatcherUnavailable("the X connection closed")
                yield None
        finally:
            with contextlib.suppress(ValueError, OSError):
                loop.remove_reader(connection.fileno())
            with contextlib.suppress(Exception):
                connection.close()
