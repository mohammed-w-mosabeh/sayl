import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daemon.proxy import ProxyError, ProxyUrl, Socks5Bridge, parse, socks5_connect
from tests.origin import BODY, ETAG, SHA256, SIZE, Origin
from tests.stack import Stack, running_stack


class FakeSocks:
    """A SOCKS5 proxy for tests: optional user/password, and a record of every target."""

    def __init__(self, user: str = "", password: str = "") -> None:
        self.user, self.password = user, password
        self.targets: list[tuple[str, int]] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._session(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        wanted = 2 if self.user else 0
        if wanted not in methods:
            writer.write(b"\x05\xff")
            return
        writer.write(bytes([5, wanted]))
        if wanted == 2:
            await reader.readexactly(1)
            user = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            good = (user.decode(), password.decode()) == (self.user, self.password)
            writer.write(b"\x01\x00" if good else b"\x01\x01")
            if not good:
                return
        _, _, _, kind = await reader.readexactly(4)
        if kind == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        else:
            host = ".".join(map(str, await reader.readexactly(4)))
        port = int.from_bytes(await reader.readexactly(2), "big")
        self.targets.append((host, port))
        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(ConnectionError):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            with contextlib.suppress(OSError, RuntimeError):
                dst.write_eof()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        up_writer.close()


@pytest.fixture
async def origin() -> AsyncIterator[Origin]:
    server = Origin()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def socks() -> AsyncIterator[FakeSocks]:
    server = FakeSocks("me", "p@ss:word")
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path) as running:
        yield running


def proxy_for(socks: FakeSocks, password: str = "p@ss:word") -> ProxyUrl:  # noqa: S107 - a test proxy
    return ProxyUrl("socks5", "127.0.0.1", socks.port, "me", password)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("http://proxy.lan:3128", ProxyUrl("http", "proxy.lan", 3128)),
        ("socks5://u:p%40w@10.0.0.1", ProxyUrl("socks5", "10.0.0.1", 1080, "u", "p@w")),
        ("https://p", ProxyUrl("https", "p", 443)),
    ],
)
def test_parse(text: str, expected: ProxyUrl | None) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize("text", ["ftp://h:1", "socks5://h:x", "http://:80", "http://h:1/path"])
def test_parse_refuses(text: str) -> None:
    with pytest.raises(ProxyError):
        parse(text)


def test_the_password_stays_out_of_logs_but_survives_the_url() -> None:
    proxy = ProxyUrl("socks5", "h", 1, "me", "p@ss:word")
    assert "p@ss" not in str(proxy)
    assert parse(proxy.url()) == proxy


async def http_get(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str) -> bytes:
    """One request; the test origin keeps connections open, so read by Content-Length."""
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    head = (await reader.readuntil(b"\r\n\r\n")).decode().lower()
    length = int(head.split("content-length: ")[1].split("\r\n")[0])
    body = await reader.readexactly(length)
    writer.close()
    return body


async def test_socks5_with_a_password_reaches_the_origin(socks: FakeSocks, origin: Origin) -> None:
    reader, writer = await socks5_connect(proxy_for(socks), "127.0.0.1", origin.port)
    assert await http_get(reader, writer, "/normal.bin") == BODY
    assert socks.targets == [("127.0.0.1", origin.port)]


async def test_a_wrong_password_is_reported(socks: FakeSocks) -> None:
    with pytest.raises(ProxyError, match="refused the user or password"):
        await socks5_connect(proxy_for(socks, "wrong"), "127.0.0.1", 9)


async def test_the_bridge_tunnels_connect_and_refuses_the_rest(
    socks: FakeSocks, origin: Origin
) -> None:
    bridge = Socks5Bridge(proxy_for(socks))
    await bridge.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        writer.write(f"CONNECT localhost:{origin.port} HTTP/1.1\r\n\r\n".encode())
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        assert await http_get(reader, writer, "/normal.bin") == BODY
        assert socks.targets == [("localhost", origin.port)]  # The proxy resolves names.

        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        writer.write(b"GET http://x/ HTTP/1.1\r\n\r\n")
        assert (await reader.read(-1)).startswith(b"HTTP/1.1 405")
    finally:
        await bridge.stop()


async def wait_done(stack: Stack, download_id: str) -> dict[str, Any]:
    for _ in range(1500):
        body: dict[str, Any] = (await stack.client.get(f"/downloads/{download_id}")).json()
        if body["status"] in ("completed", "failed", "needs_refresh"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(body["status"])


async def test_downloads_go_through_a_socks5_proxy(
    stack: Stack, origin: Origin, socks: FakeSocks
) -> None:
    proxy = f"socks5://me:p%40ss%3Aword@127.0.0.1:{socks.port}"
    saved = await stack.client.put("/settings", json={"outbound_proxy": proxy})
    assert saved.status_code == 200

    created = await stack.client.post("/downloads", json={"url": origin.url("/via-socks.bin")})
    body = await wait_done(stack, created.json()["id"])
    assert body["status"] == "completed", body["error"]
    data = (stack.downloads / "via-socks.bin").read_bytes()
    assert hashlib.sha256(data).hexdigest() == SHA256
    assert socks.targets and all(target == ("127.0.0.1", origin.port) for target in socks.targets)

    # A captured download's Range requests take the proxy too.
    before = len(socks.targets)
    attached = await stack.client.post(
        "/downloads/attach",
        json={
            "url": origin.url("/normal.bin"),
            "response_headers": {
                "Content-Length": str(SIZE),
                "Accept-Ranges": "bytes",
                "ETag": ETAG,
                "Content-Disposition": 'attachment; filename="cut.bin"',
            },
        },
    )

    async def cut() -> AsyncIterator[bytes]:
        yield BODY[: SIZE // 2]
        raise ConnectionError("capture died")

    with contextlib.suppress(Exception):
        await stack.client.put(f"/downloads/{attached.json()['id']}/stream", content=cut())
    body = await wait_done(stack, attached.json()["id"])
    assert body["status"] == "completed", body["error"]
    assert len(socks.targets) > before


async def test_a_bad_proxy_setting_is_a_422_and_direct_comes_back(
    stack: Stack, origin: Origin
) -> None:
    bad = await stack.client.put("/settings", json={"outbound_proxy": "gopher://h:70"})
    assert bad.status_code == 422
    await stack.client.put("/settings", json={"outbound_proxy": "socks5://127.0.0.1:9"})
    await stack.client.put("/settings", json={"outbound_proxy": ""})
    created = await stack.client.post("/downloads", json={"url": origin.url("/direct.bin")})
    body = await wait_done(stack, created.json()["id"])
    assert body["status"] == "completed", body["error"]
