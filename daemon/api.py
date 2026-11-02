"""REST API and WebSocket events. Every route requires the bearer token."""

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from daemon.config import Config
from daemon.engines.base import Engine, EngineError, StreamInfo
from daemon.engines.hybrid import may_resume
from daemon.events import EventBus
from daemon.filenames import filename_from_response, sanitize
from daemon.models import Category, Download, EngineName, Schedule, Status
from daemon.queue import QueueManager
from daemon.router import route
from daemon.store import Store, run
from daemon.vault import Vault

APP_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})
bearer = HTTPBearer(auto_error=False)


class RejectWebOrigins:
    """Stops a web page from reaching the daemon, for HTTP and WebSocket alike."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope["headers"])
            origin = headers.get(b"origin", b"").decode()
            if origin and origin not in APP_ORIGINS:
                await self._refuse(scope, send)
                return
        await self.app(scope, receive, send)

    async def _refuse(self, scope: Scope, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        start: Message = {"type": "http.response.start", "status": 403, "headers": []}
        await send(start)
        await send({"type": "http.response.body", "body": b""})


class AddDownload(BaseModel):
    url: str
    headers: dict[str, str] = {}
    cookies: str = ""
    referer: str = ""
    page_url: str = ""
    filename: str = ""
    dest_dir: str = ""
    category: str | None = None
    format_id: str = ""
    start_paused: bool = False
    speed_limit: int = 0
    audio_only: bool = False
    subtitles: list[str] = []
    playlist_items: str = ""
    cookies_file: str = ""
    cookies_from_browser: str = ""


class AttachDownload(BaseModel):
    url: str
    method: str = "GET"
    status: int = 200
    request_headers: dict[str, str] = {}
    response_headers: dict[str, str] = {}
    page_url: str = ""
    filename: str = ""
    dest_dir: str = ""


class PatchDownload(BaseModel):
    priority: int | None = None
    speed_limit: int | None = None
    filename: str | None = None


class RefreshUrl(BaseModel):
    url: str
    headers: dict[str, str] = {}


class Probe(BaseModel):
    url: str
    headers: dict[str, str] = {}


@dataclass
class Services:
    store: Store
    vault: Vault
    queue: QueueManager
    bus: EventBus
    config: Config
    engines: dict[EngineName, Engine]
    token: str
    default_dir: str


def public(row: Download) -> dict[str, Any]:
    """Everything a client may see. Stored headers never leave the daemon."""
    return row.model_dump(exclude={"headers_enc"}, mode="json")


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="sayld", version="0.0.0")
    app.add_middleware(RejectWebOrigins)
    store, queue = services.store, services.queue

    def authorized(token: str) -> bool:
        return secrets.compare_digest(token.encode(), services.token.encode())

    def require_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not authorized(credentials.credentials):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    auth = [Depends(require_token)]

    async def found(download_id: str) -> Download:
        row = await run(store.download, download_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown download")
        return row

    Found = Annotated[Download, Depends(found)]

    @app.post("/downloads", dependencies=auth, status_code=201)
    async def add_download(body: AddDownload) -> dict[str, Any]:
        headers = dict(body.headers)
        if body.cookies:
            headers["Cookie"] = body.cookies
        if body.referer:
            headers["Referer"] = body.referer
        row = Download(
            url=body.url,
            page_url=body.page_url,
            filename=sanitize(body.filename) if body.filename else "",
            dest_dir=body.dest_dir or services.default_dir,
            category_id=body.category,
            format_id=body.format_id,
            speed_limit=body.speed_limit,
            media_json=body.model_dump_json(
                include={
                    "audio_only",
                    "subtitles",
                    "playlist_items",
                    "cookies_file",
                    "cookies_from_browser",
                }
            ),
            headers_enc=services.vault.seal(headers),
            status=Status.PAUSED if body.start_paused else Status.QUEUED,
        )
        await run(store.add, row)
        services.bus.publish("download.added", id=row.id)
        queue.poke()
        return public(row)

    @app.post("/downloads/attach", dependencies=auth, status_code=201)
    async def attach_download(body: AttachDownload) -> dict[str, Any]:
        """Register a captured response. The origin is not contacted."""
        lower = {name.lower(): value for name, value in body.response_headers.items()}
        length = lower.get("content-length", "")
        info = StreamInfo(
            method=body.method.upper(),
            total=int(length) if length.isdigit() else None,
            accept_ranges=lower.get("accept-ranges", "").lower() == "bytes",
            etag=lower.get("etag", ""),
            last_modified=lower.get("last-modified", ""),
            content_encoding=lower.get("content-encoding", ""),
        )
        row = Download(
            url=body.url,
            final_url=body.url,
            page_url=body.page_url,
            filename=sanitize(body.filename) if body.filename else "",
            dest_dir=body.dest_dir or services.default_dir,
            engine=EngineName.HYBRID,
            status=Status.PROBING,
            total_bytes=info.total or 0,
            etag=info.etag,
            last_modified=info.last_modified,
            resumable=may_resume(info),
            headers_enc=services.vault.seal(body.request_headers),
        )
        row.filename = row.filename or filename_from_response(body.url, lower)
        row.engine_ref = row.id
        await run(store.add, row)
        services.bus.publish("capture.detected", id=row.id, filename=row.filename)
        services.bus.publish("download.added", id=row.id)
        return public(row)

    @app.put("/downloads/{download_id}/stream", dependencies=auth)
    async def put_stream(row: Found, request: Request) -> dict[str, Any]:
        """Hand over the response body from offset 0. It may be used once."""
        if row.engine is not EngineName.HYBRID or row.status is not Status.PROBING:
            raise HTTPException(status.HTTP_409_CONFLICT, "this download takes no stream")

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in request.stream():
                    if chunk:
                        yield chunk
            except ClientDisconnect as error:
                raise EngineError("the stream closed early") from error

        await queue.run_attached(row, body())
        return public(await found(row.id))

    @app.get("/downloads", dependencies=auth)
    async def list_downloads(state: Status | None = None) -> list[dict[str, Any]]:
        return [public(row) for row in await run(store.downloads, state)]

    @app.get("/downloads/{download_id}", dependencies=auth)
    async def get_download(row: Found) -> dict[str, Any]:
        return public(row)

    @app.patch("/downloads/{download_id}", dependencies=auth)
    async def patch_download(row: Found, body: PatchDownload) -> dict[str, Any]:
        if body.speed_limit is not None:
            await queue.set_speed_limit(row, body.speed_limit)
        fields: dict[str, Any] = {}
        if body.priority is not None:
            fields["priority"] = body.priority
        if body.filename is not None:
            fields["filename"] = sanitize(body.filename)
        await run(store.update, row.id, **fields)
        return public(await found(row.id))

    @app.delete("/downloads/{download_id}", dependencies=auth, status_code=204)
    async def delete_download(row: Found) -> None:
        await queue.cancel(row)
        await run(store.delete_download, row.id)

    async def act(row: Download, action: Callable[[Download], Awaitable[None]]) -> dict[str, Any]:
        try:
            await action(row)
        except EngineError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return public(await found(row.id))

    @app.post("/downloads/{download_id}/pause", dependencies=auth)
    async def pause(row: Found) -> dict[str, Any]:
        return await act(row, queue.pause)

    @app.post("/downloads/{download_id}/resume", dependencies=auth)
    async def resume(row: Found) -> dict[str, Any]:
        return await act(row, queue.resume)

    @app.post("/downloads/{download_id}/refresh-url", dependencies=auth)
    async def refresh_url(row: Found, body: RefreshUrl) -> dict[str, Any]:
        await queue.refresh_url(row, body.url, body.headers)
        return public(await found(row.id))

    @app.post("/probe", dependencies=auth)
    async def probe(body: Probe) -> dict[str, Any]:
        name = await run(route, body.url)
        try:
            result = await services.engines[name].probe(body.url, body.headers)
        except EngineError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return {**result.__dict__, "engine": name}

    @app.get("/settings", dependencies=auth)
    async def get_settings() -> dict[str, Any]:
        return await run(store.settings)

    @app.put("/settings", dependencies=auth)
    async def put_settings(values: dict[str, Any]) -> dict[str, Any]:
        await run(store.put_settings, values)
        if isinstance(values.get("speed_limit"), int):
            await queue.set_global_speed_limit(values["speed_limit"])
        if values.get("clear_host_policy"):
            await run(store.clear_host_policy)
        return await run(store.settings)

    def crud(path: str, model: type[Category] | type[Schedule]) -> None:
        async def create(row: model) -> model:  # type: ignore[valid-type]
            await run(store.put, row)
            return row

        async def read_all() -> list[model]:  # type: ignore[valid-type]
            return await run(store.all, model)

        async def replace(key: str, row: model) -> model:  # type: ignore[valid-type]
            row.id = key  # type: ignore[attr-defined]
            await run(store.put, row)
            return row

        async def remove(key: str) -> None:
            if not await run(store.remove, model, key):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

        app.post(path, dependencies=auth, status_code=201, operation_id=f"create{path}")(create)
        app.get(path, dependencies=auth, operation_id=f"list{path}")(read_all)
        app.put(path + "/{key}", dependencies=auth, operation_id=f"replace{path}")(replace)
        app.delete(
            path + "/{key}", dependencies=auth, status_code=204, operation_id=f"delete{path}"
        )(remove)

    crud("/categories", Category)
    crud("/schedules", Schedule)

    @app.websocket("/ws")
    async def events(socket: WebSocket) -> None:
        """Authenticate with the Authorization header or subprotocol `bearer.<token>`."""
        header = socket.headers.get("authorization", "").removeprefix("Bearer ")
        offered = next(
            (p for p in socket.scope.get("subprotocols", []) if p.startswith("bearer.")), ""
        )
        if not authorized(header or offered.removeprefix("bearer.")):
            await socket.close(status.WS_1008_POLICY_VIOLATION)
            return
        await socket.accept(subprotocol=offered or None)
        queue_ = services.bus.subscribe()
        receiver = asyncio.create_task(socket.receive())
        try:
            while True:
                getter = asyncio.create_task(queue_.get())
                done, _ = await asyncio.wait(
                    {getter, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done:
                    getter.cancel()
                    return
                await socket.send_json(getter.result())
        except WebSocketDisconnect:
            return
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver
            services.bus.unsubscribe(queue_)

    return app
