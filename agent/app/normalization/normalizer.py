"""Convert raw provider readings into canonical, validated :class:`MetricSample` objects.

* Units are converted to canonical units (deci-kelvin -> celsius, mW -> W, mWh -> Wh, mV -> V).
* Values are validated: NaN/inf, negative percentages or physically impossible temperatures are
  never forwarded as GOOD data. They become ``quality=ERROR`` with the original problem as reason.
* ``None`` values become ``UNAVAILABLE`` (or ``ERROR`` if the provider flagged a failure).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime

from app.contracts import Availability, MetricKind, MetricSample, MetricValue, Quality, category_for
from app.providers.base import Reading

_Converter = Callable[[float], float]

UNIT_CONVERSIONS: dict[str, tuple[str, _Converter]] = {
    "decikelvin": ("celsius", lambda v: v / 10.0 - 273.15),
    "kelvin": ("celsius", lambda v: v - 273.15),
    "mW": ("W", lambda v: v / 1000.0),
    "mWh": ("Wh", lambda v: v / 1000.0),
    "mV": ("V", lambda v: v / 1000.0),
}

# Plausibility bounds in canonical units. Outside -> ERROR ("malformed value"), never clamped.
_BOUNDS: dict[str, tuple[float, float]] = {
    "percent": (0.0, 100.5),
    "percent_of_nominal": (0.0, 1000.0),
    "celsius": (-40.0, 150.0),
    "V": (0.0, 60.0),
    "W": (0.0, 1000.0),
    "Wh": (0.0, 500.0),
    "MHz": (0.0, 10000.0),
    "rpm": (0.0, 20000.0),
    "bytes": (0.0, float("inf")),
    "B/s": (0.0, float("inf")),
}


class Normalizer:
    def normalize(self, reading: Reading, timestamp: datetime) -> MetricSample:
        unit = reading.unit
        value: MetricValue = reading.value
        quality = Quality.GOOD
        reason = reading.reason

        conversion = UNIT_CONVERSIONS.get(unit)
        if conversion is not None:
            unit = conversion[0]

        if value is None:
            return self._sample(
                reading,
                None,
                unit,
                timestamp,
                Quality.ERROR if reading.error else Quality.UNAVAILABLE,
                reason or "Value not reported",
            )

        if isinstance(value, (bool, str)):
            return self._sample(reading, value, unit, timestamp, quality, None)

        number = float(value)
        if not math.isfinite(number):
            return self._sample(reading, None, unit, timestamp, Quality.ERROR, f"Malformed value ({value!r})")
        if conversion is not None:
            number = conversion[1](number)
        bounds = _BOUNDS.get(unit)
        if bounds is not None and not (bounds[0] <= number <= bounds[1]):
            return self._sample(
                reading,
                None,
                unit,
                timestamp,
                Quality.ERROR,
                f"Implausible value {number:.3g} {unit} rejected (outside {bounds[0]}..{bounds[1]})",
            )
        if unit == "percent":
            number = min(number, 100.0)  # PDH rounding can yield 100.0x
        out: MetricValue = int(number) if isinstance(value, int) and conversion is None else round(number, 4)
        return self._sample(reading, out, unit, timestamp, quality, None)

    @staticmethod
    def _sample(
        reading: Reading, value: MetricValue, unit: str, ts: datetime, quality: Quality, reason: str | None
    ) -> MetricSample:
        available = value is not None and quality in (Quality.GOOD, Quality.DEGRADED)
        return MetricSample(
            metric=reading.metric,
            component=reading.component,
            value=value,
            unit=unit,
            timestamp=ts,
            source=reading.source,
            quality=quality,
            availability=Availability.AVAILABLE if available else Availability.UNAVAILABLE,
            kind=reading.kind,
            reason=reason,
            labels=dict(reading.labels),
            category=category_for(reading.metric, reading.kind is MetricKind.STATIC),
        )
