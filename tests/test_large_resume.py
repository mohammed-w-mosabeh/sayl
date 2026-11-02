"""SPEC acceptance: 1 GB, 16 connections, network cut mid-way, resume, SHA-256 matches.

Slow and disk-heavy, so it runs only with SAYL_LARGE_TEST=1 (the CI python job sets it).
"""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from tests.stack import free_port, running_stack

SIZE = 1024 * 1024 * 1024
BLOCK = 1024 * 1024
pytestmark = pytest.mark.skipif(not os.environ.get("SAYL_LARGE_TEST"), reason="SAYL_LARGE_TEST")


def make_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("wb") as stream:
        for index in range(SIZE // BLOCK):
            block = index.to_bytes(8, "big") * (BLOCK // 8)
            digest.update(block)
            stream.write(block)
    return digest.hexdigest()


class FileServer:
    def __init__(self, path: Path, port: int) -> None:
        self.path = path
        self.port = port
        self.server: asyncio.Server | None = None
        self.writers: set[asyncio.StreamWriter] = set()
        self.peak = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)

    async def cut(self) -> None:
        """Drop every connection and refuse new ones, as a network failure does."""
        assert self.server is not None
        self.server.close()
        for writer in list(self.writers):
            writer.transport.abort()
        self.server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        self.peak = max(self.peak, len(self.writers))
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode().lower()
                await self._answer(writer, head)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()

    async def _answer(self, writer: asyncio.StreamWriter, head: str) -> None:
        start, end = 0, SIZE - 1
        status = "200 OK"
        extra = ""
        for line in head.split("\r\n"):
            if line.startswith("range: bytes="):
                first, _, last = line.removeprefix("range: bytes=").partition("-")
                start, end = int(first), int(last or SIZE - 1)
                status = "206 Partial Content"
                extra = f"Content-Range: bytes {start}-{end}/{SIZE}\r\n"
        writer.write(
            (
                f"HTTP/1.1 {status}\r\nContent-Length: {end - start + 1}\r\n"
                f"Accept-Ranges: bytes\r\n{extra}\r\n"
            ).encode()
        )
        with self.path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = await asyncio.to_thread(stream.read, min(remaining, 256 * 1024))
                remaining -= len(chunk)
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0.002)


async def test_one_gigabyte_survives_a_network_cut(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    expected = await asyncio.to_thread(make_file, source)
    server = FileServer(source, free_port())
    await server.start()
    async with running_stack(tmp_path / "home", max_retries=5) as stack:
        url = f"http://127.0.0.1:{server.port}/big.bin"
        created = await stack.client.post("/downloads", json={"url": url})
        download_id = created.json()["id"]

        async def state() -> dict[str, object]:
            result: dict[str, object] = (await stack.client.get(f"/downloads/{download_id}")).json()
            return result

        while int(str((await state())["done_bytes"])) < SIZE // 4:
            await asyncio.sleep(0.1)
        await server.cut()
        await asyncio.sleep(3)
        await server.start()

        for _ in range(6000):
            current = await state()
            if current["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.1)
        assert current["status"] == "completed", current["error"]
        assert server.peak >= 16
        digest = await asyncio.to_thread(
            lambda: hashlib.file_digest((stack.downloads / "big.bin").open("rb"), "sha256")
        )
        assert digest.hexdigest() == expected
