import re

import pytest

from daemon.capture.detect import (
    DEFAULT_BYPASS,
    Request,
    bypass_pattern,
    bypassed,
    ignore_hosts,
    is_download,
    is_interceptable,
    is_media,
    is_media_manifest,
)


def nav(url: str = "https://a.test/f", mode: str | None = "navigate") -> Request:
    return Request("GET", url, mode)


@pytest.mark.parametrize(
    ("request_", "headers", "expected"),
    [
        (nav(), {"content-disposition": "attachment; filename=a.txt"}, True),
        (nav(), {"content-disposition": " Attachment"}, True),
        (nav(), {"content-disposition": 'inline; filename="a.pdf"'}, False),
        (nav("https://a.test/x.iso"), {"content-type": "text/html"}, True),
        (nav("https://a.test/x%2Eiso"), {}, True),
        (nav(), {"content-type": "application/pdf", "content-length": "3000000"}, True),
        (nav(), {"content-type": "application/pdf", "content-length": "100"}, False),
        (nav(), {"content-type": "video/mp4", "content-length": "99999999"}, True),
        (nav(), {"content-type": "image/png", "content-length": "99999999"}, True),
        (nav(), {"content-type": "image/png", "content-length": "1000"}, False),
        (nav(), {"content-type": "text/plain", "content-length": "99999999"}, False),
        (nav(), {"content-type": "font/woff2", "content-length": "99999999"}, False),
        (nav(), {"content-type": "application/json", "content-length": "99999999"}, False),
        (
            nav(),
            {"content-type": "application/vnd.apple.mpegurl", "content-length": "9999999"},
            False,
        ),
        (nav(), {"content-type": "application/octet-stream"}, False),  # No length: unknown size.
        (nav(), {"content-type": "application/octet-stream", "content-length": "x"}, False),
        (nav(), {}, False),
    ],
)
def test_is_download(request_: Request, headers: dict[str, str], expected: bool) -> None:
    assert is_download(request_, headers) is expected


def test_threshold_is_configurable() -> None:
    headers = {"content-type": "application/pdf", "content-length": "500"}
    assert is_download(nav(), headers, threshold=100)


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        (nav(), True),
        (nav(mode="cors"), False),
        (nav(mode="no-cors"), False),
        (Request("GET", "http://a.test/x", None, accept="text/html,*/*"), True),
        (Request("GET", "http://a.test/x", None, accept="*/*"), False),
        (Request("GET", "https://a.test/x", None, accept="text/html"), False),
    ],
)
def test_only_navigations_are_interceptable(request_: Request, expected: bool) -> None:
    assert is_interceptable(request_) is expected


def test_media() -> None:
    assert is_media_manifest("https://v.test/master.m3u8?x=1", {})
    assert is_media_manifest("https://v.test/x", {"content-type": "application/dash+xml"})
    assert not is_media_manifest("https://v.test/x.mp4", {"content-type": "video/mp4"})
    assert is_media({"content-type": "video/webm"})
    assert not is_media({"content-type": "video/mp2t"})


@pytest.mark.parametrize(
    ("entry", "host", "expected"),
    [
        ("example.com", "example.com:443", True),
        ("example.com", "www.example.com:443", True),
        ("example.com", "notexample.com:443", False),
        ("example.com", "example.com.evil.test:443", False),
        ("*bank*", "online.mybank.test:443", True),
        ("*.internal", "git.internal:8443", True),
        ("*.internal", "internal:8443", False),
    ],
)
def test_bypass_patterns_match_mitmproxy_hosts(entry: str, host: str, expected: bool) -> None:
    pattern = bypass_pattern(entry)
    assert pattern is not None
    assert bool(re.match(pattern, host)) is expected


def test_path_entries_are_checked_per_request() -> None:
    assert bypass_pattern("github.com/login") is None
    assert bypassed("https://github.com/login/oauth", ["github.com/login"])
    assert bypassed("https://github.com/login", ["github.com/login/"])
    assert not bypassed("https://github.com/loginx", ["github.com/login"])
    assert not bypassed("https://github.com/owner/repo", ["github.com/login"])
    assert bypassed("https://www.paypal.com/x", DEFAULT_BYPASS)
    assert not bypassed("https://example.com/x", DEFAULT_BYPASS)


def test_default_bypass_compiles() -> None:
    patterns = ignore_hosts([*DEFAULT_BYPASS, "", "  "])
    assert len(patterns) == len(DEFAULT_BYPASS) - 1  # The path entry is per request.
    for pattern in patterns:
        re.compile(pattern)
