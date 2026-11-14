"""REST API and WebSocket events. Every route requires the bearer token."""

import asyncio
import contextlib
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from daemon import batch, categories, checksum, cookies, history, stats
from daemon.capture.manager import CaptureManager
from daemon.config import Config
from daemon.credentials import Credentials
from daemon.engines.base import Engine, EngineError, ProbeResult, StreamInfo
from daemon.engines.hybrid import may_resume
from daemon.events import EventBus
from daemon.filenames import filename_from_response, sanitize
from daemon.grabber import Grab, Grabber, GrabOptions
from daemon.models import (
    Category,
    Download,
    EngineName,
    PostAction,
    Schedule,
    ScheduleAction,
    Status,
)
from daemon.outbound import OutboundProxy
from daemon.proxy import ProxyError
from daemon.proxy import parse as parse_proxy
from daemon.queue import QueueManager
from daemon.router import route
from daemon.scheduler import Scheduler, validate
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
    checksum: str = ""  # algo:hex, or bare hex; checked when the download completes.

    @field_validator("checksum")
    @classmethod
    def _checksum(cls, value: str) -> str:
        if value:
            checksum.parse(value)  # A ValueError becomes a 422.
        return value


class BatchIn(BaseModel):
    """Links taken as they are, patterns such as file[001-100].jpg that expand, or both."""

    urls: list[str] = []
    patterns: list[str] = []
    dest_dir: str = ""
    category: str | None = None
    referer: str = ""
    cookies: str = ""
    headers: dict[str, str] = {}
    start_paused: bool = False
    speed_limit: int = 0
    cookies_from_browser: str = ""


class BatchOut(BaseModel):
    ids: list[str]


BATCH_SCHEMES = ("http://", "https://", "ftp://", "magnet:")


class GrabIn(BaseModel):
    url: str
    depth: int = 2  # Links away from the start page; at most 5.
    extensions: list[str] = []  # Empty: every common download type.
    min_size: int = 0
    max_size: int = 0  # 0: no limit.
    max_pages: int = 200  # At most 2000.
    cookies: str = ""
    referer: str = ""
    cookies_from_browser: str = ""


class GrabFile(BaseModel):
    url: str
    size: int | None
    page: str


class GrabOut(BaseModel):
    id: str
    url: str
    status: str  # running, done, failed, canceled
    error: str
    pages: int
    skipped_robots: int
    files: list[GrabFile]


class VerifyIn(BaseModel):
    checksum: str = ""  # Replaces the stored one; empty uses the stored one.


class VerifyOut(BaseModel):
    ok: bool
    algo: str
    expected: str
    actual: str


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


class CaptureUpdate(BaseModel):
    enabled: bool
    system_proxy: bool = False


class CaInstall(BaseModel):
    system: bool = True


class MediaReport(BaseModel):
    kind: str  # manifest or page
    page_url: str
    url: str
    drm: bool = False
    extractor: str = ""


class BypassReport(BaseModel):
    host: str
    reason: str = "tls"


class ScheduleIn(BaseModel):
    name: str
    cron: str  # Five crontab fields, or six with seconds first.
    action: ScheduleAction
    params: dict[str, Any] = {}  # speed_limit: {"limit": B/s}; command: {"command": [argv]}.
    post_action: PostAction = PostAction.NONE
    enabled: bool = True

    def row(self, key: str | None = None) -> Schedule:
        values: dict[str, Any] = {
            "name": self.name,
            "cron": self.cron,
            "action": self.action.value,
            "params_json": json.dumps(self.params),
            "post_action": self.post_action.value,
            "enabled": self.enabled,
        }
        return Schedule(**values) if key is None else Schedule(id=key, **values)


class ScheduleOut(BaseModel):
    id: str
    name: str
    cron: str
    action: ScheduleAction
    params: dict[str, Any]
    post_action: PostAction
    enabled: bool
    next_run: str | None  # ISO 8601 in local time, or None when disabled.


class QueueOut(BaseModel):
    running: bool
    post_action: PostAction  # The one armed or counting down, else none.
    post_action_armed: bool  # A start_queue schedule's post action waits for the queue.
    post_action_pending: bool  # It is counting down and can still be cancelled.


class CaptureOut(BaseModel):
    # off, starting, running, or failed.
    state: str
    error: str
    port: int
    system_proxy: bool
    ca: dict[str, Any] | None


class CredentialIn(BaseModel):
    username: str = Field(min_length=1, max_length=512)
    password: str = Field(max_length=4096)


class CredentialOut(BaseModel):
    """A stored login, without its password: that never leaves the daemon."""

    host: str
    username: str


