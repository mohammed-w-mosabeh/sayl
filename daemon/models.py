"""Database models. See "Data model" in docs/SPEC.md. Change them through Alembic only."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


class Status(StrEnum):
    QUEUED = "queued"
    PROBING = "probing"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    NEEDS_REFRESH = "needs_refresh"


class EngineName(StrEnum):
    ARIA2 = "aria2"
    YTDLP = "ytdlp"
    HYBRID = "hybrid"


class SegmentSource(StrEnum):
    STREAM = "stream"
    RANGE = "range"


class SegmentStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"


class PolicyReason(StrEnum):
    AUTH = "auth"
    GONE = "gone"
    VALIDATOR_CHANGED = "validator_changed"
    NOT_206 = "not_206"


class Category(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(unique=True)
    extensions: str = ""
    mime_prefixes: str = ""
    dest_dir: str = ""
    icon: str = ""


class Download(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    url: str
    final_url: str = ""
    page_url: str = ""
    filename: str = ""
    dest_dir: str = ""
    category_id: str | None = Field(default=None, foreign_key="category.id")
    engine: EngineName = EngineName.ARIA2
    engine_ref: str = ""
    status: Status = Field(default=Status.QUEUED, index=True)
    total_bytes: int = 0
    done_bytes: int = 0
    speed: int = 0
    eta: int = 0
    connections: int = 0
    priority: int = 0
    speed_limit: int = 0
    format_id: str = ""
    media_json: str = "{}"
    etag: str = ""
    last_modified: str = ""
    resumable: bool = False
    retries: int = 0
    headers_enc: bytes = b""
    checksum_algo: str = ""
    checksum_expected: str = ""
    checksum_ok: bool | None = None
    error: str = ""
    warnings_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Segment(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    download_id: str = Field(foreign_key="download.id", index=True)
    start: int
    end: int
    done_bytes: int = 0
    source: SegmentSource = SegmentSource.RANGE
    status: SegmentStatus = SegmentStatus.PENDING


class HostPolicy(SQLModel, table=True):
    host: str = Field(primary_key=True)
    accelerate: bool = False
    reason: PolicyReason
    updated_at: datetime = Field(default_factory=utcnow)


class ScheduleAction(StrEnum):
    START_QUEUE = "start_queue"
    STOP_QUEUE = "stop_queue"
    SPEED_LIMIT = "speed_limit"


class PostAction(StrEnum):
    NONE = "none"
    SHUTDOWN = "shutdown"
    SUSPEND = "suspend"
    COMMAND = "command"


class Schedule(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    cron: str  # Five crontab fields, or six with seconds first.
    action: str  # A ScheduleAction.
    params_json: str = "{}"  # speed_limit: {"limit": B/s}; command: {"command": [argv...]}.
    post_action: str = "none"  # A PostAction, run once the queue a start_queue began is empty.
    enabled: bool = True


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value_json: str = "null"
