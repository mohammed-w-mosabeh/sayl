"""Wayland compositors with the data-control protocol (wlroots, KDE): `wl-paste --watch`.

wl-paste runs `echo` on every change, so each line is a change signal; the content is then
read separately, after checking its types for a password manager's hint.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import cast

from daemon.watchers.base import Watcher, WatcherUnavailable, capture, text_type


class WaylandWatcher(Watcher):
    name = "wayland"

    async def read(self) -> str | None:
        types = await capture(["wl-paste", "--list-types"], self.env)
        if types is None:
            return None
        kind = text_type(types.decode(errors="replace").splitlines())
        if kind is None:
            return None
        content = await capture(["wl-paste", "--no-newline", "--type", kind], self.env)
        return None if content is None else content.decode(errors="replace")

    async def changes(self) -> AsyncIterator[None]:
        try:
            process = await asyncio.create_subprocess_exec(
                "wl-paste",
                "--watch",
                "echo",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except OSError as error:
            raise WatcherUnavailable(f"wl-paste: {error}") from error
        stdout = cast(asyncio.StreamReader, process.stdout)  # Both PIPE, so never None.
        stderr = cast(asyncio.StreamReader, process.stderr)
        try:
            while line := await stdout.readline():
                if line.strip() == b"":
                    yield None
            message = (await stderr.read(1024)).decode(errors="replace").strip()
            await process.wait()
            raise WatcherUnavailable(message or f"wl-paste exited with {process.returncode}")
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
