"""Background task supervision (Phase 10).

Before Phase 10 a background loop that raised stayed dead until the process stopped, silently: telemetry
kept arriving while, for example, notification delivery or the persister no longer ran. The supervisor
restarts a failed loop with exponential backoff and jitter (1 s doubling to 60 s), resets the backoff after
5 minutes of healthy running, and reports a loop that failed 5 times within 10 minutes as
``crash_looping``. Every loop's state is exported as metrics and in ``/health/ready``.

Services that run their own subtasks use :func:`watch` instead of only waiting for ``stop``: if a subtask
ends unexpectedly, the service's ``run`` raises, its ``finally`` cancels the remaining subtasks, and the
supervisor restarts the whole service, so subtasks are never duplicated.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.core.metrics import BACKGROUND_TASK_RESTARTS, BACKGROUND_TASK_UP

log = structlog.get_logger("supervisor")

BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
HEALTHY_RESET_S = 300.0
CRASH_LOOP_FAILURES = 5
CRASH_LOOP_WINDOW_S = 600.0


@dataclass
class TaskState:
    name: str
    critical: bool
    state: str = "starting"  # running | restarting | crash_looping | finished | stopped
    restarts: int = 0
    last_error: str | None = None
    last_start: float = 0.0
    failures: deque[float] = field(default_factory=lambda: deque(maxlen=CRASH_LOOP_FAILURES))

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "critical": self.critical,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "running_for_s": round(time.monotonic() - self.last_start) if self.state == "running" else None,
        }


class Supervisor:
    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop
        self.tasks: dict[str, TaskState] = {}

    def spawn(
        self,
        name: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        critical: bool = False,
        may_finish: bool = False,
    ) -> asyncio.Task[None]:
        """Run ``factory()`` until stop; restart it (bounded backoff) whenever it fails or exits early.

        ``may_finish``: the loop may legitimately return (e.g. a feature that is switched off); that ends
        supervision with state ``finished`` instead of counting as a failure.
        """
        st = self.tasks[name] = TaskState(name, critical)
        return asyncio.create_task(self._run(st, factory, may_finish), name=name)

    async def _run(self, st: TaskState, factory: Callable[[], Awaitable[Any]], may_finish: bool) -> None:
        delay = BACKOFF_MIN_S
        first = True
        # always start the loop once, even if shutdown already began: loops flush their buffers on exit
        while first or not self._stop.is_set():
            first = False
            st.state, st.last_start = "running", time.monotonic()
            BACKGROUND_TASK_UP.labels(st.name).set(1)
            try:
                await factory()
                if self._stop.is_set():
                    break
                if may_finish:
                    st.state = "finished"
                    BACKGROUND_TASK_UP.labels(st.name).set(0)
                    return
                raise RuntimeError("background loop returned before shutdown")
            except asyncio.CancelledError:
                st.state = "stopped"
                BACKGROUND_TASK_UP.labels(st.name).set(0)
                raise
            except Exception as exc:
                now = time.monotonic()
                if now - st.last_start > HEALTHY_RESET_S:
                    delay = BACKOFF_MIN_S
                st.restarts += 1
                st.failures.append(now)
                st.last_error = f"{type(exc).__name__}: {exc}"[:300]
                looping = (
                    len(st.failures) == CRASH_LOOP_FAILURES and now - st.failures[0] < CRASH_LOOP_WINDOW_S
                )
                st.state = "crash_looping" if looping else "restarting"
                BACKGROUND_TASK_UP.labels(st.name).set(0)
                BACKGROUND_TASK_RESTARTS.labels(st.name).inc()
                wait = min(BACKOFF_MAX_S, delay) * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto
                log.error(
                    "background_task_failed",
                    task=st.name,
                    error=st.last_error,
                    restarts=st.restarts,
                    crash_looping=looping,
                    retry_in_s=round(wait, 1),
                    exc_info=exc,
                )
                delay = min(BACKOFF_MAX_S, delay * 2)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
        st.state = "stopped"
        BACKGROUND_TASK_UP.labels(st.name).set(0)

    def status(self) -> dict[str, Any]:
        bad = {
            n: s.public() for n, s in self.tasks.items() if s.state not in ("running", "stopped", "finished")
        }
        critical_down = [n for n, s in self.tasks.items() if s.critical and s.state == "crash_looping"]
        return {
            "status": "failing" if critical_down else ("degraded" if bad else "ok"),
            "tasks": len(self.tasks),
            "not_running": bad,
            "critical_crash_looping": critical_down,
        }


async def watch(tasks: list[asyncio.Task[Any]], stop: asyncio.Event) -> None:
    """Wait for ``stop``; raise if one of ``tasks`` ends first (so the supervisor restarts the service).

    The caller cancels the remaining tasks in its ``finally``.
    """
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()
    if stop.is_set():
        return
    for t in done:
        if t is stopper:
            continue
        exc = None if t.cancelled() else t.exception()
        raise RuntimeError(f"subtask {t.get_name()} stopped unexpectedly: {exc!r}")
