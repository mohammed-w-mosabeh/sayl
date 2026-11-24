"""The addon's parts in isolation: Upload back-pressure, events, video reports, TLS refusals."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from mitmproxy.test import tflow, tutils

from daemon.capture import addon as addon_module
from daemon.capture import mitm_adapter
from daemon.capture.addon import Capture, Upload, has_drm


class Gate:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.is_paused = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mitm_adapter, "pause", self.pause)
        monkeypatch.setattr(mitm_adapter, "resume", self.resume)
        monkeypatch.setattr(mitm_adapter, "paused", lambda server: self.is_paused)
        monkeypatch.setattr(mitm_adapter, "cancel", lambda server: self.calls.append("cancel"))

    def pause(self, server: Any) -> None:
        self.is_paused = True
        self.calls.append("pause")

    def resume(self, server: Any) -> None:
        self.is_paused = False
        self.calls.append("resume")


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Gate:
    fake = Gate()
    fake.install(monkeypatch)
    return fake


async def drain(upload: Upload) -> bytes:
    return b"".join([chunk async for chunk in upload.body()])


async def test_upload_pauses_above_high_and_resumes_below_low(gate: Gate) -> None:
    upload = Upload(server=None, high=10, low=4)  # type: ignore[arg-type]
    upload(b"12345")
    upload(b"67890")
    assert gate.calls == ["pause"]
    body = upload.body()
    assert await anext(body) == b"12345"
    assert gate.calls == ["pause"]  # 5 bytes still waiting: above the low mark.
    assert await anext(body) == b"67890"
    assert gate.calls == ["pause", "resume"]
    upload(b"")
    assert [chunk async for chunk in body] == []
    assert upload.received == 10


async def test_upload_failure_reaches_sayld_as_an_early_close(gate: Gate) -> None:
    upload = Upload(server=None)  # type: ignore[arg-type]
    upload(b"part")
    upload.fail()
    with pytest.raises(ConnectionError):
        await drain(upload)


async def test_stop_drops_the_rest_and_closes_the_upstream(gate: Gate) -> None:
    upload = Upload(server=None)  # type: ignore[arg-type]
    upload(b"abc")
    upload.stop()
    assert await drain(upload) == b""
    assert gate.calls == ["cancel"]
    assert upload(b"late") == ()  # Ignored after stop.
    assert upload.received == 3


def test_drm_markers() -> None:
    assert has_drm(b'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://x"')
    assert has_drm(
        b'<ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>'
    )
    assert not has_drm(b"#EXTM3U\n#EXT-X-KEY:METHOD=AES-128")


class Recorder:
    """sayld as an httpx transport: records what the addon sends."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []
        self.settings: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/settings":
            return httpx.Response(200, json=self.settings)
        self.posts.append((request.url.path, json.loads(request.content or b"null")))
        return httpx.Response(204)


def capture(recorder: Recorder, **options: Any) -> Capture:
    client = httpx.AsyncClient(base_url="http://sayld", transport=httpx.MockTransport(recorder))
    return Capture(client, "ws://unused", "token", **options)


async def settle(addon: Capture) -> None:
    await asyncio.gather(*addon.tasks)


def navigation(url: str, content_type: str, body: bytes = b"") -> Any:
    flow = tflow.tflow(
        req=tutils.treq(),
        resp=tutils.tresp(content=body, headers=[(b"content-type", content_type.encode())]),
    )
    flow.request.url = url
    flow.request.headers["sec-fetch-mode"] = "navigate"
    flow.request.headers["referer"] = "https://v.test/watch"
    return flow


async def test_reports_pages_yt_dlp_supports_once() -> None:
    recorder = Recorder()
    addon = capture(recorder, extractor=lambda url: "youtube" if "watch" in url else "")
    for url in ("https://v.test/watch?v=1", "https://v.test/watch?v=1", "https://v.test/about"):
        await addon.responseheaders(navigation(url, "text/html"))
    await settle(addon)
    assert recorder.posts == [
        (
            "/capture/media",
            {
                "kind": "page",
                "page_url": "https://v.test/watch?v=1",
                "url": "https://v.test/watch?v=1",
                "extractor": "youtube",
            },
        )
    ]


