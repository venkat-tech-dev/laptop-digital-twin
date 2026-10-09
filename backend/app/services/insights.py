"""Anomaly insights: detection confidence and root-cause correlation.

Everything here is computed from recorded telemetry - nothing is guessed:

* **Confidence** is a documented heuristic, not a probability: for threshold rules it combines how far
  the value is past the threshold with how long the breach has persisted relative to the rule's
  required duration; for the statistical detector it maps the z-score.
* **Correlated signals** are Pearson correlations between the anomalous series and other recorded
  series over the anomaly window (10 s buckets).
* **Process attribution** compares each process's mean CPU / memory during the anomaly with the
  period just before it, using the agent's process snapshots kept in memory (last 60 minutes).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.anomalies.models import Anomaly, Detector
from app.schemas.ingest import ProcessSnapshotIn

CORRELATION_CANDIDATES = (
    "cpu.usage_percent",
    "memory.usage_percent",
    "memory.used_bytes",
    "disk.active_time_percent",
    "disk.read_bytes_per_sec",
    "disk.write_bytes_per_sec",
    "network.rx_bytes_per_sec",
    "network.tx_bytes_per_sec",
    "cpu.frequency_mhz",
    "battery.discharge_rate_w",
    "cpu.temperature_c",
)
_LABELS = {
    "cpu.usage_percent": "CPU utilization",
    "memory.usage_percent": "Memory utilization",
    "memory.used_bytes": "Memory in use",
    "disk.active_time_percent": "Disk active time",
    "disk.read_bytes_per_sec": "Disk read rate",
    "disk.write_bytes_per_sec": "Disk write rate",
    "network.rx_bytes_per_sec": "Network download",
    "network.tx_bytes_per_sec": "Network upload",
    "cpu.frequency_mhz": "CPU clock",
    "battery.discharge_rate_w": "Battery discharge power",
    "cpu.temperature_c": "CPU package temperature",
}


# --------------------------------------------------------------------------- process history
@dataclass(frozen=True, slots=True)
class _Proc:
    name: str
    cpu: float | None
    mem: int | None


class ProcessHistory:
    """Compact rolling history of the agent's top-process snapshots (per device, in memory)."""

    def __init__(self, max_age_s: float = 3600.0) -> None:
        self._max_age = timedelta(seconds=max_age_s)
        self._snaps: dict[str, deque[tuple[datetime, dict[int, _Proc]]]] = {}

    def add(self, device_id: str, snap: ProcessSnapshotIn) -> None:
        ring = self._snaps.setdefault(device_id, deque())
        ring.append(
            (
                snap.timestamp,
                {p.pid: _Proc(p.name, p.cpu_percent, p.memory_rss_bytes) for p in snap.processes},
            )
        )
        cutoff = snap.timestamp - self._max_age
        while ring and ring[0][0] < cutoff:
            ring.popleft()

    def window(self, device_id: str, start: datetime, end: datetime) -> list[dict[int, _Proc]]:
        return [procs for t, procs in self._snaps.get(device_id, ()) if start <= t <= end]

    def coverage(self, device_id: str) -> tuple[datetime | None, int]:
        ring = self._snaps.get(device_id)
        return (ring[0][0], len(ring)) if ring else (None, 0)


