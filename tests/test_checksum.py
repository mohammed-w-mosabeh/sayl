import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daemon import checksum
from tests.origin import BODY, SHA256, Origin
from tests.stack import Stack, running_stack

MD5 = hashlib.md5(BODY).hexdigest()  # noqa: S324 - matching a published checksum


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"sha256:{SHA256}", ("sha256", SHA256)),
        (f"SHA-256={SHA256.upper()}", ("sha256", SHA256)),
        (f"  {SHA256}\n", ("sha256", SHA256)),
        (MD5, ("md5", MD5)),
        (f"md5:{MD5}", ("md5", MD5)),
    ],
)
def test_parse(text: str, expected: tuple[str, str]) -> None:
    assert checksum.parse(text) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("sha256:xyz", "hexadecimal"),
        ("abc", "length"),
        (f"crc32:{MD5}", "unsupported"),
        (f"sha256:{MD5}", "64 hex digits"),
    ],
)
def test_parse_refuses(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        checksum.parse(text)


def test_digest_file(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_bytes(BODY)
    assert checksum.digest_file(file, "sha256") == SHA256


async def settled(stack: Stack, download_id: str) -> dict[str, Any]:
    for _ in range(1500):
        body: dict[str, Any] = (await stack.client.get(f"/downloads/{download_id}")).json()
        if body["status"] == "failed" or body["checksum_ok"] is not None:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(body["status"])


async def test_a_download_is_verified_when_it_completes(stack: Stack, origin: Origin) -> None:
    created = await stack.client.post(
        "/downloads", json={"url": origin.url("/normal.bin"), "checksum": f"sha-256={SHA256}"}
    )
    assert created.json()["checksum_algo"] == "sha256"
    body = await settled(stack, created.json()["id"])
    assert (body["status"], body["checksum_ok"]) == ("completed", True)


async def test_a_mismatch_is_reported_and_the_file_kept(stack: Stack, origin: Origin) -> None:
    wrong = "0" * 64
    created = await stack.client.post(
        "/downloads", json={"url": origin.url("/normal.bin"), "checksum": wrong}
    )
    body = await settled(stack, created.json()["id"])
    assert (body["status"], body["checksum_ok"]) == ("completed", False)
    assert (stack.downloads / "normal.bin").exists()

    fixed = await stack.client.post(
        f"/downloads/{body['id']}/verify", json={"checksum": f"md5:{MD5}"}
    )
    assert fixed.json() == {"ok": True, "algo": "md5", "expected": MD5, "actual": MD5}
    assert (await stack.client.get(f"/downloads/{body['id']}")).json()["checksum_ok"] is True


async def test_bad_checksums_and_states_are_refused(stack: Stack, origin: Origin) -> None:
    bad = await stack.client.post(
        "/downloads", json={"url": origin.url("/normal.bin"), "checksum": "sha256:nope"}
    )
    assert bad.status_code == 422
    queued = await stack.client.post(
        "/downloads", json={"url": origin.url("/normal.bin"), "start_paused": True}
    )
    early = await stack.client.post(f"/downloads/{queued.json()['id']}/verify", json={})
    assert early.status_code == 409
