"""The Engine interface. See "Architecture" in docs/SPEC.md."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from daemon.models import EngineName, Status


class AttachNotSupportedError(Exception):
    """This engine cannot take over a byte stream that is already open."""


class EngineError(Exception):
    """A failure the user can read. Never put headers or query strings in the message."""


@dataclass(frozen=True)
class StreamInfo:
    """What the original response said. This decides whether acceleration is allowed."""

    method: str = "GET"
    total: int | None = None
    accept_ranges: bool = False
    etag: str = ""
    last_modified: str = ""
    content_encoding: str = ""


@dataclass
class Job:
    id: str
    url: str
    dest_dir: str
    filename: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    speed_limit: int = 0
    format_id: str = ""
    stream: StreamInfo = field(default_factory=StreamInfo)
    audio_only: bool = False
    subtitles: tuple[str, ...] = ()
    playlist_items: str = ""
    cookies_file: str = ""
    cookies_from_browser: str = ""


@dataclass(frozen=True)
class ProbeResult:
    url: str
    filename: str = ""
    total: int | None = None
    resumable: bool = False
    extractor: str = ""
    formats: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class Progress:
    status: Status
    total: int = 0
    done: int = 0
    speed: int = 0
    connections: int = 0
    filename: str = ""
    error: str = ""


class Engine(ABC):
    name: EngineName

    @abstractmethod
    async def probe(self, url: str, headers: dict[str, str]) -> ProbeResult: ...

    @abstractmethod
    async def add(self, job: Job) -> str: ...

    async def attach(self, job: Job, stream: AsyncIterator[bytes]) -> str:
        """Take ownership of a byte stream already open at offset 0."""
        raise AttachNotSupportedError(self.name)

    @abstractmethod
    async def pause(self, engine_id: str) -> None: ...

    @abstractmethod
    async def resume(self, engine_id: str) -> None: ...

    @abstractmethod
    async def cancel(self, engine_id: str) -> None: ...

    @abstractmethod
    async def set_speed_limit(self, engine_id: str, limit: int) -> None: ...

    @abstractmethod
    async def refresh_url(self, engine_id: str, new_url: str) -> None: ...

    @abstractmethod
    async def status(self, engine_id: str) -> Progress: ...
