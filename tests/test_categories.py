import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daemon import categories
from daemon.db import make_engine, migrate
from daemon.models import Category
from daemon.store import Store
from tests.origin import BODY, ETAG, SIZE, Origin
from tests.stack import Stack, running_stack

DEFAULTS = categories.defaults()


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
    ("filename", "mime", "expected"),
    [
        ("ubuntu.iso", "", "Disk images"),
        ("Report.PDF", "", "Documents"),
        ("backup.tar.gz", "", "Compressed"),
        ("song", "audio/mpeg", "Music"),
        ("clip", "video/mp4; codecs=avc1", "Video"),
        ("setup.deb", "video/mp4", "Programs"),  # The extension wins over the MIME type.
        ("notes", "text/html", None),
    ],
)
def test_defaults_match(filename: str, mime: str, expected: str | None) -> None:
    found = categories.match(DEFAULTS, filename, mime)
    assert (found.name if found else None) == expected


def test_the_longest_extension_wins() -> None:
    tarballs = Category(name="Tarballs", extensions="tar.gz")
    found = categories.match([*DEFAULTS, tarballs], "src.tar.gz")
    assert found is tarballs


def test_folder_prefers_the_user_then_the_category() -> None:
    video = Category(name="Video", dest_dir="~/Videos")
    assert categories.folder(video, "/chosen", "/default") == "/chosen"
    assert categories.folder(video, "", "/default") == str(Path.home() / "Videos")
    assert categories.folder(Category(name="x"), "", "/default") == "/default"
    assert categories.folder(None, "", "/default") == "/default"


def test_defaults_are_seeded_once(tmp_path: Path) -> None:
    database = tmp_path / "sayl.db"
    migrate(database)
    store = Store(make_engine(database))
    store.seed_categories(categories.defaults())
    assert len(store.all(Category)) == len(DEFAULTS)
    for category in store.all(Category):
        store.remove(Category, category.id)
    store.seed_categories(categories.defaults())
    assert store.all(Category) == []  # Deleted on purpose: not brought back.


async def category_named(stack: Stack, name: str) -> dict[str, Any]:
    listed = (await stack.client.get("/categories")).json()
    return next(item for item in listed if item["name"] == name)


async def give_folder(stack: Stack, name: str, folder: Path) -> str:
    category = await category_named(stack, name)
    category["dest_dir"] = str(folder)
    await stack.client.put(f"/categories/{category['id']}", json=category)
    return str(category["id"])


async def wait_done(stack: Stack, download_id: str) -> dict[str, Any]:
    for _ in range(1500):
        body: dict[str, Any] = (await stack.client.get(f"/downloads/{download_id}")).json()
        if body["status"] in ("completed", "failed"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(body["status"])


async def test_a_new_download_goes_to_its_category_folder(
    stack: Stack, origin: Origin, tmp_path: Path
) -> None:
    folder = tmp_path / "zips"
    category_id = await give_folder(stack, "Compressed", folder)
    created = (await stack.client.post("/downloads", json={"url": origin.url("/a.zip")})).json()
    assert (created["category_id"], created["dest_dir"]) == (category_id, str(folder))
    body = await wait_done(stack, created["id"])
    assert body["status"] == "completed"
    assert (folder / "a.zip").read_bytes() == BODY


async def test_a_chosen_folder_beats_the_category(
    stack: Stack, origin: Origin, tmp_path: Path
) -> None:
    await give_folder(stack, "Compressed", tmp_path / "zips")
    chosen = tmp_path / "mine"
    created = await stack.client.post(
        "/downloads", json={"url": origin.url("/b.zip"), "dest_dir": str(chosen)}
    )
    assert created.json()["dest_dir"] == str(chosen)


async def test_a_name_known_only_at_the_end_moves_the_file(
    stack: Stack, origin: Origin, tmp_path: Path
) -> None:
    folder = tmp_path / "docs"
    category_id = await give_folder(stack, "Documents", folder)
    created = (
        await stack.client.post("/downloads", json={"url": origin.url("/named?report.pdf")})
    ).json()
    assert created["category_id"] is None  # The URL's own name says nothing yet.
    body = await wait_done(stack, created["id"])
    assert body["status"] == "completed", body["error"]
    assert (body["category_id"], body["dest_dir"], body["filename"]) == (
        category_id,
        str(folder),
        "report.pdf",
    )
    assert (folder / "report.pdf").read_bytes() == BODY
    assert not (stack.downloads / "report.pdf").exists()


async def test_a_capture_is_sorted_by_its_mime_type(stack: Stack, tmp_path: Path) -> None:
    folder = tmp_path / "videos"
    category_id = await give_folder(stack, "Video", folder)
    attached = await stack.client.post(
        "/downloads/attach",
        json={
            "url": "https://videos.example/watch",
            "response_headers": {
                "Content-Length": str(SIZE),
                "Content-Type": "video/mp4",
                "ETag": ETAG,
            },
        },
    )
    body = attached.json()
    assert (body["category_id"], body["dest_dir"]) == (category_id, str(folder))
