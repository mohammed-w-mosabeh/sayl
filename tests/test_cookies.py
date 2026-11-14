import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daemon import cookies
from tests.origin import Origin
from tests.stack import Stack, running_stack


def firefox_profile(root: Path, rows: list[tuple[str, str, str]]) -> Path:
    """A Firefox profile whose cookies.sqlite holds (host, name, value) rows."""
    profile = root / "abcd.default-release"
    profile.mkdir(parents=True)
    database = sqlite3.connect(profile / "cookies.sqlite")
    database.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT DEFAULT '',"
        " name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)"
    )
    expiry = int(time.time()) + 86400
    database.executemany(
        "INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure)"
        " VALUES (?, ?, ?, '/', ?, 0)",
        [(name, value, host, expiry) for host, name, value in rows],
    )
    database.commit()
    database.close()
    return profile


@pytest.fixture
async def origin() -> AsyncIterator[Origin]:
    server = Origin()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path) as running:
        yield running


def test_only_the_sites_cookies_are_sent(tmp_path: Path) -> None:
    profile = firefox_profile(
        tmp_path, [(".files.example", "sid", "abc"), ("other.example", "x", "y")]
    )
    spec = f"firefox:{profile}"
    assert cookies.cookie_header_sync(spec, "https://dl.files.example/a.zip") == "sid=abc"
    assert cookies.cookie_header_sync(spec, "https://unrelated.example/") == ""


def test_unknown_browsers_and_missing_profiles_are_reported(tmp_path: Path) -> None:
    with pytest.raises(cookies.CookieError, match="unknown browser"):
        cookies.cookie_header_sync("netscape", "https://h/")
    with pytest.raises(cookies.CookieError, match="could not read cookies from firefox"):
        cookies.cookie_header_sync(f"firefox:{tmp_path / 'none'}", "https://h/")


def test_explicit_cookies_win() -> None:
    assert cookies.merge("a=1; b=2", "b=9; c=3") == "a=1; b=2; c=3"
    assert cookies.merge("", "c=3") == "c=3"
    assert cookies.merge("a=1", "") == "a=1"


def test_browsers_are_found_by_their_profile_dirs(tmp_path: Path) -> None:
    (tmp_path / "snap/firefox/common/.mozilla/firefox").mkdir(parents=True)
    (tmp_path / ".config/BraveSoftware/Brave-Browser").mkdir(parents=True)
    assert cookies.browsers(tmp_path) == ["firefox", "brave"]


async def test_a_download_carries_the_browsers_cookies(
    stack: Stack, origin: Origin, tmp_path: Path
) -> None:
    profile = firefox_profile(tmp_path / "ff", [("127.0.0.1", "session", "s3cret")])
    created = await stack.client.post(
        "/downloads",
        json={
            "url": origin.url("/members.bin"),
            "cookies": "lang=ar",
            "cookies_from_browser": f"firefox:{profile}",
        },
    )
    assert created.status_code == 201, created.text
    for _ in range(600):
        body: dict[str, Any] = (await stack.client.get(f"/downloads/{created.json()['id']}")).json()
        if body["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
    assert body["status"] == "completed"
    assert "lang=ar; session=s3cret" in origin.cookies
    assert "s3cret" not in str(body)  # Stored headers never leave the daemon.


async def test_an_unreadable_browser_is_a_422(stack: Stack, tmp_path: Path) -> None:
    response = await stack.client.post(
        "/downloads",
        json={"url": "https://h.example/f", "cookies_from_browser": f"firefox:{tmp_path}"},
    )
    assert response.status_code == 422
    assert "could not read cookies" in response.json()["detail"]
