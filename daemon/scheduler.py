"""Scheduler: cron schedules that start or stop the queue, or change the global speed limit.

A start_queue schedule may carry a post action (shutdown, suspend, or a command). It runs once
the queue that schedule started is empty, after POST_ACTION_DELAY seconds in which it can be
cancelled. Shutdown and suspend go through logind on the system bus; a command is an argument
list, never a shell string.
"""

import asyncio
import contextlib
import json
import logging
import shlex
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from jeepney import DBusAddress, new_method_call
from jeepney.io.asyncio import open_dbus_router

from daemon.events import EventBus
from daemon.models import PostAction, Schedule, ScheduleAction
from daemon.queue import QueueManager
from daemon.store import Store, run

log = logging.getLogger(__name__)

POST_ACTION_DELAY = 60.0
LOGIND = DBusAddress(
    "/org/freedesktop/login1",
    bus_name="org.freedesktop.login1",
    interface="org.freedesktop.login1.Manager",
)
_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _day_number(text: str) -> int:
    value = _DAYS.index(text.lower()) if text.lower() in _DAYS else int(text)
    if not 0 <= value <= 7:
        raise ValueError(f"day of week {value} is out of range")
    return value % 7


def _day_names(field: str) -> str:
    """crontab counts days from Sunday = 0 (and 7); APScheduler from Monday = 0, so a range
    such as 0-4 (Sunday to Thursday) cannot be passed on. Expand every part to day names."""
    if field == "*":
        return "*"
    days: list[int] = []
    for part in field.split(","):
        span, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError("a day step must be at least 1")
        if span == "*":
            first, last = 0, 6
        else:
            low, _, high = span.partition("-")
            first = _day_number(low)
            last = _day_number(high) if high else first
            if high and last == 0 and first > 0:
                last = 7  # Sunday ends a range: 1-7 and thu-sun run through the weekend.
            if last < first:
                raise ValueError(f"the day range {span} runs backwards")
        days += [day % 7 for day in range(first, last + 1, step)]
    return ",".join(_DAYS[day] for day in sorted(set(days)))


def trigger(cron: str) -> CronTrigger:
    """Five crontab fields, or six with seconds first. Raises ValueError for anything else."""
    fields = cron.split()
    if len(fields) == 5:
        fields = ["0", *fields]
    if len(fields) != 6:
        raise ValueError("cron needs five fields (minute hour day month weekday), or six")
    second, minute, hour, day, month, weekday = fields
    return CronTrigger(
        second=second,
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_day_names(weekday),
    )


def command_args(params: dict[str, Any]) -> list[str]:
    command = params.get("command")
    args = shlex.split(command) if isinstance(command, str) else command
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        raise ValueError("a command post action needs params.command: a program and arguments")
    return args


def validate(schedule: Schedule) -> None:
    """Raise ValueError when a schedule could never run."""
    trigger(schedule.cron)
    action = ScheduleAction(schedule.action)
    post = PostAction(schedule.post_action)
    params = json.loads(schedule.params_json)
    if not isinstance(params, dict):
        raise ValueError("params_json must be an object")
    if action is ScheduleAction.SPEED_LIMIT:
        limit = params.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("a speed_limit schedule needs params.limit in B/s (0 = none)")
    if post is not PostAction.NONE and action is not ScheduleAction.START_QUEUE:
        raise ValueError("only a start_queue schedule can have a post action")
    if post is PostAction.COMMAND:
        command_args(params)


class Power(Protocol):
    async def shutdown(self) -> None: ...

    async def suspend(self) -> None: ...

    async def command(self, args: list[str]) -> int: ...


class SystemPower:
    """The real machine. logind decides whether this user may power off without a password."""

    async def _logind(self, method: str) -> None:
        async with open_dbus_router("SYSTEM") as router:
            reply = await router.send_and_get_reply(new_method_call(LOGIND, method, "b", (False,)))
        if reply.header.message_type.name == "error":
            raise OSError(f"logind refused {method}: {reply.body}")

    async def shutdown(self) -> None:
        await self._logind("PowerOff")

    async def suspend(self) -> None:
        await self._logind("Suspend")

    async def command(self, args: list[str]) -> int:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait()


