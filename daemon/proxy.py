"""Outbound proxy: Sayl's own connections through an HTTP(S) or SOCKS5 proxy.

`outbound_proxy` is `http://`, `https://`, `socks5://`, or `socks5h://` with an optional
user:password, or "" for direct connections. httpx and yt-dlp speak every one of them. aria2
speaks only HTTP proxies, so for SOCKS5 the daemon runs Socks5Bridge, an HTTP CONNECT server on
127.0.0.1 that tunnels through the SOCKS5 proxy, and aria2 uses CONNECT for every request
(`proxy-method=tunnel`), plain HTTP included. Host names are resolved by the proxy, so DNS does
not leak. The proxy's password is never logged.
"""

import asyncio
import contextlib
import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

log = logging.getLogger(__name__)

SCHEMES = ("http", "https", "socks5", "socks5h")
HANDSHAKE_SECONDS = 20.0
_REPLIES = {
    1: "general failure",
    2: "not allowed by the proxy's rules",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


class ProxyError(Exception):
    """The proxy setting is unusable, or the proxy refused. The message holds no password."""


@dataclass(frozen=True)
class ProxyUrl:
    scheme: str
    host: str
    port: int
    user: str = ""
    password: str = ""

    @property
    def socks(self) -> bool:
        return self.scheme.startswith("socks5")

    def url(self, scheme: str | None = None) -> str:
        auth = ""
        if self.user:
            auth = quote(self.user, safe="") + (
                ":" + quote(self.password, safe="") if self.password else ""
            )
            auth += "@"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme or self.scheme}://{auth}{host}:{self.port}"

    def __str__(self) -> str:  # For logs: never the password.
        return f"{self.scheme}://{self.host}:{self.port}"


def parse(text: str) -> ProxyUrl | None:
    """None for "" (direct). Raises ProxyError for anything unusable."""
    text = text.strip()
    if not text:
        return None
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise ProxyError(f"a proxy starts with {', '.join(s + '://' for s in SCHEMES)}")
    try:
        port = parts.port
    except ValueError:
        raise ProxyError("the proxy port is not a number") from None
    if not parts.hostname:
        raise ProxyError("the proxy has no host")
    if parts.path not in ("", "/") or parts.query:
        raise ProxyError("a proxy is only scheme://[user:password@]host:port")
    default = {"http": 80, "https": 443}.get(scheme, 1080)
    return ProxyUrl(
        scheme,
        parts.hostname,
        port or default,
        unquote(parts.username or ""),
        unquote(parts.password or ""),
    )


async def socks5_connect(
    proxy: ProxyUrl, host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a TCP connection to host:port through a SOCKS5 proxy (RFC 1928, 1929)."""
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    try:
        await asyncio.wait_for(_handshake(proxy, reader, writer, host, port), HANDSHAKE_SECONDS)
    except BaseException:
        writer.close()
        raise
    return reader, writer


async def _handshake(
    proxy: ProxyUrl,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
) -> None:
    methods = b"\x00\x02" if proxy.user else b"\x00"
    writer.write(b"\x05" + bytes([len(methods)]) + methods)
    await writer.drain()
    version, method = await reader.readexactly(2)
    if version != 5 or method == 0xFF:
        raise ProxyError("the SOCKS5 proxy accepts none of our login methods")
    if method == 2:
        user, password = proxy.user.encode(), proxy.password.encode()
        if len(user) > 255 or len(password) > 255:
            raise ProxyError("the proxy user or password is too long")
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        await writer.drain()
        _, status = await reader.readexactly(2)
        if status != 0:
            raise ProxyError("the SOCKS5 proxy refused the user or password")
    elif method != 0:
        raise ProxyError("the SOCKS5 proxy asked for an unsupported login method")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode("idna")
        target = b"\x03" + bytes([len(name)]) + name  # The proxy resolves the name.
    else:
        target = (b"\x01" if address.version == 4 else b"\x04") + address.packed
    writer.write(b"\x05\x01\x00" + target + port.to_bytes(2, "big"))
    await writer.drain()
    _, reply, _, kind = await reader.readexactly(4)
    if reply != 0:
        raise ProxyError(f"the SOCKS5 proxy could not connect: {_REPLIES.get(reply, reply)}")
    size = {1: 4, 4: 16}.get(kind)
    if size is None:
        size = (await reader.readexactly(1))[0]
    await reader.readexactly(size + 2)  # The bound address, which we do not need.


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            writer.write_eof()


class Socks5Bridge:
    """HTTP CONNECT on 127.0.0.1, tunnelled through a SOCKS5 proxy. aria2 cannot speak SOCKS."""

    def __init__(self, proxy: ProxyUrl) -> None:
        self.proxy = proxy
        self.server: asyncio.Server | None = None
        self.port = 0
        self.tunnels: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
        for task in self.tunnels:
            task.cancel()
        await asyncio.gather(*self.tunnels, return_exceptions=True)
        if self.server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.server.wait_closed(), 5)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.tunnels.add(task)
        try:
            await self._tunnel(reader, writer)
        finally:
            writer.close()
            if task is not None:
                self.tunnels.discard(task)

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_SECONDS)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            return
        method, _, rest = head.decode("latin-1").partition(" ")
        target = rest.partition(" ")[0]
        host, _, port_text = target.rpartition(":")
        if method != "CONNECT" or not host or not port_text.isdigit():
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            upstream_reader, upstream = await socks5_connect(
                self.proxy, host.strip("[]"), int(port_text)
            )
        except (OSError, ProxyError, asyncio.IncompleteReadError, TimeoutError) as error:
            log.info("proxy tunnel failed: %s", error)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.gather(_pipe(reader, upstream), _pipe(upstream_reader, writer))
        finally:
            upstream.close()
