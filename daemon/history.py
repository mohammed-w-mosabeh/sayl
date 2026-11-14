"""History export and import. See "History export and import" in docs/SPEC.md.

An export never carries headers, cookies, credentials, or settings: only what the list shows.
"""

from datetime import datetime
from typing import Any

from daemon.filenames import sanitize
from daemon.models import Category, Download, EngineName, Status

FORMAT = "sayl-history"
VERSION = 1
MAX_ENTRIES = 100_000
FINISHED = {Status.COMPLETED, Status.FAILED, Status.CANCELED}
NEEDS_REFRESH = "the link from the history file needs a new one"

# Copied as they are. Anything not listed (headers_enc, media_json, engine_ref, ...) stays home.
FIELDS = (
    "id",
    "url",
    "final_url",
    "page_url",
    "filename",
    "dest_dir",
    "engine",
    "status",
    "total_bytes",
    "done_bytes",
    "checksum_algo",
    "checksum_expected",
    "checksum_ok",
    "error",
)
TIMES = ("created_at", "started_at", "finished_at")


class HistoryError(ValueError):
    pass


def export(rows: list[Download], categories: list[Category]) -> dict[str, Any]:
    names = {category.id: category.name for category in categories}
    entries = []
    for row in rows:
        entry: dict[str, Any] = {name: getattr(row, name) for name in FIELDS}
        entry["engine"] = row.engine.value
        entry["status"] = row.status.value
        entry["category"] = names.get(row.category_id or "", "")
        for name in TIMES:
            value: datetime | None = getattr(row, name)
            entry[name] = value.isoformat() if value is not None else None
        entries.append(entry)
    return {"format": FORMAT, "version": VERSION, "downloads": entries}


def _time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HistoryError("a timestamp is not a string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as bad:
        raise HistoryError(f"bad timestamp {value!r}") from bad


def _text(entry: dict[str, Any], name: str) -> str:
    value = entry.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HistoryError(f"{name} is not a string")
    return value


def _count(entry: dict[str, Any], name: str) -> int:
    value = entry.get(name, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HistoryError(f"{name} is not a size")
    return value


def parse(document: Any, categories: list[Category]) -> list[Download]:
    """Rows to insert, in file order. Raises HistoryError for a file this is not."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise HistoryError("not a Sayl history file")
    if document.get("version") != VERSION:
        raise HistoryError(f"unsupported history version {document.get('version')!r}")
    entries = document.get("downloads")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise HistoryError("downloads must be a list of at most 100000 entries")
    ids = {category.name: category.id for category in categories}
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise HistoryError("an entry is not an object")
        url = _text(entry, "url")
        if not url:
            raise HistoryError("an entry has no url")
        try:
            engine = EngineName(entry.get("engine", EngineName.ARIA2.value))
            status = Status(entry.get("status", Status.COMPLETED.value))
        except ValueError as bad:
            raise HistoryError(str(bad)) from bad
        message = _text(entry, "error")
        if status not in FINISHED:
            # Never start anything on import. A capture's stream is long gone.
            if engine is EngineName.HYBRID:
                status, message = Status.NEEDS_REFRESH, NEEDS_REFRESH
            else:
                status = Status.PAUSED
        checksum_ok = entry.get("checksum_ok")
        row = Download(
            url=url,
            final_url=_text(entry, "final_url"),
            page_url=_text(entry, "page_url"),
            filename=sanitize(_text(entry, "filename")) if entry.get("filename") else "",
            dest_dir=_text(entry, "dest_dir"),
            category_id=ids.get(_text(entry, "category")),
            engine=engine,
            status=status,
            total_bytes=_count(entry, "total_bytes"),
            done_bytes=_count(entry, "done_bytes"),
            checksum_algo=_text(entry, "checksum_algo"),
            checksum_expected=_text(entry, "checksum_expected"),
            checksum_ok=checksum_ok if isinstance(checksum_ok, bool) else None,
            error=message,
        )
        if entry.get("id"):
            row.id = _text(entry, "id")
        created = _time(entry.get("created_at"))
        if created is not None:
            row.created_at = created
        row.started_at = _time(entry.get("started_at"))
        row.finished_at = _time(entry.get("finished_at"))
        rows.append(row)
    return rows
