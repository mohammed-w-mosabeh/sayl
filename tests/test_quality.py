import asyncio
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import websockets

from daemon.engines.base import Job
from daemon.engines.quality import (
    Media,
    assess,
    codec_family,
    compare,
    delivered_media,
    is_solver_failure,
    probe_file,
    requested_media,
)
from daemon.engines.ytdlp import YtDlpEngine
from daemon.models import EngineName, Status
from tests.stack import Stack, running_stack
from tests.test_engines import FakeDownloader

FORMATS = [
    {"format_id": "137", "height": 1080, "vcodec": "avc1.640028", "acodec": "none"},
    {"format_id": "399", "height": 1080, "vcodec": "av01.0.08M.08", "acodec": "none"},
    {"format_id": "136", "height": 720, "vcodec": "avc1.4d401f", "acodec": "none"},
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2"},
]
SOLVER_WARNING = "[youtube] abc: n challenge solving failed: Some formats may be missing."


def job(selector: str = "", **fields: Any) -> Job:
    return Job("d", "http://v", ".", format_id=selector, **fields)


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("avc1.640028", "avc1"),
        ("h264", "avc1"),
        ("av01.0.08M.08", "av1"),
        ("vp09.00.30", "vp9"),
        ("mp4a.40.2", "aac"),
        ("none", ""),
        (None, ""),
        ("theora", "theora"),
    ],
)
def test_codec_family(name: str | None, family: str) -> None:
    assert codec_family(name) == family


def test_explicit_ids_are_compared_strictly() -> None:
    wanted = requested_media(job("137+140"), {"formats": FORMATS})
    assert wanted == Media(1080, "avc1", "aac", True)
    assert compare(wanted, Media(1080, "avc1", "aac", True)) == []
    assert compare(wanted, Media(720, "av1", "opus", False)) == [
        "height 720 is below 1080",
        "video codec av1 is not avc1",
        "audio codec opus is not aac",
        "audio is missing",
    ]


def test_a_cap_is_not_a_loss_when_the_source_is_smaller() -> None:
    small = {"formats": [f for f in FORMATS if f.get("height") != 1080]}
    assert requested_media(job("bv*[height<=1080]+ba"), small).height == 720
    assert requested_media(job("bv*[height<=1080]+ba"), {"formats": FORMATS}).height == 1080


def test_an_exact_height_is_held_to_it() -> None:
    small = {"formats": [f for f in FORMATS if f.get("height") != 1080]}
    assert requested_media(job("bv*[height=1080]+ba"), small).height == 1080


def test_default_selector_wants_the_best_available() -> None:
    assert requested_media(job(), {"formats": FORMATS}) == Media(1080, has_audio=True)


@pytest.mark.parametrize("selector", ["worst", "wv*+wa/w", "bv*[height<=720]"])
def test_selectors_with_nothing_to_lose(selector: str) -> None:
    wanted = requested_media(job(selector), {"formats": FORMATS})
    assert wanted.vcodec == ""
    if selector == "bv*[height<=720]":
        assert wanted == Media()


def test_audio_only_requires_audio() -> None:
    wanted = requested_media(job(audio_only=True), {"formats": FORMATS})
    assert compare(wanted, Media(acodec="aac", has_audio=True)) == []
    assert compare(wanted, Media(has_audio=False)) == ["audio is missing"]


def test_assess_is_silent_when_delivery_matches() -> None:
    info = {"formats": FORMATS, "height": 1080, "vcodec": "avc1", "acodec": "mp4a.40.2"}
    assert assess(job("137+140"), info, []) == {}


def test_assess_reports_requested_and_delivered() -> None:
    info = {
        "title": "T",
        "formats": FORMATS,
        "height": 720,
        "vcodec": "avc1",
        "acodec": "none",
    }
    found = assess(job("137+140"), info, [])
    item = found["items"][0]
    assert item["title"] == "T"
    assert item["requested"]["height"] == 1080
    assert item["delivered"]["height"] == 720
    assert "audio is missing" in item["reasons"]


def test_assess_checks_every_playlist_entry() -> None:
    good = {"formats": FORMATS, "height": 1080, "acodec": "aac"}
    bad = {"title": "bad", "formats": FORMATS, "height": 360, "acodec": "aac"}
    found = assess(job(), {"entries": [good, None, bad]}, [])
    assert [item["title"] for item in found["items"]] == ["bad"]


