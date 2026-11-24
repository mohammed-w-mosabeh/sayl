"""The sayl-capture mitmproxy addon: detect downloads and hand the open response to sayld.

See "Addon logic" in docs/SPEC.md. Stream first: a captured response is never requested again.
The browser gets an empty 204 at once, the upstream keeps flowing into `PUT /stream`, and the
upstream is paused whenever sayld is not reading (`Upload`). Media manifests and pages yt-dlp
supports are reported for the floating video bar. All decisions use response headers only.
"""

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
import websockets
from mitmproxy import connection, http, tls
from mitmproxy.connection import Server

from daemon.capture import detect, mitm_adapter

log = logging.getLogger(__name__)

HIGH_WATER = 4 * 1024 * 1024  # Pause the upstream with this much waiting for sayld.
LOW_WATER = 1024 * 1024  # Resume it once sayld has caught up to here.
FORWARDED = ("cookie", "authorization", "referer", "user-agent")
ENDED = frozenset(
    {"download.completed", "download.failed", "download.removed", "download.needs_refresh"}
)
DRM_MARKERS = (
    b"urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",  # Widevine
    b"urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95",  # PlayReady
    b"com.apple.streamingkeydelivery",  # FairPlay
    b"METHOD=SAMPLE-AES",
    b"cenc:default_KID",
)
TLS_FAILURE_WINDOW = 60.0
CA_ALERTS = ("unknown ca", "bad certificate", "certificate unknown")
TLS_FAILURE_LIMIT = 3  # More distinct hosts than this per minute means "CA not trusted".


class Upload:
    """One detached response body on its way to sayld, with back-pressure on the upstream.

    mitmproxy calls the instance with each chunk, then with b"" at the end of the body. The
    chunks wait here until `body()` hands them to the PUT; above HIGH_WATER the upstream
    connection stops being read, so at most HIGH_WATER plus one read sits in memory.
    """

    def __init__(self, server: Server, high: int = HIGH_WATER, low: int = LOW_WATER) -> None:
        self.server = server
        self.high = high
        self.low = low
        self.chunks: deque[bytes] = deque()
        self.buffered = 0
        self.received = 0
        self.ended = False
        self.failed = False
        self.stopped = False
        self.wake = asyncio.Event()
        self.closed = asyncio.Event()  # Set once sayld has everything, or it ended otherwise.

    def __call__(self, chunk: bytes) -> Iterable[bytes]:
        if self.stopped:
            return ()
        if chunk:
            self.chunks.append(chunk)
            self.buffered += len(chunk)
            self.received += len(chunk)
            if self.buffered >= self.high:
                mitm_adapter.pause(self.server)
        else:
            self.ended = True
        self.wake.set()
        return ()  # Nothing reaches the browser: its side already ended with the 204.

    def fail(self) -> None:
        """The upstream broke before the end: sayld must see an early close, not an end."""
        if not self.ended:
            self.failed = True
            self.wake.set()
            self.closed.set()

    def stop(self) -> None:
        """sayld is done with this download. Drop what is left and close the upstream."""
        self.stopped = True
        self.chunks.clear()
        self.buffered = 0
        if not self.ended:
            mitm_adapter.cancel(self.server)
        self.wake.set()
        self.closed.set()

    async def body(self) -> AsyncIterator[bytes]:
        try:
            while True:
                if self.chunks:
                    chunk = self.chunks.popleft()
                    self.buffered -= len(chunk)
                    if self.buffered <= self.low and mitm_adapter.paused(self.server):
                        mitm_adapter.resume(self.server)
                    yield chunk
                elif self.stopped or self.ended:
                    return
                elif self.failed:
                    raise ConnectionError("the upstream connection closed early")
                else:
                    self.wake.clear()
                    await self.wake.wait()
        finally:
            self.closed.set()


def swallow(flow: http.HTTPFlow, mode: str, stream: Any) -> None:
    """Answer the browser with an empty 204 right now; mitm_adapter keeps the upstream."""
    response = flow.response
    if response is None:
        return
    response.status_code = 204
    response.reason = "No Content"
    response.headers.clear()
    response.stream = stream
    flow.metadata[mitm_adapter.HANDOFF] = mode


