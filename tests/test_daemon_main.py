import asyncio
import os
import signal

from daemon.main import _serve, run


def test_run_returns_when_stopped() -> None:
    stop = asyncio.Event()
    stop.set()
    assert asyncio.run(run(stop)) == 0


def test_sigterm_exits_cleanly() -> None:
    async def scenario() -> int:
        task = asyncio.create_task(_serve())
        await asyncio.sleep(0)  # let _serve install its handlers and block
        os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(task, timeout=5)

    assert asyncio.run(scenario()) == 0
