import asyncio
import os
import signal
from pathlib import Path

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


def test_config_download_dir_wins() -> None:
    assert default_download_dir(Config(download_dir="/x")) == "/x"
    assert default_download_dir(Config())
