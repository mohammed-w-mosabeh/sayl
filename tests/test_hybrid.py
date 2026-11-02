import asyncio
import hashlib
import itertools
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from daemon.engines.base import EngineError, Job, StreamInfo
from daemon.engines.hybrid import (
    HybridEngine,
    SegmentState,
    may_accelerate,
    plan_segments,
)
from daemon.models import PolicyReason, SegmentSource, Status
from tests.origin import BODY, ETAG, SHA256, SIZE, TOKEN, Origin, open_stream


class MemoryStore:
    def __init__(self) -> None:
        self.blocked: dict[str, PolicyReason] = {}
        self.saved: dict[str, list[SegmentState]] = {}

    async def save_segments(self, download_id: str, segments: Sequence[SegmentState]) -> None:
        self.saved[download_id] = list(segments)

    async def host_blocked(self, host: str) -> bool:
        return host in self.blocked

    async def block_host(self, host: str, reason: PolicyReason) -> None:
        self.blocked[host] = reason


async def run_download(
    tmp_path: Path, origin: Origin, path: str, store: MemoryStore, name: str = "f.bin"
) -> tuple[HybridEngine, Path]:
    engine = HybridEngine(store, connections=4, min_bytes=1024)
    url = origin.url(path)
    info, stream = await open_stream(url)
    job = Job(id="d1", url=url, dest_dir=str(tmp_path), filename=name, stream=info)
    await engine.attach(job, stream)
    await engine.client.aclose()
    return engine, tmp_path / name


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
async def origin() -> AsyncIterator[Origin]:
    server = Origin()
    await server.start()
    yield server
    await server.stop()


async def test_range_capable_link_is_accelerated(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    engine, file = await run_download(tmp_path, origin, "/normal.bin", store)
    assert sha(file) == SHA256
    assert origin.hits["/normal.bin"] == 4  # 1 original + 3 range workers.
    assert origin.ranges["/normal.bin"] == 3
    assert not store.blocked
    assert (await engine.status("d1")).status is Status.COMPLETED


async def test_path_token_link_is_requested_exactly_once(tmp_path: Path, origin: Origin) -> None:
    path = f"/once/{TOKEN}/file.bin"
    await run_download(tmp_path, origin, path, MemoryStore())
    assert sha(tmp_path / "f.bin") == SHA256
    assert origin.hits[path] == 1


async def test_hint_miss_falls_back_and_stays_correct(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    path = "/once/short/file.bin"
    await run_download(tmp_path, origin, path, store)
    assert sha(tmp_path / "f.bin") == SHA256
    assert origin.hits[path] == 2  # The canary: only one mistaken request.
    assert store.blocked == {"127.0.0.1": PolicyReason.GONE}


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/session.bin", PolicyReason.AUTH),
        ("/changed.bin", PolicyReason.VALIDATOR_CHANGED),
        ("/plain200.bin", PolicyReason.NOT_206),
    ],
)
async def test_rejection_finishes_on_the_stream_and_records_the_host(
    tmp_path: Path, origin: Origin, path: str, reason: PolicyReason
) -> None:
    store = MemoryStore()
    await run_download(tmp_path, origin, path, store)
    assert sha(tmp_path / "f.bin") == SHA256
    assert origin.ranges[path] == 1
    assert store.blocked == {"127.0.0.1": reason}


