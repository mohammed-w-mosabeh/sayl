"""sayld runs the real sayl-capture child: enable, capture through it, supervise, uninstall."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from daemon.capture.manager import CaptureManager
from daemon.capture.sysproxy import Desktop, Result
from tests.capture_env import BigOrigin, body
from tests.stack import Stack, running_stack

FILES = {"/file.bin": body(3 * 1024 * 1024)}


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path) as running:
        yield running


@pytest.fixture
async def origin() -> AsyncIterator[BigOrigin]:
    server = BigOrigin(FILES)
    await server.start()
    yield server
    await server.stop()


def manager(stack: Stack) -> CaptureManager:
    capture = stack.daemon.services.capture
    assert capture is not None
    return capture


async def until(check: Any, seconds: float = 20) -> None:
    for _ in range(int(seconds * 20)):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never held")


async def test_enable_capture_through_the_child_then_disable(
    stack: Stack, origin: BigOrigin
) -> None:
    status = (await stack.client.get("/capture")).json()
    assert (status["state"], status["ca"]) == ("off", None)

    status = (await stack.client.put("/capture", json={"enabled": True})).json()
    assert status["state"] == "running"
    assert status["ca"]["name"].startswith("Sayl local CA")
    assert status["system_proxy"] is False

    port = status["port"]
    async with httpx.AsyncClient(
        proxy=f"http://127.0.0.1:{port}",
        headers={"Sec-Fetch-Mode": "navigate"},
        trust_env=False,
    ) as browser:
        assert (await browser.get(origin.url("/file.bin"))).status_code == 204

    async def completed() -> bool:
        rows = (await stack.client.get("/downloads")).json()
        return bool(rows) and rows[0]["status"] == "completed"

    await until(completed)
    row = (await stack.client.get("/downloads")).json()[0]
    saved = (Path(row["dest_dir"]) / row["filename"]).read_bytes()
    assert hashlib.sha256(saved).digest() == hashlib.sha256(FILES["/file.bin"]).digest()

    assert (await stack.client.put("/capture", json={"enabled": False})).json()["state"] == "off"
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    assert (await stack.client.get("/settings")).json()["capture_enabled"] is False


async def test_a_crashed_child_is_restarted(stack: Stack) -> None:
    await stack.client.put("/capture", json={"enabled": True})
    capture = manager(stack)
    first = capture.process
    first.kill()

    async def restarted() -> bool:
        return capture.process is not first and await listening(stack)

    await until(restarted)
    await stack.client.put("/capture", json={"enabled": False})


async def listening(stack: Stack) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", manager(stack).config.capture_port)
    except OSError:
        return False
    writer.close()
    return True


async def test_video_reports_and_bypass(stack: Stack) -> None:
    token = stack.daemon.services.token
    url = f"ws://127.0.0.1:{stack.port}/ws"
    async with websockets.connect(
        url, subprotocols=[websockets.Subprotocol(f"bearer.{token}")]
    ) as socket:
        report = {
            "kind": "manifest",
            "page_url": "https://v.test/watch",
            "url": "https://cdn.v.test/a.m3u8",
        }
        assert (await stack.client.post("/capture/media", json=report)).status_code == 204
        await stack.client.post("/capture/media", json=report)  # The same report again: quiet.
        event = json.loads(await asyncio.wait_for(socket.recv(), 5))
        assert event == {
            "event": "capture.detected",
            "kind": "manifest",
            "page_url": "https://v.test/watch",
            "host": "v.test",
            "url": "https://cdn.v.test/a.m3u8",
            "drm": False,
            "extractor": "",
        }
        pages = (await stack.client.get("/capture/media")).json()
        assert [(p["host"], len(p["items"])) for p in pages] == [("v.test", 1)]

        await stack.client.post("/capture/bypass", json={"host": "Pinned.example"})
        await stack.client.post("/capture/bypass", json={"host": "pinned.example"})
        event = json.loads(await asyncio.wait_for(socket.recv(), 5))
        assert event == {"event": "settings.updated", "keys": ["capture_bypass"]}
    assert (await stack.client.get("/settings")).json()["capture_bypass"] == ["pinned.example"]


async def test_uninstall_removes_the_ca(stack: Stack) -> None:
    await stack.client.put("/capture", json={"enabled": True})
    report = (await stack.client.post("/capture/uninstall")).json()
    assert report["ok"], report
    status = (await stack.client.get("/capture")).json()
    assert (status["state"], status["ca"]) == ("off", None)


async def test_stopping_sayld_restores_the_system_proxy(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeProxy:
        def enable(self, port: int) -> Result:
            calls.append(f"enable {port}")
            return Result(Desktop.GNOME, True)

        def disable(self) -> Result:
            calls.append("disable")
            return Result(Desktop.GNOME, True)

        def active(self) -> bool:
            return bool(calls) and calls[-1].startswith("enable")

    async with running_stack(tmp_path) as stack:
        capture = manager(stack)
        capture.proxy = FakeProxy()  # type: ignore[assignment]
        status = (
            await stack.client.put("/capture", json={"enabled": True, "system_proxy": True})
        ).json()
        assert status["system_proxy"] is True
        assert calls == [f"enable {capture.config.capture_port}"]
    assert calls[-1] == "disable"  # sayld stopped: browsing must not hit a dead port.


async def test_sayl_capture_stops_when_sayld_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    from daemon.capture import run as capture_run

    parents = iter([100, 100, 1])  # Reparented to init: sayld was killed outright.
    monkeypatch.setattr(capture_run.os, "getppid", lambda: next(parents))
    stopped: list[bool] = []
    await asyncio.wait_for(capture_run.watch_parent(lambda: stopped.append(True), 0.01), 5)
    assert stopped == [True]
