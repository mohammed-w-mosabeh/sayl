import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from daemon.api import VERSION, DownloadOut
from daemon.engines.hybrid import NETWORK_LOST, HybridEngine
from daemon.models import Download, EngineName, PolicyReason
from daemon.openapi import schema
from daemon.vault import Vault
from tests.origin import BODY, ETAG, SHA256, SIZE, TOKEN, Origin, open_stream
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


async def capture(stack: Stack, origin: Origin, path: str, cookie: str = "") -> dict[str, Any]:
    """Do what sayl-capture will do: one request, then hand over that same response."""
    url = origin.url(path)
    info, body = await open_stream(url)
    attached = await stack.client.post(
        "/downloads/attach",
        json={
            "url": url,
            "request_headers": {"Cookie": cookie} if cookie else {},
            "response_headers": {
                "Content-Length": str(info.total),
                "Accept-Ranges": "bytes",
                "ETag": info.etag,
                "Content-Disposition": 'attachment; filename="../../f.bin"',
            },
        },
    )
    assert attached.status_code == 201
    download_id = attached.json()["id"]
    done = await stack.client.put(f"/downloads/{download_id}/stream", content=body)
    assert done.status_code == 200
    result: dict[str, Any] = done.json()
    return result


def file_hash(stack: Stack, name: str = "f.bin") -> str:
    return hashlib.sha256((stack.downloads / name).read_bytes()).hexdigest()


async def test_acceptance_one_time_link_is_requested_exactly_once(
    stack: Stack, origin: Origin
) -> None:
    path = f"/once/{TOKEN}/file.bin"
    result = await capture(stack, origin, path)
    assert result["status"] == "completed"
    assert file_hash(stack) == SHA256
    assert origin.hits[path] == 1


async def test_acceptance_session_bound_link_finishes_on_the_stream(
    stack: Stack, origin: Origin
) -> None:
    result = await capture(stack, origin, "/session.bin", cookie="sid=1")
    assert result["status"] == "completed"
    assert file_hash(stack) == SHA256
    assert "sid=1" in origin.cookies  # The rejected Range request carried the session.
    policy = stack.daemon.services.store.policy("127.0.0.1")
    assert policy is not None
    assert policy.reason is PolicyReason.AUTH


async def test_acceptance_range_capable_link_is_accelerated(stack: Stack, origin: Origin) -> None:
    result = await capture(stack, origin, "/normal.bin")
    assert result["status"] == "completed"
    assert file_hash(stack) == SHA256
    assert origin.ranges["/normal.bin"] == 3
    assert stack.daemon.services.store.segments(result["id"])


async def test_stream_may_be_handed_over_once(stack: Stack, origin: Origin) -> None:
    result = await capture(stack, origin, "/normal.bin")
    again = await stack.client.put(f"/downloads/{result['id']}/stream", content=b"x")
    assert again.status_code == 409