async def test_a_blocked_host_skips_acceleration(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    store.blocked["127.0.0.1"] = PolicyReason.AUTH
    await run_download(tmp_path, origin, "/normal.bin", store)
    assert sha(tmp_path / "f.bin") == SHA256
    assert origin.hits["/normal.bin"] == 1


async def test_stream_only_when_ranges_are_not_offered(tmp_path: Path) -> None:
    engine = HybridEngine(MemoryStore(), connections=4, min_bytes=1)

    async def body() -> AsyncIterator[bytes]:
        yield b"ab"
        yield b"cd"

    job = Job(id="d", url="http://x/f", dest_dir=str(tmp_path), filename="f", stream=StreamInfo())
    await engine.attach(job, body())
    assert (tmp_path / "f").read_bytes() == b"abcd"
    await engine.client.aclose()


async def test_short_stream_waits_for_a_new_link_and_keeps_the_partial(tmp_path: Path) -> None:
    engine = HybridEngine(MemoryStore(), connections=4)

    async def body() -> AsyncIterator[bytes]:
        yield b"ab"

    info = StreamInfo(total=10)
    await engine.attach(Job("d", "http://x/f", str(tmp_path), "f", stream=info), body())
    status = await engine.status("d")
    assert status.status is Status.NEEDS_REFRESH
    assert status.error == "link expired, refresh it"
    assert (tmp_path / ".d.part").exists()
    await engine.client.aclose()


async def test_name_from_the_server_stays_inside_dest_dir(tmp_path: Path) -> None:
    engine = HybridEngine(MemoryStore())

    async def body() -> AsyncIterator[bytes]:
        yield b"x"

    job = Job("d", "http://x/f", str(tmp_path), "../../.bashrc")
    await engine.attach(job, body())
    assert (tmp_path / "bashrc").read_bytes() == b"x"
    await engine.client.aclose()


async def test_cancel_stops_a_stream_and_removes_the_partial(tmp_path: Path) -> None:
    engine = HybridEngine(MemoryStore())
    started = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        while True:
            yield b"x"
            started.set()
            await asyncio.sleep(0)

    job = Job("d", "http://x/f", str(tmp_path), "f")
    task = asyncio.create_task(engine.attach(job, body()))
    await started.wait()
    await engine.cancel("d")
    await asyncio.wait_for(task, 5)
    assert (await engine.status("d")).status is Status.CANCELED
    assert list(tmp_path.iterdir()) == []
    await engine.client.aclose()


async def test_pause_holds_the_stream_without_closing_it(tmp_path: Path) -> None:
    engine = HybridEngine(MemoryStore())
    pulled = 0
    gate = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        nonlocal pulled
        for _ in range(3):
            pulled += 1
            yield b"x"
            await gate.wait()

    job = Job("d", "http://x/f", str(tmp_path), "f")
    task = asyncio.create_task(engine.attach(job, body()))
    await asyncio.sleep(0.05)
    await engine.pause("d")
    gate.set()
    await asyncio.sleep(0.05)
    assert (await engine.status("d")).status is Status.PAUSED
    assert pulled <= 2
    await engine.resume("d")
    await asyncio.wait_for(task, 5)
    assert (tmp_path / "f").read_bytes() == b"xxx"
    await engine.client.aclose()


async def test_shutdown_keeps_the_partial_for_recovery(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    engine = HybridEngine(store, connections=4, min_bytes=1024)
    url = origin.url("/normal.bin")
    info = StreamInfo(total=SIZE, accept_ranges=True, etag='"lab-v1"')
    job = Job("d", url, str(tmp_path), "f", stream=info)

    async def stalled() -> AsyncIterator[bytes]:
        yield bytes(1024)
        await asyncio.Event().wait()

    task = asyncio.create_task(engine.attach(job, stalled()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / ".d.part").exists()

    await engine.continue_download(job, [SegmentState(0, SIZE - 1, SegmentSource.RANGE)])
    assert sha(tmp_path / "f") == SHA256
    await engine.client.aclose()


CUT = SIZE // 2


async def cut_stream() -> AsyncIterator[bytes]:
    """The original connection dies halfway through."""
    yield BODY[:CUT]
    raise EngineError("the stream closed early")


def info_for(**change: object) -> StreamInfo:
    values: dict[str, object] = {"total": SIZE, "accept_ranges": True, "etag": ETAG}
    return StreamInfo(**{**values, **change})  # type: ignore[arg-type]


async def lose_stream(
    tmp_path: Path, origin: Origin, store: MemoryStore, path: str, info: StreamInfo
) -> tuple[HybridEngine, Job]:
    engine = HybridEngine(store, connections=1)
    job = Job("d", origin.url(path), str(tmp_path), "f", stream=info)
    await engine.attach(job, cut_stream())
    return engine, job


async def test_lost_stream_resumes_missing_bytes_with_range(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    engine, _ = await lose_stream(tmp_path, origin, store, "/normal.bin", info_for())
    assert (await engine.status("d")).status is Status.COMPLETED
    assert sha(tmp_path / "f") == SHA256
    assert origin.range_log == [f"bytes={CUT}-{SIZE - 1}"]  # Never from the start.
    await engine.client.aclose()


async def test_range_refusal_waits_for_a_new_link(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    engine, _ = await lose_stream(tmp_path, origin, store, "/session.bin", info_for())
    status = await engine.status("d")
    assert (status.status, status.error) == (Status.NEEDS_REFRESH, "link expired, refresh it")
    assert status.done == CUT
    assert store.blocked == {"127.0.0.1": PolicyReason.AUTH}
    await engine.client.aclose()


@pytest.mark.parametrize(
    "info",
    [info_for(accept_ranges=False), info_for(etag=""), info_for(etag='W/"weak"')],
)
async def test_no_range_request_without_proof_of_support(
    tmp_path: Path, origin: Origin, info: StreamInfo
) -> None:
    engine, _ = await lose_stream(tmp_path, origin, MemoryStore(), "/normal.bin", info)
    assert (await engine.status("d")).status is Status.NEEDS_REFRESH
    assert not origin.hits
    await engine.client.aclose()


async def test_a_host_on_record_is_not_tried_again(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    store.blocked["127.0.0.1"] = PolicyReason.GONE
    engine, _ = await lose_stream(tmp_path, origin, store, "/normal.bin", info_for())
    assert (await engine.status("d")).status is Status.NEEDS_REFRESH
    assert not origin.hits
    await engine.client.aclose()


async def test_refresh_continues_from_the_same_offset(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    engine, job = await lose_stream(tmp_path, origin, store, "/session.bin", info_for())
    origin.range_log.clear()
    job.url = origin.url("/fresh.bin")
    await engine.continue_download(job, store.saved["d"], respect_policy=False)
    assert (await engine.status("d")).status is Status.COMPLETED
    assert origin.range_log == [f"bytes={CUT}-{SIZE - 1}"]
    assert sha(tmp_path / "f") == SHA256
    await engine.client.aclose()


async def test_refresh_to_a_different_file_is_refused(tmp_path: Path, origin: Origin) -> None:
    store = MemoryStore()
    engine, job = await lose_stream(tmp_path, origin, store, "/session.bin", info_for())
    job.url = origin.url("/changed.bin")
    await engine.continue_download(job, store.saved["d"], respect_policy=False)
    status = await engine.status("d")
    assert status.status is Status.NEEDS_REFRESH
    assert "another" in status.error
    assert status.done == CUT
    await engine.client.aclose()


async def test_refresh_learns_the_size_when_it_was_never_known(
    tmp_path: Path, origin: Origin
) -> None:
    store = MemoryStore()
    engine = HybridEngine(store, connections=1)
    job = Job("d", origin.url("/session.bin"), str(tmp_path), "f", stream=StreamInfo(etag=ETAG))

    async def stream() -> AsyncIterator[bytes]:
        yield BODY[:CUT]
        raise EngineError("the stream closed early")

    await engine.attach(job, stream())
    assert (await engine.status("d")).status is Status.NEEDS_REFRESH
    job.url = origin.url("/fresh.bin")
    await engine.continue_download(job, store.saved["d"], respect_policy=False)
    assert origin.range_log[-1] == f"bytes={CUT}-"
    assert sha(tmp_path / "f") == SHA256
    await engine.client.aclose()


def test_plan_covers_the_rest_exactly_once() -> None:
    boundary, ranges = plan_segments(100, 1100, 4)
    assert boundary == 350
    ordered = sorted(ranges)
    assert ordered[0][0] == boundary
    assert ordered[-1][1] == 1099
    assert ranges[0] == ordered[-1]  # Tail first.
    assert all(a[1] + 1 == b[0] for a, b in itertools.pairwise(ordered))


@pytest.mark.parametrize(
    ("change", "allowed"),
    [
        ({}, True),
        ({"method": "POST"}, False),
        ({"accept_ranges": False}, False),
        ({"total": None}, False),
        ({"total": 5}, False),
        ({"etag": "", "last_modified": ""}, False),
        ({"etag": 'W/"weak"'}, False),
        ({"etag": 'W/"weak"', "last_modified": "yesterday"}, True),
        ({"content_encoding": "gzip"}, False),
    ],
)
def test_acceleration_conditions(change: dict[str, object], allowed: bool) -> None:
    values: dict[str, object] = {"total": 100, "accept_ranges": True, "etag": '"a"'}
    info = StreamInfo(**{**values, **change})  # type: ignore[arg-type]
    assert may_accelerate(info, "http://a/f.bin", 10, 4) is allowed


def test_the_hint_skips_acceleration_but_one_connection_also_does() -> None:
    info = StreamInfo(total=100, accept_ranges=True, etag='"a"')
    assert not may_accelerate(info, f"http://a/{TOKEN}/f.bin", 10, 4)
    assert not may_accelerate(info, "http://a/f.bin?sig=1", 10, 4)
    assert not may_accelerate(info, "http://a/f.bin", 10, 1)
