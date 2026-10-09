from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.contracts import DeviceEvent, EventSeverity
from app.errors import PermissionDeniedError, TelemetryError
from app.platform.eventlog import LogEvent, read_events
from app.providers.base import MetricSpec, Reading, TelemetryProvider

SRC_APP = "Windows event log (Application: Application Error 1000 / Application Hang 1002)"
SRC_BOOT = "Windows event log (Diagnostics-Performance 100)"
CRASH_XPATH = (
    "(EventID=1000 and Provider[@Name='Application Error']) or "
    "(EventID=1002 and Provider[@Name='Application Hang'])"
)
BOOT_CHANNEL = "Microsoft-Windows-Diagnostics-Performance/Operational"
FIRST_RUN_LOOKBACK = timedelta(hours=24)
WINDOW = timedelta(hours=24)

Bookmarks = tuple[Callable[[str], str | None], Callable[[str, str], None]]
Reader = Callable[[str, str, int | None, timedelta], list[LogEvent]]


def crash_event(e: LogEvent) -> DeviceEvent:
    """Application Error (1000) / Application Hang (1002) -> DeviceEvent without paths or user data."""
    d = e.data
    app = d[0] if d else "unknown"
    if e.event_id == 1000:
        data = {
            "application": app,
            "version": d[1] if len(d) > 1 else None,
            "faulting_module": d[3] if len(d) > 3 else None,
            "exception_code": d[6] if len(d) > 6 else None,
            "event_id": 1000,
            "record_id": e.record_id,
        }
        return DeviceEvent(
            event_id=f"evt-app-{e.record_id}",
            type="app_crash",
            severity=EventSeverity.ERROR,
            timestamp=e.time,
            source=SRC_APP,
            message=f"{app} crashed (module {data['faulting_module'] or '?'}, "
            f"code 0x{data['exception_code'] or '?'})",
            data=data,
        )
    return DeviceEvent(
        event_id=f"evt-app-{e.record_id}",
        type="app_hang",
        severity=EventSeverity.WARNING,
        timestamp=e.time,
        source=SRC_APP,
        message=f"{app} stopped responding",
        data={
            "application": app,
            "version": d[1] if len(d) > 1 else None,
            "event_id": 1002,
            "record_id": e.record_id,
        },
    )


class EventLogProvider(TelemetryProvider):
    """Application crashes/hangs (incremental, bookmarked) and boot duration.

    The bookmark (last EventRecordID) is persisted in the local store so a restart neither re-sends
    nor misses events. First run looks back 24 hours.
    """

    name = "eventlog"
    component = "os"
    lane = "slow"
    timeout_s = 60.0

    def __init__(self, interval_ms: int, bookmarks: Bookmarks, reader: Reader | None = None) -> None:
        super().__init__(interval_ms)
        self._get, self._set = bookmarks
        self._read = reader or (lambda ch, xp, after, since: read_events(ch, xp, after, since))
        self._crashes: deque[datetime] = deque()
        self._hangs: deque[datetime] = deque()
        self._events: list[DeviceEvent] = []
        self._boot_reason: str | None = None
        self._boot_ms: int | None = None

    @property
    def declared_metrics(self) -> list[MetricSpec]:
        return [MetricSpec("system.app_crashes_24h", "count", SRC_APP)]

    def pop_events(self) -> list[DeviceEvent]:
        out, self._events = self._events, []
        return out

    def collect(self) -> list[Reading]:
        out = self._app_events()
        out += self._boot()
        return out

    def _app_events(self) -> list[Reading]:
        bookmark = self._get("eventlog.application")
        try:
            events = self._read(
                "Application", CRASH_XPATH, int(bookmark) if bookmark else None, FIRST_RUN_LOOKBACK
            )
        except TelemetryError as exc:
            return [self._na("system.app_crashes_24h", "count", SRC_APP, exc.detail)]
        for e in events:
            (self._crashes if e.event_id == 1000 else self._hangs).append(e.time)
            self._events.append(crash_event(e))
        if events:
            self._set("eventlog.application", str(max(e.record_id for e in events)))
        cutoff = datetime.now(UTC) - WINDOW
        for dq in (self._crashes, self._hangs):
            while dq and dq[0] < cutoff:
                dq.popleft()
        return [
            self._r("system.app_crashes_24h", len(self._crashes), "count", SRC_APP),
            self._r("system.app_hangs_24h", len(self._hangs), "count", SRC_APP),
        ]

    def _boot(self) -> list[Reading]:
        if self._boot_reason is None:
            bookmark = self._get("eventlog.boot")
            try:
                events = self._read(
                    BOOT_CHANNEL, "EventID=100", int(bookmark) if bookmark else None, timedelta(days=30)
                )
            except PermissionDeniedError:
                self._boot_reason = (
                    "Boot performance log requires administrator rights (run the agent as a service)"
                )
                events = []
            except TelemetryError as exc:
                self._boot_reason = exc.detail
                events = []
            for e in events:
                boot_ms = e.named.get("BootTime")
                if boot_ms and boot_ms.isdigit():
                    self._boot_ms = int(boot_ms)
                    self._events.append(
                        DeviceEvent(
                            event_id=f"evt-boot-{e.record_id}",
                            type="boot_performance",
                            severity=EventSeverity.INFO,
                            timestamp=e.time,
                            source=SRC_BOOT,
                            message=f"Boot took {int(boot_ms) / 1000:.1f} s",
                            data={
                                "boot_ms": int(boot_ms),
                                "main_path_ms": int(e.named.get("MainPathBootTime") or 0),
                            },
                        )
                    )
            if events:
                self._set("eventlog.boot", str(max(e.record_id for e in events)))
        if self._boot_ms is not None:
            return [self._r("system.last_boot_duration_ms", self._boot_ms, "ms", SRC_BOOT)]
        return [
            self._na(
                "system.last_boot_duration_ms",
                "ms",
                SRC_BOOT,
                self._boot_reason or "No boot performance event yet",
            )
        ]
