import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from daemon.db import make_engine, migrate
from daemon.events import EventBus
from daemon.i18n import t
from daemon.models import Download
from daemon.store import Store
from daemon.watchers.base import Watcher, WatcherUnavailable, text_type
from daemon.watchers.filter import ClipFilter, as_link, ignored
from daemon.watchers.notify import Notifier, shorten
from daemon.watchers.service import ClipboardService
from daemon.watchers.session import Backend, detect
from daemon.watchers.wayland import WaylandWatcher
from daemon.watchers.x11 import X11Watcher
from daemon.watchers.xwayland_poll import PollWatcher
from tests.clipboard_env import (
    FakeNotifications,
    copy_x11,
    headless_kwin,
    headless_sway,
    private_bus,
    xvfb,
)
from tests.origin import Origin
from tests.stack import running_stack

LINK = "https://releases.example.com/ubuntu.iso"


# Filtering


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (LINK, LINK),
        (f"  {LINK}\n", LINK),
        ("ftp://mirror.example/a.tar", "ftp://mirror.example/a.tar"),
        ("magnet:?xt=urn:btih:abc&dn=x", "magnet:?xt=urn:btih:abc&dn=x"),
        ("HTTPS://Example.com/a", "HTTPS://Example.com/a"),
        ("magnet:?dn=no-hash", None),
        ("javascript:alert(1)", None),
        ("file:///etc/passwd", None),
        ("https://", None),
        ("see https://example.com/a", None),
        (f"{LINK}\n{LINK}2", None),
        ("https://example.com/" + "a" * 5000, None),
        ("http://[broken", None),
        ("", None),
    ],
)
def test_as_link(text: str, expected: str | None) -> None:
    assert as_link(text) == expected


def test_ignore_list_matches_hosts_subdomains_and_globs() -> None:
    patterns = ["bank.example", "*.pdf", " "]
    assert ignored("https://bank.example/login", patterns)
    assert ignored("https://www.bank.example/x", patterns)
    assert not ignored("https://notbank.example/x", patterns)
    assert ignored("https://cdn.example/report.PDF", patterns)
    assert not ignored(LINK, patterns)


def test_filter_primes_debounces_and_ignores() -> None:
    now = [100.0]
    clip = ClipFilter(clock=lambda: now[0])
    clip.prime(LINK)
    assert clip.accept(LINK) is None  # On the clipboard before watching began.
    assert clip.accept("hello") is None
    assert clip.accept("https://a.example/1") == "https://a.example/1"
    assert clip.accept("other") is None
    now[0] += 5
    assert clip.accept("https://a.example/1") is None  # Within ten seconds.
    clip.accept("other")
    now[0] += 10
    assert clip.accept("https://a.example/1") == "https://a.example/1"
    clip.patterns = ("a.example",)
    assert clip.accept("https://a.example/2") is None


def test_secrets_and_non_text_are_never_read() -> None:
    assert text_type(["TARGETS", "UTF8_STRING", "x-kde-passwordManagerHint"]) is None
    assert text_type(["image/png"]) is None
    assert text_type(["text/plain", "UTF8_STRING"]) == "UTF8_STRING"
    assert text_type(["text/plain;charset=utf-8"]) == "text/plain;charset=utf-8"


def test_notification_body_is_short() -> None:
    assert shorten("x" * 500).endswith("…")
    assert len(shorten("x" * 500)) == 160


# Session detection


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "KDE"},
            Backend.WAYLAND,
        ),
        (
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "sway"},
            Backend.WAYLAND,
        ),
        (
            {
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "w",
                "DISPLAY": ":0",
                "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            },
            Backend.XWAYLAND_POLL,
        ),
        (
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "GNOME"},
            Backend.NONE,
        ),
        ({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "WAYLAND_DISPLAY": "w"}, Backend.X11),
        ({"DISPLAY": ":0"}, Backend.X11),
        ({"WAYLAND_DISPLAY": "w"}, Backend.WAYLAND),
        ({"XDG_SESSION_TYPE": "tty"}, Backend.NONE),
        ({}, Backend.NONE),
    ],
)
def test_detect(env: dict[str, str], expected: Backend) -> None:
    assert detect(env) is expected


# The service, with a scripted clipboard


class ScriptedWatcher(Watcher):
    name = "scripted"

    def __init__(self, env: Mapping[str, str]) -> None:
        super().__init__(env)
        self.text: str | None = env.get("initial")
        self.signals: asyncio.Queue[None] = asyncio.Queue()

    async def read(self) -> str | None:
        return self.text

    async def copy(self, text: str | None) -> None:
        self.text = text
        self.signals.put_nowait(None)
        await settle()

    async def changes(self) -> AsyncIterator[None]:
        while True:
            yield await self.signals.get()


class Unavailable(Watcher):
    name = "broken"

    async def read(self) -> str | None:
        return None

    async def changes(self) -> AsyncIterator[None]:
        raise WatcherUnavailable("not here")
        yield None


class FakeNotifier:
    def __init__(self) -> None:
        self.offers: list[tuple[str, object]] = []

    async def start(self, env: Mapping[str, str]) -> bool:
        return True

    async def stop(self) -> None:
        return None

    async def offer(self, link: str, language: object = "en") -> None:
        self.offers.append((link, language))


