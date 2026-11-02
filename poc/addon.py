"""Sayl PoC mitmproxy addon. Config comes from the environment; see poc/run.py."""

import asyncio
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mitmproxy import connection, http

sys.path.insert(0, str(Path(__file__).parent))

import detach
import detect

detach.install()

STATE_DIR = Path(os.environ["SAYL_POC_STATE"])
ARIA2_SECRET = os.environ["SAYL_POC_ARIA2_SECRET"]
ARIA2_URL = os.environ["SAYL_POC_ARIA2_URL"]
DOWNLOAD_DIR = STATE_DIR / "downloads"
EVENTS = STATE_DIR / "events.jsonl"
FORWARDED_HEADERS = ("cookie", "user-agent", "referer", "authorization")


def log_event(kind: str, url: str, **fields: Any) -> None:
    """Append an event. Query strings can hold secrets, so only host and path are recorded."""
    parts = urlsplit(url)
    record = {"t": round(time.time(), 3), "kind": kind, "host": parts.hostname, **fields}
    record["path"] = parts.path[:120]
    with EVENTS.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def add_to_aria2(url: str, filename: str, headers: list[str]) -> str:
    """Blocking aria2.addUri call. Run it in a thread."""
    options = {"dir": str(DOWNLOAD_DIR), "out": filename, "header": headers}
    payload = {
        "jsonrpc": "2.0",
        "id": "sayl-poc",
        "method": "aria2.addUri",
        "params": [f"token:{ARIA2_SECRET}", [url], options],
    }
    request = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        ARIA2_URL, json.dumps(payload).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return str(json.load(response)["result"])


class FileTee:
    """Writes stream-through chunks on a thread so the event loop never touches the disk."""

    def __init__(self, url: str, filename: str, expected: int) -> None:
        self.url = url
        self.filename = filename
        self.expected = expected
        self.path = DOWNLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{filename}"
        self.queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self.written = 0
        self.finished = False
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.closed = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.thread.start()

    def _write(self) -> None:
        with self.path.open("wb") as stream:
            while (chunk := self.queue.get()) is not None:
                stream.write(chunk)
                self.written += len(chunk)
        log_event(
            "stream-through-closed",
            self.url,
            bytes=self.written,
            expected=self.expected,
            complete=self.finished,
        )
        self.loop.call_soon_threadsafe(self.closed.set)

    def __call__(self, chunk: bytes) -> Iterable[bytes]:
        """mitmproxy passes each chunk here, then b"" once at the end of the upstream body."""
        if chunk:
            self.queue.put(chunk)
        else:
            self.finished = True
            self.queue.put(None)
        return ()  # Never send anything to the browser: its side of the flow is already over.

    def abort(self) -> None:
        if not self.finished:
            self.queue.put(None)


def _swallow(flow: http.HTTPFlow, mode: str, stream: Any) -> None:
    """Answer the browser with an empty 204 right now. See detach.py for why this needs a patch."""
    response = flow.response
    assert response is not None
    response.status_code = 204
    response.reason = "No Content"
    response.headers.clear()
    response.stream = stream
    flow.metadata[detach.HANDOFF] = mode


class SaylPoc:
    def __init__(self) -> None:
        self.tees: dict[str, FileTee] = {}
        self.by_client: dict[str, list[FileTee]] = {}

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        response = flow.response
        assert response is not None
        request = flow.request
        url = request.pretty_url
        summary = detect.Request(
            request.method,
            url,
            request.headers.get("sec-fetch-mode"),
            request.headers.get("accept", ""),
        )
        headers: Mapping[str, str] = response.headers
        if detect.is_media_manifest(url, headers):
            log_event("media-manifest", url, page=_host_of(request.headers.get("referer", "")))
            return
        if request.headers.get("sec-fetch-dest") == "document" and "youtube.com" in url:
            log_event("video-page", url)
        if response.status_code != 200 or not detect.is_interceptable(summary):
            return
        if not detect.is_download(summary, headers):
            return
        filename = detect.filename_for(url, headers)
        strategy = detect.choose_strategy(summary, headers)
        expected = int(headers.get("content-length", "0") or 0)
        log_event(
            "detected",
            url,
            strategy=strategy.value,
            bytes=expected,
            http=response.http_version,
            file=filename,
        )
        if strategy is detect.Strategy.RE_REQUEST:
            forwarded = [
                f"{name}: {request.headers[name]}"
                for name in FORWARDED_HEADERS
                if name in request.headers
            ]
            try:
                gid = await asyncio.to_thread(add_to_aria2, url, filename, forwarded)
            except OSError as error:
                log_event("aria2-failed", url, error=type(error).__name__)
                return  # Leave the download to the browser rather than lose it.
            log_event("handed-to-aria2", url, gid=gid)
            _swallow(flow, detach.ABORT, True)
        else:
            tee = FileTee(url, filename, expected)
            self.tees[flow.id] = tee
            self.by_client.setdefault(flow.client_conn.id, []).append(tee)
            _swallow(flow, detach.DETACH, tee)

    def error(self, flow: http.HTTPFlow) -> None:
        tee = self.tees.pop(flow.id, None)
        if tee is not None:
            log_event("stream-through-error", tee.url, written=tee.written)
            tee.abort()

    def response(self, flow: http.HTTPFlow) -> None:
        self.tees.pop(flow.id, None)

    async def client_disconnected(self, client: connection.Client) -> None:
        """Hold the upstream open: mitmproxy tears it down as soon as this hook returns."""
        for tee in self.by_client.pop(client.id, []):
            await tee.closed.wait()


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


addons = [SaylPoc()]
