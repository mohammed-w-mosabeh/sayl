"""Per-site credentials. See "Site credentials" in docs/SPEC.md.

The password is sealed with the vault key and kept in memory only for matching. Nothing here
logs a username or a password, and nothing returns a password to an API client.
"""

import base64
from dataclasses import dataclass
from urllib.parse import urlsplit

from daemon.models import Credential, utcnow
from daemon.store import Store, run
from daemon.vault import Vault


@dataclass(frozen=True)
class Login:
    username: str
    password: str

    def basic(self) -> str:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Basic {token}"


def normalize_host(value: str) -> str:
    """Accept a host or a URL; return the lowercase host without port or brackets."""
    text = value.strip()
    host = urlsplit(text if "//" in text else f"//{text}").hostname or ""
    return host.lower()


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class Credentials:
    def __init__(self, store: Store, vault: Vault) -> None:
        self.store = store
        self.vault = vault
        self._logins: dict[str, Login] = {}

    async def load(self) -> None:
        rows = await run(self.store.all, Credential)
        self._logins = {row.host: self._open(row) for row in rows}

    def _open(self, row: Credential) -> Login:
        return Login(row.username, self.vault.open(row.secret_enc).get("password", ""))

    def hosts(self) -> list[tuple[str, str]]:
        """(host, username) pairs, sorted. Passwords stay inside the daemon."""
        return sorted((host, login.username) for host, login in self._logins.items())

    async def set(self, host: str, username: str, password: str) -> str:
        key = normalize_host(host)
        if not key:
            raise ValueError("not a host name")
        sealed = self.vault.seal({"password": password})
        row = Credential(host=key, username=username, secret_enc=sealed, updated_at=utcnow())
        await run(self.store.put, row)
        self._logins[key] = Login(username, password)
        return key

    async def remove(self, host: str) -> bool:
        key = normalize_host(host)
        self._logins.pop(key, None)
        return await run(self.store.remove, Credential, key)

    def for_url(self, url: str) -> Login | None:
        return self._logins.get(host_of(url))
