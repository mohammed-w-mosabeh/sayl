"""Download statistics. See "Statistics" in docs/SPEC.md. Pure functions over stored rows."""

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from daemon.models import Category, Download, Status

DAYS = 30
TOP_HOSTS = 10


def _utc(value: datetime) -> datetime:
    """SQLite drops the zone; stored times are UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def compute(rows: list[Download], categories: list[Category], today: date) -> dict[str, Any]:
    by_status = Counter(row.status.value for row in rows)
    completed = [row for row in rows if row.status is Status.COMPLETED]
    first = today - timedelta(days=DAYS - 1)
    per_day = {first + timedelta(days=n): 0 for n in range(DAYS)}
    hosts: Counter[str] = Counter()
    names = {category.id: category.name for category in categories}
    by_category: Counter[str] = Counter()
    timed_bytes = 0
    timed_seconds = 0.0
    for row in completed:
        size = row.total_bytes or row.done_bytes
        hosts[(urlsplit(row.final_url or row.url).hostname or "").lower()] += size
        by_category[names.get(row.category_id or "", "")] += size
        if row.finished_at is not None:
            day = _utc(row.finished_at).date()
            if day in per_day:
                per_day[day] += size
            if row.started_at is not None:
                seconds = (_utc(row.finished_at) - _utc(row.started_at)).total_seconds()
                if seconds > 0:
                    timed_bytes += size
                    timed_seconds += seconds
    return {
        "by_status": dict(sorted(by_status.items())),
        "completed": len(completed),
        "completed_bytes": sum(row.total_bytes or row.done_bytes for row in completed),
        "per_day": [{"day": day.isoformat(), "bytes": size} for day, size in per_day.items()],
        "top_hosts": [
            {"host": host, "bytes": size} for host, size in hosts.most_common(TOP_HOSTS) if host
        ],
        "by_category": [
            {"category": name, "bytes": size} for name, size in by_category.most_common()
        ],
        "average_speed": int(timed_bytes / timed_seconds) if timed_seconds else 0,
    }
