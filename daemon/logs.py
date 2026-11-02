"""Structured JSON logs with redaction and rotation."""

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SENSITIVE_KEYS = frozenset(
    ["authorization", "cookie", "cookies", "headers", "proxy-authorization", "set-cookie", "token"]
)
REDACTED = "[redacted]"
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]+")
_COOKIE = re.compile(r"(?i)\b(cookie|authorization)\s*[:=]\s*\S+")
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)


def redact_url(url: str) -> str:
    """Query strings and user info can hold secrets. Keep the host and path only."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def redact_text(text: str) -> str:
    text = _URL.sub(lambda match: redact_url(match.group()), text)
    text = _BEARER.sub(rf"\1 {REDACTED}", text)
    return _COOKIE.sub(rf"\1: {REDACTED}", text)


def redact_value(key: str, value: Any) -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {name: redact_value(str(name), item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value("", item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update({key: redact_value(key, value) for key, value in fields.items()})
        if record.exc_info:
            entry["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def setup_logging(path: Path, level: int = logging.INFO) -> None:
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
