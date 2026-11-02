"""Aria2Engine: a managed aria2c child process driven by JSON-RPC over WebSocket."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from daemon.engines.base import Engine, EngineError, Job, ProbeResult, Progress
from daemon.filenames import sanitize
from daemon.models import EngineName, Status

log = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 100
_STATES = {
    "active": Status.ACTIVE,
    "waiting": Status.QUEUED,
    "paused": Status.PAUSED,
    "complete": Status.COMPLETED,
    "error": Status.FAILED,
    "removed": Status.CANCELED,
}


def file_allocation(directory: Path) -> str:
    """falloc, or none on filesystems that do not support it."""
    try:
        with tempfile.NamedTemporaryFile(dir=directory) as probe:
            os.posix_fallocate(probe.fileno(), 0, 4096)
    except OSError:
        return "none"
    return "falloc"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def command_line(binary: str, port: int, secret: str, data: Path, allocation: str) -> list[str]:
    session = data / "aria2.session"
    arguments = [
        binary,
        "--enable-rpc",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}",
        "--continue=true",
        f"--save-session={session}",
        "--save-session-interval=30",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        f"--file-allocation={allocation}",
        f"--stop-with-process={os.getpid()}",  # Never outlive a killed daemon.
        "--quiet=true",
    ]
    if session.exists():
        arguments.append(f"--input-file={session}")
    return arguments


async def probe_url(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> ProbeResult:
    """HEAD, falling back to a ranged GET for servers that refuse it."""
    response = await client.head(url, headers=headers, follow_redirects=True)
    if response.status_code >= 400:
        ranged = {**headers, "Range": "bytes=0-0"}
        async with client.stream("GET", url, headers=ranged, follow_redirects=True) as stream:
            response = stream
    resumable = response.status_code == 206 or (
        response.headers.get("accept-ranges", "").lower() == "bytes"
    )
    content_range = response.headers.get("content-range", "")
    size = content_range.rpartition("/")[2] or response.headers.get("content-length", "")
    final = str(response.url)
    return ProbeResult(
        final,
        sanitize(final.split("?")[0].rsplit("/", 1)[-1]),
        int(size) if size.isdigit() else None,
        resumable,
    )


class Aria2Engine(Engine):
    name = EngineName.ARIA2

    def __init__(
        self,
        data: Path,
        *,
        binary: str | None = None,
        client: httpx.AsyncClient | None = None,
        on_restart: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.data = data
        self.binary = binary or shutil.which("aria2c")
        self.client = client or httpx.AsyncClient(timeout=30)
        self.on_restart = on_restart
        self.process: subprocess.Popen[bytes] | None = None
        self.socket: ClientConnection | None = None
        self.secret = ""
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.reader: asyncio.Task[None] | None = None
        self.watchdog: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        await self._spawn()
        self.watchdog = asyncio.create_task(self._watch())

    async def _spawn(self) -> None:
        if self.binary is None:
            raise EngineError("aria2c is not installed; run 'sayl doctor'")
        port = _free_port()
        self.secret = secrets.token_hex(16)
        allocation = await asyncio.to_thread(file_allocation, self.data)
        arguments = command_line(self.binary, port, self.secret, self.data, allocation)
        self.process = subprocess.Popen(arguments)  # noqa: S603, ASYNC220 - argument list
        for _ in range(CONNECT_ATTEMPTS):
            try:
                self.socket = await websockets.connect(f"ws://127.0.0.1:{port}/jsonrpc")
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            raise EngineError("aria2c did not start")
        self.reader = asyncio.create_task(self._read(self.socket))

    async def _read(self, connection: ClientConnection) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            async for message in connection:
                reply = json.loads(message)
                future = self.pending.pop(reply.get("id", ""), None)
                if future is None or future.done():
                    continue  # A notification. The queue polls status instead.
                if "error" in reply:
                    future.set_exception(EngineError(reply["error"]["message"]))
                else:
                    future.set_result(reply["result"])
        for future in self.pending.values():
            if not future.done():
                future.set_exception(EngineError("aria2c connection lost"))
        self.pending.clear()

    async def _watch(self) -> None:
        """Restart aria2c if it dies. Its session file brings back unfinished downloads."""
        while not self.stopping:
            await asyncio.sleep(0.5)
            if self.process is not None and self.process.poll() is not None:
                log.warning("aria2c exited, restarting")
                await self._spawn()
                if self.on_restart is not None:
                    await self.on_restart()

    async def stop(self) -> None:
        self.stopping = True
        if self.watchdog is not None:
            self.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog
        with contextlib.suppress(EngineError, OSError):
            await asyncio.wait_for(self.rpc("aria2.saveSession"), 5)
        if self.socket is not None:
            await self.socket.close()
        if self.reader is not None:
            await self.reader
        if self.process is not None:
            self.process.terminate()
            await asyncio.to_thread(self.process.wait, 10)
        await self.client.aclose()

    async def rpc(self, method: str, *params: Any) -> Any:
        if self.socket is None:
            raise EngineError("aria2c is not running")
        call = secrets.token_hex(8)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[call] = future
        body = {
            "jsonrpc": "2.0",
            "id": call,
            "method": method,
            "params": [f"token:{self.secret}", *params],
        }
        try:
            await self.socket.send(json.dumps(body))
        except websockets.ConnectionClosed as error:
            self.pending.pop(call, None)
            raise EngineError("aria2c connection lost") from error
        return await future

    async def probe(self, url: str, headers: dict[str, str]) -> ProbeResult:
        return await probe_url(self.client, url, headers)

    async def add(self, job: Job) -> str:
        options: dict[str, Any] = {
            "dir": job.dest_dir,
            "header": [f"{name}: {value}" for name, value in job.headers.items()],
        }
        if job.filename:
            options["out"] = sanitize(job.filename)
        if job.speed_limit:
            options["max-download-limit"] = str(job.speed_limit)
        return str(await self.rpc("aria2.addUri", [job.url], options))

    async def pause(self, engine_id: str) -> None:
        await self.rpc("aria2.forcePause", engine_id)

    async def resume(self, engine_id: str) -> None:
        await self.rpc("aria2.unpause", engine_id)

    async def cancel(self, engine_id: str) -> None:
        await self.rpc("aria2.forceRemove", engine_id)

    async def set_speed_limit(self, engine_id: str, limit: int) -> None:
        await self.rpc("aria2.changeOption", engine_id, {"max-download-limit": str(limit)})

    async def set_global_speed_limit(self, limit: int) -> None:
        await self.rpc("aria2.changeGlobalOption", {"max-overall-download-limit": str(limit)})

    async def refresh_url(self, engine_id: str, new_url: str) -> None:
        details = await self.rpc("aria2.tellStatus", engine_id, ["files"])
        old = sorted({entry["uri"] for entry in details["files"][0]["uris"]})
        await self.rpc("aria2.changeUri", engine_id, 1, old, [new_url])

    async def status(self, engine_id: str) -> Progress:
        details = await self.rpc("aria2.tellStatus", engine_id)
        files = details.get("files") or [{}]
        return Progress(
            _STATES[details["status"]],
            int(details["totalLength"]),
            int(details["completedLength"]),
            int(details["downloadSpeed"]),
            int(details.get("connections", 0)),
            Path(files[0].get("path", "")).name,
            details.get("errorMessage", ""),
        )
