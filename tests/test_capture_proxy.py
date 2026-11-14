"""The production addon end to end: proxy, real sayld, counting origin. Plain HTTP/1.1 here;
the browser lab covers TLS and HTTP/2 (poc/lab/run_matrix.py --target capture)."""

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.capture_env import BigOrigin, body, running_proxy
from tests.stack import Stack, running_stack

MB = 1024 * 1024
FILES = {
    "/plain.bin": body(40 * MB),
    "/huge.bin": body(64 * MB),
    "/small.bin": body(3 * MB),
    "/login.bin": body(3 * MB),
}


@pytest.fixture
async def origin() -> AsyncIterator[BigOrigin]:
    server = BigOrigin(FILES)
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path, accelerate_min_bytes=1024, connections=4) as running:
        yield running


async def wait_done(stack: Stack, seconds: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rows = (await stack.client.get("/downloads")).json()
        if rows and rows[0]["status"] in ("completed", "failed", "needs_refresh"):
            return dict(rows[0])
        await asyncio.sleep(0.1)
    raise AssertionError(f"not finished: {rows}")


def saved_hash(stack: Stack, row: dict[str, Any]) -> str:
    return hashlib.sha256((Path(row["dest_dir"]) / row["filename"]).read_bytes()).hexdigest()


async def test_capture_hands_the_open_response_to_sayld(
    stack: Stack, origin: BigOrigin, tmp_path: Path
) -> None:
    async with running_proxy(stack, tmp_path) as proxy, proxy.client() as browser:
        answer = await browser.get(origin.url("/small.bin"))
        assert (answer.status_code, answer.content) == (204, b"")  # The browser does nothing.
        row = await wait_done(stack)
    assert row["status"] == "completed"
    assert row["engine"] == "hybrid"
    assert saved_hash(stack, row) == hashlib.sha256(FILES["/small.bin"]).hexdigest()
    assert origin.hits["/small.bin"] == 1  # Stream first: never requested again.


async def test_cookies_and_authorization_reach_the_download(
    stack: Stack, origin: BigOrigin, tmp_path: Path
) -> None:
    async with running_proxy(stack, tmp_path) as proxy, proxy.client() as browser:
        browser.headers["Cookie"] = "sid=secret"
        browser.headers["Authorization"] = "Bearer lab-token"
        assert (await browser.get(origin.url("/login.bin"))).status_code == 204
        row = await wait_done(stack)
    assert row["status"] == "completed"
    assert saved_hash(stack, row) == hashlib.sha256(FILES["/login.bin"]).hexdigest()
    sent = origin.headers_seen[-1]
    assert (sent["cookie"], sent["authorization"]) == ("sid=secret", "Bearer lab-token")


async def test_upstream_is_paused_while_sayld_is_slow(
    stack: Stack, origin: BigOrigin, tmp_path: Path
) -> None:
    await stack.client.put("/settings", json={"speed_limit": 2 * MB})
    async with running_proxy(stack, tmp_path) as proxy, proxy.client() as browser:
        assert (await browser.get(origin.url("/huge.bin"))).status_code == 204
        await asyncio.sleep(3)  # Socket buffers on every hop fill first.
        early = origin.sent["/huge.bin"]
        await asyncio.sleep(3)
        late = origin.sent["/huge.bin"]
        upload = next(iter(proxy.addon.uploads.values()))
        buffered = upload.buffered
        await stack.client.put("/settings", json={"speed_limit": 0})
        row = await wait_done(stack)
    # Without back-pressure all 64 MiB arrive in well under a second. With it, the origin
    # sends at sayld's pace once the buffers are full: about 2 MiB/s, not the whole file.
    assert late < 48 * MB, late
    assert late - early < 3 * 2 * MB + 4 * MB, (early, late)
    assert buffered <= 4 * MB + 256 * 1024
    assert row["status"] == "completed"
    assert saved_hash(stack, row) == hashlib.sha256(FILES["/huge.bin"]).hexdigest()
    assert origin.hits["/huge.bin"] == 1


async def test_upstream_closes_once_range_workers_finish_the_file(
    stack: Stack, origin: BigOrigin, tmp_path: Path
) -> None:
    await stack.client.put("/settings", json={"speed_limit": 3 * MB})
    async with running_proxy(stack, tmp_path) as proxy, proxy.client() as browser:
        assert (await browser.get(origin.url("/ranged/plain.bin"))).status_code == 204
        await asyncio.sleep(1)
        await stack.client.put("/settings", json={"speed_limit": 0})
        row = await wait_done(stack)
        await asyncio.sleep(0.5)
        assert not proxy.addon.uploads  # The upload was stopped, not left waiting.
    assert row["status"] == "completed"
    assert saved_hash(stack, row) == hashlib.sha256(FILES["/plain.bin"]).hexdigest()
    assert origin.ranges["/ranged/plain.bin"] >= 1
    # The original response was cut off: it did not run to the end in the background.
    assert origin.sent["/ranged/plain.bin"] < 40 * MB * 3
    assert origin.closed_early["/ranged/plain.bin"] >= 1


async def test_sayld_unreachable_leaves_the_download_to_the_browser(
    origin: BigOrigin, tmp_path: Path
) -> None:
    async with (
        running_stack(tmp_path) as stack,
        running_proxy(stack, tmp_path) as proxy,
        proxy.client() as browser,
    ):
        await stack.client.aclose()
        proxy.addon.client = type(proxy.addon.client)(base_url="http://127.0.0.1:9")
        answer = await browser.get(origin.url("/small.bin"))
        assert answer.status_code == 200
        assert answer.content == FILES["/small.bin"]


async def test_many_held_downloads_do_not_block_the_site(
    stack: Stack, origin: BigOrigin, tmp_path: Path
) -> None:
    """mitmproxy allows five upstreams per host and browser connection; held ones must not count."""
    await stack.client.put("/settings", json={"speed_limit": 1 * MB})
    async with running_proxy(stack, tmp_path) as proxy, proxy.client() as browser:
        for _ in range(6):
            answer = await asyncio.wait_for(browser.get(origin.url("/huge.bin")), 10)
            assert answer.status_code == 204
        assert len(proxy.addon.uploads) >= 5  # Still open, each holding its upstream.
        await stack.client.put("/settings", json={"speed_limit": 0})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = (await stack.client.get("/downloads")).json()
            if len(rows) == 6 and all(row["status"] == "completed" for row in rows):
                break
            await asyncio.sleep(0.2)
    assert [row["status"] for row in rows] == ["completed"] * 6
