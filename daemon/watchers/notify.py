"""Actionable desktop notifications over D-Bus (org.freedesktop.Notifications).

The daemon sends them itself, so a copied link is offered even when the desktop app is closed.
jeepney talks to the bus on the event loop; nothing here blocks.
"""

import asyncio
import contextlib
import html
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass

from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.asyncio import DBusRouter, Proxy, open_dbus_router
from jeepney.low_level import HeaderFields, Message, MessageType

log = logging.getLogger(__name__)

PATH = "/org/freedesktop/Notifications"
NAME = "org.freedesktop.Notifications"
SERVER = DBusAddress(PATH, bus_name=NAME, interface=NAME)
TIMEOUT_MS = 10_000
BODY_LENGTH = 160

DOWNLOAD = "download"
IGNORE = "ignore_site"

Action = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class Texts:
    summary: str
    download: str
    ignore: str


TEXTS = {
    "en": Texts("Download this link?", "Download", "Ignore this site"),
    "ar": Texts("أتريد تنزيل هذا الرابط؟", "تنزيل", "تجاهل هذا الموقع"),
}


def texts(language: object) -> Texts:
    return TEXTS.get(str(language), TEXTS["en"])


def shorten(link: str) -> str:
    return link if len(link) <= BODY_LENGTH else link[: BODY_LENGTH - 1] + "…"


class Notifier:
    """Offers links and reports which button was pressed: `on_action(action, link)`."""

    def __init__(self, on_action: Action) -> None:
        self.on_action = on_action
        self.pending: dict[int, str] = {}
        self.router: DBusRouter | None = None
        self.stack = AsyncExitStack()
        self.listener: asyncio.Task[None] | None = None

    async def start(self, env: Mapping[str, str]) -> bool:
        """Connect to the session bus. False when there is none: offers are then skipped."""
        address = env.get("DBUS_SESSION_BUS_ADDRESS", "")
        if not address:
            return False
        try:
            router = await self.stack.enter_async_context(open_dbus_router(address))
            rule = MatchRule(type="signal", interface=NAME, path=PATH)
            await Proxy(message_bus, router).AddMatch(rule)
            signals = self.stack.enter_context(router.filter(rule, bufsize=64))
        except (OSError, ValueError, KeyError) as error:
            log.warning("no session bus for notifications: %s", error)
            await self.stack.aclose()
            self.router = None
            return False
        self.router = router
        self.listener = asyncio.create_task(self._listen(signals))
        return True

    async def stop(self) -> None:
        if self.listener:
            self.listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.listener
        await self.stack.aclose()
        self.router = None

    async def offer(self, link: str, language: object = "en") -> None:
        if self.router is None:
            return
        words = texts(language)
        message = new_method_call(
            SERVER,
            "Notify",
            "susssasa{sv}i",
            (
                "Sayl",
                0,
                "folder-download",
                words.summary,
                html.escape(shorten(link)),  # Servers may read the body as markup.
                [DOWNLOAD, words.download, IGNORE, words.ignore],
                {"urgency": ("y", 1), "category": ("s", "transfer")},
                TIMEOUT_MS,
            ),
        )
        try:
            reply = await asyncio.wait_for(self.router.send_and_get_reply(message), 5)
        except (TimeoutError, OSError) as error:
            log.warning("notification failed: %s", error)
            return
        if reply.header.message_type is MessageType.error:
            log.warning("no notification server: %s", reply.header.fields)
            return
        self.pending[int(reply.body[0])] = link

    async def _listen(self, signals: "asyncio.Queue[Message]") -> None:
        while True:
            message = await signals.get()
            member = message.header.fields.get(HeaderFields.member)
            body = message.body
            if member == "ActionInvoked" and body[1] in (DOWNLOAD, IGNORE):
                link = self.pending.pop(int(body[0]), None)
                if link is not None:
                    try:
                        await self.on_action(str(body[1]), link)
                    except Exception:
                        log.exception("clipboard action failed")
            elif member == "NotificationClosed":
                self.pending.pop(int(body[0]), None)
