import asyncio
import hashlib
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yt_dlp

from daemon.engines.aria2 import Aria2Engine, command_line, file_allocation
from daemon.engines.base import AttachNotSupportedError, EngineError, Job
from daemon.engines.ytdlp import YtDlpEngine, js_runtimes, media_options, supported_extractor
from daemon.models import EngineName, Status
from daemon.router import route
from tests.origin import SHA256, SIZE, Origin


@pytest.fixture
async def origin() -> AsyncIterator[Origin]:
    server = Origin()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def aria2(tmp_path: Path) -> AsyncIterator[Aria2Engine]:
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is not installed")
    engine = Aria2Engine(tmp_path)
    await engine.start()
    yield engine
    await engine.stop()


async def until_done(engine: Aria2Engine, gid: str) -> Status:
    for _ in range(200):
        status = (await engine.status(gid)).status
        if status in (Status.COMPLETED, Status.FAILED):
            return status
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_aria2_downloads_with_a_correct_hash(
    aria2: Aria2Engine, origin: Origin, tmp_path: Path
) -> None:
    job = Job("d", origin.url("/normal.bin"), str(tmp_path / "out"), "../evil.bin")
    gid = await aria2.add(job)
    assert await until_done(aria2, gid) is Status.COMPLETED
    file = tmp_path / "out/evil.bin"
    assert hashlib.sha256(file.read_bytes()).hexdigest() == SHA256


async def test_aria2_probe(aria2: Aria2Engine, origin: Origin) -> None:
    result = await aria2.probe(origin.url("/normal.bin"), {})
    assert (result.total, result.resumable, result.filename) == (SIZE, True, "normal.bin")


async def test_aria2_rpc_errors_are_engine_errors(aria2: Aria2Engine) -> None:
    with pytest.raises(EngineError):
        await aria2.status("0000000000000000")


async def test_aria2_pause_limit_refresh_cancel(
    aria2: Aria2Engine, origin: Origin, tmp_path: Path
) -> None:
    job = Job("d", origin.url("/normal.bin"), str(tmp_path), "a.bin", speed_limit=1024)
    gid = await aria2.add(job)
    await aria2.set_speed_limit(gid, 2048)
    await aria2.set_global_speed_limit(0)
    await aria2.pause(gid)
    assert (await aria2.status(gid)).status is Status.PAUSED
    await aria2.refresh_url(gid, origin.url("/moved.bin"))
    await aria2.resume(gid)
    await aria2.cancel(gid)
    assert (await aria2.status(gid)).status is Status.CANCELED


async def test_aria2_restarts_after_it_dies(tmp_path: Path) -> None:
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is not installed")
    restarted = asyncio.Event()

    async def on_restart() -> None:
        restarted.set()

    engine = Aria2Engine(tmp_path, on_restart=on_restart)
    await engine.start()
    assert engine.process is not None
    engine.process.kill()
    await asyncio.wait_for(restarted.wait(), 10)
    assert await engine.rpc("aria2.getGlobalStat")
    await engine.stop()


async def test_engines_refuse_attach(tmp_path: Path) -> None:
    async def nothing() -> AsyncIterator[bytes]:
        yield b""

    with pytest.raises(AttachNotSupportedError):
        await Aria2Engine(tmp_path).attach(Job("d", "u", "."), nothing())


def test_missing_binary_is_reported(tmp_path: Path) -> None:
    engine = Aria2Engine(tmp_path)
    engine.binary = None
    with pytest.raises(EngineError, match="doctor"):
        asyncio.run(engine.start())


def test_command_line_is_loopback_and_resumes_a_session(tmp_path: Path) -> None:
    arguments = command_line("aria2c", 1, "s", tmp_path, "none")
    assert "--rpc-listen-all=false" in arguments
    assert not any(a.startswith("--input-file") for a in arguments)
    (tmp_path / "aria2.session").touch()
    assert f"--input-file={tmp_path / 'aria2.session'}" in command_line(
        "aria2c", 1, "s", tmp_path, "none"
    )


def test_file_allocation_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object) -> None:
        raise OSError

    assert file_allocation(tmp_path) == "falloc"
    monkeypatch.setattr("os.posix_fallocate", refuse)
    assert file_allocation(tmp_path) == "none"