class HistorySave(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class HistoryImportOut(BaseModel):
    added: int
    skipped: int


class DayBytes(BaseModel):
    day: str
    bytes: int


class HostBytes(BaseModel):
    host: str
    bytes: int


class CategoryBytes(BaseModel):
    category: str
    bytes: int


class StatsOut(BaseModel):
    by_status: dict[str, int]
    completed: int
    completed_bytes: int
    per_day: list[DayBytes]
    top_hosts: list[HostBytes]
    by_category: list[CategoryBytes]
    average_speed: int


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
    capture: CaptureManager | None = None
    scheduler: Scheduler | None = None
    grabber: Grabber | None = None
    outbound: OutboundProxy | None = None


def checksum_fields(value: str) -> dict[str, str]:
    if not value:
        return {}
    algo, digest = checksum.parse(value)
    return {"checksum_algo": algo, "checksum_expected": digest}


async def create_download(services: Services, body: AddDownload) -> Download:
    """Queue a download, as `POST /downloads` and the clipboard notification both do."""
    headers = dict(body.headers)
    known = await run(services.store.all, Category)
    name = sanitize(body.filename) if body.filename else filename_from_response(body.url, {})
    category = categories.by_id(known, body.category) or (
        None if body.category else categories.match(known, name)
    )
    if body.cookies:
        headers["Cookie"] = body.cookies
    if body.referer:
        headers["Referer"] = body.referer
    if body.cookies_from_browser and not body.url.startswith("magnet:"):
        imported = await cookies.cookie_header(body.cookies_from_browser, body.url)
        if imported:
            headers["Cookie"] = cookies.merge(headers.get("Cookie", ""), imported)
    row = Download(
        url=body.url,
        page_url=body.page_url,
        filename=sanitize(body.filename) if body.filename else "",
        dest_dir=categories.folder(category, body.dest_dir, services.default_dir),
        category_id=category.id if category else body.category,
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
    if body.checksum:
        row.checksum_algo, row.checksum_expected = checksum.parse(body.checksum)
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
        try:
            return public(await create_download(services, body))
        except cookies.CookieError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error

    @app.get("/browsers", dependencies=auth)
    async def list_browsers() -> list[str]:
        """Browsers with a profile here, for cookies_from_browser."""
        return await asyncio.to_thread(cookies.browsers, Path.home())

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
        filename = sanitize(body.filename) if body.filename else ""
        filename = filename or filename_from_response(body.url, lower)
        category = categories.match(
            await run(store.all, Category), filename, lower.get("content-type", "")
        )
        row = Download(
            url=body.url,
            final_url=body.url,
            page_url=body.page_url,
            filename=filename,
            dest_dir=categories.folder(category, body.dest_dir, services.default_dir),
            category_id=category.id if category else None,
            engine=EngineName.HYBRID,
            status=Status.PROBING,
            total_bytes=info.total or 0,
            etag=info.etag,
            last_modified=info.last_modified,
            resumable=may_resume(info),
            headers_enc=services.vault.seal(body.request_headers),
        )
        row.engine_ref = row.id
        await run(store.add, row)
        services.bus.publish("capture.detected", kind="download", id=row.id, filename=row.filename)
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

    @app.post("/batch", dependencies=auth, status_code=201)
    async def add_batch(body: BatchIn) -> BatchOut:
        """Queue every link, in order, once each, with the same options."""
        links = [url.strip() for url in body.urls]
        try:
            for pattern in body.patterns:
                links += batch.expand(pattern.strip()) if pattern.strip() else []
                if len(links) > batch.MAX_ITEMS:
                    raise ValueError(f"more than {batch.MAX_ITEMS} links")
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        unique = list(dict.fromkeys(link for link in links if link))
        if not unique:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no links")
        if len(unique) > batch.MAX_ITEMS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"more than {batch.MAX_ITEMS} links"
            )
        refused = [link for link in unique if not link.lower().startswith(BATCH_SCHEMES)]
        if refused:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"not a download link: {refused[0][:200]}"
            )
        shared = body.model_dump(exclude={"urls", "patterns"})
        try:
            ids = [
                (await create_download(services, AddDownload(url=link, **shared))).id
                for link in unique
            ]
        except cookies.CookieError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        return BatchOut(ids=ids)

    def grabber() -> Grabber:
        if services.grabber is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no site grabber")
        return services.grabber

    def grab_out(grab: Grab) -> GrabOut:
        return GrabOut(
            id=grab.id,
            url=grab.options.url,
            status=grab.status,
            error=grab.error,
            pages=grab.pages,
            skipped_robots=grab.skipped_robots,
            files=[GrabFile(url=f.url, size=f.size, page=f.page) for f in grab.files],
        )

    @app.post("/grab-site", dependencies=auth, status_code=202)
    async def grab_site(body: GrabIn) -> GrabOut:
        """Start crawling one site for files. Poll GET /grab-site/{id}, then queue with /batch."""
        if urlsplit(body.url).scheme not in ("http", "https"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "an http(s) link is needed")
        headers: dict[str, str] = {"Referer": body.referer} if body.referer else {}
        try:
            imported = (
                await cookies.cookie_header(body.cookies_from_browser, body.url)
                if body.cookies_from_browser
                else ""
            )
        except cookies.CookieError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        if body.cookies or imported:
            headers["Cookie"] = cookies.merge(body.cookies, imported)
        options = GrabOptions(
            url=body.url,
            depth=body.depth,
            extensions=tuple(e.strip().lower().lstrip(".") for e in body.extensions if e.strip()),
            min_size=max(0, body.min_size),
            max_size=max(0, body.max_size),
            max_pages=body.max_pages,
            headers=headers,
        )
        grab = grabber().start(options)
        if grab.task is not None:
            grab.task.add_done_callback(
                lambda _: services.bus.publish("grab.updated", id=grab.id, status=grab.status)
            )
        return grab_out(grab)

    @app.get("/grab-site/{grab_id}", dependencies=auth)
    async def grab_status(grab_id: str) -> GrabOut:
        grab = grabber().grabs.get(grab_id)
        if grab is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such grab")
        return grab_out(grab)

    @app.delete("/grab-site/{grab_id}", dependencies=auth, status_code=204)
    async def grab_cancel(grab_id: str) -> None:
        if not await grabber().cancel(grab_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such grab")

    @app.post("/downloads/{download_id}/verify", dependencies=auth)
    async def verify(row: Found, body: VerifyIn) -> VerifyOut:
        """Hash the finished file and compare. The result is stored as checksum_ok."""
        if row.status is not Status.COMPLETED:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "only a completed download can be verified"
            )
        try:
            fields = checksum_fields(body.checksum) or {
                "checksum_algo": row.checksum_algo,
                "checksum_expected": row.checksum_expected,
            }
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        if not fields["checksum_expected"]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "no checksum to compare with"
            )
        await run(store.update, row.id, **fields)
        try:
            result = await queue.verify(row.id)
        except OSError as error:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"cannot read the file: {error}"
            ) from error
        return VerifyOut(
            ok=result.ok,
            algo=fields["checksum_algo"],
            expected=fields["checksum_expected"],
            actual=result.actual,
        )

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
        if "outbound_proxy" in values:
            try:
                parse_proxy(str(values["outbound_proxy"] or ""))
            except ProxyError as error:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        await run(store.put_settings, values)
        if "outbound_proxy" in values and services.outbound is not None:
            await services.outbound.apply(str(values["outbound_proxy"] or ""))
        if isinstance(values.get("speed_limit"), int):
            await queue.set_global_speed_limit(values["speed_limit"])
        if values.get("clear_host_policy"):
            await run(store.clear_host_policy)
        settings = await run(store.settings)
        if services.clipboard is not None:
            await services.clipboard.apply(settings)
        services.bus.publish("settings.updated", keys=sorted(values))
        return settings

    def capture() -> CaptureManager:
        if services.capture is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "capture is not available")
        return services.capture

    @app.get("/capture", dependencies=auth)
    async def capture_status() -> CaptureOut:
        return CaptureOut(**await run(capture().status))

    @app.put("/capture", dependencies=auth)
    async def capture_update(body: CaptureUpdate) -> CaptureOut:
        """Start or stop sayl-capture, and point the system proxy at it if asked."""
        await capture().apply(enabled=body.enabled, system_proxy=body.system_proxy)
        return CaptureOut(**await run(capture().status))

    @app.post("/capture/ca", dependencies=auth)
    async def capture_install_ca(body: CaInstall) -> dict[str, Any]:
        """Create the CA if needed and trust it. The system store asks for a password."""
        return (await capture().install_ca(body.system)).as_dict()

    @app.post("/capture/uninstall", dependencies=auth)
    async def capture_uninstall() -> dict[str, Any]:
        """Restore the proxy settings, stop capturing, remove the CA from every store."""
        return (await capture().uninstall()).as_dict()

    @app.get("/capture/media", dependencies=auth)
    async def capture_media() -> list[dict[str, Any]]:
        return capture().media_list()

    @app.post("/capture/media", dependencies=auth, status_code=204)
    async def capture_report_media(body: MediaReport) -> None:
        capture().report_media(body.model_dump())

    @app.post("/capture/bypass", dependencies=auth, status_code=204)
    async def capture_bypass(body: BypassReport) -> None:
        await capture().add_bypass(body.host)

    def crud(path: str, model: type[Category]) -> None:
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

    def scheduler() -> Scheduler:
        if services.scheduler is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no scheduler")
        return services.scheduler

    def schedule_out(row: Schedule) -> ScheduleOut:
        return ScheduleOut(
            id=row.id,
            name=row.name,
            cron=row.cron,
            action=ScheduleAction(row.action),
            params=json.loads(row.params_json),
            post_action=PostAction(row.post_action),
            enabled=row.enabled,
            next_run=scheduler().next_run(row.id),
        )

    async def save_schedule(row: Schedule) -> ScheduleOut:
        try:
            validate(row)
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        await run(store.put, row)
        await scheduler().reload()
        services.bus.publish("schedules.updated", id=row.id)
        return schedule_out(row)

    @app.get("/schedules", dependencies=auth)
    async def list_schedules() -> list[ScheduleOut]:
        return [schedule_out(row) for row in await run(store.all, Schedule)]

    @app.post("/schedules", dependencies=auth, status_code=201)
    async def create_schedule(body: ScheduleIn) -> ScheduleOut:
        return await save_schedule(body.row())

    @app.put("/schedules/{key}", dependencies=auth)
    async def replace_schedule(key: str, body: ScheduleIn) -> ScheduleOut:
        if await run(store.get, Schedule, key) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        return await save_schedule(body.row(key))

    @app.delete("/schedules/{key}", dependencies=auth, status_code=204)
    async def delete_schedule(key: str) -> None:
        if not await run(store.remove, Schedule, key):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        await scheduler().reload()
        services.bus.publish("schedules.updated", id=key)

    @app.post("/schedules/{key}/run", dependencies=auth, status_code=204)
    async def run_schedule(key: str) -> None:
        """Do what the schedule does now, post action included."""
        if await run(store.get, Schedule, key) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        await scheduler().fire(key)

    def queue_out() -> QueueOut:
        pending = scheduler().pending is not None
        armed = queue.post_action is not None
        return QueueOut(
            running=queue.running,
            post_action=scheduler().armed if armed or pending else PostAction.NONE,
            post_action_armed=queue.post_action is not None,
            post_action_pending=pending,
        )

    @app.get("/queue", dependencies=auth)
    async def queue_state() -> QueueOut:
        return queue_out()

    @app.post("/queue/start", dependencies=auth)
    async def queue_start() -> QueueOut:
        await queue.start_queue()
        return queue_out()

    @app.post("/queue/stop", dependencies=auth)
    async def queue_stop() -> QueueOut:
        await queue.stop_queue()
        return queue_out()

    @app.post("/queue/post-action/cancel", dependencies=auth)
    async def cancel_post_action() -> QueueOut:
        """Cancel a shutdown, suspend, or command before it runs."""
        await scheduler().cancel_post_action()
        return queue_out()

    def credentials() -> Credentials:
        if queue.credentials is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no credential store")
        return queue.credentials

    @app.get("/credentials", dependencies=auth)
    async def list_credentials() -> list[CredentialOut]:
        return [CredentialOut(host=h, username=u) for h, u in credentials().hosts()]

    @app.put("/credentials/{host}", dependencies=auth)
    async def put_credential(host: str, body: CredentialIn) -> CredentialOut:
        """Store or replace the login for a host. The password can be set, never read."""
        try:
            key = await credentials().set(host, body.username, body.password)
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        return CredentialOut(host=key, username=body.username)

    @app.delete("/credentials/{host}", dependencies=auth, status_code=204)
    async def delete_credential(host: str) -> None:
        if not await credentials().remove(host):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    @app.get("/history/export", dependencies=auth)
    async def export_history() -> dict[str, Any]:
        """Every download, without headers, cookies, or credentials."""
        rows = await run(store.downloads)
        return history.export(rows, await run(store.all, Category))

    @app.post("/history/save", dependencies=auth, status_code=204)
    async def save_history(body: HistorySave) -> None:
        """Write the export to a file the user chose in a save dialog: an absolute .json path
        in an existing folder. The app's webview has no file access of its own."""
        target = Path(body.path)
        if not target.is_absolute() or target.suffix.lower() != ".json":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "needs an absolute .json path"
            )
        document = history.export(await run(store.downloads), await run(store.all, Category))
        text = json.dumps(document, indent=1, ensure_ascii=False) + "\n"
        try:
            await asyncio.to_thread(target.write_text, text)
        except OSError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error

    @app.post("/history/import", dependencies=auth)
    async def import_history(document: dict[str, Any]) -> HistoryImportOut:
        """Add the downloads of an export. Nothing starts; existing ids are skipped."""
        try:
            rows = history.parse(document, await run(store.all, Category))
        except history.HistoryError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        added = await run(store.import_downloads, rows)
        if added:
            services.bus.publish("history.imported", added=added)
        return HistoryImportOut(added=added, skipped=len(rows) - added)

    @app.get("/stats", dependencies=auth)
    async def statistics() -> StatsOut:
        rows = await run(store.downloads)
        today = datetime.now(UTC).date()
        result = stats.compute(rows, await run(store.all, Category), today)
        return StatsOut.model_validate(result)

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
