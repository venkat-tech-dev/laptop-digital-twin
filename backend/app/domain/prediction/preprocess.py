"""Time-series preparation for forecasting: validation, resampling, regime detection.

``BucketSeries`` is an incremental, bounded resampler per (device, target): raw samples are folded
into fixed buckets with the target's aggregation (mean / last / max) as they arrive, so forecasting
never rescans raw telemetry. ``prepare`` turns it into the series a model is fitted on and reports
every data-quality problem it found instead of silently forecasting through it:

* non-finite, sentinel and physically impossible values are dropped and counted
* duplicates / out-of-order samples (timestamp <= the newest folded sample) are ignored
* samples from the future (clock skew beyond ``max_future_s``) are rejected
* a stale newest sample -> STALE_DATA (never a forecast from old data)
* a jump larger than ``max_regime_jump`` between consecutive buckets (disk cleanup, charger
  plugged in, workload switch) is a regime change: only data after the last break is used
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.domain.prediction.targets import Target

SENTINEL_MAX = 4.0e9  # e.g. Windows "unknown" battery time 4294967295


@dataclass(slots=True)
class _Bucket:
    total: float = 0.0
    n: int = 0
    last: float = 0.0
    last_t: float = 0.0
    peak: float = -math.inf


@dataclass
class BucketSeries:
    bucket_s: int
    max_buckets: int
    buckets: dict[int, _Bucket] = field(default_factory=dict)
    newest_t: float = 0.0
    dropped_invalid: int = 0
    dropped_out_of_order: int = 0
    dropped_future: int = 0

    def add(
        self,
        points: Iterable[tuple[float, float]],
        plausible: tuple[float, float],
        now: float,
        max_future_s: float = 5.0,
    ) -> int:
        added = 0
        lo, hi = plausible
        for t, v in points:
            if t <= self.newest_t:
                self.dropped_out_of_order += 1
                continue
            if t > now + max_future_s:
                self.dropped_future += 1
                continue
            if not math.isfinite(v) or abs(v) >= SENTINEL_MAX or not lo <= v <= hi:
                self.dropped_invalid += 1
                self.newest_t = t
                continue
            self.newest_t = t
            b = self.buckets.setdefault(int(t // self.bucket_s), _Bucket())
            b.total += v
            b.n += 1
            b.last, b.last_t = v, t
            b.peak = max(b.peak, v)
            added += 1
        if len(self.buckets) > self.max_buckets:
            for k in sorted(self.buckets)[: len(self.buckets) - self.max_buckets]:
                del self.buckets[k]
        return added

    def load_history(self, points: Iterable[tuple[float, float]]) -> None:
        """Seed with already-aggregated history (one value per bucket, e.g. from TimescaleDB)."""
        for t, v in points:
            if not math.isfinite(v):
                continue
            k = int(t // self.bucket_s)
            if k not in self.buckets:
                self.buckets[k] = _Bucket(v, 1, v, t, v)
                self.newest_t = max(self.newest_t, t)

    def series(self, aggregation: str, since: float) -> list[tuple[float, float]]:
        out = []
        for k in sorted(self.buckets):
            b = self.buckets[k]
            if b.n == 0:
                continue
            t = (k + 0.5) * self.bucket_s
            if t < since:
                continue
            v = b.last if aggregation == "last" else b.peak if aggregation == "max" else b.total / b.n
            out.append((t, v))
        return out


@dataclass(frozen=True, slots=True)
class Prepared:
    points: list[tuple[float, float]]  # (bucket centre epoch s, value), regime-trimmed
    span_s: float
    coverage: float  # share of expected buckets present over the used span
    newest_age_s: float | None
    largest_gap_s: float
    regime_break_at: float | None  # epoch of the last break inside the window
    regime_jump: float | None
    dropped_invalid: int
    issue: str | None  # None | STALE_DATA | INSUFFICIENT_HISTORY


def prepare(series: BucketSeries, target: Target, now: float, regime_start: float | None = None) -> Prepared:
    pts = series.series(target.aggregation, now - target.window_s)
    if regime_start is not None:  # external regime boundary (battery: since unplugged)
        pts = [p for p in pts if p[0] >= regime_start]
    newest_age = (now - series.newest_t) if series.newest_t else None
    brk: float | None = None
    jump: float | None = None
    for (_t0, v0), (t1, v1) in itertools.pairwise(pts):
        if abs(v1 - v0) > target.max_regime_jump:
            brk, jump = t1, v1 - v0
    if brk is not None:
        pts = [p for p in pts if p[0] >= brk]
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(pts)]
    span = pts[-1][0] - pts[0][0] if len(pts) > 1 else 0.0
    expected = span / target.bucket_s + 1 if span else 1
    coverage = min(1.0, len(pts) / expected) if pts else 0.0
    issue = None
    if newest_age is None or newest_age > target.stale_after_s:
        issue = "STALE_DATA"
    elif len(pts) < target.min_points or span < target.min_history_s:
        issue = "INSUFFICIENT_HISTORY"
    return Prepared(
        pts, span, coverage, newest_age, max(gaps, default=0.0), brk, jump, series.dropped_invalid, issue
    )
