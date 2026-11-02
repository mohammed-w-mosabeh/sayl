"""YtDlpEngine: yt-dlp as a library, on a worker thread so it never blocks the event loop."""

import asyncio
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

import yt_dlp

from daemon.engines.base import Engine, EngineError, Job, ProbeResult, Progress
from daemon.models import EngineName, Status


class Downloader(Protocol):
    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]: ...

    def __enter__(self) -> "Downloader": ...

    def __exit__(self, *args: object) -> None: ...


Factory = Callable[[dict[str, Any]], Downloader]


class _Stop(Exception):
    """Raised from a progress hook, the only way to interrupt yt-dlp from outside."""


@dataclass
class _Run:
    job: Job
    status: Status = Status.ACTIVE
    total: int = 0
    done: int = 0
    speed: int = 0
    filename: str = ""
    error: str = ""
    parts: dict[str, tuple[int, int]] = field(default_factory=dict)
    stop: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None


def supported_extractor(url: str) -> str:
    """Return the name of the yt-dlp extractor for this URL, ignoring the generic one."""
    for extractor in yt_dlp.extractor.gen_extractor_classes():
        if extractor.IE_NAME != "generic" and extractor.suitable(url):
            return str(extractor.IE_NAME)
    return ""


def js_runtimes(which: Callable[[str], str | None] = shutil.which) -> dict[str, dict[str, str]]:
    """yt-dlp only enables deno by itself, so name whichever runtime `sayl doctor` found."""
    return {name: {} for name in ("deno", "node", "bun", "quickjs") if which(name)}


def media_options(job: Job) -> dict[str, Any]:
    """Map a job to yt-dlp's audio, subtitle, playlist, and cookie options."""
    options: dict[str, Any] = {"merge_output_format": "mp4", "js_runtimes": js_runtimes()}
    processors: list[dict[str, Any]] = []
    if job.audio_only:
        options["format"] = job.format_id or "bestaudio/best"
        del options["merge_output_format"]
        processors.append({"key": "FFmpegExtractAudio"})
    if job.subtitles:
        options["writesubtitles"] = True
        options["subtitleslangs"] = list(job.subtitles)
        processors.append({"key": "FFmpegEmbedSubtitle"})
    if job.playlist_items:
        options["playlist_items"] = job.playlist_items
    if job.cookies_file:
        options["cookiefile"] = job.cookies_file
    if job.cookies_from_browser:
        options["cookiesfrombrowser"] = (job.cookies_from_browser,)
    if processors:
        options["postprocessors"] = processors
    return options


def _default_factory(options: dict[str, Any]) -> Downloader:
    return cast(Downloader, yt_dlp.YoutubeDL(cast(Any, options)))


class YtDlpEngine(Engine):
    name = EngineName.YTDLP

    def __init__(self, factory: Factory = _default_factory) -> None:
        self.factory = factory
        self.runs: dict[str, _Run] = {}

    async def probe(self, url: str, headers: dict[str, str]) -> ProbeResult:
        def extract() -> dict[str, Any]:
            options = {"quiet": True, "http_headers": headers, "js_runtimes": js_runtimes()}
            with self.factory(options) as downloader:
                return downloader.extract_info(url, download=False)

        info = await asyncio.to_thread(extract)
        return ProbeResult(
            url,
            str(info.get("title", "")),
            info.get("filesize_approx"),
            True,
            str(info.get("extractor", "")),
            tuple(info.get("formats") or ()),
        )

    def _options(self, run: _Run) -> dict[str, Any]:
        job = run.job
        options: dict[str, Any] = {
            "quiet": True,
            "noprogress": True,
            "continuedl": True,
            "paths": {"home": job.dest_dir},
            "progress_hooks": [lambda update: self._on_progress(run, update)],
            "postprocessor_hooks": [lambda update: self._on_postprocess(run, update)],
            "http_headers": job.headers,
        }
        if job.format_id:
            options["format"] = job.format_id
        if job.speed_limit:
            options["ratelimit"] = job.speed_limit
        return {**options, **media_options(job)}

    def _on_progress(self, run: _Run, update: dict[str, Any]) -> None:
        if run.stop.is_set():
            raise _Stop
        name = Path(str(update.get("filename") or "")).name
        total = update.get("total_bytes") or update.get("total_bytes_estimate") or 0
        run.parts[name] = (int(update.get("downloaded_bytes") or 0), int(total))
        run.done = sum(done for done, _ in run.parts.values())  # Video and audio are separate.
        run.total = sum(size for _, size in run.parts.values())
        run.speed = int(update.get("speed") or 0)
        run.filename = name or run.filename

    def _on_postprocess(self, run: _Run, update: dict[str, Any]) -> None:
        """The merged file is the real result, not the last stream downloaded."""
        if run.stop.is_set():
            raise _Stop
        path = update.get("info_dict", {}).get("filepath")
        if update.get("status") == "finished" and path:
            run.filename = Path(str(path)).name

    def _work(self, run: _Run) -> None:
        with self.factory(self._options(run)) as downloader:
            downloader.extract_info(run.job.url, download=True)

    async def _run(self, run: _Run) -> None:
        try:
            await asyncio.to_thread(self._work, run)
        except _Stop:
            return  # pause() or cancel() already set the status.
        except yt_dlp.utils.DownloadError as error:
            if run.stop.is_set():
                return
            run.status = Status.FAILED
            run.error = str(error)
        else:
            run.status = Status.COMPLETED

    def _start(self, run: _Run) -> None:
        run.stop.clear()
        run.status = Status.ACTIVE
        run.task = asyncio.create_task(self._run(run))

    async def add(self, job: Job) -> str:
        engine_id = uuid.uuid4().hex
        run = _Run(job)
        self.runs[engine_id] = run
        self._start(run)
        return engine_id

    def _get(self, engine_id: str) -> _Run:
        run = self.runs.get(engine_id)
        if run is None:
            raise EngineError("unknown download")
        return run

    async def _halt(self, run: _Run, status: Status) -> None:
        run.status = status
        run.stop.set()
        if run.task is not None:
            await run.task

    async def pause(self, engine_id: str) -> None:
        await self._halt(self._get(engine_id), Status.PAUSED)

    async def resume(self, engine_id: str) -> None:
        self._start(self._get(engine_id))

    async def cancel(self, engine_id: str) -> None:
        await self._halt(self._get(engine_id), Status.CANCELED)

    async def set_speed_limit(self, engine_id: str, limit: int) -> None:
        """yt-dlp reads its options once, so a running download restarts with the new limit."""
        run = self._get(engine_id)
        run.job.speed_limit = limit
        if run.status is Status.ACTIVE:
            await self._halt(run, Status.PAUSED)
            self._start(run)

    async def refresh_url(self, engine_id: str, new_url: str) -> None:
        self._get(engine_id).job.url = new_url

    async def status(self, engine_id: str) -> Progress:
        run = self._get(engine_id)
        return Progress(run.status, run.total, run.done, run.speed, 1, run.filename, run.error)
