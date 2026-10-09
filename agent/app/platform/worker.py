"""Isolated worker lanes for blocking Windows API work (COM/WMI, PDH, event log, ICMP).

Each lane is one daemon thread with COM initialised (MTA). Collectors are assigned to lanes by the
kind of API they call, so a slow or hung call (a WMI provider, the Windows Update searcher, an ICMP
timeout) only delays collectors on the *same* lane. A lane whose current call exceeds
``hung_after_s`` is abandoned and replaced with a fresh thread; the stuck thread is a daemon and
cannot block shutdown.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import structlog

T = TypeVar("T")
log = structlog.get_logger("agent.worker")

# Lane names (collectors declare one of these).
LANE_FAST = "fast"  # PDH counters, psutil: sub-millisecond to tens of ms
LANE_WMI = "wmi"  # WMI / CIM queries
LANE_SLOW = "slow"  # event log scans, security posture, NVMe IOCTL
LANE_NET = "net"  # ICMP echo (bounded by timeouts)
LANE_UPDATES = "updates"  # Windows Update searcher (can take tens of seconds)
LANE_PROC = "proc"  # process snapshot (+ optional per-process details)
LANES = (LANE_FAST, LANE_WMI, LANE_SLOW, LANE_NET, LANE_UPDATES, LANE_PROC)

_STOP = object()


def _init_com() -> None:
    if sys.platform == "win32":
        import pythoncom

        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)


@dataclass
class LaneStats:
    name: str
    busy_since: float | None
    completed: int
    replaced: int


class _Lane:
    def __init__(self, name: str, init: Callable[[], None]) -> None:
        self.name = name
        self._init = init
        self.completed = 0
        self.replaced = 0
        self.busy_since: float | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = self._spawn()

    def _spawn(self) -> threading.Thread:
        q = self._queue

        def loop() -> None:
            try:
                self._init()
            except Exception as exc:  # COM init failure must not kill the agent
                log.error("lane_init_failed", lane=self.name, error=str(exc))
            while True:
                item = q.get()
                if item is _STOP:
                    return
                fn, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue
                self.busy_since = time.monotonic()
                try:
                    fut.set_result(fn())
                except BaseException as exc:
                    fut.set_exception(exc)
                finally:
                    self.busy_since = None
                    self.completed += 1

        t = threading.Thread(target=loop, name=f"ldt-{self.name}", daemon=True)
        t.start()
        return t

    def submit(self, fn: Callable[[], T]) -> cf.Future[T]:
        fut: cf.Future[T] = cf.Future()
        self._queue.put((fn, fut))
        return fut

    def replace(self) -> None:
        """Abandon a hung thread: pending work moves to a fresh thread with a new queue."""
        old_queue = self._queue
        self._queue = queue.Queue()
        self.busy_since = None
        self.replaced += 1
        while True:
            try:
                item = old_queue.get_nowait()
            except queue.Empty:
                break
            if item is not _STOP:
                self._queue.put(item)
        old_queue.put(_STOP)  # the stuck thread exits if its call ever returns
        self._thread = self._spawn()

    def stop(self) -> None:
        self._queue.put(_STOP)


class WorkerPool:
    def __init__(self, hung_after_s: float = 180.0, init: Callable[[], None] = _init_com) -> None:
        self._hung_after = hung_after_s
        self._lanes = {name: _Lane(name, init) for name in LANES}

    def _lane(self, name: str) -> _Lane:
        return self._lanes.get(name) or self._lanes[LANE_FAST]

    async def run(self, fn: Callable[[], T], timeout_s: float = 10.0, lane: str = LANE_FAST) -> T:
        return await asyncio.wait_for(asyncio.wrap_future(self._lane(lane).submit(fn)), timeout=timeout_s)

    def submit_sync(self, fn: Callable[[], T], lane: str = LANE_FAST, timeout_s: float | None = None) -> T:
        return self._lane(lane).submit(fn).result(timeout=timeout_s)

    def busy(self, lane: str) -> bool:
        return self._lane(lane).busy_since is not None

    def check_hung(self) -> list[str]:
        """Replace lanes whose current call has run longer than ``hung_after_s``. Returns their names."""
        now = time.monotonic()
        replaced = []
        for lane in self._lanes.values():
            started = lane.busy_since
            if started is not None and now - started > self._hung_after:
                log.error("lane_hung_replaced", lane=lane.name, busy_s=round(now - started, 1))
                lane.replace()
                replaced.append(lane.name)
        return replaced

    def stats(self) -> list[LaneStats]:
        return [
            LaneStats(n, lane.busy_since, lane.completed, lane.replaced) for n, lane in self._lanes.items()
        ]

    def shutdown(self) -> None:
        for lane in self._lanes.values():
            lane.stop()


# Backwards-compatible name used by older call sites/tests.
PlatformWorker = WorkerPool
