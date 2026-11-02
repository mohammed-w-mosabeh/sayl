"""Run with: uv run --group poc pytest --no-cov poc/test_detect.py"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import detect
from detect import Request, Strategy

NAV = "navigate"


def req(url: str = "https://a.test/f", mode: str | None = NAV, method: str = "GET") -> Request:
    return Request(method, url, mode)


@pytest.mark.parametrize(
    ("request_", "headers", "expected"),
    [
        (req(), {"content-disposition": "attachment; filename=a.txt"}, True),
        (req(), {"content-disposition": 'inline; filename="a.pdf"'}, False),
        (req("https://a.test/x.iso"), {"content-type": "text/html"}, True),
        (req(), {"content-type": "application/pdf", "content-length": "3000000"}, True),
        (req(), {"content-type": "application/pdf", "content-length": "100"}, False),
        (req(), {"content-type": "text/plain", "content-length": "99999999"}, False),
        (req(), {"content-type": "image/png", "content-length": "99999999"}, False),
        (req(), {"content-type": "video/mp4", "content-length": "99999999"}, False),
        (req(), {"content-type": "application/octet-stream"}, False),
        (req(), {}, False),
    ],
)
def test_is_download(request_: Request, headers: dict[str, str], expected: bool) -> None:
    assert detect.is_download(request_, headers) is expected


def test_only_navigations_are_interceptable() -> None:
    assert detect.is_interceptable(req(mode=NAV))
    html = "text/html,application/xhtml+xml,*/*;q=0.8"
    assert detect.is_interceptable(Request("GET", "http://a.test/f", None, html))
    assert not detect.is_interceptable(Request("GET", "https://a.test/f", None, html))
    assert not detect.is_interceptable(Request("GET", "http://a.test/f", None, "*/*"))
    assert not detect.is_interceptable(req(mode="cors"))
    assert not detect.is_interceptable(req(mode="no-cors"))


@pytest.mark.parametrize(
    ("request_", "headers", "expected"),
    [
        (req(), {"accept-ranges": "bytes"}, Strategy.RE_REQUEST),
        (req(), {}, Strategy.STREAM_THROUGH),
        (req(method="POST"), {"accept-ranges": "bytes"}, Strategy.STREAM_THROUGH),
        (req("https://a.test/f?sig=x"), {"accept-ranges": "bytes"}, Strategy.STREAM_THROUGH),
        (req("https://a.test/f?v=2"), {"accept-ranges": "bytes"}, Strategy.RE_REQUEST),
    ],
)
def test_strategy(request_: Request, headers: dict[str, str], expected: Strategy) -> None:
    assert detect.choose_strategy(request_, headers) is expected


@pytest.mark.parametrize(
    ("url", "headers", "expected"),
    [
        ("https://a.test/dir/a.zip", {}, "a.zip"),
        ("https://a.test/", {}, "download.bin"),
        (
            "https://a.test/f",
            {"content-disposition": 'attachment; filename="../../.bashrc"'},
            "bashrc",
        ),
        (
            "https://a.test/f",
            {"content-disposition": "attachment; filename=..\\..\\x.exe"},
            "x.exe",
        ),
        (
            "https://a.test/f",
            {"content-disposition": "attachment; filename*=UTF-8''a%20b.zip"},
            "a b.zip",
        ),
    ],
)
def test_filename(url: str, headers: dict[str, str], expected: str) -> None:
    assert detect.filename_for(url, headers) == expected


def test_filename_is_capped() -> None:
    assert len(detect.filename_for("https://a.test/" + "x" * 400, {}).encode()) <= 255


def test_manifests() -> None:
    assert detect.is_media_manifest("https://a.test/v/master.m3u8?x=1", {})
    assert detect.is_media_manifest("https://a.test/m", {"content-type": "application/dash+xml"})
    assert not detect.is_media_manifest("https://a.test/m.mp4", {"content-type": "video/mp4"})