def has_drm(body: bytes) -> bool:
    return any(marker in body for marker in DRM_MARKERS)


class Capture:
    """The addon. `client` talks to sayld; `extractor` names a yt-dlp extractor for a URL."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        socket_url: str,
        token: str,
        extractor: Callable[[str], str] | None = None,
        ca_trusted: bool = True,
        passthrough: tuple[str, ...] = (),
    ) -> None:
        self.client = client
        self.socket_url = socket_url
        self.token = token
        self.extractor = extractor
        self.ca_trusted = ca_trusted
        self.passthrough = (*detect.DEFAULT_BYPASS, *passthrough)  # Built in plus config.toml.
        self.bypass: tuple[str, ...] = self.passthrough
        self.threshold = detect.DEFAULT_THRESHOLD
        self.uploads: dict[str, Upload] = {}  # Download id -> upload.
        self.by_flow: dict[str, Upload] = {}
        self.by_client: dict[str, list[Upload]] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.watcher: asyncio.Task[None] | None = None
        self.reported: set[tuple[str, str]] = set()
        self.tls_failures: dict[str, float] = {}
        self.on_settings: Callable[[Mapping[str, Any]], None] | None = None

    # Lifecycle

    async def running(self) -> None:
        await self.reload()
        self.watcher = asyncio.create_task(self._watch())

    async def done(self) -> None:
        if self.watcher:
            self.watcher.cancel()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def reload(self) -> None:
        """Read the bypass list and threshold from sayld's settings."""
        try:
            settings = (await self.client.get("/settings")).json()
        except (httpx.HTTPError, ValueError) as error:
            log.warning("cannot read settings: %s", error)
            return
        self.apply(settings)

    def apply(self, settings: Mapping[str, Any]) -> None:
        extra = settings.get("capture_bypass", [])
        user = tuple(str(item) for item in extra) if isinstance(extra, list) else ()
        self.bypass = (*self.passthrough, *user)
        threshold = settings.get("capture_threshold")
        if isinstance(threshold, int) and threshold > 0:
            self.threshold = threshold
        if self.on_settings is not None:
            self.on_settings(settings)

    async def _watch(self) -> None:
        """Follow sayld's events: stop uploads it no longer needs, and reload settings."""
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    self.socket_url,
                    subprotocols=[websockets.Subprotocol(f"bearer.{self.token}")],
                ) as socket:
                    attempt = 0
                    async for message in socket:
                        self._on_event(json.loads(message))
            except (OSError, websockets.WebSocketException, ValueError) as error:
                log.info("event stream lost: %s", error)
            attempt += 1
            await asyncio.sleep(min(10.0, 0.5 * 2**attempt))

    def _on_event(self, event: Mapping[str, Any]) -> None:
        name = event.get("event")
        if name in ENDED and (upload := self.uploads.pop(str(event.get("id")), None)):
            upload.stop()
        elif name == "download.updated" and event.get("status") == "canceled":
            if upload := self.uploads.pop(str(event.get("id")), None):
                upload.stop()
        elif name == "settings.updated":
            self._spawn(self.reload())

    # Hooks

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        await self._decide(flow)
        response = flow.response
        if (
            response is not None
            and response.stream is False
            and not flow.metadata.get("sayl_manifest")
        ):
            # Pass everything else straight through. mitmproxy would otherwise hold each body
            # until its end: a video segment, a progressive video, a page. Only manifests are
            # read whole, and they are small.
            response.stream = True

    async def _decide(self, flow: http.HTTPFlow) -> None:
        response, request = flow.response, flow.request
        if response is None:
            return
        url = request.pretty_url
        if detect.bypassed(url, self.bypass):
            return
        headers: Mapping[str, str] = response.headers
        summary = detect.Request(
            request.method,
            url,
            request.headers.get("sec-fetch-mode"),
            request.headers.get("sec-fetch-dest"),
            request.headers.get("accept", ""),
        )
        if detect.is_media_manifest(url, headers):
            flow.metadata["sayl_manifest"] = True  # Reported from `response`, with its body.
            return
        if response.status_code != 200 or not detect.is_interceptable(summary):
            return
        if detect.is_download(summary, headers, self.threshold):
            await self._capture(flow)
        elif "text/html" in detect.mime_of(headers) and self.extractor is not None:
            self._spawn(self._report_page(url))

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("sayl_manifest") and flow.response is not None:
            body = flow.response.raw_content or b""
            page = flow.request.headers.get("referer", "")
            self._spawn(
                self._report("manifest", page, flow.request.pretty_url, drm=has_drm(body[:65536]))
            )
        self.by_flow.pop(flow.id, None)

    def error(self, flow: http.HTTPFlow) -> None:
        upload = self.by_flow.pop(flow.id, None)
        if upload is not None:
            upload.fail()

    async def client_disconnected(self, client: connection.Client) -> None:
        """Hold the upstream open: mitmproxy tears it down as soon as this hook returns.

        The browser closing a tab, or quitting, must not end a download it handed over.
        """
        for upload in self.by_client.pop(client.id, []):
            await upload.closed.wait()

    def tls_failed_client(self, data: tls.TlsData) -> None:
        """The browser refused our certificate: pinning, or the CA is not trusted there.

        Only an explicit "unknown CA" or "bad certificate" alert counts. A client that just hangs
        up mid-handshake, or fails for any other reason, says nothing about our CA.
        """
        host = data.context.client.sni
        if not host or not self.ca_trusted or not rejected_ca(data.conn.error or ""):
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        self.tls_failures = {
            name: at for name, at in self.tls_failures.items() if now - at < TLS_FAILURE_WINDOW
        }
        self.tls_failures[host] = now
        if len(self.tls_failures) > TLS_FAILURE_LIMIT:
            return  # Many hosts at once: a browser without the CA, not one pinned site.
        self._spawn(self._post("/capture/bypass", {"host": host, "reason": "tls"}))

    # Capture

    async def _capture(self, flow: http.HTTPFlow) -> None:
        request, response = flow.request, flow.response
        if response is None:
            return
        try:
            reply = await self.client.post(
                "/downloads/attach",
                json={
                    "url": request.pretty_url,
                    "method": request.method,
                    "status": response.status_code,
                    "request_headers": {
                        name: request.headers[name] for name in FORWARDED if name in request.headers
                    },
                    "response_headers": dict(response.headers),
                    "page_url": request.headers.get("referer", ""),
                },
            )
            reply.raise_for_status()
            download_id = str(reply.json()["id"])
        except (httpx.HTTPError, KeyError, ValueError) as error:
            log.warning("attach failed, leaving the download to the browser: %s", error)
            return
        upload = Upload(flow.server_conn)
        self.uploads[download_id] = upload
        self.by_flow[flow.id] = upload
        self.by_client.setdefault(flow.client_conn.id, []).append(upload)
        swallow(flow, mitm_adapter.DETACH, upload)
        self._spawn(self._upload(download_id, upload))

    async def _upload(self, download_id: str, upload: Upload) -> None:
        try:
            await self.client.put(
                f"/downloads/{download_id}/stream",
                content=upload.body(),
                timeout=httpx.Timeout(30, read=None, write=None),
            )
        except (httpx.HTTPError, ConnectionError) as error:
            if not upload.stopped:
                log.info("upload ended early: %s", type(error).__name__)
        finally:
            self.uploads.pop(download_id, None)
            if not upload.ended and not upload.stopped:
                upload.stop()  # sayld is gone or refused the rest: do not keep the upstream.

    # Video grabber

    async def _report_page(self, url: str) -> None:
        if self.extractor is None:
            return
        name = await asyncio.to_thread(self.extractor, url)
        if name:
            await self._report("page", url, url, extractor=name)

    async def _report(self, kind: str, page: str, url: str, **extra: Any) -> None:
        key = (page, url)
        if key in self.reported:
            return
        self.reported.add(key)
        await self._post("/capture/media", {"kind": kind, "page_url": page, "url": url, **extra})

    async def _post(self, path: str, body: Mapping[str, Any]) -> None:
        with contextlib.suppress(httpx.HTTPError):
            await self.client.post(path, json=body)


def rejected_ca(error: str) -> bool:
    """Does mitmproxy's handshake error carry a client alert refusing our certificate?"""
    return any(alert in error.lower() for alert in CA_ALERTS)


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""