class Scheduler:
    def __init__(
        self,
        store: Store,
        queue: QueueManager,
        bus: EventBus,
        power: Power | None = None,
        *,
        post_delay: float = POST_ACTION_DELAY,
    ) -> None:
        self.store = store
        self.queue = queue
        self.bus = bus
        self.power = power or SystemPower()
        self.post_delay = post_delay
        self.scheduler = AsyncIOScheduler()
        self.pending: asyncio.Task[None] | None = None  # A post action counting down.
        self.armed = PostAction.NONE  # The post action armed or counting down.

    async def start(self) -> None:
        self.scheduler.start()
        await self.reload()

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        await self.cancel_post_action()

    async def reload(self) -> None:
        """Replace every job with the schedules in the database."""
        self.scheduler.remove_all_jobs()
        for schedule in await run(self.store.all, Schedule):
            if not schedule.enabled:
                continue
            try:
                when = trigger(schedule.cron)
            except ValueError as error:
                log.warning("schedule %s skipped: %s", schedule.id, error)
                continue
            self.scheduler.add_job(
                self.fire,
                when,
                args=[schedule.id],
                id=schedule.id,
                coalesce=True,
                misfire_grace_time=60,
            )

    def next_run(self, schedule_id: str) -> str | None:
        job = self.scheduler.get_job(schedule_id)
        when = job.next_run_time if job is not None else None
        return when.isoformat() if when is not None else None

    async def fire(self, schedule_id: str) -> None:
        schedule = await run(self.store.get, Schedule, schedule_id)
        if schedule is None or not schedule.enabled:
            return
        action = ScheduleAction(schedule.action)
        params = json.loads(schedule.params_json)
        log.info("schedule fired", extra={"fields": {"id": schedule.id, "action": action}})
        if action is ScheduleAction.START_QUEUE:
            post = PostAction(schedule.post_action)
            if post is not PostAction.NONE:
                self.armed = post
                self.queue.arm_post_action(self._post_action(schedule.id, post, params))
            await self.queue.start_queue()
        elif action is ScheduleAction.STOP_QUEUE:
            await self.queue.stop_queue()
        else:
            limit = int(params["limit"])
            await run(self.store.put_settings, {"speed_limit": limit})
            await self.queue.set_global_speed_limit(limit)
            self.bus.publish("settings.updated", keys=["speed_limit"])
        self.bus.publish("schedule.fired", id=schedule.id, action=action.value)

    def _post_action(
        self, schedule_id: str, post: PostAction, params: dict[str, Any]
    ) -> Callable[[], Coroutine[Any, Any, None]]:
        async def countdown() -> None:
            self.pending = asyncio.current_task()
            self.bus.publish(
                "schedule.post_action", id=schedule_id, action=post.value, delay=self.post_delay
            )
            try:
                await asyncio.sleep(self.post_delay)
                await self._run(post, params)
            except OSError as error:
                log.warning("post action %s failed: %s", post, error)
            finally:
                self.pending = None

        return countdown

    async def _run(self, post: PostAction, params: dict[str, Any]) -> None:
        log.info("running post action", extra={"fields": {"action": post}})
        if post is PostAction.SHUTDOWN:
            await self.power.shutdown()
        elif post is PostAction.SUSPEND:
            await self.power.suspend()
        elif post is PostAction.COMMAND:
            code = await self.power.command(command_args(params))
            log.info("post command exited", extra={"fields": {"code": code}})

    async def cancel_post_action(self) -> bool:
        """Cancel a post action that is counting down, or one still waiting for the queue."""
        armed = self.queue.post_action is not None
        self.queue.post_action = None
        task, self.pending = self.pending, None
        if task is None:
            return armed
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.bus.publish("schedule.post_action", id="", action="canceled", delay=0)
        return True
