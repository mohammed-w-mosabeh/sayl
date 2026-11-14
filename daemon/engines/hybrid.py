"""HybridEngine: stream first, accelerate if allowed. See "Addon logic" in docs/SPEC.md.

The original stream is the guarantee: it stays open until the segment map shows the file is
complete, so a rejected Range connection can never lose a download.
"""

import asyncio
import contextlib
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from daemon.capture.hints import looks_single_use
from daemon.config import DEFAULT_SPOOL_MAX_BYTES
from daemon.engines.base import (
    Engine,
    EngineError,
    Job,
    ProbeResult,
    Progress,
    StreamInfo,
)
from daemon.engines.limiter import Limiter
from daemon.filenames import sanitize, unique_path
from daemon.models import EngineName, PolicyReason, SegmentSource, Status

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
STALL_SECONDS = 120.0  # A stream silent this long while we read it is treated as lost.
RANGE_RETRY_SECONDS = 600.0  # A Range connection retries network errors this long without progress.
SAVE_EVERY_SECONDS = 1.0
NEEDS_REFRESH = "link expired, refresh it"
NETWORK_LOST = "the network dropped, resume to retry"
CANNOT_CONTINUE = "this link cannot continue the file, refresh it with another"
FORWARDED = ("cookie", "authorization", "referer", "user-agent")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)$")


@dataclass
class SegmentState:
    start: int
    end: int  # Inclusive.
    source: SegmentSource
    done: int = 0

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def open_ended(self) -> bool:
        return self.end < self.start

    @property
    def complete(self) -> bool:
        return not self.open_ended and self.done >= self.size


class SegmentStore(Protocol):
    async def save_segments(self, download_id: str, segments: Sequence[SegmentState]) -> None: ...

    async def host_blocked(self, host: str) -> bool: ...

    async def block_host(self, host: str, reason: PolicyReason) -> None: ...


def may_accelerate(info: StreamInfo, url: str, min_bytes: int, connections: int) -> bool:
    """Every SPEC condition except the host policy, which needs the database."""
    strong_etag = bool(info.etag) and not info.etag.startswith("W/")
    return (
        info.method == "GET"
        and info.accept_ranges
        and info.total is not None
        and info.total > min_bytes
        and (strong_etag or bool(info.last_modified))
        and not info.content_encoding
        and connections > 1
        and not looks_single_use(url)
    )


def may_resume(info: StreamInfo) -> bool:
    """Whether Range requests could safely finish this file. Size and the hint do not matter."""
    strong_etag = bool(info.etag) and not info.etag.startswith("W/")
    return (
        info.method == "GET"
        and info.accept_ranges
        and info.total is not None
        and (strong_etag or bool(info.last_modified))
        and not info.content_encoding
    )


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


class _SpoolFull(EngineError):
    """A paused stream that cannot resume read as far as it may. It ends in needs_refresh."""