# --------------------------------------------------------------------------- confidence
def detection_confidence(anomaly: Anomaly, now: datetime | None = None) -> dict[str, Any]:
    """Heuristic 0..0.99 confidence that the detected condition is real and persistent."""
    now = now or datetime.now(UTC)
    end = anomaly.resolved_at or anomaly.last_seen_at or now
    persisted_s = max(0.0, (end - anomaly.started_at).total_seconds())
    if anomaly.detector in (Detector.BEHAVIORAL, Detector.MULTIVARIATE) and anomaly.confidence is not None:
        ev = anomaly.evidence or {}
        return {
            "value": anomaly.confidence,
            "band": ev.get("confidence_band"),
            "method": "Evidence-based: evidence^0.40 x persistence^0.20 x baseline^0.25 x data quality^0.15",
            "factors": ev.get("confidence_factors") or {},
        }
    if anomaly.detector is Detector.STATISTICAL:
        z = float(anomaly.context.get("zscore", 0) or 0)
        value = min(0.95, max(0.5, 0.5 + 0.1 * (z - 2.0)))
        return {
            "value": round(value, 2),
            "method": "Statistical: mapped from EWMA z-score (z=4 -> 0.70, z>=6.5 -> 0.95)",
            "factors": {"zscore": z, "persisted_s": round(persisted_s, 1)},
        }
    try:
        value_f = float(anomaly.value)  # type: ignore[arg-type]
        threshold = float(anomaly.threshold)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {"value": 0.6, "method": "Rule on a non-numeric value: fixed confidence", "factors": {}}
    margin = abs(value_f - threshold) / max(abs(threshold), 1e-9)
    required = float(anomaly.context.get("duration_s", 0) or 0) or 30.0
    persistence = min(1.0, persisted_s / (2.0 * required))
    value = 0.5 + 0.3 * min(1.0, margin * 5.0) + 0.19 * persistence
    return {
        "value": round(min(0.99, value), 2),
        "method": "Rule: 0.5 + 0.3 x margin past threshold (saturates at 20 %) + 0.19 x persistence "
        "(saturates at 2x the rule's duration)",
        "factors": {
            "margin_percent": round(margin * 100, 1),
            "persisted_s": round(persisted_s, 1),
            "required_duration_s": required,
        },
    }


# --------------------------------------------------------------------------- correlation
def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 6:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def correlate(
    target: dict[datetime, float], others: dict[str, dict[datetime, float]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, series in others.items():
        common = sorted(set(target) & set(series))
        r = pearson([target[t] for t in common], [series[t] for t in common])
        if r is None:
            continue
        out.append(
            {"metric_key": key, "label": _LABELS.get(key, key), "r": round(r, 3), "samples": len(common)}
        )
    return sorted(out, key=lambda c: abs(c["r"]), reverse=True)


def attribute_processes(
    during: list[dict[int, _Proc]], before: list[dict[int, _Proc]], by_memory: bool
) -> list[dict[str, Any]]:
    """Mean CPU / memory per process name during vs before the anomaly (top 5 by increase)."""

    def means(snaps: list[dict[int, _Proc]]) -> dict[str, tuple[float, float]]:
        totals: dict[str, list[float]] = {}
        for snap in snaps:
            per_name: dict[str, list[float]] = {}
            for p in snap.values():
                agg = per_name.setdefault(p.name, [0.0, 0.0])
                agg[0] += p.cpu or 0.0
                agg[1] += float(p.mem or 0)
            for name, (cpu, mem) in per_name.items():
                t = totals.setdefault(name, [0.0, 0.0])
                t[0] += cpu
                t[1] += mem
        n = max(1, len(snaps))
        return {k: (v[0] / n, v[1] / n) for k, v in totals.items()}

    cur, base = means(during), means(before)
    rows: list[tuple[float, dict[str, Any]]] = []
    for name, (cpu, mem) in cur.items():
        b_cpu, b_mem = base.get(name, (0.0, 0.0))
        rank = ((mem - b_mem) if by_memory else (cpu - b_cpu)) if before else (mem if by_memory else cpu)
        rows.append(
            (
                rank,
                {
                    "process": name,
                    "cpu_percent_during": round(cpu, 2),
                    "cpu_percent_before": round(b_cpu, 2) if before else None,
                    "memory_bytes_during": int(mem),
                    "memory_bytes_before": int(b_mem) if before else None,
                },
            )
        )
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r for _, r in rows[:5]]


def build_summary(anomaly: Anomaly, correlated: list[dict[str, Any]], procs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    strong = [c for c in correlated if abs(c["r"]) >= 0.6]
    if strong:
        c = strong[0]
        direction = "rises" if c["r"] > 0 else "falls"
        parts.append(f"{anomaly.title} {direction} together with {c['label'].lower()} (r = {c['r']:+.2f}).")
    if procs:
        top = procs[0]
        if anomaly.metric_key.startswith("memory"):
            parts.append(
                f"Largest memory user during the anomaly: {top['process']} "
                f"({top['memory_bytes_during'] / 1024**3:.2f} GB on average)."
            )
        else:
            parts.append(
                f"Largest CPU contributor during the anomaly: {top['process']} "
                f"({top['cpu_percent_during']:.1f} % on average)."
            )
    if not parts:
        return "No recorded signal or process correlates clearly with this anomaly in the analysed window."
    return " ".join(parts) + " Correlation indicates association, not proven causation."
