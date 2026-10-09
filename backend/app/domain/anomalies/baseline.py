"""Device-specific, context-aware baselines.

A baseline answers "what is normal for *this* laptop, at *this* time?" for one signal. It is learned
from clean history (1-minute means), with incidents cut out so that an outage cannot redefine normal.

Contexts, most specific first (the first one with enough samples is used):

    how:wd:10   weekday, 10:00-10:59 (local hour of the device's UTC timestamp)
    dt:wd       all weekday minutes
    all         every minute

Status of a device's baseline for a signal:

    COLD        < cold_min_samples minutes: no device baseline; the fleet baseline is used with a
                much stricter trigger and low confidence
    DEVELOPING  enough for a device baseline, less than stable_min_span_days of history
    STABLE      >= stable_min_span_days and stable_min_samples: contextual baselines are trusted
    DEGRADED    a large share of the history had to be excluded as incidents, or the recent data is
                too sparse: detection still runs, confidence is reduced
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.domain.anomalies.policy import AnomalyPolicy
from app.domain.anomalies.stats import Summary


class BaselineStatus(StrEnum):
    COLD = "COLD"
    DEVELOPING = "DEVELOPING"
    STABLE = "STABLE"
    DEGRADED = "DEGRADED"


def day_type(ts: datetime) -> str:
    return "we" if ts.weekday() >= 5 else "wd"


def context_keys(ts: datetime) -> list[str]:
    dt = day_type(ts)
    return [f"how:{dt}:{ts.hour:02d}", f"dt:{dt}", "all"]


@dataclass(frozen=True, slots=True)
class ContextStats:
    context: str
    stats: Summary

    def public(self) -> dict[str, Any]:
        s = self.stats
        return {
            "context": self.context,
            "sample_count": s.count,
            "median": round(s.median, 4),
            "mad": round(s.mad, 4),
            "mean": round(s.mean, 4),
            "std": round(s.std, 4),
            "p05": round(s.p05, 4),
            "p25": round(s.p25, 4),
            "p50": round(s.p50, 4),
            "p75": round(s.p75, 4),
            "p90": round(s.p90, 4),
            "p95": round(s.p95, 4),
            "p99": round(s.p99, 4),
        }


@dataclass
class SignalBaseline:
    signal_id: str
    status: BaselineStatus
    version: str
    trained_from: datetime | None
    trained_until: datetime | None
    sample_count: int
    excluded_count: int
    contexts: dict[str, ContextStats] = field(default_factory=dict)
    source: str = "device"  # device | fleet

    def for_time(self, ts: datetime, min_samples: int) -> ContextStats | None:
        for key in context_keys(ts):
            c = self.contexts.get(key)
            if c is not None and c.stats.count >= min_samples:
                return c
        return self.contexts.get("all")

    def public(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "status": self.status.value,
            "version": self.version,
            "source": self.source,
            "trained_from": self.trained_from.isoformat() if self.trained_from else None,
            "trained_until": self.trained_until.isoformat() if self.trained_until else None,
            "sample_count": self.sample_count,
            "excluded_count": self.excluded_count,
            "contexts": {k: c.public() for k, c in sorted(self.contexts.items())},
        }


def _in_intervals(t: float, intervals: Sequence[tuple[float, float]]) -> bool:
    return any(a <= t <= b for a, b in intervals)


def build_signal_baseline(
    signal_id: str,
    points: Iterable[tuple[datetime, float]],
    exclude: Sequence[tuple[datetime, datetime]],
    policy: AnomalyPolicy,
    version: str,
    now: datetime | None = None,
) -> SignalBaseline:
    """Learn a baseline from (time, 1-minute mean) points, excluding incident intervals."""
    excl = [(a.timestamp(), b.timestamp()) for a, b in exclude]
    clean: list[tuple[datetime, float]] = []
    excluded = 0
    for ts, v in points:
        if _in_intervals(ts.timestamp(), excl):
            excluded += 1
            continue
        clean.append((ts, v))
    total = len(clean) + excluded
    if len(clean) < policy.cold_min_samples:
        return SignalBaseline(signal_id, BaselineStatus.COLD, version, None, None, len(clean), excluded)
    buckets: dict[str, list[float]] = {}
    for ts, v in clean:
        for key in context_keys(ts):
            buckets.setdefault(key, []).append(v)
    contexts = {k: ContextStats(k, Summary.of(vs)) for k, vs in buckets.items() if len(vs) >= 2}
    first, last = clean[0][0], clean[-1][0]
    span_days = (last - first).total_seconds() / 86400
    if total and excluded / total > policy.degraded_excluded_fraction:
        status = BaselineStatus.DEGRADED
    elif span_days >= policy.stable_min_span_days and len(clean) >= policy.stable_min_samples:
        status = BaselineStatus.STABLE
    else:
        status = BaselineStatus.DEVELOPING
    return SignalBaseline(signal_id, status, version, first, last, len(clean), excluded, contexts)


def fleet_baseline(
    signal_id: str, device_baselines: Iterable[SignalBaseline], version: str, now: datetime
) -> SignalBaseline | None:
    """Cold-start fallback: pooled view of the devices that do have a baseline ("all" context)."""
    meds: list[float] = []
    p99s: list[float] = []
    mads: list[float] = []
    for b in device_baselines:
        c = b.contexts.get("all")
        if b.source == "device" and c is not None and b.status is not BaselineStatus.COLD:
            meds.append(c.stats.median)
            p99s.append(c.stats.p99)
            mads.append(c.stats.mad)
    if not meds:
        return None
    pooled = Summary.of(meds)
    # spread: the larger of the typical within-device spread and the between-device spread
    spread_mad = max(Summary.of(mads).median, pooled.mad)
    stats = Summary(
        count=len(meds),
        median=pooled.median,
        mad=spread_mad,
        mean=pooled.mean,
        std=pooled.std,
        p05=pooled.p05,
        p25=pooled.p25,
        p50=pooled.p50,
        p75=pooled.p75,
        p90=max(pooled.p90, Summary.of(p99s).p50),
        p95=max(pooled.p95, Summary.of(p99s).p75),
        p99=max(pooled.p99, max(p99s)),
    )
    return SignalBaseline(
        signal_id,
        BaselineStatus.COLD,
        version,
        None,
        now,
        len(meds),
        0,
        {"all": ContextStats("all", stats)},
        source="fleet",
    )
