"""A real HTTP origin for engine tests. It counts every request, like the capture lab origin."""

import asyncio
import hashlib
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from daemon.engines.base import StreamInfo

SIZE = 4 * 1024 * 1024
BODY = bytes(i % 251 for i in range(SIZE))
SHA256 = hashlib.sha256(BODY).hexdigest()
ETAG = '"lab-v1"'
TRICKLE = 64 * 1024
TOKEN = "k9Xq2vLw7Rt4Zp1Bn6Ys"  # noqa: S105 - a fake path token


@dataclass
class Origin:
    hits: Counter[str] = field(default_factory=Counter)
    ranges: Counter[str] = field(default_factory=Counter)
    cookies: list[str] = field(default_factory=list)
    range_log: list[str] = field(default_factory=list)
    down: bool = False  # Every Range connection drops with no answer, as with no network.
    server: asyncio.Server | None = None
    port: int = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, path = lines[0].split()[:2]
                headers = {
                    name.lower(): value
                    for name, _, value in (line.partition(": ") for line in lines[1:] if line)
                }
                self.hits[path] += 1
                wanted = headers.get("range")
                if wanted:
                    self.ranges[path] += 1
                    self.range_log.append(wanted)
                self.cookies.append(headers.get("cookie", ""))
                await self._respond(writer, path, wanted, head_only=method == "HEAD")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _respond(
        self, writer: asyncio.StreamWriter, path: str, wanted: str | None, *, head_only: bool
    ) -> None:
        def send(status: str, body: bytes, *extra: str) -> None:
            lines = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", *extra]
            writer.write("\r\n".join(lines).encode() + b"\r\n\r\n")
            if not head_only:
                writer.write(body)

        common = ["Accept-Ranges: bytes", f"ETag: {ETAG}"]
        if path.startswith("/x3/"):
            body = BODY * 3
            if wanted is None:
                return send("200 OK", body, *common)
            first, _, last = wanted.removeprefix("bytes=").partition("-")
            start, end = int(first), int(last or len(body) - 1)
            content_range = f"Content-Range: bytes {start}-{end}/{len(body)}"
            return send("206 Partial Content", body[start : end + 1], content_range, *common)
        if path.startswith("/slow/"):
            await self._trickle(writer, wanted)
            return None
        outage = path.startswith("/outage/") and self.ranges[path] <= int(path.split("/")[2])
        middle = path.startswith("/tailonly/") and not (wanted or "").endswith(f"-{SIZE - 1}")
        if wanted and (outage or middle or self.down):
            # /outage/<n>/: the first n Range connections drop with no answer, as while the
            # Wi-Fi is down. /tailonly/: every Range but the one ending the file does.
            writer.close()
            return None
        if path.startswith("/flaky/") and wanted and self.ranges[path] == 1:
            # The first Range connection drops halfway, as on a network change.
            first = int(wanted.removeprefix("bytes=").partition("-")[0])
            head = f"HTTP/1.1 206 Partial Content\r\nContent-Length: {SIZE - first}\r\n"
            head += f"ETag: {ETAG}\r\nContent-Range: bytes {first}-{SIZE - 1}/{SIZE}\r\n\r\n"
            writer.write(head.encode() + BODY[first : first + (SIZE - first) // 2])
            writer.close()
            return None
        if path.startswith("/once/") and self.hits[path] > 1:
            return send("410 Gone", b"used")
        if wanted is None:
            return send("200 OK", BODY, *common)
        if path == "/session.bin":
            return send("403 Forbidden", b"session bound")
        if path == "/plain200.bin":
            return send("200 OK", BODY, *common)
        first, _, last = wanted.removeprefix("bytes=").partition("-")
        start, end = int(first), int(last or SIZE - 1)
        etag = '"lab-v2"' if path == "/changed.bin" else ETAG
        content_range = f"Content-Range: bytes {start}-{end}/{SIZE}"
        return send("206 Partial Content", BODY[start : end + 1], f"ETag: {etag}", content_range)

    async def _trickle(self, writer: asyncio.StreamWriter, wanted: str | None) -> None:
        """Slow enough that a daemon can be killed in the middle of a download."""
        first, _, last = (wanted or "bytes=0-").removeprefix("bytes=").partition("-")
        start, end = int(first), int(last or SIZE - 1)
        lines = [
            "HTTP/1.1 206 Partial Content" if wanted else "HTTP/1.1 200 OK",
            f"Content-Length: {end - start + 1}",
            "Accept-Ranges: bytes",
            f"ETag: {ETAG}",
        ]
        if wanted:
            lines.append(f"Content-Range: bytes {start}-{end}/{SIZE}")
        writer.write("\r\n".join(lines).encode() + b"\r\n\r\n")
        for offset in range(start, end + 1, TRICKLE):
            writer.write(BODY[offset : min(offset + TRICKLE, end + 1)])
            await writer.drain()
            await asyncio.sleep(0.05)


async def open_stream(url: str) -> tuple[StreamInfo, AsyncIterator[bytes]]:
    """Plays sayl-capture: one request, and that same response becomes the download."""
    client = httpx.AsyncClient()
    request = client.build_request("GET", url)
    response = await client.send(request, stream=True)
    info = StreamInfo(
        total=int(response.headers["content-length"]),
        accept_ranges=response.headers.get("accept-ranges") == "bytes",
        etag=response.headers.get("etag", ""),
    )

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw(64 * 1024):
                await asyncio.sleep(0.002)  # Keep the stream slow enough to be overtaken.
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return info, body()
