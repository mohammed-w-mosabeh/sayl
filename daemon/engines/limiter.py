"""Token-bucket speed limit shared by every connection it is passed to."""

import asyncio
import contextlib
import time

BURST_SECONDS = 0.25


class Limiter:
    def __init__(self, rate: int = 0) -> None:
        self.rate = rate
        self._tokens = 0.0
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()

    def set_rate(self, rate: int) -> None:
        """Takes effect at once, also for a transfer already waiting on the old rate."""
        self.rate = rate
        self._changed.set()

    def _refill(self) -> None:
        now = time.monotonic()
        if self.rate > 0:
            burst = self.rate * BURST_SECONDS
            self._tokens = min(burst, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self, size: int) -> None:
        """Wait until size bytes may pass. A rate of 0 means unlimited."""
        if self.rate <= 0:
            return
        async with self._lock:
            self._refill()
            self._tokens -= size
            # The rate may change while this waits: to 0 (unlimited), or to anything else.
            while self._tokens < 0 and self.rate > 0:
                self._changed.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), -self._tokens / self.rate)
                self._refill()
            if self.rate <= 0:
                self._tokens = 0.0
