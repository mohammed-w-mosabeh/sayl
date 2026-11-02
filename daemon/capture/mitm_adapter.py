"""Detach a flow's browser side from its upstream side. Pinned to mitmproxy 12.3.1.

This is the only file allowed to touch non-public mitmproxy APIs. To upgrade, follow
"Upgrading mitmproxy" in docs/SPEC.md.

Stock mitmproxy cannot do what the handoff needs:

* A response replaced without `stream` is sent only after the whole upstream body is buffered.
* A streamed response stays open until the upstream ends. On HTTP/1.1 that blocks the browser's
  next request on the connection. On HTTP/2 the browser cancels the stream when it navigates, and
  mitmproxy forwards that cancel upstream, which destroys a single-use download.

So a flow with `flow.metadata[HANDOFF]` set ends the browser side immediately, then either cancels
the upstream (`ABORT`, the file will be fetched again) or keeps reading it (`DETACH`).
"""

from collections.abc import Generator
from typing import Any, cast

import mitmproxy.version
from mitmproxy.http import Response
from mitmproxy.proxy.layers import http as http_layer
from mitmproxy.proxy.layers.http import HttpLayer, HttpStream
from mitmproxy.proxy.layers.http._events import (
    ErrorCode,
    RequestProtocolError,
    ResponseEndOfMessage,
    ResponseHeaders,
)

SUPPORTED_VERSION = "12.3.1"
HANDOFF = "sayl_handoff"
ABORT = "abort"
DETACH = "detach"

_start_response_stream = HttpStream.start_response_stream
_flow_done = HttpStream.flow_done
_handle_protocol_error = HttpStream.handle_protocol_error
_get_connection = HttpLayer.get_connection

# HTTP/1.1 upstreams still carrying a detached body. mitmproxy assumes a browser's requests are
# serial, so without this the next request would be sent down the busy connection.
_busy: set[Any] = set()

Commands = Generator[Any, Any, None]


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
        cancel = RequestProtocolError(
            self.stream_id, "handed off to the download engine", code=ErrorCode.CANCEL
        )
        yield http_layer.SendHttp(cancel, self.context.server)
        # state_errored swallows events; upstream data already queued during the hook still arrives.
        self.client_state = self.server_state = self.state_errored
        self.flow.live = False
        yield http_layer.DropStream(self.stream_id)
    else:
        if self.context.server.alpn != b"h2":
            _busy.add(self.context.server)
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