class FakeDownloader:
    """Stands in for YoutubeDL: no network, and it drives the real progress hooks."""

    gate: asyncio.Event | None = None
    steps = 3
    error: Exception | None = None
    warnings: tuple[str, ...] = ()
    result: ClassVar[dict[str, Any]] = {}

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def __enter__(self) -> "FakeDownloader":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        if not download:
            return {"title": "T", "extractor": "fake", "formats": [{"format_id": "137"}]}
        if self.error:
            raise self.error
        for index in range(self.steps):
            for hook in self.options["progress_hooks"]:
                hook({"downloaded_bytes": index + 1, "total_bytes": 3, "filename": "/d/v.f1.mp4"})
        for hook in self.options["progress_hooks"]:
            hook({"downloaded_bytes": 2, "total_bytes": 2, "filename": "/d/v.f2.m4a"})
        for hook in self.options["postprocessor_hooks"]:
            hook({"status": "started", "info_dict": {}})
            hook({"status": "finished", "info_dict": {"filepath": "/d/v.mp4"}})
        for message in self.warnings:
            self.options["logger"].warning(message)
        return self.result


async def settle(engine: YtDlpEngine, engine_id: str) -> Status:
    run = engine.runs[engine_id]
    assert run.task is not None
    await run.task
    return (await engine.status(engine_id)).status


async def test_ytdlp_probe_and_download() -> None:
    engine = YtDlpEngine(FakeDownloader)
    probe = await engine.probe("http://v", {})
    assert (probe.filename, probe.extractor) == ("T", "fake")
    engine_id = await engine.add(Job("d", "http://v", ".", format_id="137", speed_limit=5))
    assert await settle(engine, engine_id) is Status.COMPLETED
    progress = await engine.status(engine_id)
    assert (progress.done, progress.total, progress.filename) == (5, 5, "v.mp4")


async def test_ytdlp_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeDownloader, "error", yt_dlp.utils.DownloadError("no such video"))
    engine = YtDlpEngine(FakeDownloader)
    engine_id = await engine.add(Job("d", "http://v", "."))
    assert await settle(engine, engine_id) is Status.FAILED
    assert "no such video" in (await engine.status(engine_id)).error


async def test_ytdlp_pause_resume_and_cancel() -> None:
    engine = YtDlpEngine(FakeDownloader)
    engine_id = await engine.add(Job("d", "http://v", "."))
    run = engine.runs[engine_id]
    run.stop.set()  # The first hook call stops the run, as a real pause does mid-download.
    await engine.pause(engine_id)
    assert (await engine.status(engine_id)).status is Status.PAUSED
    await engine.resume(engine_id)
    assert await settle(engine, engine_id) is Status.COMPLETED
    await engine.set_speed_limit(engine_id, 10)
    await engine.refresh_url(engine_id, "http://w")
    await engine.cancel(engine_id)
    assert (await engine.status(engine_id)).status is Status.CANCELED


async def test_ytdlp_unknown_id() -> None:
    with pytest.raises(EngineError):
        await YtDlpEngine(FakeDownloader).status("x")


def test_real_extractor_matching() -> None:
    assert supported_extractor("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    assert supported_extractor("https://example.com/file.zip") == ""


@pytest.mark.parametrize(
    ("url", "engine"),
    [
        ("magnet:?xt=urn:btih:abc", EngineName.ARIA2),
        ("https://a.io/x.torrent", EngineName.ARIA2),
        ("https://a.io/file.zip", EngineName.ARIA2),
        ("https://www.youtube.com/watch?v=aqz-KE-bpKQ", EngineName.YTDLP),
    ],
)
def test_route(url: str, engine: EngineName) -> None:
    assert route(url) is engine


def test_media_options_map_to_ytdlp() -> None:
    assert media_options(Job("d", "u", "."))["merge_output_format"] == "mp4"
    assert js_runtimes(lambda name: name if name == "node" else None) == {"node": {}}
    audio = media_options(Job("d", "u", ".", audio_only=True))
    assert audio["format"] == "bestaudio/best"
    assert audio["postprocessors"] == [{"key": "FFmpegExtractAudio"}]
    full = media_options(
        Job(
            "d",
            "u",
            ".",
            subtitles=("ar", "en"),
            playlist_items="1,3,5-7",
            cookies_file="/c.txt",
            cookies_from_browser="firefox",
        )
    )
    assert full["subtitleslangs"] == ["ar", "en"]
    assert full["postprocessors"] == [{"key": "FFmpegEmbedSubtitle"}]
    assert full["playlist_items"] == "1,3,5-7"
    assert full["cookiefile"] == "/c.txt"
    assert full["cookiesfrombrowser"] == ("firefox",)
