"""Incremental Windows event log reader (EvtQuery with an EventRecordID bookmark).

Only the fields needed for IT health are extracted (application name/version, faulting module,
exception code, event id, timestamp). Paths and user names in event payloads are not forwarded.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.errors import DriverUnavailableError, PermissionDeniedError, TelemetryError

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_ACCESS_DENIED = 5


@dataclass(frozen=True, slots=True)
class LogEvent:
    record_id: int
    event_id: int
    provider: str
    level: int
    time: datetime
    data: list[str] = field(default_factory=list)  # EventData values in order
    named: dict[str, str] = field(default_factory=dict)  # EventData values by Name


def parse_system_time(stamp: str) -> datetime:
    """Event log SystemTime (``2026-10-01T10:00:00.1234567Z``) -> aware UTC datetime."""
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?", stamp or "")
    if not m:
        return datetime.now(UTC)
    base = datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC)
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    return base.replace(microsecond=int(frac))


def parse_event(xml: str) -> LogEvent | None:
    try:
        root = ET.fromstring(xml)  # noqa: S314  (rendered by the local Windows event log service)
    except ET.ParseError:
        return None
    system = root.find(f"{_NS}System")
    if system is None:
        return None

    def text(tag: str) -> str:
        el = system.find(f"{_NS}{tag}") if system is not None else None
        return (el.text or "") if el is not None else ""

    prov = system.find(f"{_NS}Provider")
    created = system.find(f"{_NS}TimeCreated")
    stamp = created.get("SystemTime", "") if created is not None else ""
    when = parse_system_time(stamp)
    values: list[str] = []
    named: dict[str, str] = {}
    data_el = root.find(f"{_NS}EventData")
    if data_el is not None:
        for d in data_el.findall(f"{_NS}Data"):
            values.append(d.text or "")
            if d.get("Name"):
                named[str(d.get("Name"))] = d.text or ""
    rid, eid, lvl = text("EventRecordID"), text("EventID"), text("Level")
    return LogEvent(
        record_id=int(rid) if rid.isdigit() else 0,
        event_id=int(eid) if eid.isdigit() else 0,
        provider=prov.get("Name", "") if prov is not None else "",
        level=int(lvl) if lvl.isdigit() else 4,
        time=when,
        data=values,
        named=named,
    )


def read_events(
    channel: str, xpath: str, after_record_id: int | None, since: timedelta, limit: int = 200
) -> list[LogEvent]:
    """Events matching ``xpath`` newer than the bookmark (or within ``since`` on first run), oldest first."""
    if sys.platform != "win32":
        raise DriverUnavailableError("Requires Windows")
    import win32evtlog

    if after_record_id:
        query = f"*[System[({xpath}) and EventRecordID > {int(after_record_id)}]]"
    else:
        window_ms = int(since.total_seconds() * 1000)
        query = f"*[System[({xpath}) and TimeCreated[timediff(@SystemTime) <= {window_ms}]]]"
    try:
        handle = win32evtlog.EvtQuery(channel, win32evtlog.EvtQueryReverseDirection, query)
    except Exception as exc:  # pywintypes.error
        if getattr(exc, "winerror", None) == _ACCESS_DENIED or "denied" in str(exc).lower():
            raise PermissionDeniedError(f"Event log '{channel}' requires administrator rights") from exc
        raise TelemetryError(f"Event log '{channel}' not readable: {exc}") from exc
    out: list[LogEvent] = []
    while len(out) < limit:
        batch = win32evtlog.EvtNext(handle, min(64, limit - len(out)))
        if not batch:
            break
        for ev in batch:
            parsed = parse_event(win32evtlog.EvtRender(ev, win32evtlog.EvtRenderEventXml))
            if parsed is not None:
                out.append(parsed)
    out.sort(key=lambda e: e.record_id)
    return out
