"""Detach a flow's browser side from its upstream, with back-pressure. Pinned to mitmproxy 12.3.1.

This is the only file allowed to touch non-public mitmproxy APIs. To upgrade, follow
"Upgrading mitmproxy" in docs/SPEC.md.

Stock mitmproxy cannot do what the handoff needs:

* A response replaced without `stream` is sent only after the whole upstream body is buffered.
* A streamed response stays open until the upstream ends. On HTTP/1.1 that blocks the browser's
  next request on the connection. On HTTP/2 the browser cancels the stream when it navigates, and
  mitmproxy forwards that cancel upstream, which destroys a single-use download.
* The stream callback cannot slow the upstream down: mitmproxy reads every connection as fast
  as data arrives.
* Each browser connection may hold five upstreams per host, for their whole life. Detached
  downloads keep theirs, so a few of them would stall every later request to that site.

So a flow with `flow.metadata[HANDOFF]` set ends the browser side immediately, then either cancels
the upstream (`ABORT`) or keeps reading it (`DETACH`). While a detached body is live, its upstream
connection serves nothing else, HTTP/2 included, so it can be paused: every upstream reader goes
through a gate, and `pause`/`resume` close and open it. TCP flow control then holds the server.
`cancel` closes the connection once the download no longer needs it. A detached upstream
gives its slot in that per-host limit back (`_Slots`), since it serves the browser no more.
"""

import asyncio
import collections
import contextlib
from collections.abc import Generator
from typing import Any, cast

import mitmproxy.version
from mitmproxy.connection import Server
from mitmproxy.http import Response
from mitmproxy.proxy.layers import http as http_layer
from mitmproxy.proxy.layers.http import HttpLayer, HttpStream
from mitmproxy.proxy.layers.http._events import (
    ErrorCode,
    RequestProtocolError,
    ResponseEndOfMessage,
    ResponseHeaders,
)
from mitmproxy.proxy.server import ConnectionHandler

SUPPORTED_VERSION = "12.3.1"
HANDOFF = "sayl_handoff"
ABORT = "abort"
DETACH = "detach"

_start_response_stream = HttpStream.start_response_stream
_flow_done = HttpStream.flow_done
_handle_protocol_error = HttpStream.handle_protocol_error
_get_connection = HttpLayer.get_connection
_handle_connection = ConnectionHandler.handle_connection
_handler_init = ConnectionHandler.__init__
MAX_CONNS = 5  # mitmproxy's own limit per host and browser connection.

# Upstreams still carrying a detached body. mitmproxy would otherwise send the browser's next
# request down them: on HTTP/1.1 it assumes requests are serial, on HTTP/2 it multiplexes, and
# either way a paused download would stall that request.
_busy: set[Any] = set()

Commands = Generator[Any, Any, None]


class _Slots(asyncio.Semaphore):
    """mitmproxy's per-host connection limit, except that detached upstreams can leave it.

    mitmproxy holds a slot with `async with` for the life of a connection, so a detached one
    returns its slot early with `free`, and the release at its real end is then skipped.
    """

    def __init__(self) -> None:
        super().__init__(MAX_CONNS)
        self.skip = 0

    def free(self) -> None:
        super().release()

    def closing_freed(self) -> None:
        self.skip += 1

    def release(self) -> None:
        if self.skip:
            self.skip -= 1
            return
        super().release()


def _init(self: ConnectionHandler, *args: Any, **kwargs: Any) -> None:
    _handler_init(self, *args, **kwargs)
    self.max_conns = collections.defaultdict(_Slots)


class _Gate:
    """Wraps an upstream reader so reads wait while the download is paused."""

    def __init__(self, reader: Any, writer: Any, slots: _Slots | None = None) -> None:
        self.reader = reader
        self.writer = writer
        self.slots = slots
        self.freed = False
        self.open = asyncio.Event()
        self.open.set()

    def free_slot(self) -> None:
        if self.slots is not None and not self.freed:
            self.freed = True
            self.slots.free()

    async def read(self, size: int = -1) -> bytes:
        await self.open.wait()
        data: bytes = await self.reader.read(size)
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self.reader, name)


_gates: dict[Server, _Gate] = {}


