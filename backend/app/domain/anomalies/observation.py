"""Observations: what the detectors look at, after data-quality checks.

An observation is the mean of one signal's samples in the last ``observation_window_s`` (the same
1-minute-scale quantity the baselines are learned on). It is only produced when the data is
trustworthy *now*:

* the device is connected (ONLINE / DEGRADED) - never from the last state of an offline device
* the newest sample is fresh (not older than the signal's live limit)
* enough samples arrived in the window (``min_window_coverage`` of the expected count)
* impossible values (percent outside 0-100, negative rates/temperatures) are dropped and counted

Out-of-order and duplicate samples never reach this point: the twin only appends readings that are
newer than the current one (Phase 2/3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.domain.anomalies.signals import Signal


class QualityIssue(StrEnum):
    OK = "ok"
    DEVICE_NOT_CONNECTED = "device_not_connected"
    NO_DATA = "no_data"
    STALE = "stale"
    INSUFFICIENT = "insufficient_samples"
    IMPOSSIBLE = "impossible_values"


@dataclass(frozen=True, slots=True)
class Observation:
    signal: Signal
    value: float  # mean over the window
    latest: float
    n: int
    coverage: float  # 0..1
    newest_ts: float
    points: tuple[tuple[float, float], ...]  # (epoch_s, value) inside the window
    dropped_impossible: int = 0


def plausible(signal: Signal, v: float) -> bool:
    if v != v:  # NaN
        return False
    if signal.unit == "%":
        return 0.0 <= v <= 100.0
    if signal.unit == "°C":
        return -20.0 <= v <= 130.0
    return v >= 0.0


def observe(
    signal: Signal,
    points: Sequence[tuple[float, float]],
    now: float,
    window_s: float,
    interval_s: float,
    live_limit_s: float,
    min_coverage: float,
    connected: bool,
) -> tuple[Observation | None, QualityIssue]:
    if not connected:
        return None, QualityIssue.DEVICE_NOT_CONNECTED
    recent = [(t, v) for t, v in points if now - window_s <= t <= now + 1.0]
    if not recent:
        return None, QualityIssue.NO_DATA if not points else QualityIssue.STALE
    if now - recent[-1][0] > live_limit_s:
        return None, QualityIssue.STALE
    good = [(t, v) for t, v in recent if plausible(signal, v)]
    dropped = len(recent) - len(good)
    if not good:
        return None, QualityIssue.IMPOSSIBLE
    expected = max(1.0, window_s / max(interval_s, 0.5))
    coverage = min(1.0, len(good) / expected)
    if coverage < min_coverage:
        return None, QualityIssue.INSUFFICIENT
    mean = sum(v for _, v in good) / len(good)
    return (
        Observation(signal, mean, good[-1][1], len(good), coverage, good[-1][0], tuple(good), dropped),
        QualityIssue.OK,
    )
