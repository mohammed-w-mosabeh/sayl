"""REST API and WebSocket events. Every route requires the bearer token."""

import asyncio
import contextlib
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from daemon.config import Config
from daemon.engines.base import Engine, EngineError, ProbeResult, StreamInfo
from daemon.engines.hybrid import may_resume
from daemon.events import EventBus
from daemon.filenames import filename_from_response, sanitize
from daemon.models import Category, Download, EngineName, Schedule, Status
from daemon.queue import QueueManager
from daemon.router import route
from daemon.store import Store, run
from daemon.vault import Vault
from daemon.watchers.service import ClipboardService

VERSION = "0.1.0"
APP_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})
bearer = HTTPBearer(auto_error=False)


class RejectWebOrigins:
    """Stops a web page from reaching the daemon, for HTTP and WebSocket alike."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str] = APP_ORIGINS) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope["headers"])
            origin = headers.get(b"origin", b"").decode()
            if origin and origin not in self.allowed:
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


class DownloadOut(BaseModel):
    """A download as clients see it. Stored headers never leave the daemon."""

    id: str
    url: str
    final_url: str
    page_url: str
    filename: str
    dest_dir: str
    category_id: str | None
    engine: EngineName
    engine_ref: str
    status: Status
    total_bytes: int
    done_bytes: int
    speed: int
    eta: int
    connections: int
    priority: int
    speed_limit: int
    format_id: str
    media_json: str
    etag: str
    last_modified: str
    resumable: bool
    retries: int
    checksum_algo: str
    checksum_expected: str
    checksum_ok: bool | None
    error: str
    warnings: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ProbeOut(BaseModel):
    url: str
    filename: str
    total: int | None
    resumable: bool
    extractor: str
    formats: list[dict[str, Any]]
    entries: list[dict[str, Any]]
    subtitles: list[str]
    duration: float | None
    thumbnail: str
    engine: EngineName


class InfoOut(BaseModel):
    version: str
    default_dir: str
    max_concurrent: int
    # off, unavailable, or the watcher in use: wayland, xwayland_poll, x11, or poll.
    clipboard: str


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
    clipboard: ClipboardService | None = None


async def create_download(services: Services, body: AddDownload) -> Download:
    """Queue a download, as `POST /downloads` and the clipboard notification both do."""
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
    await run(services.store.add, row)
    services.bus.publish("download.added", id=row.id)
    services.queue.poke()
    return row


def public(row: Download) -> DownloadOut:
    """Everything a client may see. Stored headers never leave the daemon."""
    fields = row.model_dump(exclude={"headers_enc", "warnings_json"})
    return DownloadOut(**fields, warnings=json.loads(row.warnings_json))


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="sayld", version=VERSION)
    allowed = APP_ORIGINS | frozenset(services.config.dev_origins)
    # The webview runs on its own origin, so it needs CORS; the outer check runs first.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(allowed),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type"],
        max_age=600,
    )
    app.add_middleware(RejectWebOrigins, allowed=allowed)
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

    def changed(row: Download) -> DownloadOut:
        """Tell every client, so a pause from the CLI shows up in the desktop app."""
        services.bus.publish("download.updated", id=row.id, status=row.status)
        return public(row)

    @app.post("/downloads", dependencies=auth, status_code=201)
    async def add_download(body: AddDownload) -> DownloadOut:
        return public(await create_download(services, body))

    @app.post("/downloads/attach", dependencies=auth, status_code=201)
    async def attach_download(body: AttachDownload) -> DownloadOut:
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
    async def put_stream(row: Found, request: Request) -> DownloadOut:
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
    async def list_downloads(state: Status | None = None) -> list[DownloadOut]:
        return [public(row) for row in await run(store.downloads, state)]

    @app.get("/downloads/{download_id}", dependencies=auth)
    async def get_download(row: Found) -> DownloadOut:
        return public(row)

    @app.patch("/downloads/{download_id}", dependencies=auth)
    async def patch_download(row: Found, body: PatchDownload) -> DownloadOut:
        if body.speed_limit is not None:
            await queue.set_speed_limit(row, body.speed_limit)
        fields: dict[str, Any] = {}
        if body.priority is not None:
            fields["priority"] = body.priority
        if body.filename is not None:
            fields["filename"] = sanitize(body.filename)
        await run(store.update, row.id, **fields)
        return changed(await found(row.id))

    @app.delete("/downloads/{download_id}", dependencies=auth, status_code=204)
    async def delete_download(row: Found) -> None:
        await queue.cancel(row)
        await run(store.delete_download, row.id)
        services.bus.publish("download.removed", id=row.id)

    async def act(row: Download, action: Callable[[Download], Awaitable[None]]) -> DownloadOut:
        try:
            await action(row)
        except EngineError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return changed(await found(row.id))

    @app.post("/downloads/{download_id}/pause", dependencies=auth)
    async def pause(row: Found) -> DownloadOut:
        return await act(row, queue.pause)

    @app.post("/downloads/{download_id}/resume", dependencies=auth)
    async def resume(row: Found) -> DownloadOut:
        return await act(row, queue.resume)

    @app.post("/downloads/{download_id}/refresh-url", dependencies=auth)
    async def refresh_url(row: Found, body: RefreshUrl) -> DownloadOut:
        await queue.refresh_url(row, body.url, body.headers)
        return changed(await found(row.id))

    @app.post("/probe", dependencies=auth)
    async def probe(body: Probe) -> ProbeOut:
        name = await run(route, body.url)
        try:
            result: ProbeResult = await services.engines[name].probe(body.url, body.headers)
        except EngineError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        return ProbeOut(**result.__dict__, engine=name)

    @app.get("/info", dependencies=auth)
    async def info() -> InfoOut:
        return InfoOut(
            version=VERSION,
            default_dir=services.default_dir,
            max_concurrent=services.config.max_concurrent,
            clipboard=services.clipboard.state if services.clipboard else "off",
        )

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
        settings = await run(store.settings)
        if services.clipboard is not None:
            await services.clipboard.apply(settings)
        return settings

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