async def attach_and_cut(stack: Stack, url: str) -> str:
    attached = await stack.client.post(
        "/downloads/attach",
        json={
            "url": url,
            "response_headers": {
                "Content-Length": str(SIZE),
                "Accept-Ranges": "bytes",
                "ETag": ETAG,
                "Content-Disposition": 'attachment; filename="f.bin"',
            },
        },
    )
    download_id: str = attached.json()["id"]

    async def body() -> AsyncIterator[bytes]:
        yield BODY[: SIZE // 2]
        await asyncio.sleep(0.2)
        raise ConnectionError("capture died")

    with pytest.raises((httpx.HTTPError, ConnectionError)):
        await stack.client.put(f"/downloads/{download_id}/stream", content=body())
    return download_id


async def test_cut_stream_resumes_by_range_through_the_api(stack: Stack, origin: Origin) -> None:
    download_id = await attach_and_cut(stack, origin.url("/normal.bin"))
    body = await wait_for(stack, download_id, "completed", "needs_refresh", "failed")
    assert body["status"] == "completed"
    assert file_hash(stack) == SHA256
    assert origin.hits == origin.ranges  # Only Range requests: the URL was never fetched whole.


async def test_cut_stream_needs_refresh_then_continues(stack: Stack, origin: Origin) -> None:
    download_id = await attach_and_cut(stack, origin.url("/session.bin"))
    body = await wait_for(stack, download_id, "needs_refresh", "completed", "failed")
    assert (body["status"], body["error"]) == ("needs_refresh", "link expired, refresh it")
    refused = await stack.client.post(f"/downloads/{download_id}/resume")
    assert refused.status_code == 409
    fresh = origin.url("/fresh.bin")
    await stack.client.post(f"/downloads/{download_id}/refresh-url", json={"url": fresh})
    body = await wait_for(stack, download_id, "completed", "failed")
    assert body["status"] == "completed"
    assert origin.range_log[-1].startswith(f"bytes={SIZE // 2}-")
    assert file_hash(stack) == SHA256


async def test_a_network_outage_waits_then_resumes(stack: Stack, origin: Origin) -> None:
    hybrid = stack.daemon.services.engines[EngineName.HYBRID]
    assert isinstance(hybrid, HybridEngine)
    hybrid.retry_delay, hybrid.range_retry_seconds = 0.01, 0.2
    origin.down = True
    download_id = await attach_and_cut(stack, origin.url("/normal.bin"))
    body = await wait_for(stack, download_id, "needs_refresh", "completed", "failed")
    assert (body["status"], body["error"]) == ("needs_refresh", NETWORK_LOST)
    kept = body["done_bytes"]
    assert kept > 0
    origin.down = False
    before = len(origin.range_log)
    resumed = await stack.client.post(f"/downloads/{download_id}/resume")
    assert resumed.status_code == 200
    body = await wait_for(stack, download_id, "completed", "needs_refresh", "failed")
    assert body["status"] == "completed"
    assert file_hash(stack) == SHA256
    assert origin.hits == origin.ranges  # Never fetched whole: every kept byte was reused.
    spans = (r.removeprefix("bytes=").split("-") for r in origin.range_log[before:])
    assert sum(int(last) - int(first) + 1 for first, last in spans) == SIZE - kept


async def test_token_is_required_everywhere(stack: Stack) -> None:
    client = httpx.AsyncClient(base_url=str(stack.client.base_url))
    assert (await client.get("/downloads")).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (await client.get("/downloads", headers=wrong)).status_code == 401
    await client.aclose()


async def test_every_route_declares_the_token() -> None:
    for path, operations in schema()["paths"].items():
        for operation in operations.values():
            assert operation.get("security"), path


async def test_web_origins_are_rejected(stack: Stack) -> None:
    web = await stack.client.get("/downloads", headers={"Origin": "https://evil.example"})
    assert web.status_code == 403
    app = await stack.client.get("/downloads", headers={"Origin": "tauri://localhost"})
    assert app.status_code == 200


async def test_the_app_origin_gets_cors_and_a_web_origin_does_not(stack: Stack) -> None:
    preflight = {
        "Origin": "tauri://localhost",
        "Access-Control-Request-Method": "DELETE",
        "Access-Control-Request-Headers": "authorization",
    }
    allowed = await stack.client.options("/downloads/x", headers=preflight)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "tauri://localhost"
    web = await stack.client.options("/downloads/x", headers={**preflight, "Origin": "http://a.b"})
    assert web.status_code == 403
    dev = await stack.client.get("/downloads", headers={"Origin": "http://localhost:3000"})
    assert dev.status_code == 403  # Dev origins are off unless config.toml lists them.


async def test_dev_origins_are_opt_in(tmp_path: Path) -> None:
    async with running_stack(tmp_path, dev_origins=("http://localhost:3000",)) as stack:
        dev = await stack.client.get("/downloads", headers={"Origin": "http://localhost:3000"})
        assert dev.status_code == 200
        assert dev.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_download_out_lists_every_public_column() -> None:
    columns = set(Download.model_fields) - {"headers_enc", "warnings_json"}
    assert set(DownloadOut.model_fields) == columns | {"warnings"}


async def test_info(stack: Stack) -> None:
    info = (await stack.client.get("/info")).json()
    assert info == {
        "version": VERSION,
        "default_dir": str(stack.downloads),
        "max_concurrent": 3,
        "clipboard": "unavailable",  # Tests run with no display; see conftest.py.
    }


async def test_stored_headers_are_encrypted_and_never_returned(stack: Stack) -> None:
    created = await stack.client.post(
        "/downloads",
        json={
            "url": "http://127.0.0.1:9/x",
            "cookies": "sid=secret",
            "referer": "http://a",
            "start_paused": True,
            "filename": "../x.bin",
        },
    )
    body = created.json()
    assert "headers_enc" not in body
    assert body["filename"] == "x.bin"
    row = stack.daemon.services.store.download(body["id"])
    assert row is not None
    assert b"secret" not in row.headers_enc
    assert Vault.open(stack.daemon.services.vault, row.headers_enc)["Cookie"] == "sid=secret"


async def wait_for(stack: Stack, download_id: str, *states: str) -> dict[str, Any]:
    for _ in range(1500):  # Extractor loading is slow under coverage tracing.
        body: dict[str, Any] = (await stack.client.get(f"/downloads/{download_id}")).json()
        if body["status"] in states:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"still {body['status']}: {body['error']}")


async def test_add_download_runs_through_aria2(stack: Stack, origin: Origin) -> None:
    created = await stack.client.post("/downloads", json={"url": origin.url("/normal.bin")})
    body = await wait_for(stack, created.json()["id"], "completed", "failed")
    assert body["status"] == "completed"
    assert body["engine"] == "aria2"
    assert file_hash(stack, "normal.bin") == SHA256


