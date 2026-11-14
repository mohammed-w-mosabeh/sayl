"""All database access. Every method blocks, so async callers go through `run`."""

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any, TypeVar, cast

from sqlalchemy import CursorResult, Engine, delete, update
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, col, select

from daemon.engines.hybrid import SegmentState
from daemon.models import (
    Category,
    Credential,
    Download,
    HostPolicy,
    PolicyReason,
    Schedule,
    Segment,
    SegmentStatus,
    Setting,
    Status,
)

T = TypeVar("T")


async def run[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(function, *args, **kwargs)


class Store:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def add(self, row: T) -> T:
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(row)
            session.commit()
            return row

    def download(self, download_id: str) -> Download | None:
        with Session(self.engine) as session:
            return session.get(Download, download_id)

    def downloads(self, status: Status | None = None) -> list[Download]:
        with Session(self.engine) as session:
            query = select(Download).order_by(
                col(Download.priority).desc(), col(Download.created_at)
            )
            if status is not None:
                query = query.where(Download.status == status)
            return list(session.exec(query))

    def has_url(self, url: str) -> bool:
        with Session(self.engine) as session:
            query = select(Download.id).where((Download.url == url) | (Download.final_url == url))
            return session.exec(query.limit(1)).first() is not None

    def update(self, download_id: str, **fields: Any) -> Download | None:
        with Session(self.engine, expire_on_commit=False) as session:
            row = session.get(Download, download_id)
            if row is None:
                return None
            for name, value in fields.items():
                setattr(row, name, value)
            session.add(row)
            try:
                session.commit()
            except StaleDataError:
                return None  # Deleted between the read and the write.
            return row

    def update_unless(self, download_id: str, statuses: Sequence[Status], **fields: Any) -> bool:
        """Update only while the status is not one of statuses, in one statement, so a stale
        progress snapshot cannot undo a finish that another task recorded meanwhile."""
        query = (
            update(Download)
            .where(col(Download.id) == download_id, col(Download.status).not_in(statuses))
            .values(**fields)
        )
        with Session(self.engine) as session:
            result = cast(CursorResult[Any], session.execute(query))
            session.commit()
            return bool(result.rowcount)

    def import_downloads(self, rows: Sequence[Download]) -> int:
        """Insert rows whose id is new, in one transaction. Returns how many were added."""
        with Session(self.engine) as session:
            existing = set(session.exec(select(Download.id)))
            added = 0
            for row in rows:
                if row.id in existing:
                    continue
                existing.add(row.id)
                session.add(row)
                added += 1
            session.commit()
            return added

    def delete_download(self, download_id: str) -> bool:
        with Session(self.engine) as session:
            session.execute(delete(Segment).where(col(Segment.download_id) == download_id))
            row = session.get(Download, download_id)
            if row is not None:
                session.delete(row)
            session.commit()
            return row is not None

    def save_segments_sync(self, download_id: str, segments: Sequence[SegmentState]) -> None:
        """Replace the segment map in one transaction, so a crash never leaves half of one."""
        with Session(self.engine) as session:
            session.execute(delete(Segment).where(col(Segment.download_id) == download_id))
            for state in segments:
                session.add(
                    Segment(
                        download_id=download_id,
                        start=state.start,
                        end=state.end,
                        done_bytes=state.done,
                        source=state.source,
                        status=SegmentStatus.DONE if state.complete else SegmentStatus.ACTIVE,
                    )
                )
            session.commit()

    async def save_segments(self, download_id: str, segments: Sequence[SegmentState]) -> None:
        await run(self.save_segments_sync, download_id, segments)

    def segments(self, download_id: str) -> list[SegmentState]:
        with Session(self.engine) as session:
            rows = session.exec(
                select(Segment)
                .where(Segment.download_id == download_id)
                .order_by(col(Segment.start))
            )
            return [SegmentState(r.start, r.end, r.source, r.done_bytes) for r in rows]

    def policy(self, host: str) -> HostPolicy | None:
        with Session(self.engine) as session:
            return session.get(HostPolicy, host)

    async def host_blocked(self, host: str) -> bool:
        policy = await run(self.policy, host)
        return policy is not None and not policy.accelerate

    def block_host_sync(self, host: str, reason: PolicyReason) -> None:
        with Session(self.engine) as session:
            session.merge(HostPolicy(host=host, reason=reason))
            session.commit()

    async def block_host(self, host: str, reason: PolicyReason) -> None:
        await run(self.block_host_sync, host, reason)

    def clear_host_policy(self) -> None:
        with Session(self.engine) as session:
            session.execute(delete(HostPolicy))
            session.commit()

    def settings(self) -> dict[str, Any]:
        with Session(self.engine) as session:
            return {row.key: json.loads(row.value_json) for row in session.exec(select(Setting))}

    def seed_categories(self, categories: list[Category]) -> None:
        """Add the default categories once. A user who deletes them does not get them back."""
        with Session(self.engine) as session:
            seeded = session.get(Setting, "categories_seeded")
            if seeded is not None:
                return
            if session.exec(select(Category)).first() is None:
                session.add_all(categories)
            session.add(Setting(key="categories_seeded", value_json="true"))
            session.commit()

    def put_settings(self, values: dict[str, Any]) -> None:
        with Session(self.engine) as session:
            for key, value in values.items():
                session.merge(Setting(key=key, value_json=json.dumps(value)))
            session.commit()

    def all(self, model: type[T]) -> list[T]:
        with Session(self.engine) as session:
            return list(session.exec(select(model)))

    def get(self, model: type[T], key: str) -> T | None:
        with Session(self.engine) as session:
            return session.get(model, key)

    def put(self, row: Category | Schedule | Credential) -> None:
        with Session(self.engine) as session:
            session.merge(row)
            session.commit()

    def remove(self, model: type[Category] | type[Schedule] | type[Credential], key: str) -> bool:
        with Session(self.engine) as session:
            row = session.get(model, key)
            if row is not None:
                session.delete(row)
            session.commit()
            return row is not None
