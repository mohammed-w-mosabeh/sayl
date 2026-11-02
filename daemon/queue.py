"""Queue manager: concurrency, priorities, retries, speed limits, and crash recovery."""

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from daemon.config import Config
from daemon.engines.aria2 import Aria2Engine
from daemon.engines.base import Engine, EngineError, Job, Progress, StreamInfo
from daemon.engines.hybrid import NEEDS_REFRESH, HybridEngine
from daemon.events import EventBus
from daemon.models import Download, EngineName, Status, utcnow
from daemon.router import route
from daemon.store import Store, run
from daemon.vault import Vault

log = logging.getLogger(__name__)

TICK_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 60.0
FINISHED = (Status.COMPLETED, Status.FAILED, Status.CANCELED, Status.NEEDS_REFRESH)


def backoff(retries: int) -> float:
    """Exponential with jitter, so a dead host is not hit in lockstep."""
    return min(MAX_BACKOFF_SECONDS, 2.0**retries) * random.uniform(0.5, 1.0)  # noqa: S311


def job_from(row: Download, vault: Vault) -> Job:
    media = json.loads(row.media_json)
    return Job(
        row.id,
        row.final_url or row.url,
        row.dest_dir,
        row.filename,
        vault.open(row.headers_enc),
        row.speed_limit,
        row.format_id,
        StreamInfo(
            total=row.total_bytes or None,
            accept_ranges=row.resumable,
            etag=row.etag,
            last_modified=row.last_modified,
        ),
        media.get("audio_only", False),
        tuple(media.get("subtitles", ())),
        media.get("playlist_items", ""),
        media.get("cookies_file", ""),
        media.get("cookies_from_browser", ""),
    )


