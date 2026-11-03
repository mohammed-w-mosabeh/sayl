"""Proves the limits locally: elapsed time must be within 15% of size / limit.

Run alone, as tracing slows yt-dlp's per-segment work: uv run pytest -m timing --no-cov

The origin serves at full speed, so only the limit under test can slow a download down.
"""

import asyncio
import hashlib
import shutil
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from daemon.config import Config
from daemon.engines.aria2 import Aria2Engine
from daemon.engines.base import Job
from daemon.engines.hybrid import HybridEngine
from daemon.engines.ytdlp import YtDlpEngine
from daemon.models import Status
from tests.origin import BODY, SHA256, SIZE, Origin, open_stream
from tests.test_hybrid import MemoryStore

pytestmark = pytest.mark.timing

GLOBAL = 1_000_000
PER_DOWNLOAD = 512 * 1024
TOLERANCE = 0.15
SEGMENT = 512 * 1024  # A real 720p segment. Smaller ones measure per-segment overhead instead.
SEGMENTS = 12
ARIA2_SIZE = SIZE * 3  # aria2 finishes on a 1 s tick, so short transfers measure the tick.
PLAYLIST = "".join(
    [
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n",
        *(f"#EXTINF:1.0,\ns{n}.ts\n" for n in range(SEGMENTS)),
        "#EXT-X-ENDLIST\n",
    ]
)


def assert_rate(elapsed: float, size: int, limit: int) -> None:
    expected = size / limit
    print(f"RATE {size} {limit} {elapsed:.2f} {expected:.2f} {elapsed / expected:.3f}")
    assert expected * (1 - TOLERANCE) <= elapsed <= expected * (1 + TOLERANCE), (
        f"{size} bytes at {limit} B/s took {elapsed:.2f}s, expected {expected:.2f}s"
    )


@pytest.fixture
async def origin() -> AsyncIterator[Origin]:
    server = Origin()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def aria2(tmp_path: Path) -> AsyncIterator[Aria2Engine]:
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is not installed")
    engine = Aria2Engine(tmp_path)
    await engine.start()
    yield engine
    await engine.stop()


async def timed(start: Callable[[], Coroutine[Any, Any, None]]) -> float:
    began = time.monotonic()
    await start()
    return time.monotonic() - began


async def aria2_download(engine: Aria2Engine, url: str, directory: Path, limit: int = 0) -> None:
    gid = await engine.add(Job("d", url, str(directory), "f.bin", speed_limit=limit))
    while (await engine.status(gid)).status is not Status.COMPLETED:
        await asyncio.sleep(0.02)
    expected = hashlib.sha256(BODY * 3).hexdigest()
    assert hashlib.sha256((directory / "f.bin").read_bytes()).hexdigest() == expected


async def test_aria2_global_limit(aria2: Aria2Engine, origin: Origin, tmp_path: Path) -> None:
    await aria2.set_global_speed_limit(GLOBAL)
    url = origin.url("/x3/a.bin")
    elapsed = await timed(lambda: aria2_download(aria2, url, tmp_path / "o"))
    assert_rate(elapsed, ARIA2_SIZE, GLOBAL)


async def test_aria2_per_download_limit(aria2: Aria2Engine, origin: Origin, tmp_path: Path) -> None:
    url = origin.url("/x3/b.bin")
    elapsed = await timed(lambda: aria2_download(aria2, url, tmp_path / "o", PER_DOWNLOAD))
    assert_rate(elapsed, ARIA2_SIZE, PER_DOWNLOAD)


async def test_aria2_global_limit_holds_after_a_slow_period(
    aria2: Aria2Engine, origin: Origin, tmp_path: Path
) -> None:
    await aria2.set_global_speed_limit(PER_DOWNLOAD)
    await aria2_download(aria2, origin.url("/x3/slow.bin"), tmp_path / "a")
    await aria2.set_global_speed_limit(GLOBAL)
    url = origin.url("/x3/fast.bin")
    elapsed = await timed(lambda: aria2_download(aria2, url, tmp_path / "b"))
    assert_rate(elapsed, ARIA2_SIZE, GLOBAL)


async def test_aria2_unlimited_is_far_faster(
    aria2: Aria2Engine, origin: Origin, tmp_path: Path
) -> None:
    url = origin.url("/x3/c.bin")
    elapsed = await timed(lambda: aria2_download(aria2, url, tmp_path / "o"))
    assert elapsed < ARIA2_SIZE / GLOBAL / 3