def make_store(tmp_path: Path) -> Store:
    migrate(tmp_path / "db.sqlite")
    return Store(make_engine(tmp_path / "db.sqlite"))


async def settle() -> None:
    """Long enough for the service to read, check the store on its thread, and offer."""
    await asyncio.sleep(0.1)


async def service_with(
    tmp_path: Path, *watchers: type[Watcher], env: dict[str, str] | None = None
) -> tuple[ClipboardService, list[str], FakeNotifier]:
    added: list[str] = []

    async def add(link: str) -> None:
        added.append(link)

    service = ClipboardService(
        make_store(tmp_path),
        EventBus(),
        add,
        {"DISPLAY": ":9", **(env or {})},
        chains={Backend.X11: watchers},
        which=lambda tool: "/usr/bin/" + tool,
    )
    notifier = FakeNotifier()
    service.notifier = notifier  # type: ignore[assignment]
    return service, added, notifier


async def test_offers_new_links_once_and_skips_the_rest(tmp_path: Path) -> None:
    created: list[ScriptedWatcher] = []

    class Recording(ScriptedWatcher):
        def __init__(self, env: Mapping[str, str]) -> None:
            super().__init__(env)
            created.append(self)

    service, _, notifier = await service_with(tmp_path, Recording, env={"initial": LINK})
    events = service.bus.subscribe()
    await service.start()
    await settle()
    watcher = created[0]
    assert service.state == "scripted"

    await watcher.copy(LINK)  # Was there before the daemon started.
    await watcher.copy("not a link")
    await watcher.copy(None)  # A password manager's secret: nothing is read.
    await watcher.copy("https://a.example/f.zip")
    await watcher.copy("https://a.example/f.zip")
    await settle()
    service.store.add(Download(url="https://a.example/known.bin"))
    await watcher.copy("https://a.example/known.bin")  # Already in Sayl.
    await settle()

    assert notifier.offers == [("https://a.example/f.zip", "en")]
    assert events.get_nowait() == {"event": "clipboard.detected", "url": "https://a.example/f.zip"}
    await service.stop()


async def test_settings_disable_watching_and_set_ignores_and_language(tmp_path: Path) -> None:
    created: list[ScriptedWatcher] = []

    class Recording(ScriptedWatcher):
        def __init__(self, env: Mapping[str, str]) -> None:
            super().__init__(env)
            created.append(self)

    service, _, notifier = await service_with(tmp_path, Recording)
    await service.start()
    await settle()
    await service.apply({"clipboard_ignore": ["a.example"], "ui": {"language": "ar"}})
    await created[0].copy("https://a.example/x")
    await created[0].copy("https://b.example/x")
    await settle()
    assert notifier.offers == [("https://b.example/x", "ar")]

    await service.apply({"clipboard_enabled": False})
    assert (service.task, service.state) == (None, "off")
    await service.apply({})
    await settle()
    assert len(created) == 2  # Enabled again: a fresh watcher.
    await service.stop()


async def test_falls_back_to_the_next_watcher(tmp_path: Path) -> None:
    service, _, _ = await service_with(tmp_path, Unavailable, ScriptedWatcher)
    await service.start()
    await settle()
    assert service.state == "scripted"
    await service.stop()


async def test_reports_unavailable_when_nothing_works(tmp_path: Path) -> None:
    service, _, _ = await service_with(tmp_path, Unavailable)
    await service.start()
    await settle()
    assert service.state == "unavailable"

    missing = ClipboardService(
        service.store, EventBus(), service.add, {"DISPLAY": ":9"}, which=lambda _: None
    )
    await missing.start()
    await settle()
    assert missing.state == "unavailable"  # xclip is not installed.
    await missing.stop()


async def test_notification_actions(tmp_path: Path) -> None:
    service, added, _ = await service_with(tmp_path, ScriptedWatcher)
    await service._on_action("download", LINK)
    assert added == [LINK]
    await service._on_action("ignore_site", LINK)
    await service._on_action("ignore_site", LINK)
    assert service.store.settings()["clipboard_ignore"] == ["releases.example.com"]
    assert service.filter.patterns == ("releases.example.com",)


# Real display servers


async def first_change(watcher: Watcher) -> None:
    async for _ in watcher.changes():
        return


async def time_to_detect(watcher: Watcher, display: str, text: str) -> float:
    waiting = asyncio.create_task(first_change(watcher))
    await asyncio.sleep(0.3)  # Let the watcher subscribe or take its first reading.
    started = time.monotonic()
    with copy_x11(display, text):
        await asyncio.wait_for(waiting, 5)
        elapsed = time.monotonic() - started
        assert await watcher.read() == text
    return elapsed


async def test_x11_xfixes_sees_a_copy_within_a_second() -> None:
    with xvfb() as display:
        elapsed = await time_to_detect(
            X11Watcher({**os.environ, "DISPLAY": display}), display, LINK
        )
    assert elapsed < 1.0


