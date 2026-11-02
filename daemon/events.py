"""Fan-out of daemon events to WebSocket clients. A slow client loses events, not the daemon."""

import asyncio
from typing import Any

QUEUE_SIZE = 256


class EventBus:
    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_SIZE)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    def publish(self, event: str, **data: Any) -> None:
        for queue in self.subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait({"event": event, **data})
