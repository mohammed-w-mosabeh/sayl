import asyncio
import time

from daemon.engines.limiter import Limiter


async def test_unlimited_never_waits() -> None:
    limiter = Limiter()
    started = time.monotonic()
    for _ in range(100):
        await limiter.acquire(10_000_000)
    assert time.monotonic() - started < 0.1


async def test_limit_holds_the_rate() -> None:
    limiter = Limiter(1_000_000)
    started = time.monotonic()
    for _ in range(10):
        await limiter.acquire(100_000)
    assert 0.6 < time.monotonic() - started < 1.2  # 1 MB at 1 MB/s, less the burst.


async def test_removing_the_limit_releases_every_waiter() -> None:
    """Regression: waiters queued on the lock divided by a rate of 0 and died."""
    limiter = Limiter(100_000)
    waiters = [asyncio.create_task(limiter.acquire(1_000_000)) for _ in range(3)]
    await asyncio.sleep(0.1)
    limiter.set_rate(0)
    await asyncio.wait_for(asyncio.gather(*waiters), 1)


async def test_raising_the_limit_wakes_a_waiter() -> None:
    limiter = Limiter(10_000)
    waiter = asyncio.create_task(limiter.acquire(100_000))  # Ten seconds at the old rate.
    await asyncio.sleep(0.1)
    limiter.set_rate(10_000_000)
    await asyncio.wait_for(waiter, 1)
