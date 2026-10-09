"""Operator configuration pulled from the backend (``GET /api/v1/agent/config``).

The agent stays authoritative over what it *can* do: values are clamped to the same bounds as the
local settings, and an unreachable backend simply keeps the current configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.settings import AgentSettings


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    telemetry_interval_ms: int
    process_interval_ms: int
    top_process_count: int
    collect_process_details: bool
    version: int = 0

    @classmethod
    def from_settings(cls, s: AgentSettings) -> RemoteConfig:
        return cls(
            s.telemetry_interval_ms, s.process_interval_ms, s.top_process_count, s.collect_process_details
        )

    def merged(self, data: dict[str, Any]) -> RemoteConfig:
        def clamp(value: Any, lo: int, hi: int, default: int) -> int:
            try:
                return max(lo, min(hi, int(value)))
            except (TypeError, ValueError):
                return default

        return RemoteConfig(
            telemetry_interval_ms=clamp(
                data.get("telemetry_interval_ms"), 250, 60_000, self.telemetry_interval_ms
            ),
            process_interval_ms=clamp(
                data.get("process_interval_ms"), 1000, 60_000, self.process_interval_ms
            ),
            top_process_count=clamp(data.get("top_process_count"), 1, 100, self.top_process_count),
            collect_process_details=bool(data.get("collect_process_details", self.collect_process_details)),
            version=clamp(data.get("version"), 0, 2**31, self.version),
        )
