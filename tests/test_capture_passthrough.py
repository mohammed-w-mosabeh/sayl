"""Passthrough end to end: the real proxy options, a TLS origin with its own CA, a raw client.

A passed-through host must reach the client with the origin's certificate (not re-signed); every
other host must be re-signed by Sayl's CA. The client picks the SNI, so no DNS is involved: each
CONNECT goes to the local origin, whose certificate is minted for whatever name was asked.
"""

import asyncio
import datetime
import ssl
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mitmproxy.tools.dump import DumpMaster

from daemon.capture import ca, detect
from daemon.capture.run import UPSTREAM_CA_ENV, options
from tests.stack import free_port

ORIGIN_CA = "Test Origin CA"


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


class Origin:
    """A TLS server that presents a certificate for any SNI, signed by its own CA."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        self.now = now
        self.ca = (
            x509.CertificateBuilder()
            .subject_name(_name(ORIGIN_CA))
            .issuer_name(_name(ORIGIN_CA))
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self.key, hashes.SHA256())
        )
        self.ca_file = root / "origin-ca.pem"
        self.ca_file.write_bytes(self.ca.public_bytes(serialization.Encoding.PEM))
        self.contexts: dict[str, ssl.SSLContext] = {}
        self.default = self._context("default.test")
        self.default.sni_callback = self._pick
        self.server: asyncio.Server | None = None
        self.port = 0

    def _context(self, host: str) -> ssl.SSLContext:
        if host in self.contexts:
            return self.contexts[host]
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = (
            x509.CertificateBuilder()
            .subject_name(_name(host))
            .issuer_name(self.ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(self.now - datetime.timedelta(days=1))
            .not_valid_after(self.now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(self.key, hashes.SHA256())
        )
        safe = host.replace("*", "_")
        chain = self.root / f"{safe}.pem"
        chain.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            + certificate.public_bytes(serialization.Encoding.PEM)
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(chain)
        context.set_alpn_protocols(["http/1.1"])
        self.contexts[host] = context
        return context

    def _pick(self, connection: ssl.SSLObject, name: str | None, _: ssl.SSLContext) -> None:
        if name:
            connection.context = self._context(name)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.default)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()


@asynccontextmanager
async def proxy_for(
    root: Path, origin: Origin, bypass: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[int]:
    """sayl-capture's own options: the same ignore_hosts and upstream verification."""
    monkeypatch.setenv(UPSTREAM_CA_ENV, str(origin.ca_file))
    ca.ensure(root / "ca")
    port = free_port()
    master = DumpMaster(
        options(port, str(root / "ca"), bypass), with_termlog=False, with_dumper=False
    )
    running = asyncio.create_task(master.run())
    for _ in range(200):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            break
        except OSError:
            await asyncio.sleep(0.02)
    try:
        yield port
    finally:
        master.shutdown()
        await asyncio.wait_for(running, 10)


@contextmanager
def trusting(*files: Path) -> Iterator[ssl.SSLContext]:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.load_default_certs()
    for file in files:
        context.load_verify_locations(file)
    context.set_alpn_protocols(["http/1.1"])
    yield context


async def issuer_through(port: int, origin: Origin, host: str, context: ssl.SSLContext) -> str:
    """CONNECT to the origin through the proxy with this SNI; return the leaf's issuer CN."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    target = f"127.0.0.1:{origin.port}"
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = await reader.readuntil(b"\r\n\r\n")
    assert b" 200" in head.split(b"\r\n")[0]
    await writer.start_tls(context, server_hostname=host)
    tls = writer.get_extra_info("ssl_object")
    der = tls.getpeercert(binary_form=True)
    writer.close()
    leaf = x509.load_der_x509_certificate(der)
    return str(leaf.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value)


@pytest.fixture
async def origin(tmp_path: Path) -> AsyncIterator[Origin]:
    server = Origin(tmp_path)
    await server.start()
    yield server
    await server.stop()


PASSED = ["api.anthropic.com", "claude.ai", "downloads.claude.ai"]
INTERCEPTED = [
    "github.com", "objects.githubusercontent.com", "registry.npmjs.org", "pypi.org",
    "files.pythonhosted.org", "docker.io", "ghcr.io", "example.com",
    "anthropic.com.evil.test", "notanthropic.com", "claude.ai.evil.test",
]  # fmt: skip


async def test_default_passthrough_is_not_resigned_and_the_rest_is(
    tmp_path: Path, origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    sayl = ca.load(tmp_path / "ca") or ca.ensure(tmp_path / "ca")
    async with proxy_for(tmp_path, origin, detect.DEFAULT_BYPASS, monkeypatch) as port:
        with trusting(origin.ca_file, sayl.cert_file) as context:
            for host in PASSED:  # Spliced: the client sees the origin's own certificate.
                assert await issuer_through(port, origin, host, context) == ORIGIN_CA, host
            for host in INTERCEPTED:  # Terminated and re-signed for capture.
                assert await issuer_through(port, origin, host, context) == sayl.name, host


async def test_passthrough_is_verifiable_without_sayls_ca(
    tmp_path: Path, origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What curl or an API client sees: only the real roots, never a failed handshake."""
    sayl = ca.ensure(tmp_path / "ca")
    async with proxy_for(tmp_path, origin, detect.DEFAULT_BYPASS, monkeypatch) as port:
        with trusting(origin.ca_file) as context:
            assert await issuer_through(port, origin, "api.anthropic.com", context) == ORIGIN_CA
            with pytest.raises(ssl.SSLCertVerificationError):
                await issuer_through(port, origin, "github.com", context)
    assert sayl.name.startswith("Sayl local CA")


async def test_opt_in_passthrough_from_config(
    tmp_path: Path, origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    sayl = ca.ensure(tmp_path / "ca")
    bypass = (*detect.DEFAULT_BYPASS, "github.com", "*.pythonhosted.org")
    async with proxy_for(tmp_path, origin, bypass, monkeypatch) as port:
        with trusting(origin.ca_file, sayl.cert_file) as context:
            assert await issuer_through(port, origin, "github.com", context) == ORIGIN_CA
            assert await issuer_through(port, origin, "files.pythonhosted.org", context) == (
                ORIGIN_CA
            )
            assert await issuer_through(port, origin, "pypi.org", context) == sayl.name
