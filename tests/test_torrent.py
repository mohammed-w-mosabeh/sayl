"""Real BitTorrent on the loopback: a tiny tracker, a seeding aria2c, and Aria2Engine."""

import asyncio
import hashlib
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from daemon.engines.aria2 import Aria2Engine, Seeding, local_torrent
from daemon.engines.base import Job, Progress
from daemon.models import Status
from tests.stack import Stack, free_port, running_stack

pytestmark = pytest.mark.skipif(shutil.which("aria2c") is None, reason="needs aria2c")

PIECE = 64 * 1024
PAYLOAD = os.urandom(5 * PIECE + 1234)


def bencode(value: object) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(type(value))


class Tracker:
    """Answers every announce with the other peers that announced the same torrent."""

    def __init__(self) -> None:
        self.peers: dict[bytes, dict[bytes, tuple[str, int]]] = {}
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    @property
    def announce(self) -> str:
        return f"http://127.0.0.1:{self.port}/announce"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            writer.close()
            return
        target = head.split(b" ", 2)[1].decode("latin-1")
        query = parse_qs(urlsplit(target).query, encoding="latin-1")
        info_hash = query["info_hash"][0].encode("latin-1")
        peer_id = query["peer_id"][0].encode("latin-1")
        swarm = self.peers.setdefault(info_hash, {})
        swarm[peer_id] = ("127.0.0.1", int(query["port"][0]))
        compact = b"".join(
            bytes(map(int, ip.split("."))) + port.to_bytes(2, "big")
            for other, (ip, port) in swarm.items()
            if other != peer_id
        )
        body = bencode({"interval": 2, "min interval": 1, "peers": compact})
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()


def make_torrent(directory: Path, announce: str) -> tuple[Path, str]:
    """Write payload.bin and its .torrent; return the torrent and the info hash."""
    (directory / "payload.bin").write_bytes(PAYLOAD)
    pieces = b"".join(
        hashlib.sha1(PAYLOAD[i : i + PIECE]).digest()  # noqa: S324 - BitTorrent v1 pieces
        for i in range(0, len(PAYLOAD), PIECE)
    )
    info = {"name": "payload.bin", "piece length": PIECE, "length": len(PAYLOAD), "pieces": pieces}
    torrent = directory / "payload.torrent"
    torrent.write_bytes(bencode({"announce": announce, "info": info}))
    return torrent, hashlib.sha1(bencode(info)).hexdigest()  # noqa: S324


@pytest.fixture
async def tracker() -> AsyncIterator[Tracker]:
    server = Tracker()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def seeder(tmp_path: Path, tracker: Tracker) -> Iterator[tuple[Path, str]]:
    seed = tmp_path / "seed"
    seed.mkdir()
    torrent, info_hash = make_torrent(seed, tracker.announce)
    binary = shutil.which("aria2c")
    assert binary is not None
    process = subprocess.Popen(  # noqa: S603 - argument list
        [
            binary,
            f"--dir={seed}",
            "--check-integrity=true",
            "--seed-ratio=0.0",
            "--enable-dht=false",
            "--bt-enable-lpd=false",
            "--enable-peer-exchange=false",
            f"--listen-port={free_port()}",
            "--bt-tracker-interval=2",
            "--quiet=true",
            str(torrent),
        ],
    )
    try:
        yield torrent, info_hash
    finally:
        process.terminate()
        process.wait(timeout=10)


async def finish(engine: Aria2Engine, gid: str) -> Progress:
    for _ in range(600):
        progress = await engine.status(gid)
        if progress.status in (Status.COMPLETED, Status.FAILED):
            return progress
        await asyncio.sleep(0.1)
    raise AssertionError(progress)


async def test_a_local_torrent_downloads_and_reports_complete_while_seeding(
    tmp_path: Path, seeder: tuple[Path, str]
) -> None:
    torrent, _ = seeder
    engine = Aria2Engine(tmp_path / "data", seeding=Seeding(ratio=1.0, minutes=5))
    (tmp_path / "data").mkdir()
    await engine.start()
    try:
        gid = await engine.add(Job("t", torrent.as_uri(), str(tmp_path / "out")))
        progress = await finish(engine, gid)
        assert progress.status is Status.COMPLETED, progress.error
        assert progress.filename == "payload.bin"
        assert (tmp_path / "out/payload.bin").read_bytes() == PAYLOAD
    finally:
        await engine.stop()


async def test_a_magnet_follows_its_metadata_to_the_files(
    tmp_path: Path, tracker: Tracker, seeder: tuple[Path, str]
) -> None:
    _, info_hash = seeder
    engine = Aria2Engine(tmp_path / "data", seeding=Seeding(ratio=0.1, minutes=1))
    (tmp_path / "data").mkdir()
    await engine.start()
    try:
        magnet = f"magnet:?xt=urn:btih:{info_hash}&tr={tracker.announce}"
        gid = await engine.add(Job("m", magnet, str(tmp_path / "out")))
        progress = await finish(engine, gid)
        assert progress.status is Status.COMPLETED, progress.error
        assert progress.engine_ref and progress.engine_ref != gid  # The files' own id.
        assert progress.total == len(PAYLOAD)
        assert (tmp_path / "out/payload.bin").read_bytes() == PAYLOAD
    finally:
        await engine.stop()


def test_local_torrent_paths() -> None:
    assert local_torrent("file:///srv/a%20b.torrent") == Path("/srv/a b.torrent")
    assert local_torrent("/srv/x.TORRENT") == Path("/srv/x.TORRENT")
    assert local_torrent("https://h/x.torrent") is None
    assert local_torrent("/srv/x.zip") is None


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path) as running:
        yield running


async def test_the_queue_follows_a_magnet_to_its_files(
    stack: Stack, tracker: Tracker, seeder: tuple[Path, str]
) -> None:
    _, info_hash = seeder
    magnet = f"magnet:?xt=urn:btih:{info_hash}&tr={tracker.announce}"
    created = (await stack.client.post("/downloads", json={"url": magnet})).json()
    for _ in range(600):
        body = (await stack.client.get(f"/downloads/{created['id']}")).json()
        if body["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.1)
    assert (body["status"], body["filename"]) == ("completed", "payload.bin")
    assert body["engine"] == "aria2"
    first = body["engine_ref"]
    assert first and (stack.downloads / "payload.bin").read_bytes() == PAYLOAD
    # The row now names the files' download, not the finished metadata one.
    progress = await stack.daemon.aria2.status(first)
    assert progress.engine_ref == ""
