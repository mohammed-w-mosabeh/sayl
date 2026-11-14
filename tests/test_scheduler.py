import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from daemon.models import Schedule
from daemon.scheduler import Scheduler, trigger, validate
from tests.origin import Origin
from tests.stack import Stack, running_stack


class FakePower:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def shutdown(self) -> None:
        self.calls.append("shutdown")

    async def suspend(self) -> None:
        self.calls.append("suspend")

    async def command(self, args: list[str]) -> int:
        self.calls.append(" ".join(args))
        return 0


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


def scheduler_of(stack: Stack) -> Scheduler:
    scheduler = stack.daemon.services.scheduler
    assert scheduler is not None
    return scheduler


def local(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour).astimezone()


@pytest.mark.parametrize(
    ("cron", "after", "expected"),
    [
        ("0 9 * * 1-5", local(2026, 11, 1), local(2026, 11, 2, 9)),  # Sunday -> Monday 09:00.
        ("0 0 * * 0", local(2026, 11, 2), local(2026, 11, 8)),  # crontab 0 is Sunday.
        ("0 0 * * 7", local(2026, 11, 2), local(2026, 11, 8)),  # So is 7.
        ("0 7 * * 0-4", local(2026, 11, 6), local(2026, 11, 8, 7)),  # Fri -> Sunday 07:00.
        ("0 7 * * sun-thu", local(2026, 11, 6), local(2026, 11, 8, 7)),
        ("0 7 * * 5-7", local(2026, 11, 3), local(2026, 11, 6, 7)),  # Tue -> Friday.
        ("0 7 * * thu-sun", local(2026, 11, 3), local(2026, 11, 5, 7)),
        ("0 7 * * */3", local(2026, 11, 2), local(2026, 11, 4, 7)),  # Sun, Wed, Sat.
        ("30 2 1 * *", local(2026, 11, 3), local(2026, 12, 1, 2) + timedelta(minutes=30)),
        ("15 * * * * *", local(2026, 11, 3), local(2026, 11, 3) + timedelta(seconds=15)),
    ],
)
def test_cron_follows_crontab(cron: str, after: datetime, expected: datetime) -> None:
    assert trigger(cron).get_next_fire_time(None, after) == expected


@pytest.mark.parametrize(
    "cron", ["", "* * *", "61 * * * *", "0 0 * * 8", "a b c d e", "0 0 * * 5-1", "0 0 * * */0"]
)
def test_bad_cron_is_refused(cron: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        trigger(cron)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"action": "speed_limit"}, "params.limit"),
        ({"action": "speed_limit", "params_json": '{"limit": -1}'}, "params.limit"),
        ({"action": "stop_queue", "post_action": "shutdown"}, "only a start_queue"),
        ({"action": "start_queue", "post_action": "command"}, "params.command"),
        ({"action": "explode"}, "explode"),
    ],
)
def test_impossible_schedules_are_refused(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate(Schedule(name="x", cron="0 9 * * *", **values))


async def wait_for(stack: Stack, download_id: str, *states: str) -> dict[str, Any]:
    for _ in range(1500):
        body: dict[str, Any] = (await stack.client.get(f"/downloads/{download_id}")).json()
        if body["status"] in states:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"still {body['status']}: {body['error']}")


def at(when: datetime) -> str:
    """A six-field cron that fires once a day at this second, in local time."""
    return f"{when.second} {when.minute} {when.hour} * * *"