class QueueManager:
    def __init__(
        self,
        store: Store,
        vault: Vault,
        engines: Mapping[EngineName, Engine],
        bus: EventBus,
        config: Config,
    ) -> None:
        self.store = store
        self.vault = vault
        self.engines = engines
        self.bus = bus
        self.config = config
        self.active: dict[str, EngineName] = {}
        self.retry_at: dict[str, float] = {}
        self.wake = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.warmup: asyncio.Task[EngineName] | None = None
        self.recoveries: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await self.recover()
        self.task = asyncio.create_task(self._loop())
        # The first extractor scan takes seconds, so do not make the first download wait for it.
        self.warmup = asyncio.create_task(run(route, "https://sayl.invalid/"))

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.warmup is not None:
            await self.warmup
        for task in self.recoveries:
            task.cancel()
        await asyncio.gather(*self.recoveries, return_exceptions=True)

    def poke(self) -> None:
        self.wake.set()

    async def _loop(self) -> None:
        while True:
            self.wake.clear()
            await self._schedule()
            await self.poll()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), TICK_SECONDS)

    async def _schedule(self) -> None:
        now = time.monotonic()
        for row in await run(self.store.downloads, Status.QUEUED):
            if len(self.active) >= self.config.max_concurrent:
                return
            if self.retry_at.get(row.id, 0.0) <= now:
                await self._start(row)

    async def _start(self, row: Download) -> None:
        self.retry_at.pop(row.id, None)
        name = await run(route, row.url)
        job = job_from(row, self.vault)
        try:
            reference = await self.engines[name].add(job)
        except (EngineError, OSError) as error:
            await self._failed(row, str(error))
            return
        self.active[row.id] = name
        await run(
            self.store.update,
            row.id,
            engine=name,
            engine_ref=reference,
            status=Status.ACTIVE,
            started_at=row.started_at or utcnow(),
        )

    async def poll(self) -> None:
        for download_id, name in list(self.active.items()):
            row = await run(self.store.download, download_id)
            if row is None:
                self.active.pop(download_id, None)
                continue
            try:
                progress = await self.engines[name].status(row.engine_ref)
            except EngineError as error:
                if name is not EngineName.HYBRID:  # A resumed capture may not have registered yet.
                    await self._failed(row, str(error))
                continue
            await self.apply(row, progress)

    async def apply(self, row: Download, progress: Progress) -> None:
        if progress.status in FINISHED and self.active.pop(row.id, None) is None:
            return  # Another path already recorded the end of this download.
        fields = {
            "total_bytes": progress.total or row.total_bytes,
            "done_bytes": progress.done,
            "speed": progress.speed,
            "connections": progress.connections,
            "filename": progress.filename or row.filename,
        }
        if progress.status is Status.NEEDS_REFRESH:
            await run(
                self.store.update, row.id, **fields, status=progress.status, error=progress.error
            )
            self.bus.publish("download.needs_refresh", id=row.id, error=progress.error)
            self.poke()
        elif progress.status is Status.FAILED:
            await run(self.store.update, row.id, **fields)
            await self._failed(row, progress.error or "download failed")
        elif progress.status in FINISHED:
            await run(
                self.store.update, row.id, **fields, status=progress.status, finished_at=utcnow()
            )
            if progress.status is Status.COMPLETED:
                self.bus.publish("download.completed", id=row.id)
            self.poke()
        else:
            await run(self.store.update, row.id, **fields, status=progress.status)
            self.bus.publish(
                "download.progress",
                id=row.id,
                done=progress.done,
                total=fields["total_bytes"],
                speed=progress.speed,
                connections=progress.connections,
            )

    async def _failed(self, row: Download, error: str) -> None:
        self.active.pop(row.id, None)
        retryable = row.engine is not EngineName.HYBRID and row.retries < self.config.max_retries
        if retryable:
            self.retry_at[row.id] = time.monotonic() + backoff(row.retries)
            await run(
                self.store.update,
                row.id,
                status=Status.QUEUED,
                retries=row.retries + 1,
                error=error,
            )
            return
        await run(
            self.store.update, row.id, status=Status.FAILED, error=error, finished_at=utcnow()
        )
        self.bus.publish("download.failed", id=row.id, error=error)
        self.poke()

    async def run_attached(self, row: Download, stream: AsyncIterator[bytes]) -> None:
        """Consume a captured stream. It is already open, so it never waits for a free slot."""
        hybrid = self.engines[EngineName.HYBRID]
        self.active[row.id] = EngineName.HYBRID
        await run(self.store.update, row.id, status=Status.ACTIVE, started_at=utcnow())
        job = job_from(row, self.vault)
        await hybrid.attach(job, stream)
        await self.apply(row, await hybrid.status(row.id))

    async def pause(self, row: Download) -> None:
        if row.id in self.active:
            await self.engines[row.engine].pause(row.engine_ref)
        await run(self.store.update, row.id, status=Status.PAUSED)

    async def resume(self, row: Download) -> None:
        if row.engine is EngineName.HYBRID and row.id not in self.active:
            raise EngineError(NEEDS_REFRESH)  # Queueing it would request the URL again.
        if row.id in self.active:
            await self.engines[row.engine].resume(row.engine_ref)
            await run(self.store.update, row.id, status=Status.ACTIVE)
        else:
            await run(self.store.update, row.id, status=Status.QUEUED)
        self.poke()

    async def cancel(self, row: Download) -> None:
        if row.id in self.active:
            await self.engines[row.engine].cancel(row.engine_ref)
            self.active.pop(row.id, None)
        elif row.engine is EngineName.HYBRID:
            (Path(row.dest_dir) / f".{row.id}.part").unlink(missing_ok=True)
        await run(self.store.update, row.id, status=Status.CANCELED, finished_at=utcnow())
        self.poke()

    async def set_speed_limit(self, row: Download, limit: int) -> None:
        await run(self.store.update, row.id, speed_limit=limit)
        if row.id in self.active:
            await self.engines[row.engine].set_speed_limit(row.engine_ref, limit)

    async def set_global_speed_limit(self, limit: int) -> None:
        for engine in self.engines.values():
            if isinstance(engine, HybridEngine):
                engine.limiter.set_rate(limit)
            elif isinstance(engine, Aria2Engine):
                await engine.set_global_speed_limit(limit)

    async def refresh_url(self, row: Download, url: str, headers: dict[str, str]) -> None:
        sealed = self.vault.seal(headers) if headers else row.headers_enc
        updated = await run(self.store.update, row.id, url=url, final_url=url, headers_enc=sealed)
        if updated is None:
            return
        if updated.status is Status.NEEDS_REFRESH:
            await self._continue_captured(updated, respect_policy=False)
        elif row.id in self.active:
            await self.engines[row.engine].refresh_url(row.engine_ref, url)

    async def recover(self) -> None:
        """Restart anything the last process left running. See "Crash recovery" in the SPEC."""
        for status in (Status.ACTIVE, Status.PROBING):
            for row in await run(self.store.downloads, status):
                await self._recover(row)
        for row in await run(self.store.downloads, Status.PAUSED):
            if row.engine is EngineName.HYBRID:
                await self._recover_hybrid(row)  # Its stream died, so it cannot stay paused.

    async def _recover(self, row: Download) -> None:
        if row.engine is EngineName.HYBRID:
            await self._recover_hybrid(row)
        elif row.engine is EngineName.ARIA2 and await self._aria2_alive(row):
            self.active[row.id] = row.engine  # aria2's session file already restored it.
        else:
            await run(self.store.update, row.id, status=Status.QUEUED)

    async def _aria2_alive(self, row: Download) -> bool:
        try:
            await self.engines[EngineName.ARIA2].status(row.engine_ref)
        except EngineError:
            return False
        return True

    async def _recover_hybrid(self, row: Download) -> None:
        await self._continue_captured(row, respect_policy=True)

    async def _continue_captured(self, row: Download, *, respect_policy: bool) -> None:
        """Continue from the segment map, on a task so the caller is not held up."""
        hybrid = self.engines[EngineName.HYBRID]
        assert isinstance(hybrid, HybridEngine)  # noqa: S101 - wired in daemon.main
        segments = await run(self.store.segments, row.id)
        job = job_from(row, self.vault)
        await run(self.store.update, row.id, status=Status.ACTIVE, error="")

        async def resume() -> None:
            await hybrid.continue_download(job, segments, respect_policy=respect_policy)
            await self.apply(row, await hybrid.status(row.id))

        self.active[row.id] = EngineName.HYBRID
        task = asyncio.create_task(resume())
        self.recoveries.add(task)
        task.add_done_callback(self.recoveries.discard)
