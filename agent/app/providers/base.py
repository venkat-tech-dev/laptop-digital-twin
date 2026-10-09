"""Telemetry provider abstraction.

A provider reads one hardware domain and returns :class:`Reading` objects in the *raw* units of its
source (for example deci-kelvin or milliwatts). Unit conversion and validation are the job of the
normalization layer, so providers stay small and testable.

Rules every provider follows:

* Never invent a value. If a metric cannot be read, return ``Reading.unavailable(...)`` with a reason.
* Failures are local: one metric failing must not prevent the others from being reported.
* ``collect`` is synchronous and runs on the platform worker thread.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.contracts import MetricKind, MetricValue


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Declares a metric a provider is responsible for (used to report failures precisely)."""

    metric: str
    unit: str
    source: str


@dataclass(frozen=True, slots=True)
class Reading:
    metric: str
    component: str
    value: MetricValue
    unit: str
    source: str
    kind: MetricKind = MetricKind.MEASURED
    labels: Mapping[str, str] = field(default_factory=dict)
    reason: str | None = None
    error: bool = False

    @classmethod
    def unavailable(
        cls,
        metric: str,
        component: str,
        unit: str,
        source: str,
        reason: str,
        labels: Mapping[str, str] | None = None,
    ) -> Reading:
        return cls(metric, component, None, unit, source, labels=labels or {}, reason=reason)

    @classmethod
    def failed(
        cls,
        metric: str,
        component: str,
        unit: str,
        source: str,
        reason: str,
        labels: Mapping[str, str] | None = None,
    ) -> Reading:
        return cls(metric, component, None, unit, source, labels=labels or {}, reason=reason, error=True)


class TelemetryProvider(ABC):
    """Base class for all hardware-domain providers."""

    name: str = "provider"
    component: str = "system"
    #: Worker lane (see ``app.platform.worker``): collectors on different lanes never block each other.
    lane: str = "fast"
    #: Per-collection timeout; on expiry the collector's metrics are reported as ERROR for that cycle.
    timeout_s: float = 15.0

    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms

    @property
    @abstractmethod
    def declared_metrics(self) -> list[MetricSpec]:
        """Metrics reported as ERROR if ``collect`` raises unexpectedly."""

    @abstractmethod
    def collect(self) -> list[Reading]:
        """Read the current values. Must not block for long (< 1 s)."""

    def close(self) -> None:  # noqa: B027  (optional hook)
        """Release OS handles."""

    def _r(
        self,
        metric: str,
        value: MetricValue,
        unit: str,
        source: str,
        *,
        kind: MetricKind = MetricKind.MEASURED,
        labels: Mapping[str, str] | None = None,
        reason: str | None = None,
    ) -> Reading:
        return Reading(metric, self.component, value, unit, source, kind, labels or {}, reason)

    def _na(
        self, metric: str, unit: str, source: str, reason: str, labels: Mapping[str, str] | None = None
    ) -> Reading:
        return Reading.unavailable(metric, self.component, unit, source, reason, labels)
