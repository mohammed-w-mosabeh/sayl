import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from daemon.grabber import Grab, Grabber, GrabOptions, links_in
from tests.stack import Stack, running_stack

SITE = "https://files.example"
PAGES = {
    "/": '<a href="/a.zip">a</a> <a href="docs/">docs</a> <a href="/private/">p</a>'
    ' <a href="https://other.example/x.zip">x</a> <img src="/logo.png">'
    ' <a href="/#top">top</a> <a href="javascript:void(0)">js</a>',
    "/docs/": '<base href="/docs/"><a href="b.pdf">b</a> <a href="deep/">deeper</a>',
    "/docs/deep/": '<a href="c.zip">c</a>',
    "/private/": '<a href="/secret.zip">s</a>',
}
SIZES = {"/a.zip": 1000, "/docs/b.pdf": 50}


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host != "files.example":
        raise AssertionError(f"left the site: {request.url}")
    if path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    if path in PAGES:
        return httpx.Response(200, html=PAGES[path])
    if request.method == "HEAD":
        size = SIZES.get(path)
        headers = {"Content-Length": str(size)} if size is not None else {}
        return httpx.Response(200, headers=headers)
    return httpx.Response(404)


def grabber() -> Grabber:
    return Grabber(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def grab(**options: Any) -> Grab:
    tool = grabber()
    started = tool.start(GrabOptions(url=f"{SITE}/", **options))
    assert started.task is not None
    await started.task
    await tool.stop()
    return started


def names(found: Grab) -> list[str]:
    return sorted(f.url.removeprefix(SITE) for f in found.files)


def test_links_in_resolves_base_and_drops_fragments() -> None:
    links = links_in(PAGES["/docs/"], f"{SITE}/somewhere/else")
    assert links == [f"{SITE}/docs/b.pdf", f"{SITE}/docs/deep/"]
    assert f"{SITE}/" in links_in(PAGES["/"], f"{SITE}/")  # "/#top" is the page itself.
    assert not any(link.startswith("javascript") for link in links_in(PAGES["/"], SITE))


async def test_a_grab_stays_on_the_site_and_honours_robots() -> None:
    found = await grab()
    assert found.status == "done", found.error
    assert names(found) == ["/a.zip", "/docs/b.pdf", "/docs/deep/c.zip"]
    assert found.skipped_robots == 1
    assert found.pages == 3
    page = {f.url: f.page for f in found.files}
    assert page[f"{SITE}/docs/b.pdf"] == f"{SITE}/docs/"


async def test_depth_limits_the_crawl() -> None:
    assert names(await grab(depth=1)) == ["/a.zip", "/docs/b.pdf"]
    assert names(await grab(depth=0)) == ["/a.zip"]


async def test_size_and_type_filters() -> None:
    assert names(await grab(extensions=("zip",), min_size=100)) == ["/a.zip"]
    small = await grab(max_size=100)
    assert names(small) == ["/docs/b.pdf", "/docs/deep/c.zip"]  # Unknown sizes pass a maximum.
    assert {f.size for f in small.files} == {50, None}


async def test_max_pages_stops_the_crawl() -> None:
    found = await grab(max_pages=1)
    assert found.pages == 1
    assert names(found) == ["/a.zip"]


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path) as running:
        old = running.daemon.services.grabber
        running.daemon.services.grabber = grabber()
        yield running
        if old is not None:
            await old.stop()


async def test_grab_through_the_api_then_queue_the_pick(stack: Stack) -> None:
    started = await stack.client.post(
        "/grab-site", json={"url": f"{SITE}/", "extensions": [".PDF"]}
    )
    assert started.status_code == 202
    grab_id = started.json()["id"]
    for _ in range(200):
        body: dict[str, Any] = (await stack.client.get(f"/grab-site/{grab_id}")).json()
        if body["status"] != "running":
            break
        await asyncio.sleep(0.02)
    assert body["status"] == "done"
    assert [f["url"] for f in body["files"]] == [f"{SITE}/docs/b.pdf"]
    queued = await stack.client.post(
        "/batch", json={"urls": [f["url"] for f in body["files"]], "start_paused": True}
    )
    assert len(queued.json()["ids"]) == 1
    assert (await stack.client.delete(f"/grab-site/{grab_id}")).status_code == 204
    assert (await stack.client.get("/grab-site/none")).status_code == 404
    bad = await stack.client.post("/grab-site", json={"url": "file:///etc"})
    assert bad.status_code == 422
