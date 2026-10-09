"""Agent presence: ONLINE / STALE / OFFLINE / UNKNOWN from the agent's last contact.

Presence answers "is the agent alive and talking to us?", which differs from the twin's
telemetry status ("is the data fresh?"): an agent whose collectors all fail still heartbeats
(ONLINE, telemetry stale), and a backlog replay after an outage marks the agent ONLINE although
its newest samples are old.

Contact = a heartbeat (``POST /api/v1/agent/heartbeat``, every ~30 s) or any accepted batch.

    age <= PRESENCE_STALE_AFTER_S     ONLINE
    age <= PRESENCE_OFFLINE_AFTER_S   STALE    (missed a few heartbeats)
    otherwise                         OFFLINE
    never contacted                   UNKNOWN  (registered/known device without contact)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.metrics import DEVICES_BY_PRESENCE
from app.domain.events.events import PresenceChanged


class Presence(StrEnum):
    ONLINE = "ONLINE"
    STALE = "STALE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


@dataclass
class DevicePresence:
    device_id: str
    presence: Presence = Presence.UNKNOWN
    last_contact_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_batch_at: datetime | None = None
    changed_at: datetime | None = None
    heartbeat: dict[str, Any] | None = None  # last heartbeat body (agent-reported, non-sensitive)

    def to_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "presence": self.presence.value,
            "last_contact_at": _iso(self.last_contact_at),
            "last_heartbeat_at": _iso(self.last_heartbeat_at),
            "last_batch_at": _iso(self.last_batch_at),
            "contact_age_s": round((now - self.last_contact_at).total_seconds(), 1)
            if self.last_contact_at
            else None,
            "changed_at": _iso(self.changed_at),
            "heartbeat": self.heartbeat,
        }


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


class PresenceService:
    def __init__(self, stale_after_s: float, offline_after_s: float) -> None:
        self.stale_after_s = stale_after_s
        self.offline_after_s = max(offline_after_s, stale_after_s + 1)
        self._devices: dict[str, DevicePresence] = {}

    def _classify(self, last: datetime | None, now: datetime) -> Presence:
        if last is None:
            return Presence.UNKNOWN
        age = (now - last).total_seconds()
        if age <= self.stale_after_s:
            return Presence.ONLINE
        if age <= self.offline_after_s:
            return Presence.STALE
        return Presence.OFFLINE

    def known(self, device_id: str, last_seen: datetime | None = None) -> None:
        """Register a device known from storage (restored at startup) without counting it as contact."""
        if device_id not in self._devices:
            p = DevicePresence(device_id, last_contact_at=last_seen)
            p.presence = self._classify(last_seen, datetime.now(UTC))
            self._devices[device_id] = p

    def _contact(self, device_id: str, now: datetime) -> tuple[DevicePresence, PresenceChanged | None]:
        p = self._devices.setdefault(device_id, DevicePresence(device_id))
        if p.last_contact_at is None or now >= p.last_contact_at:
            p.last_contact_at = now
        return p, self._transition(p, Presence.ONLINE, now)

    def heartbeat(
        self, device_id: str, info: dict[str, Any], now: datetime | None = None
    ) -> tuple[DevicePresence, PresenceChanged | None]:
        now = now or datetime.now(UTC)
        p, change = self._contact(device_id, now)
        p.last_heartbeat_at = now
        p.heartbeat = info
        return p, change

    def batch_received(self, device_id: str, now: datetime | None = None) -> PresenceChanged | None:
        now = now or datetime.now(UTC)
        p, change = self._contact(device_id, now)
        p.last_batch_at = now
        return change

    def _transition(self, p: DevicePresence, new: Presence, now: datetime) -> PresenceChanged | None:
        if p.presence is new:
            return None
        previous = p.presence
        p.presence = new
        p.changed_at = now
        return PresenceChanged(
            device_id=p.device_id,
            previous_presence=previous.value,
            presence=new.value,
            last_contact_at=p.last_contact_at,
        )

    def evaluate(self, now: datetime | None = None) -> list[PresenceChanged]:
        now = now or datetime.now(UTC)
        changes = []
        counts = dict.fromkeys(Presence, 0)
        for p in self._devices.values():
            change = self._transition(p, self._classify(p.last_contact_at, now), now)
            if change is not None:
                changes.append(change)
            counts[p.presence] += 1
        for state, n in counts.items():
            DEVICES_BY_PRESENCE.labels(state.value).set(n)
        return changes

    def get(self, device_id: str) -> DevicePresence | None:
        return self._devices.get(device_id)

    def presence_of(self, device_id: str) -> str:
        p = self._devices.get(device_id)
        return p.presence.value if p else Presence.UNKNOWN.value

    def all(self) -> list[DevicePresence]:
        return list(self._devices.values())

    def summary(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Presence}
        for p in self._devices.values():
            counts[p.presence.value] += 1
        return counts
