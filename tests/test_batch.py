import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from daemon.batch import MAX_ITEMS, expand, pattern_ranges
from tests.origin import Origin
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


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("h/f[1-3].jpg", ["h/f1.jpg", "h/f2.jpg", "h/f3.jpg"]),
        ("h/f[008-010].jpg", ["h/f008.jpg", "h/f009.jpg", "h/f010.jpg"]),
        ("h/[a-c]", ["h/a", "h/b", "h/c"]),
        ("h/[3-1]", ["h/3", "h/2", "h/1"]),
        ("h/[0-10:5]", ["h/0", "h/5", "h/10"]),
        ("s[1-2]e[01-02]", ["s1e01", "s1e02", "s2e01", "s2e02"]),
        ("http://[::1]:8080/x", ["http://[::1]:8080/x"]),  # IPv6 stays literal.
        ("h/plain", ["h/plain"]),
    ],
)
def test_expand(pattern: str, expected: list[str]) -> None:
    assert expand(pattern) == expected
    assert pattern_ranges(pattern) == (expected != [pattern])


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("[a-Z]", "one case"),
        ("[1-5:0]", "at least 1"),
        (f"[1-{MAX_ITEMS + 1}]", "more than"),
        ("[1-200][1-200]", "more than"),
    ],
)
def test_expand_refuses(pattern: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        expand(pattern)


async def test_batch_queues_each_link_once_in_order(
    stack: Stack, origin: Origin, tmp_path: Path
) -> None:
    folder = tmp_path / "batch"
    response = await stack.client.post(
        "/batch",
        json={
            "urls": [origin.url("/one.bin"), "", origin.url("/b02.bin")],
            "patterns": [origin.url("/b[01-03].bin")],
            "dest_dir": str(folder),
        },
    )
    assert response.status_code == 201, response.text
    ids = response.json()["ids"]
    assert len(ids) == 4  # b02 appears twice but is queued once.
    rows = [(await stack.client.get(f"/downloads/{i}")).json() for i in ids]
    assert [row["url"].rsplit("/", 1)[1] for row in rows] == [
        "one.bin",
        "b02.bin",
        "b01.bin",
        "b03.bin",
    ]
    for _ in range(600):
        states = {(await stack.client.get(f"/downloads/{i}")).json()["status"] for i in ids}
        if states <= {"completed", "failed"}:
            break
        await asyncio.sleep(0.05)
    assert states == {"completed"}
    assert sorted(p.name for p in folder.iterdir()) == ["b01.bin", "b02.bin", "b03.bin", "one.bin"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"urls": []}, "no links"),
        ({"urls": ["file:///etc/passwd"]}, "not a download link"),
        ({"patterns": ["https://h/[1-200][1-200]"]}, "more than"),
    ],
)
async def test_batch_refuses(stack: Stack, body: dict[str, object], message: str) -> None:
    response = await stack.client.post("/batch", json=body)
    assert response.status_code == 422
    assert message in response.json()["detail"]
