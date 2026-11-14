"""sayl-capture in this event loop, against a counting origin. Only the browser is simulated."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from mitmproxy.tools.dump import DumpMaster

from daemon.capture import ca, mitm_adapter
from daemon.capture.addon import Capture
from daemon.capture.run import options
from tests.stack import Stack, free_port

CHUNK = 64 * 1024


def body(size: int) -> bytes:
    return bytes((i * 7) % 251 for i in range(size))


@dataclass
class BigOrigin:
    """Serves large files slowly enough to measure, and counts the bytes it really sent."""

    files: dict[str, bytes]
    sent: Counter[str] = field(default_factory=Counter)
    hits: Counter[str] = field(default_factory=Counter)
    ranges: Counter[str] = field(default_factory=Counter)
    headers_seen: list[dict[str, str]] = field(default_factory=list)
    closed_early: Counter[str] = field(default_factory=Counter)
    server: asyncio.Server | None = None
    port: int = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                path = lines[0].split()[1].split("?")[0]
                headers = {
                    name.lower(): value
                    for name, _, value in (line.partition(": ") for line in lines[1:] if line)
                }
                self.headers_seen.append(headers)
                self.hits[path] += 1
                await self._respond(writer, path, headers)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def _respond(
        self, writer: asyncio.StreamWriter, path: str, headers: dict[str, str]
    ) -> None:
        ranged = path.startswith("/ranged")
        name = path.removeprefix("/ranged")
        if name == "/login.bin" and "sid=secret" not in headers.get("cookie", ""):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        data = self.files[name]
        start, end = 0, len(data) - 1
        status = "200 OK"
        extra = ""
        if ranged:
            extra = 'Accept-Ranges: bytes\r\nETag: "big-v1"\r\n'
            if wanted := headers.get("range"):
                self.ranges[path] += 1
                first, _, last = wanted.removeprefix("bytes=").partition("-")
                start, end = int(first), int(last) if last else len(data) - 1
                status = "206 Partial Content"
                extra += f"Content-Range: bytes {start}-{end}/{len(data)}\r\n"
        writer.write(
            (
                f"HTTP/1.1 {status}\r\nContent-Type: application/octet-stream\r\n"
                f'Content-Disposition: attachment; filename="{name.strip("/")}"\r\n'
                f"Content-Length: {end - start + 1}\r\n{extra}\r\n"
            ).encode()
        )
        position = start
        try:
            while position <= end:
                piece = data[position : min(end + 1, position + CHUNK)]
                writer.write(piece)
                await writer.drain()
                self.sent[path] += len(piece)
                position += len(piece)
        except ConnectionError:
            self.closed_early[path] += 1
            raise


@dataclass
class Proxy:
    master: DumpMaster
    addon: Capture
    port: int

    def client(self) -> httpx.AsyncClient:
        """A stand-in browser: it navigates, so its requests are interceptable."""
        return httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{self.port}",
            headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
            timeout=30,
        )


@asynccontextmanager
async def running_proxy(stack: Stack, root: Path) -> AsyncIterator[Proxy]:
    mitm_adapter.install()
    ca.ensure(root / "ca")
    port = free_port()
    master = DumpMaster(
        options(port, str(root / "ca"), ("example.invalid",)),
        with_termlog=False,
        with_dumper=False,
    )
    token = stack.daemon.services.token
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{stack.port}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        trust_env=False,
    )
    addon = Capture(client, f"ws://127.0.0.1:{stack.port}/ws", token)
    master.addons.add(addon)  # type: ignore[no-untyped-call]
    running = asyncio.create_task(master.run())
    for _ in range(200):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            break
        except OSError:
            await asyncio.sleep(0.02)
    try:
        yield Proxy(master, addon, port)
    finally:
        master.shutdown()
        await asyncio.wait_for(running, 10)
        await client.aclose()