async def test_pause_resume_patch_refresh_and_delete(stack: Stack, origin: Origin) -> None:
    created = await stack.client.post("/downloads", json={"url": origin.url("/slow/a.bin")})
    download_id = created.json()["id"]
    await wait_for(stack, download_id, "active")
    assert (await stack.client.post(f"/downloads/{download_id}/pause")).json()["status"] == "paused"
    patched = await stack.client.patch(
        f"/downloads/{download_id}",
        json={"priority": 5, "speed_limit": 1000000, "filename": "../b.bin"},
    )
    assert (patched.json()["priority"], patched.json()["filename"]) == (5, "b.bin")
    await stack.client.post(f"/downloads/{download_id}/resume")
    await wait_for(stack, download_id, "active")
    moved = origin.url("/slow/a2.bin")
    refreshed = await stack.client.post(
        f"/downloads/{download_id}/refresh-url", json={"url": moved}
    )
    assert refreshed.json()["url"] == moved
    listed = await stack.client.get("/downloads", params={"state": "active"})
    assert [row["id"] for row in listed.json()] == [download_id]
    assert (await stack.client.delete(f"/downloads/{download_id}")).status_code == 204
    assert (await stack.client.get(f"/downloads/{download_id}")).status_code == 404


async def test_concurrency_limit_holds(tmp_path: Path, origin: Origin) -> None:
    async with running_stack(tmp_path, max_concurrent=1) as stack:
        rows = [
            (await stack.client.post("/downloads", json={"url": origin.url(f"/slow/{n}.bin")}))
            for n in range(2)
        ]
        await wait_for(stack, rows[0].json()["id"], "active")
        second = (await stack.client.get(f"/downloads/{rows[1].json()['id']}")).json()
        assert second["status"] == "queued"
        for row in rows:
            await stack.client.delete(f"/downloads/{row.json()['id']}")


async def test_failed_download_retries_then_stops(tmp_path: Path) -> None:
    async with running_stack(tmp_path, max_retries=0) as stack:
        created = await stack.client.post("/downloads", json={"url": "http://127.0.0.1:9/x.bin"})
        body = await wait_for(stack, created.json()["id"], "failed")
        assert body["error"]


async def test_probe(stack: Stack, origin: Origin) -> None:
    result = (await stack.client.post("/probe", json={"url": origin.url("/normal.bin")})).json()
    assert (result["engine"], result["resumable"]) == ("aria2", True)


async def test_settings_categories_and_schedules(stack: Stack) -> None:
    values = {"speed_limit": 1000, "theme": "dark", "clear_host_policy": True}
    assert (await stack.client.put("/settings", json=values)).json()["theme"] == "dark"
    assert (await stack.client.get("/settings")).json()["speed_limit"] == 1000
    await stack.client.put("/settings", json={"speed_limit": 0})
    assert stack.daemon.hybrid.limiter.rate == 0

    made = (await stack.client.post("/categories", json={"name": "ISO"})).json()
    await stack.client.put(f"/categories/{made['id']}", json={"name": "Images"})
    assert (await stack.client.get("/categories")).json()[0]["name"] == "Images"
    assert (await stack.client.delete(f"/categories/{made['id']}")).status_code == 204
    assert (await stack.client.delete("/categories/none")).status_code == 404

    schedule = {"name": "n", "cron": "* * * * *", "action": "start_queue"}
    assert (await stack.client.post("/schedules", json=schedule)).status_code == 201


async def test_websocket_events_and_auth(stack: Stack, origin: Origin) -> None:
    url = f"ws://127.0.0.1:{stack.port}/ws"
    with pytest.raises(websockets.InvalidStatus):
        async with websockets.connect(url):
            pass
    token = stack.daemon.services.token
    async with websockets.connect(url, subprotocols=[f"bearer.{token}"]) as socket:  # type: ignore[list-item]
        await stack.client.post("/downloads", json={"url": origin.url("/normal.bin")})
        seen = set()
        while "download.completed" not in seen:
            seen.add(json.loads(await asyncio.wait_for(socket.recv(), 15))["event"])
        assert "download.added" in seen

        paused = await stack.client.post(
            "/downloads", json={"url": "http://x", "start_paused": True}
        )
        download_id = paused.json()["id"]
        await stack.client.patch(f"/downloads/{download_id}", json={"priority": 2})
        await stack.client.delete(f"/downloads/{download_id}")
        events = []
        while not events or events[-1]["event"] != "download.removed":
            events.append(json.loads(await asyncio.wait_for(socket.recv(), 15)))
        assert {"event": "download.updated", "id": download_id, "status": "paused"} in events
        assert events[-1] == {"event": "download.removed", "id": download_id}


def test_published_schema_is_current() -> None:
    published = json.loads(Path("docs/openapi.json").read_text())
    assert published == json.loads(json.dumps(schema()))
