"""Diagnostic context: the minimised, structured, sanitised input to evidence collection and reasoning.

Data minimisation (what can reach a model):
    operational metrics (CPU, memory, temperature, drive, network latency/loss, battery) as numbers,
    Phase 4 anomalies / Phase 5 predictions / Phase 6 alerts (titles, levels, values), process *names*
    with CPU / memory figures (only when the enterprise policy shows process names; otherwise
    "process #n"), security posture findings, recent twin timeline messages, device model / OS.
Never: command lines, paths, user names, host names, IP / MAC addresses, serial numbers, window titles,
documents, clipboard, keystrokes, credentials or tokens (the platform does not collect them).

Every string from the endpoint is untrusted: ``clean`` strips control characters and markup-like
sequences and caps the length, and the prompt carries it as JSON data, never as instructions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


def clean(value: Any, limit: int = 80) -> str:
    """Untrusted endpoint string -> safe, short data string."""
    s = _CTRL.sub(" ", str(value))
    s = s.replace("```", "'''").replace("<", "(").replace(">", ")")
    return _SPACES.sub(" ", s).strip()[:limit]


@dataclass
class SeriesSummary:
    key: str  # cpu, memory, temperature, disk_active, disk_usage, net_latency, packet_loss, gpu, battery
    label: str
    unit: str
    points: list[tuple[float, float]]  # (epoch s, value) 1-minute means, oldest first
    baseline_median: float | None = None
    baseline_high: float | None = None  # usual upper bound (p95) for this device and time
    warning: float | None = None
    critical: float | None = None
    direction: str = "up"  # "down": low values are bad (battery)
    #: where baseline_high comes from: "device" (Phase 4 learned, per hour/day type) or "window" (typical
    #: level of this hour's data, used while the device baseline is still cold)
    baseline_source: str = "device"

    @property
    def current(self) -> float | None:
        return self.points[-1][1] if self.points else None


@dataclass
class ProcessFigure:
    name: str  # cleaned; "process #n" when names are not permitted
    cpu_during: float
    cpu_before: float | None
    mem_during_mb: float
    mem_before_mb: float | None
    started_at: float | None = None  # epoch s, when the agent reports it


@dataclass
class DiagnosticContext:
    device_id: str
    generated_at: float  # epoch s
    trigger: dict[str, Any]  # {kind, id, title, severity, signal, metric, started_at}
    device: dict[str, Any] = field(default_factory=dict)
    series: dict[str, SeriesSummary] = field(default_factory=dict)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    processes: list[ProcessFigure] = field(default_factory=list)
    process_window: dict[str, Any] = field(default_factory=dict)  # {"start","end","snapshots","before"}
    process_names_allowed: bool = True
    security: dict[str, Any] = field(default_factory=dict)  # {"posture", "findings": [str]}
    timeline: list[dict[str, Any]] = field(default_factory=list)
    data_quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        """Stable hash of the *material* facts (rounded), for caching: unchanged facts -> same hash."""

        def r(v: float | None, step: float) -> float | None:
            return None if v is None else round(round(v / step) * step, 3)

        material = {
            "trigger": {k: self.trigger.get(k) for k in ("kind", "id", "severity")},
            "series": {
                k: [r(s.current, 5.0), r(s.baseline_high, 5.0)] for k, s in sorted(self.series.items())
            },
            "anomalies": sorted((a.get("id"), a.get("level"), a.get("status")) for a in self.anomalies),
            "predictions": sorted(
                (p.get("id"), p.get("status"), r(p.get("eta_s"), 600.0)) for p in self.predictions
            ),
            "alerts": sorted((a.get("id"), a.get("severity"), a.get("status")) for a in self.alerts),
            "processes": [
                (p.name, r(p.cpu_during, 10.0), r(p.mem_during_mb, 500.0)) for p in self.processes[:3]
            ],
            "security": sorted(self.security.get("findings") or []),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:32]
