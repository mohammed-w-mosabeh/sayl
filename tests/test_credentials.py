"""Site credentials: stored sealed, never returned, sent only to their own host."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daemon.credentials import Login, normalize_host
from daemon.engines.base import Job
from daemon.models import Credential, Download, EngineName
from tests.origin import BASIC, LOGIN, SHA256, Origin
from tests.stack import Stack, running_stack


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


async def finish(stack: Stack, download_id: str) -> dict[str, Any]:
    for _ in range(400):
        body: dict[str, Any] = (await stack.client.get(f"/downloads/{download_id}")).json()
        if body["status"] in ("completed", "failed"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"still {body['status']}")


@pytest.mark.parametrize(
    ("given", "host"),
    [
        ("Example.COM", "example.com"),
        ("https://user@files.example.com:8443/a?b", "files.example.com"),
        ("[::1]:80", "::1"),
        ("", ""),
    ],
)
def test_hosts_are_normalized(given: str, host: str) -> None:
    assert normalize_host(given) == host


def test_basic_header() -> None:
    assert Login(*LOGIN).basic() == BASIC


async def test_api_never_returns_a_password(stack: Stack) -> None:
    put = await stack.client.put(
        "/credentials/Files.Example.com", json={"username": "me", "password": "s3cret"}
    )
    assert put.json() == {"host": "files.example.com", "username": "me"}
    listed = await stack.client.get("/credentials")
    assert listed.json() == [{"host": "files.example.com", "username": "me"}]
    assert "s3cret" not in listed.text
    row = stack.daemon.services.store.get(Credential, "files.example.com")
    assert row is not None
    assert b"s3cret" not in row.secret_enc  # Sealed at rest.
    assert (
        await stack.client.put("/credentials/%20", json={"username": "x", "password": ""})
    ).status_code == 422
    assert (await stack.client.delete("/credentials/files.example.com")).status_code == 204
    assert (await stack.client.delete("/credentials/files.example.com")).status_code == 404
    assert (await stack.client.get("/credentials")).json() == []


async def test_credentials_survive_a_restart(tmp_path: Path) -> None:
    async with running_stack(tmp_path) as first:
        await first.client.put("/credentials/a.example", json={"username": "u", "password": "p"})
    async with running_stack(tmp_path) as second:
        login = second.daemon.services.queue.credentials
        assert login is not None
        assert login.for_url("https://A.example/x") == Login("u", "p")


async def test_aria2_download_authenticates_with_the_stored_login(
    stack: Stack, origin: Origin
) -> None:
    url = origin.url("/auth/file.bin")
    await stack.client.put(
        "/credentials/127.0.0.1", json={"username": LOGIN[0], "password": LOGIN[1]}
    )
    created = await stack.client.post("/downloads", json={"url": url})
    body = await finish(stack, created.json()["id"])
    assert body["status"] == "completed", body["error"]
    data = (stack.downloads / "file.bin").read_bytes()
    assert hashlib.sha256(data).hexdigest() == SHA256


async def test_a_login_never_follows_a_redirect_to_another_host(
    stack: Stack, origin: Origin
) -> None:
    await stack.client.put(
        "/credentials/127.0.0.1", json={"username": LOGIN[0], "password": LOGIN[1]}
    )
    created = await stack.client.post("/downloads", json={"url": origin.url("/away/normal.bin")})
    body = await finish(stack, created.json()["id"])
    assert body["status"] == "completed", body["error"]
    elsewhere = [auth for host, _, auth in origin.auth_log if host.startswith("localhost")]
    assert elsewhere
    assert not any(elsewhere)


async def test_explicit_authorization_wins_and_ytdlp_gets_a_login(stack: Stack) -> None:
    queue = stack.daemon.services.queue
    assert queue.credentials is not None
    await queue.credentials.set("video.example", "viewer", "pw")
    plain = Download(url="https://video.example/watch?v=1", dest_dir="out")
    job: Job = await queue.job(plain, EngineName.YTDLP)
    assert (job.username, job.password) == ("viewer", "pw")
    assert "Authorization" not in job.headers
    sealed = stack.daemon.services.vault.seal({"Authorization": "Bearer mine"})
    own = Download(url="https://video.example/f.bin", headers_enc=sealed, dest_dir="out")
    job = await queue.job(own, EngineName.HYBRID)
    assert job.headers == {"Authorization": "Bearer mine"}
    magnet = Download(url="magnet:?xt=urn:btih:abc", dest_dir="out")
    assert (await queue.job(magnet, EngineName.ARIA2)).headers == {}