def plan_segments(offset: int, total: int, connections: int) -> tuple[int, list[tuple[int, int]]]:
    """Split what is left. The stream keeps the first share; ranges are returned tail first."""
    share = max(1, (total - offset) // connections)
    boundary = offset + share
    workers = connections - 1
    size = max(1, (total - boundary) // workers)
    ranges = []
    start = boundary
    for index in range(workers):
        end = total - 1 if index == workers - 1 else start + size - 1
        ranges.append((start, end))
        start = end + 1
    return boundary, ranges[::-1]


def rejection(
    response: httpx.Response, start: int, end: int | None, info: StreamInfo
) -> PolicyReason | None:
    """Return why this Range response may not be used, or None when it is safe.

    end is None for an open-ended request, when the original size was never known.
    """
    if response.status_code in (401, 403):
        return PolicyReason.AUTH
    if response.status_code == 410:
        return PolicyReason.GONE
    if response.status_code != 206:
        return PolicyReason.NOT_206
    match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
    if match is None:
        return PolicyReason.NOT_206
    first, last, total = map(int, match.groups())
    if first != start or (end is not None and last != end):
        return PolicyReason.NOT_206
    if info.total is not None and total != info.total:
        return PolicyReason.NOT_206
    if info.etag and response.headers.get("etag") != info.etag:
        return PolicyReason.VALIDATOR_CHANGED
    if info.last_modified and response.headers.get("last-modified") != info.last_modified:
        return PolicyReason.VALIDATOR_CHANGED
    return None


class _Canceled(Exception):
    pass


class _Rejected(Exception):
    def __init__(self, reason: PolicyReason | None) -> None:
        self.reason = reason  # None means a network failure: keep the host and every byte.


class _Transfer:
    def __init__(self, engine: "HybridEngine", job: Job) -> None:
        self.engine = engine
        self.job = job
        self.limiter = Limiter(job.speed_limit)
        directory = Path(job.dest_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.part = directory / f".{job.id}.part"
        self.fd = os.open(self.part, os.O_RDWR | os.O_CREAT, 0o600)
        self.offset = 0
        self.reserved = 0  # End of the stream chunk being written right now.
        self.boundary: int | None = None
        self.stream_segment = SegmentState(0, -1, SegmentSource.STREAM)
        self.ranges: list[SegmentState] = []
        self.changed = asyncio.Event()
        self.running = asyncio.Event()
        self.running.set()
        self.status = Status.ACTIVE
        self.error = ""
        self.filename = job.filename
        self.saved_at = 0.0
        self.last_done = 0
        self.last_seen = 0.0
        self.tasks: set[asyncio.Task[None]] = set()
        self.finished = asyncio.Event()
        self.canceled = False
        self.filling = False  # The stream is gone, so ranges alone describe the file.
        self.network_lost = False  # Range connections gave up on the network, not the server.
        # A paused stream normally waits under back-pressure. A link that cannot resume would
        # die if the server times the idle connection out, so it keeps writing ahead instead.
        self.spool = not may_resume(job.stream)
        self.spool_start: int | None = None

    @property
    def total(self) -> int | None:
        return self.job.stream.total

    @property
    def done(self) -> int:
        return self.offset + sum(segment.done for segment in self.ranges)

    @property
    def connections(self) -> int:
        active = sum(1 for segment in self.ranges if not segment.complete)
        return active + (0 if self.at_boundary else 1)

    @property
    def at_boundary(self) -> bool:
        return self.boundary is not None and self.offset >= self.boundary

    def _ranges_complete(self) -> bool:
        return bool(self.ranges) and all(segment.complete for segment in self.ranges)

    def _notify(self) -> None:
        self.changed.set()

    async def _write(self, data: bytes, offset: int) -> None:
        await self.running.wait()
        await self._write_now(data, offset)

    async def _write_now(self, data: bytes, offset: int) -> None:
        await self.limiter.acquire(len(data))
        await self.engine.limiter.acquire(len(data))
        await asyncio.to_thread(os.pwrite, self.fd, data, offset)

    def segments(self) -> list[SegmentState]:
        if self.filling:
            return list(self.ranges)
        end = (self.boundary or self.total or self.offset) - 1
        stream = SegmentState(0, end, SegmentSource.STREAM, self.offset)
        return [stream, *self.ranges]

    async def _save(self, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if force or now - self.saved_at >= SAVE_EVERY_SECONDS:
            self.saved_at = now
            await self.engine.store.save_segments(self.job.id, self.segments())

    async def _chunks(self, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """The stream's chunks. A dead connection can stay silent forever: give up on it."""
        iterator = aiter(stream)
        while True:
            try:
                chunk = await asyncio.wait_for(anext(iterator), self.engine.stall_seconds)
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise EngineError("the stream stalled") from None
            yield chunk

    async def pump(self, stream: AsyncIterator[bytes]) -> None:
        """Write the original stream, pausing it at the boundary without closing it."""
        async for chunk in self._chunks(stream):
            data = chunk
            while data:
                self._check_canceled()
                if self.boundary is not None and self.offset >= self.boundary:
                    await self._wait_at_boundary()
                    if self._ranges_complete():
                        return
                    continue
                room = len(data)
                limit = self.boundary if self.boundary is not None else self.total
                if limit is not None:
                    room = min(room, limit - self.offset)
                if room <= 0:
                    return
                room = await self._hold(room)
                self.reserved = self.offset + room
                await self._write_now(data[:room], self.offset)
                self.offset += room
                data = data[room:]
                self._notify()
                await self._save()
            if self.total is not None and self.offset >= self.total:
                return
            if self.at_boundary and self._ranges_complete():
                return

    async def _hold(self, room: int) -> int:
        """Wait while paused, or spool ahead within the bound. Returns how much may be written.

        See "Spool bound" in docs/SPEC.md: the bound counts from the pause, and a spool that
        cannot start or has to stop leaves the stream for needs_refresh with every byte kept.
        """
        if self.running.is_set():
            self.spool_start = None
            return room
        if not self.spool:
            await self.running.wait()
            self._check_canceled()
            return room
        limit = self.engine.spool_max
        if self.spool_start is None:
            left = limit if self.total is None else min(limit, self.total - self.offset)
            if await asyncio.to_thread(free_bytes, self.directory) < left:
                raise _SpoolFull("not enough free space to hold a paused stream")
            self.spool_start = self.offset
        allowed = self.spool_start + limit - self.offset
        if allowed <= 0:
            raise _SpoolFull("a paused stream reached the spool limit")
        return min(room, allowed)

    def _check_canceled(self) -> None:
        if self.canceled:
            raise _Canceled

    async def _wait_at_boundary(self) -> None:
        while self.boundary is not None and not self._ranges_complete():
            self._check_canceled()
            if self.network_lost:
                # Finish from the segment map by Range, keeping what the ranges already wrote.
                raise EngineError("Range connections lost the network")
            self.changed.clear()
            await self.changed.wait()

    def _fall_back(self) -> None:
        for task in self.tasks:
            task.cancel()
        self.ranges = []
        self.boundary = None
        self._notify()

    def _learn_total(self, segment: SegmentState, response: httpx.Response) -> None:
        total = int(response.headers["content-range"].rpartition("/")[2])
        segment.end = total - 1
        self.job.stream = replace(self.job.stream, total=total)

    def _headers(self, start: int, end: int | None) -> dict[str, str]:
        wanted = {
            name: value for name, value in self.job.headers.items() if name.lower() in FORWARDED
        }
        last = "" if end is None else end
        return {**wanted, "Range": f"bytes={start}-{last}", "Accept-Encoding": "identity"}

    async def _fetch(self, segment: SegmentState, validated: asyncio.Event) -> None:
        """Fetch one segment. A network error is retried from where it stopped; a refusal is not.

        Retrying stops once the segment has made no progress for range_retry_seconds, so a
        Wi-Fi outage of a few minutes costs nothing.
        """
        loop = asyncio.get_running_loop()
        attempt = 0
        failing_since = loop.time()
        while True:
            before = segment.done
            try:
                await self._fetch_once(segment, validated)
                return
            except _Rejected as rejected:
                if rejected.reason is not None:
                    raise
                if segment.done > before:
                    attempt, failing_since = 0, loop.time()
                if loop.time() - failing_since >= self.engine.range_retry_seconds:
                    raise
            await asyncio.sleep(min(30.0, self.engine.retry_delay * 2**attempt))
            attempt += 1

    async def _fetch_once(self, segment: SegmentState, validated: asyncio.Event) -> None:
        start = segment.start + segment.done
        end = None if segment.open_ended else segment.end
        request = self.engine.client.stream("GET", self.job.url, headers=self._headers(start, end))
        try:
            async with request as response:
                reason = rejection(response, start, end, self.job.stream)
                if reason is not None:
                    raise _Rejected(reason)
                validated.set()
                if end is None:
                    self._learn_total(segment, response)
                async for chunk in response.aiter_bytes(CHUNK):
                    self._check_canceled()
                    if segment.done + len(chunk) > segment.size:
                        raise _Rejected(PolicyReason.NOT_206)
                    await self._write(chunk, segment.start + segment.done)
                    segment.done += len(chunk)
                    self._notify()
                    await self._save()
        except httpx.HTTPError as error:
            raise _Rejected(None) from error
        if not segment.complete:
            raise _Rejected(None)

    async def _run_workers(self) -> None:
        """Open one connection first, so a mistaken request stays a single request."""
        first, *rest = self.ranges
        validated = asyncio.Event()
        canary = asyncio.create_task(self._fetch(first, validated))
        passed = asyncio.create_task(validated.wait())
        self.tasks.update({canary})
        try:
            await asyncio.wait({canary, passed}, return_when=asyncio.FIRST_COMPLETED)
            passed.cancel()
            if canary.done():
                canary.result()
            others = [
                asyncio.create_task(self._fetch(segment, asyncio.Event())) for segment in rest
            ]
            self.tasks.update(others)
            await asyncio.gather(canary, *others)
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()

    async def accelerate(self) -> None:
        total = self.total
        if total is None:
            return
        host = urlsplit(self.job.url).hostname or ""
        if await self.engine.store.host_blocked(host):
            return
        position = max(self.offset, self.reserved)
        if total - position < self.engine.connections * CHUNK:
            return  # Nearly done: extra connections could not help.
        self.boundary, planned = plan_segments(position, total, self.engine.connections)
        self.ranges = [SegmentState(s, e, SegmentSource.RANGE) for s, e in planned]
        await self._save(force=True)
        try:
            await self._run_workers()
        except _Rejected as rejected:
            if rejected.reason is None:
                log.info("acceleration lost the network", extra={"fields": {"host": host}})
                self.network_lost = True
                self._notify()
                await self._save(force=True)
                return
            log.info("acceleration cancelled", extra={"fields": {"host": host}})
            await self.engine.store.block_host(host, rejected.reason)
            self._fall_back()
            await self._save(force=True)
            return
        self._notify()

    def verify_complete(self) -> None:
        total = self.total
        if total is not None and self.done < total:
            raise EngineError(f"the stream ended at {self.done} of {total} bytes")

    async def finish(self) -> None:
        await asyncio.to_thread(os.close, self.fd)
        self.fd = -1
        name = sanitize(self.job.filename)
        target = unique_path(self.directory, name)
        await asyncio.to_thread(self.part.rename, target)
        self.filename = target.name
        self.status = Status.COMPLETED
        await self._save(force=True)

    def suspend(self) -> None:
        """Shutdown, not failure: keep the partial file for crash recovery."""
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def discard(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        self.part.unlink(missing_ok=True)

    async def fill(self, segments: Sequence[SegmentState]) -> None:
        """Finish with Range requests only. The original stream is gone."""
        self.filling = True
        self.offset = 0
        self.ranges = [
            SegmentState(s.start, s.end, SegmentSource.RANGE, s.done)
            for s in segments
            if s.open_ended or s.size > 0
        ]
        if self.total is None and not any(s.open_ended for s in self.ranges):
            start = max((s.start + s.done for s in self.ranges), default=0)
            self.ranges.append(SegmentState(start, -1, SegmentSource.RANGE))
        elif not self.ranges and self.total is not None:
            self.ranges.append(SegmentState(0, self.total - 1, SegmentSource.RANGE))
        pending = [segment for segment in self.ranges if not segment.complete]
        tasks = [asyncio.create_task(self._fetch(s, asyncio.Event())) for s in pending]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class HybridEngine(Engine):
    name = EngineName.HYBRID

    def __init__(
        self,
        store: SegmentStore,
        *,
        client: httpx.AsyncClient | None = None,
        connections: int = 16,
        min_bytes: int = 20 * 1024 * 1024,
        limiter: Limiter | None = None,
        spool_max: int = DEFAULT_SPOOL_MAX_BYTES,
        stall_seconds: float = STALL_SECONDS,
        retry_delay: float = 1.0,
        range_retry_seconds: float = RANGE_RETRY_SECONDS,
    ) -> None:
        self.range_retry_seconds = range_retry_seconds
        self.store = store
        self.spool_max = spool_max
        self.stall_seconds = stall_seconds
        self.retry_delay = retry_delay
        self.client = client or httpx.AsyncClient(follow_redirects=False, timeout=30)
        self.connections = connections
        self.min_bytes = min_bytes
        self.limiter = limiter or Limiter()
        self.transfers: dict[str, _Transfer] = {}

    async def probe(self, url: str, headers: dict[str, str]) -> ProbeResult:
        raise EngineError("captured downloads are never probed")

    async def add(self, job: Job) -> str:
        raise EngineError("HybridEngine only takes attached streams")

    async def attach(self, job: Job, stream: AsyncIterator[bytes]) -> str:
        """Consume the stream until the download ends. The caller keeps it open until then."""
        transfer = _Transfer(self, job)
        self.transfers[job.id] = transfer
        accelerator: asyncio.Task[None] | None = None
        if may_accelerate(job.stream, job.url, self.min_bytes, self.connections):
            accelerator = asyncio.create_task(transfer.accelerate())
        try:
            await transfer.pump(stream)
            if accelerator is not None:
                await accelerator
            transfer.verify_complete()
            await transfer.finish()
        except asyncio.CancelledError:
            await self._stop(accelerator)
            transfer.suspend()
            raise
        except _Canceled:
            await self._stop(accelerator)
            transfer.discard()
        except (EngineError, OSError, httpx.HTTPError):
            await self._stop(accelerator)
            segments = transfer.segments()
            transfer.suspend()
            await self.continue_download(job, segments)
        finally:
            transfer.finished.set()
        return job.id

    async def continue_download(
        self, job: Job, segments: Sequence[SegmentState], *, respect_policy: bool = True
    ) -> str:
        """Finish a capture whose stream is gone, from the same offset.

        Allowed only for a Range-capable host with no recorded refusal. Anything else waits
        for a new link in needs_refresh. respect_policy is off for a link the user supplied.
        """
        transfer = _Transfer(self, job)
        self.transfers[job.id] = transfer
        host = urlsplit(job.url).hostname or ""
        allowed = may_resume(job.stream) and not await self.store.host_blocked(host)
        if respect_policy and not allowed:
            await self._needs_refresh(transfer, list(segments), NEEDS_REFRESH)
            return job.id
        try:
            await transfer.fill(segments)
            await transfer.finish()
        except _Rejected as rejected:
            if rejected.reason is None:
                message = NETWORK_LOST
            elif respect_policy:
                await self.store.block_host(host, rejected.reason)
                message = NEEDS_REFRESH
            else:
                message = CANNOT_CONTINUE
            await self._needs_refresh(transfer, transfer.ranges, message)
        except _Canceled:
            transfer.discard()
        except asyncio.CancelledError:
            transfer.suspend()
            raise
        except OSError as error:
            transfer.suspend()
            transfer.status = Status.FAILED
            transfer.error = str(error)
        finally:
            transfer.finished.set()
        return job.id

    async def _needs_refresh(
        self, transfer: _Transfer, segments: Sequence[SegmentState], message: str
    ) -> None:
        transfer.offset = 0
        transfer.boundary = None
        transfer.ranges = [s for s in segments if not s.open_ended or s.done]
        transfer.suspend()
        transfer.status = Status.NEEDS_REFRESH
        transfer.error = message
        await self.store.save_segments(transfer.job.id, transfer.ranges)

    async def _stop(self, accelerator: asyncio.Task[None] | None) -> None:
        if accelerator is not None:
            accelerator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await accelerator

    def _transfer(self, engine_id: str) -> _Transfer:
        transfer = self.transfers.get(engine_id)
        if transfer is None:
            raise EngineError("unknown download")
        return transfer

    async def pause(self, engine_id: str) -> None:
        """Stop reading. Back-pressure holds the stream open, so nothing is requested again."""
        transfer = self._transfer(engine_id)
        transfer.running.clear()
        transfer.status = Status.PAUSED

    async def resume(self, engine_id: str) -> None:
        transfer = self._transfer(engine_id)
        transfer.status = Status.ACTIVE
        transfer.running.set()

    async def cancel(self, engine_id: str) -> None:
        transfer = self._transfer(engine_id)
        transfer.status = Status.CANCELED
        transfer.canceled = True
        transfer.running.set()
        transfer.changed.set()

    async def set_speed_limit(self, engine_id: str, limit: int) -> None:
        self._transfer(engine_id).limiter.set_rate(limit)

    async def refresh_url(self, engine_id: str, new_url: str) -> None:
        self._transfer(engine_id).job.url = new_url

    async def status(self, engine_id: str) -> Progress:
        transfer = self._transfer(engine_id)
        now = asyncio.get_running_loop().time()
        elapsed = now - transfer.last_seen
        speed = int((transfer.done - transfer.last_done) / elapsed) if transfer.last_seen else 0
        transfer.last_seen, transfer.last_done = now, transfer.done
        return Progress(
            transfer.status,
            transfer.total or 0,
            transfer.done,
            max(0, speed),
            transfer.connections,
            transfer.filename,
            transfer.error,
        )
