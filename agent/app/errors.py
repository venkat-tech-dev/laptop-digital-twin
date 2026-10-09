"""Exception hierarchy for telemetry collection.

Providers raise these so the scheduler can translate a failure into a precise, user-visible reason
instead of a generic error. One provider failing never stops the others.
"""

from __future__ import annotations


class TelemetryError(Exception):
    """Base class for all agent-side collection failures."""

    reason: str = "Telemetry collection failed"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.reason)
        self.detail = detail or self.reason


class SensorUnavailableError(TelemetryError):
    reason = "Hardware sensor not exposed"


class HardwareMissingError(TelemetryError):
    reason = "Hardware not present"


class PermissionDeniedError(TelemetryError):
    reason = "Permission denied (administrator rights required)"


class DriverUnavailableError(TelemetryError):
    reason = "Driver or provider service unavailable"


class UnsupportedMetricError(TelemetryError):
    reason = "Metric not supported on this platform"


class MalformedValueError(TelemetryError):
    reason = "Sensor returned a malformed value"


class ProviderTimeoutError(TelemetryError):
    reason = "Sensor read timed out"
