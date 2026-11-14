import asyncio
import os
import signal
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from daemon.config import Config, Paths
from daemon.main import _serve, default_download_dir, run
from tests.stack import free_port


def write_config(home: Path) -> None:
    config = home / ".config/sayl"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(f"port = {free_port()}\n")


async def test_run_returns_when_stopped(tmp_path: Path) -> None:
    write_config(tmp_path)
    stop = asyncio.Event()
    stop.set()
    assert await run(stop, Paths.from_env({"HOME": str(tmp_path)})) == 0


async def test_sigterm_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    task = asyncio.create_task(_serve())
    await asyncio.sleep(0.5)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 20) == 0


async def test_shutdown_does_not_wait_for_an_open_capture(tmp_path: Path) -> None:
    """A captured stream stays open as long as its body; SIGTERM must not wait for it."""
    port = free_port()
    config = tmp_path / ".config/sayl"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(f'port = {port}\ndownload_dir = "{tmp_path / "out"}"\n')
    stop = asyncio.Event()
    paths = Paths.from_env({"HOME": str(tmp_path)})
    daemon = asyncio.create_task(run(stop, paths))
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        for _ in range(200):
            if paths.token.exists():
                break
            await asyncio.sleep(0.05)
        client.headers["Authorization"] = f"Bearer {paths.token.read_text().strip()}"
        for _ in range(200):
            try:
                await client.get("/info")
                break
            except httpx.TransportError:
                await asyncio.sleep(0.05)
        attached = await client.post(
            "/downloads/attach",
            json={
                "url": "http://127.0.0.1:9/big.bin",
                "status": 200,
                "response_headers": {"content-type": "application/octet-stream"},
            },
        )
        download = attached.json()["id"]

        async def endless() -> AsyncIterator[bytes]:
            while True:
                yield b"x" * 65536
                await asyncio.sleep(0.01)

        upload = asyncio.create_task(client.put(f"/downloads/{download}/stream", content=endless()))
        await asyncio.sleep(1)
        started = time.monotonic()
        stop.set()
        assert await asyncio.wait_for(daemon, 30) == 0
        assert time.monotonic() - started < 15
        upload.cancel()
        await asyncio.gather(upload, return_exceptions=True)
    assert (tmp_path / "out" / f".{download}.part").stat().st_size > 0  # Kept for recovery.


def test_config_download_dir_wins() -> None:
    assert default_download_dir(Config(download_dir="/x")) == "/x"
    assert default_download_dir(Config())