async def hybrid_download(
    tmp_path: Path, origin: Origin, path: str, *, per_download: int = 0, shared: int = 0
) -> float:
    engine = HybridEngine(MemoryStore(), connections=4, min_bytes=1024)
    engine.limiter.set_rate(shared)
    url = origin.url(path)
    info, stream = await open_stream(url)
    job = Job("d", url, str(tmp_path), "f.bin", speed_limit=per_download, stream=info)
    elapsed = await timed(lambda: _attach(engine, job, stream))
    await engine.client.aclose()
    assert hashlib.sha256((tmp_path / "f.bin").read_bytes()).hexdigest() == SHA256
    return elapsed


async def _attach(engine: HybridEngine, job: Job, stream: AsyncIterator[bytes]) -> None:
    await engine.attach(job, stream)


async def test_hybrid_global_limit_spans_every_connection(tmp_path: Path, origin: Origin) -> None:
    assert_rate(await hybrid_download(tmp_path, origin, "/a.bin", shared=GLOBAL), SIZE, GLOBAL)
    assert origin.ranges["/a.bin"] == 3  # The limit held while accelerated.


async def test_hybrid_per_download_limit(tmp_path: Path, origin: Origin) -> None:
    elapsed = await hybrid_download(tmp_path, origin, "/b.bin", per_download=PER_DOWNLOAD)
    assert_rate(elapsed, SIZE, PER_DOWNLOAD)


async def test_hybrid_unlimited_is_far_faster(tmp_path: Path, origin: Origin) -> None:
    assert await hybrid_download(tmp_path, origin, "/c.bin") < SIZE / GLOBAL / 3


async def serve_hls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """A local HLS stream: one media playlist and a fixed number of segments."""
    try:
        while True:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            method, target = head.split()[:2]
            path = urlsplit(target).path
            if path.endswith(".m3u8"):
                body, kind = PLAYLIST.encode(), "application/vnd.apple.mpegurl"
            else:
                body, kind = bytes(SEGMENT), "video/mp2t"
            lines = ["HTTP/1.1 200 OK", f"Content-Length: {len(body)}", f"Content-Type: {kind}"]
            writer.write("\r\n".join(lines).encode() + b"\r\n\r\n")
            if method != "HEAD":
                writer.write(body)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


@pytest.fixture
async def hls_url() -> AsyncIterator[str]:
    server = await asyncio.start_server(serve_hls, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/stream/v.m3u8"
    server.close()


async def hls_rate_window(engine: YtDlpEngine, url: str, directory: Path, limit: int) -> None:
    """Time from the first byte: extractor loading has nothing to do with the limit."""
    engine_id = await engine.add(Job("d", url, str(directory), speed_limit=limit))
    while (await engine.status(engine_id)).done == 0:
        await asyncio.sleep(0.01)
    began = time.monotonic()
    first = (await engine.status(engine_id)).done
    run = engine.runs[engine_id]
    assert run.task is not None
    await run.task
    elapsed = time.monotonic() - began
    status = await engine.status(engine_id)
    assert status.status is Status.COMPLETED, status.error
    assert_rate(elapsed, SEGMENTS * SEGMENT - first, limit or engine.global_limit)


async def test_ytdlp_per_download_limit_on_hls(hls_url: str, tmp_path: Path) -> None:
    await hls_rate_window(YtDlpEngine(), hls_url, tmp_path, PER_DOWNLOAD)


async def test_ytdlp_global_limit_on_hls(hls_url: str, tmp_path: Path) -> None:
    engine = YtDlpEngine()
    await engine.set_global_speed_limit(GLOBAL)
    await hls_rate_window(engine, hls_url, tmp_path, 0)


async def test_saved_limit_is_applied_at_startup(tmp_path: Path) -> None:
    from tests.stack import running_stack

    async with running_stack(tmp_path, speed_limit=GLOBAL) as stack:
        assert stack.daemon.hybrid.limiter.rate == GLOBAL
        await stack.client.put("/settings", json={"speed_limit": PER_DOWNLOAD})
        assert stack.daemon.hybrid.limiter.rate == PER_DOWNLOAD
    assert Config().speed_limit == 0
