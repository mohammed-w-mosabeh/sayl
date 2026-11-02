"""The SPEC acceptance test: kill sayld during 5 active downloads, restart, all resume."""

import asyncio
import hashlib
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from daemon.config import Paths
from daemon.engines.hybrid import SegmentState
from daemon.models import Download, EngineName, SegmentSource, Status
from daemon.queue import backoff
from tests.origin import SHA256, SIZE, Origin
from tests.stack import free_port, running_stack


@pytest.fixture
async def origin() -> AsyncIterator[Origin]:
    server = Origin()
    await server.start()
    yield server
    await server.stop()


def start_daemon(home: Path) -> subprocess.Popen[bytes]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("XDG_")}
    code = "from daemon.main import main; main()"
    return subprocess.Popen(  # noqa: S603 - argument list
        [sys.executable, "-c", code], env={**env, "HOME": str(home)}
    )


async def client_for(home: Path, port: int) -> httpx.AsyncClient:
    token = Paths.from_env({"HOME": str(home)}).token
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30)
    for _ in range(600):
        try:
            client.headers["Authorization"] = f"Bearer {token.read_text()}"
            await client.get("/settings")
            return client
        except (httpx.TransportError, OSError):
            await asyncio.sleep(0.05)
    raise AssertionError("sayld did not start")


async def test_kill_during_five_downloads_then_all_resume(
    tmp_path: Path, origin: Origin, request: pytest.FixtureRequest
) -> None:
    port = free_port()
    config = tmp_path / ".config/sayl"
    config.mkdir(parents=True)
    out = tmp_path / "out"
    (config / "config.toml").write_text(
        f'port = {port}\nmax_concurrent = 5\ndownload_dir = "{out}"\n'
    )
    first = start_daemon(tmp_path)
    request.addfinalizer(first.kill)  # A failed test must not leave a daemon running.
    client = await client_for(tmp_path, port)
    ids = []
    for number in range(5):
        created = await client.post("/downloads", json={"url": origin.url(f"/slow/{number}.bin")})
        ids.append(created.json()["id"])
    for download_id in ids:
        while (await client.get(f"/downloads/{download_id}")).json()["done_bytes"] == 0:
            await asyncio.sleep(0.05)

    first.send_signal(signal.SIGKILL)
    first.wait()
    await asyncio.sleep(1.5)  # aria2c stops with its parent.

    second = start_daemon(tmp_path)
    request.addfinalizer(second.kill)
    await client.aclose()
    client = await client_for(tmp_path, port)
    try:
        for _ in range(1200):
            rows = [(await client.get(f"/downloads/{i}")).json() for i in ids]
            if all(row["status"] in ("completed", "failed") for row in rows):
                break
            await asyncio.sleep(0.1)
        assert [row["status"] for row in rows] == ["completed"] * 5, rows[0]["error"]
        for number in range(5):
            data = (out / f"{number}.bin").read_bytes()
            assert hashlib.sha256(data).hexdigest() == SHA256
    finally:
        await client.aclose()
        second.send_signal(signal.SIGTERM)
        assert await asyncio.to_thread(second.wait, 30) == 0


async def test_a_stream_only_capture_needs_a_new_link_after_a_crash(tmp_path: Path) -> None:
    async with running_stack(tmp_path) as stack:
        store = stack.daemon.services.store
        row = Download(
            url="http://127.0.0.1:9/u",
            dest_dir=str(tmp_path),
            engine=EngineName.HYBRID,
            status=Status.ACTIVE,
        )
        row.engine_ref = row.id
        store.add(row)
        await stack.daemon.services.queue.recover()
        await asyncio.gather(*stack.daemon.services.queue.recoveries)
        result = store.download(row.id)
        assert result is not None
        assert (result.status, result.error) == (Status.NEEDS_REFRESH, "link expired, refresh it")


async def test_paused_capture_recovers_from_its_segment_map(tmp_path: Path, origin: Origin) -> None:
    async with running_stack(tmp_path) as stack:
        store = stack.daemon.services.store
        stack.downloads.mkdir()
        row = Download(
            url=origin.url("/normal.bin"),
            filename="f.bin",
            dest_dir=str(stack.downloads),
            engine=EngineName.HYBRID,
            status=Status.PAUSED,
            total_bytes=SIZE,
            etag='"lab-v1"',
            resumable=True,
        )
        row.engine_ref = row.id
        store.add(row)
        store.save_segments_sync(row.id, [SegmentState(0, SIZE - 1, SegmentSource.STREAM)])
        (stack.downloads / f".{row.id}.part").touch()
        await stack.daemon.services.queue.recover()
        await asyncio.gather(*stack.daemon.services.queue.recoveries)
        data = (stack.downloads / "f.bin").read_bytes()
        assert hashlib.sha256(data).hexdigest() == SHA256
        result = store.download(row.id)
        assert result is not None
        assert result.status is Status.COMPLETED


def test_backoff_grows_and_is_capped() -> None:
    assert 0.5 <= backoff(0) <= 1
    assert 30 <= backoff(20) <= 60