def test_a_solver_failure_warns_even_when_the_quality_looks_fine() -> None:
    info = {"formats": FORMATS, "height": 1080, "acodec": "aac"}
    found = assess(job(), info, [SOLVER_WARNING, SOLVER_WARNING])
    assert found == {"items": [], "solver_failures": [SOLVER_WARNING]}


@pytest.mark.parametrize(
    "message",
    [
        SOLVER_WARNING,
        "[youtube] x: Signature solving failed: Some formats may be missing.",
        "[youtube] [jsc] Remote component challenge solver script (node) was skipped.",
        "[youtube] No supported JavaScript runtime could be found.",
    ],
)
def test_solver_failures_are_recognised(message: str) -> None:
    assert is_solver_failure(message)


def test_ordinary_warnings_are_not_solver_failures() -> None:
    assert not is_solver_failure("[youtube:tab] YouTube said: INFO - 1 unavailable video is hidden")


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed")
    path = tmp_path / "clip.mp4"
    subprocess.run(  # noqa: S603 - argument list, path from shutil.which
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=5:duration=1",
            "-c:v",
            "libx264",
            str(path),
        ],
        check=True,
    )
    return path


def test_the_real_file_outranks_the_metadata(clip: Path) -> None:
    assert probe_file(clip) == Media(240, "avc1", "", False)
    claimed = {
        "height": 1080,
        "acodec": "mp4a.40.2",
        "requested_downloads": [{"filepath": str(clip)}],
    }
    delivered = delivered_media(claimed)
    assert (delivered.height, delivered.has_audio) == (240, False)
    found = assess(job("137+140"), {**claimed, "formats": FORMATS}, [])
    assert "height 240 is below 1080" in found["items"][0]["reasons"]


def test_metadata_is_the_fallback_when_there_is_no_file(tmp_path: Path) -> None:
    assert probe_file(tmp_path / "missing.mp4") is None
    delivered = delivered_media({"height": 480, "filepath": str(tmp_path / "missing.mp4")})
    assert delivered.height == 480


class Degraded(FakeDownloader):
    result: ClassVar[dict[str, Any]] = {
        "title": "T",
        "formats": FORMATS,
        "height": 480,
        "vcodec": "avc1",
        "acodec": "aac",
    }
    warnings = (SOLVER_WARNING,)


class Fine(FakeDownloader):
    result: ClassVar[dict[str, Any]] = {
        "formats": FORMATS,
        "height": 1080,
        "vcodec": "avc1",
        "acodec": "aac",
    }
    warnings = ("[youtube:tab] YouTube said: INFO - 1 unavailable video is hidden",)


async def finish(engine: YtDlpEngine, selector: str) -> Any:
    engine_id = await engine.add(job(selector))
    task = engine.runs[engine_id].task
    assert task is not None
    await task
    return await engine.status(engine_id)


async def test_engine_completes_with_a_warning() -> None:
    status = await finish(YtDlpEngine(Degraded), "137+140")
    assert status.status is Status.COMPLETED
    warning = status.warnings["quality_degraded"]
    assert warning["solver_failures"] == [SOLVER_WARNING]
    assert warning["items"][0]["delivered"]["height"] == 480


async def test_engine_has_no_warning_when_nothing_was_lost() -> None:
    status = await finish(YtDlpEngine(Fine), "137+140")
    assert status.warnings == {}


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path) as running:
        yield running


async def test_warning_reaches_the_api_and_the_websocket(stack: Stack) -> None:
    engine = stack.daemon.services.engines[EngineName.YTDLP]
    assert isinstance(engine, YtDlpEngine)
    engine.factory = Degraded
    url = f"ws://127.0.0.1:{stack.port}/ws"
    subprotocol = f"bearer.{stack.daemon.services.token}"
    async with websockets.connect(url, subprotocols=[subprotocol]) as socket:  # type: ignore[list-item]
        created = await stack.client.post(
            "/downloads",
            json={"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "format_id": "137+140"},
        )
        while True:
            event = json.loads(await asyncio.wait_for(socket.recv(), 60))
            if event["event"] == "download.quality_degraded":
                break
    assert event["warning"]["items"][0]["requested"]["height"] == 1080
    row = (await stack.client.get(f"/downloads/{created.json()['id']}")).json()
    assert row["status"] == "completed"
    assert row["warnings"]["quality_degraded"]["solver_failures"] == [SOLVER_WARNING]