async def test_a_scheduled_queue_starts_on_time_and_runs_the_post_action(
    stack: Stack, origin: Origin, tmp_path: Path
) -> None:
    """SPEC acceptance: a scheduled queue starts and stops on time and runs the post action."""
    scheduler_of(stack).post_delay = 0.1
    assert (await stack.client.post("/queue/stop")).json()["running"] is False
    created = await stack.client.post("/downloads", json={"url": origin.url("/normal.bin")})
    download_id = created.json()["id"]
    await asyncio.sleep(1.5)
    assert (await stack.client.get(f"/downloads/{download_id}")).json()["status"] == "queued"

    marker = tmp_path / "post-action-ran"
    when = (datetime.now().astimezone() + timedelta(seconds=3)).replace(microsecond=0)
    schedule = await stack.client.post(
        "/schedules",
        json={
            "name": "night",
            "cron": at(when),
            "action": "start_queue",
            "post_action": "command",
            "params": {"command": ["touch", str(marker)]},
        },
    )
    assert schedule.status_code == 201, schedule.text
    assert datetime.fromisoformat(schedule.json()["next_run"]) == when

    body = await wait_for(stack, download_id, "completed", "failed")
    assert body["status"] == "completed"
    started = datetime.fromisoformat(body["started_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)  # The database keeps naive UTC.
    delay = (started - when).total_seconds()
    assert 0 <= delay < 2, delay  # On time: not before, and within the queue's tick.
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.05)
    assert marker.exists()
    assert (stack.downloads / "normal.bin").exists()


async def test_a_scheduled_stop_pauses_and_a_start_resumes(stack: Stack, origin: Origin) -> None:
    created = await stack.client.post(
        "/downloads", json={"url": origin.url("/normal.bin"), "speed_limit": 200_000}
    )
    download_id = created.json()["id"]
    await wait_for(stack, download_id, "active")
    when = (datetime.now().astimezone() + timedelta(seconds=2)).replace(microsecond=0)
    await stack.client.post(
        "/schedules", json={"name": "stop", "cron": at(when), "action": "stop_queue"}
    )
    body = await wait_for(stack, download_id, "paused", "completed", "failed")
    assert body["status"] == "paused"
    assert datetime.now().astimezone() >= when
    state = (await stack.client.get("/queue")).json()
    assert state["running"] is False

    await stack.client.post("/queue/start")
    body = await wait_for(stack, download_id, "active", "completed", "failed")
    assert body["status"] in ("active", "completed")
    assert (await stack.client.get("/queue")).json()["running"] is True


async def test_a_speed_schedule_sets_the_global_limit(stack: Stack) -> None:
    created = await stack.client.post(
        "/schedules",
        json={
            "name": "day",
            "cron": "0 9 * * 1-5",
            "action": "speed_limit",
            "params": {"limit": 1000},
        },
    )
    assert (await stack.client.post(f"/schedules/{created.json()['id']}/run")).status_code == 204
    assert (await stack.client.get("/settings")).json()["speed_limit"] == 1000


async def test_bad_schedules_get_422(stack: Stack) -> None:
    bad = await stack.client.post(
        "/schedules", json={"name": "x", "cron": "every night", "action": "start_queue"}
    )
    assert bad.status_code == 422
    assert (await stack.client.get("/schedules")).json() == []


async def test_post_action_is_skipped_when_the_queue_had_nothing(stack: Stack) -> None:
    scheduler = scheduler_of(stack)
    power = FakePower()
    scheduler.power, scheduler.post_delay = power, 0.01
    created = await stack.client.post(
        "/schedules",
        json={"name": "x", "cron": "0 1 * * *", "action": "start_queue", "post_action": "shutdown"},
    )
    await stack.client.post(f"/schedules/{created.json()['id']}/run")
    await asyncio.sleep(1.5)
    assert power.calls == []  # An empty queue never powers the machine off.
    assert (await stack.client.get("/queue")).json()["post_action_armed"] is False


async def test_a_counting_down_post_action_can_be_cancelled(stack: Stack, origin: Origin) -> None:
    scheduler = scheduler_of(stack)
    power = FakePower()
    scheduler.power, scheduler.post_delay = power, 30
    created = await stack.client.post(
        "/schedules",
        json={"name": "x", "cron": "0 1 * * *", "action": "start_queue", "post_action": "suspend"},
    )
    await stack.client.post("/queue/stop")
    added = await stack.client.post("/downloads", json={"url": origin.url("/normal.bin")})
    await stack.client.post(f"/schedules/{created.json()['id']}/run")
    await wait_for(stack, added.json()["id"], "completed", "failed")
    for _ in range(100):
        if (await stack.client.get("/queue")).json()["post_action_pending"]:
            break
        await asyncio.sleep(0.05)
    state = (await stack.client.post("/queue/post-action/cancel")).json()
    assert state["post_action_pending"] is False
    assert power.calls == []


async def test_a_post_action_runs_after_the_delay(stack: Stack, origin: Origin) -> None:
    scheduler = scheduler_of(stack)
    power = FakePower()
    scheduler.power, scheduler.post_delay = power, 0.05
    created = await stack.client.post(
        "/schedules",
        json={"name": "x", "cron": "0 1 * * *", "action": "start_queue", "post_action": "shutdown"},
    )
    await stack.client.post("/queue/stop")
    added = await stack.client.post("/downloads", json={"url": origin.url("/normal.bin")})
    await stack.client.post(f"/schedules/{created.json()['id']}/run")
    await wait_for(stack, added.json()["id"], "completed", "failed")
    for _ in range(100):
        if power.calls:
            break
        await asyncio.sleep(0.05)
    assert power.calls == ["shutdown"]
