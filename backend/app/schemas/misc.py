from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain.simulation.model import Scenario


class SimulationRequest(BaseModel):
    scenario: Scenario
    duration_minutes: int = Field(default=10, ge=1, le=240)
    cpu_load: float | None = Field(default=None, ge=0.0, le=1.0)
    gpu_load: float | None = Field(default=None, ge=0.0, le=1.0)
    ram_gb: float | None = Field(default=None, ge=0.0, le=256.0)
    on_battery: bool | None = None
    ambient_c: float | None = Field(default=None, ge=0.0, le=45.0)
    thermal_profile: Literal["quiet", "balanced", "performance"] | None = None
    adapter_w: float | None = Field(default=None, ge=20.0, le=240.0)

    def overrides(self) -> dict[str, float | bool]:
        out: dict[str, float | bool] = {}
        for key in ("cpu_load", "gpu_load", "ram_gb", "on_battery"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


class SimulationOut(BaseModel):
    mode: str
    label: str
    generated_at: datetime
    device_id: str
    baseline_captured_at: datetime | None
    baseline_device_status: str
    scenario: str
    workload: dict[str, Any]
    duration_s: int
    current: dict[str, Any]
    predicted: dict[str, Any]
    difference: dict[str, Any]
    assumptions: list[str]
    confidence: str
    confidence_score: float
    warnings: list[str]
    trajectory: list[dict[str, Any]]


class TokenRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_at: datetime


class AuthConfigOut(BaseModel):
    mode: str
    websocket_token_param: str = "token"  # noqa: S105
    setup_required: bool = False
    setup_token_required: bool = False


class ReadinessOut(BaseModel):
    status: str
    checks: dict[str, dict[str, Any]]


class SystemInfoOut(BaseModel):
    app_env: str
    version: str
    persistence: str
    timescaledb: bool
    redis: str
    auth_mode: str
    websocket_clients: int
    persisted_samples: int
    persist_queue_depth: int
    uptime_s: float
    retention_days: int = 0
    retention_mechanism: str = ""
    persist_sample_interval_s: float = 0
    history_aggregation: str = ""
    persist_excluded_prefixes: list[str] = []