async def test_xwayland_poll_sees_a_copy_within_two_seconds() -> None:
    with xvfb() as display:
        watcher = PollWatcher({**os.environ, "DISPLAY": display})
        elapsed = await time_to_detect(watcher, display, LINK)
    assert elapsed < 2.0


async def test_x11_without_a_server_is_unavailable() -> None:
    with pytest.raises(WatcherUnavailable):
        await first_change(X11Watcher({"DISPLAY": ":97"}))
    with pytest.raises(WatcherUnavailable):
        await first_change(PollWatcher({}))


@pytest.mark.parametrize("compositor", [headless_sway, headless_kwin], ids=["wlroots", "kde"])
async def test_wayland_data_control_sees_a_copy_within_a_second(
    compositor: Callable[[], AbstractContextManager[dict[str, str]]],
) -> None:
    with compositor() as env:
        watcher = WaylandWatcher(env)
        waiting = asyncio.create_task(first_change(watcher))
        await asyncio.sleep(0.5)
        copier = await asyncio.create_subprocess_exec("wl-copy", "--foreground", LINK, env=env)
        started = time.monotonic()
        try:
            await asyncio.wait_for(waiting, 5)
            elapsed = time.monotonic() - started
            assert await watcher.read() == LINK
        finally:
            copier.kill()
            await copier.wait()
    assert elapsed < 1.0


async def test_wayland_without_data_control_is_unavailable(tmp_path: Path) -> None:
    fake = tmp_path / "wl-paste"
    fake.write_text("#!/bin/sh\necho 'Watch mode requires data-control' >&2\nexit 1\n")
    fake.chmod(0o755)
    watcher = WaylandWatcher({"PATH": f"{tmp_path}:{os.environ['PATH']}"})
    with pytest.raises(WatcherUnavailable, match="data-control"):
        await first_change(watcher)


# Notifications over a private bus


async def test_notifier_offers_and_reports_the_pressed_button() -> None:
    with private_bus() as address:
        server = FakeNotifications(address)
        await server.start()
        pressed: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        async def on_action(action: str, link: str) -> None:
            await pressed.put((action, link))

        notifier = Notifier(on_action)
        assert await notifier.start({"DBUS_SESSION_BUS_ADDRESS": address})
        await notifier.offer("https://a.example/<b>", "ar")
        await asyncio.wait_for(server.received.wait(), 5)
        number, body, _ = server.calls[0]
        assert body[3] == t("ar", "clipboard.summary")
        assert body[4] == "https://a.example/&lt;b&gt;"
        assert body[5] == [
            "download",
            t("ar", "clipboard.download"),
            "ignore_site",
            t("ar", "clipboard.ignore"),
        ]

        await server.press(number, "default")  # Clicking the body does nothing.
        await server.press(number, "download")
        assert await asyncio.wait_for(pressed.get(), 5) == ("download", "https://a.example/<b>")
        await notifier.stop()
        await server.stop()


async def test_notifier_without_a_bus_stays_quiet() -> None:
    notifier = Notifier(lambda action, link: asyncio.sleep(0))
    assert not await notifier.start({})
    assert not await notifier.start({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus"})
    await notifier.offer(LINK)  # No error.
    await notifier.stop()


# Acceptance: copy a link, see the notification, press Download, get the file.


async def test_acceptance_copied_link_downloads_from_the_notification(tmp_path: Path) -> None:
    origin = Origin()
    await origin.start()
    with xvfb() as display, private_bus() as address:
        server = FakeNotifications(address)
        await server.start()
        async with running_stack(tmp_path) as stack:
            clipboard = stack.daemon.services.clipboard
            assert clipboard is not None
            clipboard.env = {
                **os.environ,
                "DISPLAY": display,
                "XDG_SESSION_TYPE": "x11",
                "DBUS_SESSION_BUS_ADDRESS": address,
            }
            await clipboard.start()
            await asyncio.sleep(0.5)
            assert (await stack.client.get("/info")).json()["clipboard"] == "x11"

            link = origin.url("/normal.bin")
            copied = time.monotonic()
            with copy_x11(display, link):
                await asyncio.wait_for(server.received.wait(), 5)
            number, body, at = server.calls[0]
            assert at - copied < 1.0
            assert body[4] == link

            await server.press(number, "download")
            for _ in range(200):
                rows = (await stack.client.get("/downloads")).json()
                if rows and rows[0]["status"] == "completed":
                    break
                await asyncio.sleep(0.1)
            assert [(row["url"], row["status"]) for row in rows] == [(link, "completed")]
            await clipboard.stop()
        await server.stop()
    await origin.stop()


def test_daemon_catalogs_match() -> None:
    from daemon.i18n import catalog, language_of, t

    assert catalog("ar").keys() == catalog("en").keys()
    assert t("ar", "clipboard.download") == catalog("ar")["clipboard.download"] != "Download"
    assert t("xx", "clipboard.download") == "Download"
    assert t("en", "capture.bypass.summary", host="a.test") == "Sayl stopped intercepting a.test"
    assert language_of({"ui": {"language": "ar"}}) == "ar"
    assert language_of({"ui": "nonsense"}) == "en"
