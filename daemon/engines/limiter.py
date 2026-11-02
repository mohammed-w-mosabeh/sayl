"""Token-bucket speed limit shared by every connection it is passed to."""

import asyncio
import time

BURST_SECONDS = 0.25


class Limiter:
    def __init__(self, rate: int = 0) -> None:
        self.rate = rate
        self._tokens = 0.0
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def set_rate(self, rate: int) -> None:
        self.rate = rate

    async def acquire(self, size: int) -> None:
        """Wait until size bytes may pass. A rate of 0 means unlimited."""
        if self.rate <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            burst = self.rate * BURST_SECONDS
            self._tokens = min(burst, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            self._tokens -= size
            if self._tokens < 0:
                await asyncio.sleep(-self._tokens / self.rate)
