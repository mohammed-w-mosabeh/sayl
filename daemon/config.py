"""XDG paths, config.toml, and the API bearer token."""

import os
import secrets
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

DEFAULT_ACCELERATE_MIN_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class Paths:
    config: Path
    data: Path
    cache: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Paths":
        source = os.environ if env is None else env
        home = Path(source.get("HOME") or Path.home())

        def base(name: str, fallback: str) -> Path:
            return Path(source.get(name) or home / fallback) / "sayl"

        return cls(
            base("XDG_CONFIG_HOME", ".config"),
            base("XDG_DATA_HOME", ".local/share"),
            base("XDG_CACHE_HOME", ".cache"),
        )

    def create(self) -> None:
        for path in (self.config, self.data, self.cache):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def token(self) -> Path:
        return self.config / "token"

    @property
    def database(self) -> Path:
        return self.data / "sayl.db"

    @property
    def log(self) -> Path:
        return self.data / "sayld.log"


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 6971
    download_dir: str = ""
    max_concurrent: int = 3
    max_retries: int = 5
    speed_limit: int = 0
    connections: int = 16
    accelerate_min_bytes: int = DEFAULT_ACCELERATE_MIN_BYTES

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("all local services bind to 127.0.0.1 only")


def load_config(paths: Paths) -> Config:
    """Read config.toml. Unknown keys are ignored so a newer file still starts."""
    file = paths.config / "config.toml"
    values: dict[str, Any] = tomllib.loads(file.read_text()) if file.exists() else {}
    known = {field.name for field in fields(Config)}
    return Config(**{key: value for key, value in values.items() if key in known})


def ensure_token(paths: Paths) -> str:
    """Return the bearer token, creating it with mode 0600 and repairing a looser mode."""
    file = paths.token
    try:
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        file.chmod(0o600)
        return file.read_text().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(token)
    return token
