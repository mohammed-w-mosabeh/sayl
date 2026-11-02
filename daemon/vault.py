"""Encrypts stored request headers. They can hold cookies and tokens, so never log them."""

import json
import logging
import os
from pathlib import Path

import keyring
from cryptography.fernet import Fernet
from keyring.errors import KeyringError

SERVICE = "sayl"
ACCOUNT = "headers-key"
log = logging.getLogger(__name__)


def _file_key(path: Path) -> str:
    """Fallback for sessions with no keyring, such as a headless systemd unit."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        path.chmod(0o600)
        return path.read_text().strip()
    key = Fernet.generate_key().decode()
    with os.fdopen(descriptor, "w") as stream:
        stream.write(key)
    return key


def load_key(fallback: Path) -> str:
    try:
        key = keyring.get_password(SERVICE, ACCOUNT)
        if key is None:
            key = Fernet.generate_key().decode()
            keyring.set_password(SERVICE, ACCOUNT, key)
    except KeyringError:
        log.warning("no system keyring: the headers key is in a 0600 file")
        return _file_key(fallback)
    return key


def keyring_available() -> bool:
    try:
        keyring.get_password(SERVICE, ACCOUNT)
    except KeyringError:
        return False
    return True


class Vault:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def seal(self, headers: dict[str, str]) -> bytes:
        return self._fernet.encrypt(json.dumps(headers).encode()) if headers else b""

    def open(self, sealed: bytes) -> dict[str, str]:
        if not sealed:
            return {}
        opened: dict[str, str] = json.loads(self._fernet.decrypt(sealed))
        return opened
