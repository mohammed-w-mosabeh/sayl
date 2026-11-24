"""sayld runs the real sayl-capture child: enable, capture through it, supervise, uninstall."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

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


class FakeProxy:
    """Stands in for SystemProxy, so no test can touch the person's desktop settings."""

    calls: ClassVar[list[str]] = []
    stale = False  # Pretend a killed sayld left its setting behind.

    def __init__(self, state_dir: Path, env: Any) -> None:
        self.on = FakeProxy.stale

    def enable(self, port: int, pac_url: str) -> Result:
        FakeProxy.calls.append(f"enable {port} {pac_url}")
        self.on = True
        return Result(Desktop.GNOME, True)

    def disable(self) -> Result:
        FakeProxy.calls.append("disable" if self.on else "disable (nothing)")
        applied, self.on = self.on, False
        return Result(Desktop.GNOME, applied)

    def active(self) -> bool:
        return self.on


@pytest.fixture
def fake_proxy(monkeypatch: pytest.MonkeyPatch) -> type[FakeProxy]:
    monkeypatch.setattr("daemon.capture.manager.SystemProxy", FakeProxy)
    monkeypatch.setattr(FakeProxy, "calls", [])
    monkeypatch.setattr(FakeProxy, "stale", False)
    return FakeProxy


async def test_stopping_sayld_restores_the_system_proxy(
    tmp_path: Path, fake_proxy: type[FakeProxy]
) -> None:
    async with running_stack(tmp_path) as stack:
        capture = manager(stack)
        status = (
            await stack.client.put("/capture", json={"enabled": True, "system_proxy": True})
        ).json()
        assert status["system_proxy"] is True
        assert fake_proxy.calls == [f"enable {capture.config.capture_port} {capture.pac_url}"]
    assert fake_proxy.calls[-1] == "disable"  # sayld stopped: nothing may point at a dead port.


async def test_a_stale_setting_is_restored_on_startup(
    tmp_path: Path, fake_proxy: type[FakeProxy]
) -> None:
    fake_proxy.stale = True  # The last sayld was killed with the proxy applied.
    async with running_stack(tmp_path) as stack:
        assert fake_proxy.calls == ["disable"]  # First thing, before capture is even asked for.
        assert manager(stack).proxy.active() is False


async def test_pac_file_needs_its_secret_not_the_token(
    tmp_path: Path, fake_proxy: type[FakeProxy]
) -> None:
    async with running_stack(tmp_path, capture_passthrough=("opt-in.test",)) as stack:
        capture = manager(stack)
        await stack.client.post("/capture/bypass", json={"host": "pinned.example"})
        async with httpx.AsyncClient(trust_env=False) as browser:  # No bearer token.
            answer = await browser.get(capture.pac_url)
            wrong = await browser.get(f"http://127.0.0.1:{stack.port}/capture/pac/guess.pac")
        assert answer.status_code == 200
        assert answer.headers["content-type"].startswith("application/x-ns-proxy-autoconfig")
        script = answer.text
        assert f'"PROXY 127.0.0.1:{capture.config.capture_port}; DIRECT"' in script
        for host in ("anthropic.com", "claude.ai", "opt-in.test", "pinned.example"):
            assert f'"{host}"' in script
        assert "github.com" not in script
        assert wrong.status_code == 404
        secret = tmp_path / ".local/share/sayl/capture/pac-secret"
        assert secret.stat().st_mode & 0o777 == 0o600


async def test_sayl_capture_stops_when_sayld_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    from daemon.capture import run as capture_run

    parents = iter([100, 100, 1])  # Reparented to init: sayld was killed outright.
    monkeypatch.setattr(capture_run.os, "getppid", lambda: next(parents))
    stopped: list[bool] = []
    await asyncio.wait_for(capture_run.watch_parent(lambda: stopped.append(True), 0.01), 5)
    assert stopped == [True]
