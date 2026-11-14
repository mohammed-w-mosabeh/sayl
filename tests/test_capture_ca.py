import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from mitmproxy.certs import CertStore

from daemon.capture import ca


def test_creates_a_private_ec_ca(tmp_path: Path) -> None:
    directory = tmp_path / "ca"
    authority = ca.create(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / ca.KEY_FILE).stat().st_mode) == 0o600
    assert b"PRIVATE KEY" not in authority.cert_file.read_bytes()
    public = authority.certificate.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    assert public.curve.name == "secp256r1"
    constraints = authority.certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.value.ca and constraints.critical
    assert authority.name.startswith("Sayl local CA ")
    assert len(authority.fingerprint) == 64


def test_never_replaces_an_existing_ca(tmp_path: Path) -> None:
    first = ca.ensure(tmp_path)
    assert ca.ensure(tmp_path).fingerprint == first.fingerprint
    with pytest.raises(FileExistsError):
        ca.create(tmp_path)


def test_each_machine_gets_its_own_name(tmp_path: Path) -> None:
    assert ca.create(tmp_path / "a").name != ca.create(tmp_path / "b").name


def test_mitmproxy_signs_with_it(tmp_path: Path) -> None:
    authority = ca.create(tmp_path)
    store = CertStore.from_store(tmp_path, ca.MITMPROXY_BASENAME, 2048)
    assert store.default_ca.fingerprint().hex() == authority.fingerprint  # Loaded, not replaced.
    leaf = store.get_cert("download.example", [x509.DNSName("download.example")]).cert._cert
    public = authority.certificate.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    public.verify(
        leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256())
    )  # Raises if mitmproxy signed with anything else.
    assert leaf.issuer == authority.certificate.subject


def test_remove(tmp_path: Path) -> None:
    ca.create(tmp_path)
    CertStore.from_store(tmp_path, ca.MITMPROXY_BASENAME, 2048)
    ca.remove(tmp_path)
    assert ca.load(tmp_path) is None
    assert not list(tmp_path.glob("mitmproxy-*"))
