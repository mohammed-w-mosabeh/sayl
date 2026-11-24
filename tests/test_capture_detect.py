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
    assert bypassed("https://api.anthropic.com/v1/messages", DEFAULT_BYPASS)
    assert not bypassed("https://example.com/x", DEFAULT_BYPASS)


def test_default_bypass_is_only_anthropic_and_claude() -> None:
    """Every other host must stay capturable: more passthrough is opt-in."""
    assert DEFAULT_BYPASS == ("anthropic.com", "claude.ai")
    patterns = ignore_hosts([*DEFAULT_BYPASS, "", "  "])
    assert len(patterns) == len(DEFAULT_BYPASS)
    for pattern in patterns:
        re.compile(pattern)


PASSED_THROUGH = [
    "api.anthropic.com", "anthropic.com", "console.anthropic.com", "mcp-proxy.anthropic.com",
    "claude.ai", "downloads.claude.ai", "API.ANTHROPIC.COM",
]  # fmt: skip
STILL_INTERCEPTED = [
    # Download sites Sayl must keep capturing: never passed through by default.
    "github.com", "objects.githubusercontent.com", "raw.githubusercontent.com", "ghcr.io",
    "registry.npmjs.org", "npmjs.org", "pypi.org", "files.pythonhosted.org", "docker.io",
    "download.docker.com", "dl.google.com", "download.mozilla.org", "deb.debian.org",
    "www.youtube.com", "rr1---sn-25auxa-b156.googlevideo.com", "example.com",
    # Look-alikes of the default entries.
    "anthropic.com.evil.test", "notanthropic.com", "anthropic.co", "claude.ai.evil.test",
    "evilclaude.ai", "claude.aix", "anthropic-com.test",
]  # fmt: skip


@pytest.mark.parametrize("host", PASSED_THROUGH)
def test_default_passthrough_hosts(host: str) -> None:
    assert any(re.match(p, f"{host}:443", re.IGNORECASE) for p in ignore_hosts(DEFAULT_BYPASS))


@pytest.mark.parametrize("host", STILL_INTERCEPTED)
def test_every_other_host_is_still_intercepted(host: str) -> None:
    assert not any(re.match(p, f"{host}:443") for p in ignore_hosts(DEFAULT_BYPASS))
    assert not bypassed(f"https://{host}/file.zip", DEFAULT_BYPASS)
