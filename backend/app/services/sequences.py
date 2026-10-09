"""Per-device sequence and clock tracking for the ingest path (cheap, in-memory, O(1) per batch).

Sequences increase monotonically per agent installation (persisted on the endpoint), so:
- ``seq == last + 1``         in order
- ``seq > last + 1``          gap (batches still queued on the agent, thinned by backpressure, or lost)
- ``seq <= last``             out of order (normal for an offline backlog replay); an exact repeat of
                              an already-seen batch id is a duplicate and is caught earlier by dedupe
- ``seq`` far below ``last``  agent state was reset (reinstall / wiped data directory)

Gaps later filled by replayed batches are removed again, so ``missing`` converges to the number of
batches the backend genuinely never received.

Clock drift = ``server_received_at - sent_at`` (an upper bound: it includes network transit). It is
tracked as an EWMA. Device timestamps are never rewritten; the drift is exposed so consumers can
judge it, and grossly future-dated batches are rejected at validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

MAX_TRACKED_GAPS = 4096
RESET_THRESHOLD = 10_000


@dataclass
class DeviceSequenceState:
    last_sequence: int | None = None
    received: int = 0
    in_order: int = 0
    out_of_order: int = 0
    duplicates: int = 0
    resets: int = 0
    gaps_detected: int = 0
    missing: set[int] = field(default_factory=set)
    clock_drift_s: float | None = None
    max_abs_drift_s: float = 0.0
    last_received_at: datetime | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "last_sequence": self.last_sequence,
            "received": self.received,
            "in_order": self.in_order,
            "out_of_order": self.out_of_order,
            "duplicates": self.duplicates,
            "resets": self.resets,
            "gaps_detected": self.gaps_detected,
            "missing": len(self.missing),
            "clock_drift_s": None if self.clock_drift_s is None else round(self.clock_drift_s, 3),
            "max_abs_drift_s": round(self.max_abs_drift_s, 3),
            "last_received_at": self.last_received_at.isoformat() if self.last_received_at else None,
        }


class SequenceTracker:
    def __init__(self, drift_alpha: float = 0.2) -> None:
        self._state: dict[str, DeviceSequenceState] = {}
        self._alpha = drift_alpha

    def state(self, device_id: str) -> DeviceSequenceState:
        return self._state.setdefault(device_id, DeviceSequenceState())

    def duplicate(self, device_id: str) -> None:
        self.state(device_id).duplicates += 1

    def observe(self, device_id: str, sequence: int, sent_at: datetime, received_at: datetime) -> str:
        """Record an accepted batch: ``first`` | ``in_order`` | ``gap`` | ``out_of_order`` | ``reset``."""
        st = self.state(device_id)
        st.received += 1
        st.last_received_at = received_at
        drift = (received_at - sent_at).total_seconds()
        if st.clock_drift_s is None:
            st.clock_drift_s = drift
        else:
            st.clock_drift_s = (1 - self._alpha) * st.clock_drift_s + self._alpha * drift
        st.max_abs_drift_s = max(st.max_abs_drift_s, abs(drift))
        last = st.last_sequence
        if last is None:
            st.last_sequence = sequence
            return "first"
        if sequence == last + 1:
            st.in_order += 1
            st.last_sequence = sequence
            return "in_order"
        if sequence > last + 1:
            st.gaps_detected += 1
            room = MAX_TRACKED_GAPS - len(st.missing)
            if room > 0:
                st.missing.update(range(last + 1, min(sequence, last + 1 + room)))
            st.last_sequence = sequence
            return "gap"
        if last - sequence > RESET_THRESHOLD and sequence < RESET_THRESHOLD:
            st.resets += 1
            st.missing.clear()
            st.last_sequence = sequence
            return "reset"
        st.out_of_order += 1
        st.missing.discard(sequence)  # a replayed batch fills a previously detected gap
        return "out_of_order"

    def last_sequence(self, device_id: str) -> int | None:
        st = self._state.get(device_id)
        return st.last_sequence if st else None

    def snapshot(self, device_id: str | None = None) -> dict[str, dict[str, object]]:
        if device_id is not None:
            return {device_id: self._state[device_id].snapshot()} if device_id in self._state else {}
        return {d: s.snapshot() for d, s in self._state.items()}