async def _handle(self: ConnectionHandler, connection: Any) -> None:
    gate = None
    if isinstance(connection, Server):
        io = self.transports[connection]
        if io.reader is not None:
            slots = self.max_conns.get(connection.address) if connection.address else None
            gate = _Gate(io.reader, io.writer, slots if isinstance(slots, _Slots) else None)
            io.reader = cast(Any, gate)  # handle_connection reads this attribute once, here.
            _gates[connection] = gate
    try:
        await _handle_connection(self, connection)
    finally:
        if gate is not None:
            _gates.pop(connection, None)
            if gate.freed and gate.slots is not None:
                gate.slots.closing_freed()  # mitmproxy releases this slot next; it was freed.


def pause(server: Server) -> None:
    """Stop reading this upstream. The bytes already read are still delivered."""
    if gate := _gates.get(server):
        gate.open.clear()


def resume(server: Server) -> None:
    if gate := _gates.get(server):
        gate.open.set()


def paused(server: Server) -> bool:
    gate = _gates.get(server)
    return gate is not None and not gate.open.is_set()


def cancel(server: Server) -> None:
    """Close this upstream: the download finished without the rest of its body."""
    gate = _gates.get(server)
    if gate is None:
        return
    with contextlib.suppress(Exception):
        gate.writer.close()
    gate.open.set()  # Let the read loop see the closed connection and tear down normally.


def _start(self: HttpStream) -> Commands:
    mode = self.flow.metadata.get(HANDOFF)
    if mode is None:
        yield from _start_response_stream(self)
        return
    client = self.context.client
    response = ResponseHeaders(self.stream_id, cast(Response, self.flow.response), end_stream=False)
    yield http_layer.SendHttp(response, client)
    yield http_layer.SendHttp(ResponseEndOfMessage(self.stream_id), client)
    if mode == ABORT:
        cancel_event = RequestProtocolError(
            self.stream_id, "handed off to the download engine", code=ErrorCode.CANCEL
        )
        yield http_layer.SendHttp(cancel_event, self.context.server)
        # state_errored swallows events; upstream data already queued during the hook still arrives.
        self.client_state = self.server_state = self.state_errored
        self.flow.live = False
        yield http_layer.DropStream(self.stream_id)
    else:
        _busy.add(self.context.server)
        if gate := _gates.get(self.context.server):
            gate.free_slot()
        self.server_state = self.state_stream_response_body


def _done(self: HttpStream) -> Commands:
    if self.flow.metadata.get(HANDOFF) is None:
        yield from _flow_done(self)
        return
    _busy.discard(self.context.server)
    self.flow.live = False  # The browser side already received its end of message.
    yield http_layer.DropStream(self.stream_id)


def _error(self: HttpStream, event: Any) -> Commands:
    mode = self.flow.metadata.get(HANDOFF)
    if mode == DETACH and isinstance(event, RequestProtocolError):
        return  # The browser left. It has nothing to do with this body any more.
    if mode is not None:
        _busy.discard(self.context.server)
        self.client_state = self.state_errored  # Do not send this to a finished browser stream.
    yield from _handle_protocol_error(self, event)


def _connection(self: HttpLayer, event: Any, *, reuse: bool = True) -> Commands:
    if any(event.connection_spec_matches(connection) for connection in _busy):
        reuse = False
    yield from _get_connection(self, event, reuse=reuse)


def check_version() -> None:
    if mitmproxy.version.VERSION != SUPPORTED_VERSION:
        raise RuntimeError(
            f"mitm_adapter supports mitmproxy {SUPPORTED_VERSION} only, "
            f"found {mitmproxy.version.VERSION}. See 'Upgrading mitmproxy' in docs/SPEC.md."
        )


def install() -> None:
    check_version()
    HttpStream.start_response_stream = _start  # type: ignore[method-assign]
    HttpStream.flow_done = _done  # type: ignore[method-assign]
    HttpStream.handle_protocol_error = _error  # type: ignore[method-assign]
    HttpLayer.get_connection = _connection  # type: ignore[method-assign]
    ConnectionHandler.handle_connection = _handle  # type: ignore[method-assign]
    ConnectionHandler.__init__ = _init  # type: ignore[method-assign]
