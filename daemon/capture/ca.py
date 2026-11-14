"""The per-machine certificate authority. See "Certificate authority" in docs/SPEC.md.

Generated on first enable, EC P-256, never shipped. The directory is mitmproxy's `confdir`:
`mitmproxy-ca.pem` holds the key and certificate (mode 0600), `sayl-ca.crt` only the
certificate, which is what the trust stores receive.
"""

import datetime
import hashlib
import os
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

MITMPROXY_BASENAME = "mitmproxy"
KEY_FILE = f"{MITMPROXY_BASENAME}-ca.pem"
CERT_FILE = "sayl-ca.crt"
VALIDITY = datetime.timedelta(days=3650)
TRUSTED_ENV = "SAYL_CA_TRUSTED"  # Set by sayld for sayl-capture: "1" once a store trusts the CA.


@dataclass(frozen=True)
class Authority:
    directory: Path
    certificate: x509.Certificate

    @property
    def cert_file(self) -> Path:
        return self.directory / CERT_FILE

    @property
    def name(self) -> str:
        """The common name, also the nickname in NSS databases. Unique per machine."""
        value = self.certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        return str(value)

    @property
    def fingerprint(self) -> str:
        der = self.certificate.public_bytes(serialization.Encoding.DER)
        return hashlib.sha256(der).hexdigest()

    @property
    def pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)


def ca_dir(data: Path) -> Path:
    return data / "ca"


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    path.chmod(0o600)


def create(directory: Path, now: datetime.datetime | None = None) -> Authority:
    """Generate a new CA. Refuses to replace one: uninstall it from every store first."""
    if (directory / KEY_FILE).exists():
        raise FileExistsError(f"a CA already exists in {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    start = (now or datetime.datetime.now(datetime.UTC)) - datetime.timedelta(days=1)
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()[:32] or "localhost"
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"Sayl local CA {host} {secrets.token_hex(3)}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sayl"),
        ]
    )
    public = key.public_key()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    _write_private(directory / KEY_FILE, key_pem + cert_pem)
    (directory / CERT_FILE).write_bytes(cert_pem)
    (directory / CERT_FILE).chmod(0o644)
    return Authority(directory, certificate)


def load(directory: Path) -> Authority | None:
    file = directory / CERT_FILE
    if not file.exists() or not (directory / KEY_FILE).exists():
        return None
    return Authority(directory, x509.load_pem_x509_certificate(file.read_bytes()))


def ensure(directory: Path) -> Authority:
    return load(directory) or create(directory)


def remove(directory: Path) -> None:
    """Delete the key and certificate. Only after the CA left every trust store."""
    for path in directory.glob(f"{MITMPROXY_BASENAME}-*"):
        path.unlink(missing_ok=True)
    (directory / CERT_FILE).unlink(missing_ok=True)