async def test_reports_manifests_with_their_drm_flag() -> None:
    recorder = Recorder()
    addon = capture(recorder)
    flow = navigation(
        "https://cdn.v.test/a.m3u8", "application/vnd.apple.mpegurl", b"METHOD=SAMPLE-AES"
    )
    flow.request.headers["sec-fetch-mode"] = "cors"
    await addon.responseheaders(flow)
    addon.response(flow)
    await settle(addon)
    assert recorder.posts[0][1] == {
        "kind": "manifest",
        "page_url": "https://v.test/watch",
        "url": "https://cdn.v.test/a.m3u8",
        "drm": True,
    }


async def test_everything_but_manifests_streams_through_unbuffered() -> None:
    addon = capture(Recorder())
    segment = navigation("https://cdn.v.test/seg1.ts", "video/mp2t")
    segment.request.headers["sec-fetch-mode"] = "cors"
    page = navigation("https://v.test/about", "text/html")
    manifest = navigation("https://cdn.v.test/a.m3u8", "application/vnd.apple.mpegurl")
    manifest.request.headers["sec-fetch-mode"] = "cors"
    for flow in (segment, page, manifest):
        await addon.responseheaders(flow)
    assert segment.response.stream is True
    assert page.response.stream is True
    assert manifest.response.stream is False  # Read whole for the video bar; it is small.


async def test_bypassed_paths_and_settings() -> None:
    recorder = Recorder()
    recorder.settings = {"capture_bypass": ["files.example/private"], "capture_threshold": 10}
    addon = capture(recorder)
    await addon.reload()
    assert addon.threshold == 10
    flow = navigation("https://files.example/private/a.zip", "application/zip")
    await addon.responseheaders(flow)
    assert flow.response.status_code == 200  # Untouched: no attach was attempted.
    assert recorder.posts == []


UNKNOWN_CA = (
    "The client does not trust the proxy's certificate for pinned.example (tlsv1 alert unknown ca)"
)
HUNG_UP = (
    "The client disconnected during the handshake. If this happens consistently for x, "
    "this may indicate that the client does not trust the proxy's certificate."
)


async def test_tls_refusals_bypass_one_site_but_not_a_whole_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    addon = capture(recorder)

    def refused(host: str, error: str = UNKNOWN_CA) -> Any:
        data = type("Data", (), {})()
        data.context = type("Context", (), {"client": type("Client", (), {"sni": host})()})()
        data.conn = type("Conn", (), {"error": error})()
        return data

    addon.tls_failed_client(refused("hungup.example", HUNG_UP))  # Says nothing about the CA.
    addon.tls_failed_client(refused("other.example", "OpenSSL Error('no shared cipher')"))
    assert not addon.tasks
    addon.tls_failed_client(refused("pinned.example"))
    await settle(addon)
    assert recorder.posts == [("/capture/bypass", {"host": "pinned.example", "reason": "tls"})]
    for n in range(5):  # Many hosts at once: the CA is missing from that browser.
        addon.tls_failed_client(refused(f"site{n}.example"))
    await settle(addon)
    assert len(recorder.posts) <= 1 + addon_module.TLS_FAILURE_LIMIT - 1

    untrusted = capture(Recorder(), ca_trusted=False)
    untrusted.tls_failed_client(refused("any.example"))
    assert not untrusted.tasks


async def test_ended_downloads_stop_their_upload(gate: Gate) -> None:
    addon = capture(Recorder())
    upload = Upload(server=None)  # type: ignore[arg-type]
    addon.uploads["d1"] = upload
    addon._on_event({"event": "download.progress", "id": "d1"})
    assert not upload.stopped
    addon._on_event({"event": "download.completed", "id": "d1"})
    assert upload.stopped and "d1" not in addon.uploads

    canceled = Upload(server=None)  # type: ignore[arg-type]
    addon.uploads["d2"] = canceled
    addon._on_event({"event": "download.updated", "id": "d2", "status": "canceled"})
    assert canceled.stopped
