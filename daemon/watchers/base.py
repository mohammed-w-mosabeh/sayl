"""What every clipboard watcher provides, and the subprocess helper they share."""

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import cast

READ_TIMEOUT = 2.0
MAX_READ_BYTES = 64 * 1024
# Password managers mark what they copy. Sayl never reads it.
SECRET_HINTS = frozenset({"x-kde-passwordManagerHint"})
TEXT_TYPES = ("UTF8_STRING", "text/plain;charset=utf-8", "text/plain", "STRING", "TEXT")


class WatcherUnavailable(Exception):
    """This display server does not support the watcher; try the next one."""


async def capture(argv: Sequence[str], env: Mapping[str, str]) -> bytes | None:
    """Run a clipboard tool and return its output, or None on failure, timeout, or excess."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=dict(env),
        )
    except OSError:
        return None
    stdout = cast(asyncio.StreamReader, process.stdout)  # PIPE above, so never None.
    try:
        output = await asyncio.wait_for(stdout.read(MAX_READ_BYTES + 1), READ_TIMEOUT)
        await asyncio.wait_for(process.wait(), READ_TIMEOUT)
    except TimeoutError:
        return None
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if process.returncode != 0 or len(output) > MAX_READ_BYTES:
        return None
    return output


def text_type(types: Sequence[str]) -> str | None:
    """The type to read, or None for a secret or for content that is not text."""
    if SECRET_HINTS & set(types):
        return None
    return next((kind for kind in TEXT_TYPES if kind in types), None)


class Watcher(ABC):
    name: str

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = dict(env)

    @abstractmethod
    async def read(self) -> str | None:
        """The clipboard text now, or None when it is empty, not text, or a secret."""

    @abstractmethod
    def changes(self) -> AsyncIterator[None]:
        """Yields each time the clipboard may have changed. Raises WatcherUnavailable."""
