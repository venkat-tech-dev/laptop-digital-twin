"""Windows Performance Data Helper (PDH) wrapper.

Counters are added by their *English* names (``AddEnglishCounter``) so the agent also works on
localized Windows installations. Rate counters need two collections before they produce data; until
then values are reported as ``None`` (warming up) rather than a made-up number.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.errors import DriverUnavailableError, SensorUnavailableError

_PDH_FMT_DOUBLE = 0x00000200
_PDH_FMT_NOCAP100 = 0x00008000


class CounterReader(Protocol):
    def collect(self) -> dict[str, float | dict[str, float] | None]: ...

    def close(self) -> None: ...


@dataclass
class _Counter:
    key: str
    path: str
    wildcard: bool
    handle: Any


@dataclass
class PdhCounterSet:
    """A PDH query holding a fixed set of counters, keyed by caller-chosen names."""

    paths: dict[str, str]
    _query: Any = field(init=False, default=None)
    _counters: list[_Counter] = field(init=False, default_factory=list)
    missing: dict[str, str] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if sys.platform != "win32":
            raise DriverUnavailableError("Performance counters are only available on Windows")
        import win32pdh

        self._pdh = win32pdh
        self._query = win32pdh.OpenQuery()
        add = getattr(win32pdh, "AddEnglishCounter", win32pdh.AddCounter)
        for key, path in self.paths.items():
            try:
                handle = add(self._query, path)
            except Exception as exc:  # counter object not present on this machine
                self.missing[key] = f"Performance counter not available: {path} ({exc})"
                continue
            self._counters.append(_Counter(key, path, "(*)" in path, handle))
        if not self._counters:
            raise SensorUnavailableError("None of the requested performance counters exist")
        # Prime rate counters.
        try:
            win32pdh.CollectQueryData(self._query)
        except Exception as exc:
            raise SensorUnavailableError(f"PDH collection failed: {exc}") from exc

    def collect(self) -> dict[str, float | dict[str, float] | None]:
        pdh = self._pdh
        try:
            pdh.CollectQueryData(self._query)
        except Exception as exc:
            raise SensorUnavailableError(f"PDH collection failed: {exc}") from exc
        fmt = _PDH_FMT_DOUBLE | _PDH_FMT_NOCAP100
        out: dict[str, float | dict[str, float] | None] = {}
        for counter in self._counters:
            try:
                if counter.wildcard:
                    raw = pdh.GetFormattedCounterArray(counter.handle, fmt)
                    out[counter.key] = {str(k): float(v) for k, v in raw.items()}
                else:
                    _, value = pdh.GetFormattedCounterValue(counter.handle, fmt)
                    out[counter.key] = float(value)
            except Exception:
                # PDH_INVALID_DATA / PDH_CALC_NEGATIVE_VALUE: no valid sample this cycle.
                out[counter.key] = None
        return out

    def close(self) -> None:
        if self._query is not None:
            try:
                self._pdh.CloseQuery(self._query)
            finally:
                self._query = None
