"""Centralised agent configuration. Every value can be overridden with an environment variable or the
``.env`` file; nothing else in the agent hard-codes intervals, limits, paths or endpoints.

Secrets (the enrollment key) come only from the environment; the per-device token issued by the
backend is stored encrypted with Windows DPAPI in the data directory, never in configuration.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_SERVICE_ALLOWLIST = (
    "WinDefend",  # Microsoft Defender Antivirus
    "mpssvc",  # Windows Defender Firewall
    "wuauserv",  # Windows Update
    "BITS",  # Background Intelligent Transfer
    "EventLog",
    "Dhcp",
    "Dnscache",
    "WlanSvc",
    "LanmanWorkstation",
    "W32Time",
    "SecurityHealthService",
)


class SensorProvider(StrEnum):
    AUTO = "auto"  # LibreHardwareMonitor if reachable, plus ACPI thermal zones
    LHM = "lhm"  # LibreHardwareMonitor only
    ACPI = "acpi"  # Windows ACPI thermal-zone performance counters only
    NONE = "none"


class RunMode(StrEnum):
    CONSOLE = "console"
    SERVICE = "service"
    TASK = "task"


def default_data_dir(service: bool) -> Path:
    if service:
        base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "LaptopDigitalTwin" / "agent"


class AgentSettings(BaseSettings):
    """Agent configuration (see ``.env.example`` for the documented list)."""

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore", populate_by_name=True)

    # ------------------------------------------------------------------ identity / backend
    backend_url: str = Field(default="http://127.0.0.1:8000", alias="AGENT_BACKEND_URL")
    # Enrollment secret shared with the backend. Used once to register the device; afterwards the
    # agent authenticates with its own per-device token (DPAPI-protected on disk).
    agent_ingest_key: str = Field(default="", alias="AGENT_INGEST_KEY")
    agent_id: str | None = Field(default=None, alias="AGENT_ID")  # optional fleet label; device_id is derived
    ca_bundle: str | None = Field(default=None, alias="AGENT_CA_BUNDLE")  # custom CA (corporate PKI)
    allow_insecure_localhost: bool = Field(default=True, alias="AGENT_ALLOW_INSECURE_LOCALHOST")
    request_timeout_s: float = Field(default=10.0, gt=0, alias="AGENT_REQUEST_TIMEOUT_S")
    use_device_tokens: bool = Field(default=True, alias="AGENT_USE_DEVICE_TOKENS")
    # Phase 9: single-use organization enrollment token (``ldt_enr_...``) issued by an administrator.
    # Preferred over the shared key; spent on first use (the device token is then kept with DPAPI).
    enrollment_token: str = Field(default="", alias="AGENT_ENROLLMENT_TOKEN", repr=False)
    credential_rotate_days: float = Field(default=7.0, ge=0, le=365, alias="AGENT_CREDENTIAL_ROTATE_DAYS")

    # ------------------------------------------------------------------ collection intervals
    telemetry_interval_ms: int = Field(default=5000, ge=250, alias="TELEMETRY_INTERVAL_MS")
    cpu_interval_ms: int | None = Field(default=None, ge=250, alias="CPU_INTERVAL_MS")
    memory_interval_ms: int | None = Field(default=None, ge=250, alias="MEMORY_INTERVAL_MS")
    disk_interval_ms: int | None = Field(default=None, ge=250, alias="DISK_INTERVAL_MS")
    network_interval_ms: int | None = Field(default=None, ge=250, alias="NETWORK_INTERVAL_MS")
    temperature_interval_ms: int | None = Field(default=None, ge=250, alias="TEMPERATURE_INTERVAL_MS")
    battery_interval_ms: int = Field(default=30000, ge=1000, alias="BATTERY_INTERVAL_MS")
    process_interval_ms: int = Field(default=5000, ge=1000, alias="PROCESS_INTERVAL_MS")
    disk_space_interval_ms: int = Field(default=60000, ge=5000, alias="DISK_SPACE_INTERVAL_MS")
    reliability_interval_ms: int = Field(default=60000, ge=5000, alias="RELIABILITY_INTERVAL_MS")
    network_health_interval_ms: int = Field(default=30000, ge=5000, alias="NETWORK_HEALTH_INTERVAL_MS")
    security_interval_ms: int = Field(default=300000, ge=30000, alias="SECURITY_INTERVAL_MS")
    services_interval_ms: int = Field(default=60000, ge=10000, alias="SERVICES_INTERVAL_MS")
    eventlog_interval_ms: int = Field(default=60000, ge=10000, alias="EVENTLOG_INTERVAL_MS")
    updates_interval_ms: int = Field(default=1_800_000, ge=60000, alias="UPDATES_INTERVAL_MS")
    updates_search_interval_s: int = Field(default=21600, ge=600, alias="UPDATES_SEARCH_INTERVAL_S")
    static_refresh_interval_s: int = Field(default=3600, ge=60, alias="STATIC_REFRESH_INTERVAL_S")
    # MAX_BATCH_WAIT_TIME: a batch is flushed to the outbox at least this often (HIGH/CRITICAL events
    # flush immediately). PUBLISH_INTERVAL_MS is the Phase-1 name.
    publish_interval_ms: int = Field(
        default=5000, ge=250, validation_alias=AliasChoices("MAX_BATCH_WAIT_MS", "PUBLISH_INTERVAL_MS")
    )
    heartbeat_interval_s: int = Field(default=30, ge=5, alias="HEARTBEAT_INTERVAL_S")
    # STATIC samples (capacities, versions...) are only re-sent when changed or every this many seconds.
    static_resend_s: int = Field(default=600, ge=30, alias="STATIC_RESEND_S")
    critical_temperature_c: float = Field(default=95.0, ge=50, le=120, alias="CRITICAL_TEMPERATURE_C")
    collector_failed_after: int = Field(default=3, ge=1, alias="COLLECTOR_FAILED_AFTER")
    # While the backend is unreachable, batches are coalesced to one per this interval to bound disk use.
    offline_flush_interval_s: int = Field(default=30, ge=5, alias="OFFLINE_FLUSH_INTERVAL_S")
    agent_health_interval_s: int = Field(default=60, ge=10, alias="AGENT_HEALTH_INTERVAL_S")

    # ------------------------------------------------------------------ feature switches
    hardware_sensor_provider: SensorProvider = Field(
        default=SensorProvider.AUTO, alias="HARDWARE_SENSOR_PROVIDER"
    )
    lhm_url: str = Field(default="http://127.0.0.1:8085/data.json", alias="LHM_URL")
    enable_security_collection: bool = Field(default=True, alias="ENABLE_SECURITY_COLLECTION")
    enable_update_collection: bool = Field(default=True, alias="ENABLE_UPDATE_COLLECTION")
    enable_eventlog_collection: bool = Field(default=True, alias="ENABLE_EVENTLOG_COLLECTION")
    service_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_SERVICE_ALLOWLIST), alias="SERVICE_ALLOWLIST"
    )
    # Optional ICMP latency probe beyond the default gateway (empty = gateway only; no extra traffic).
    latency_probe_host: str = Field(default="", alias="LATENCY_PROBE_HOST")
    icmp_timeout_ms: int = Field(default=1000, ge=100, le=5000, alias="ICMP_TIMEOUT_MS")
    icmp_count: int = Field(default=4, ge=1, le=20, alias="ICMP_COUNT")

    # ------------------------------------------------------------------ privacy
    top_process_count: int = Field(default=15, ge=1, le=100, alias="TOP_PROCESS_COUNT")
    collect_process_details: bool = Field(default=False, alias="COLLECT_PROCESS_DETAILS")
    include_serial_numbers: bool = Field(default=False, alias="INCLUDE_SERIAL_NUMBERS")
    include_mac_addresses: bool = Field(default=False, alias="INCLUDE_MAC_ADDRESSES")
    include_ip_addresses: bool = Field(default=False, alias="INCLUDE_IP_ADDRESSES")
    include_hostname: bool = Field(default=True, alias="INCLUDE_HOSTNAME")
    # Phase 6: show alerts routed to the "windows" channel as Windows toasts (0 = never poll)
    toast_notifications: bool = Field(default=True, alias="TOAST_NOTIFICATIONS")
    toast_poll_interval_s: float = Field(default=30.0, ge=0, le=3600, alias="TOAST_POLL_INTERVAL_S")
    # ---- Phase 8: remediation (the endpoint owner's own allowlist; the platform cannot widen it)
    remediation_enabled: bool = Field(default=True, alias="AGENT_REMEDIATION_ENABLED")
    remediation_actions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["REFRESH_TELEMETRY", "REQUEST_SYSTEM_RESCAN", "RECONNECT_AGENT"],
        alias="AGENT_REMEDIATION_ACTIONS",
    )
    restartable_applications: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="AGENT_RESTARTABLE_APPLICATIONS"
    )
    #: pin the platform signing key explicitly (base64 Ed25519 public key); empty = trust on first use
    action_public_key: str = Field(default="", alias="AGENT_ACTION_PUBLIC_KEY")
    action_poll_interval_s: float = Field(default=10.0, ge=0, le=3600, alias="AGENT_ACTION_POLL_INTERVAL_S")

    # ------------------------------------------------------------------ local store / sync
    data_dir: Path | None = Field(default=None, alias="AGENT_DATA_DIR")
    queue_max_batches: int = Field(default=20000, ge=100, alias="QUEUE_MAX_BATCHES")
    queue_max_mb: int = Field(default=200, ge=5, alias="QUEUE_MAX_MB")
    queue_max_age_h: int = Field(default=72, ge=1, alias="QUEUE_MAX_AGE_H")
    # MAX_BATCH_SIZE / MAX_BATCH_BYTES bound one upload request (batches per request, JSON bytes).
    batch_size: int = Field(
        default=50, ge=1, le=500, validation_alias=AliasChoices("MAX_BATCH_SIZE", "BATCH_SIZE")
    )
    max_batch_bytes: int = Field(default=1_000_000, ge=10_000, alias="MAX_BATCH_BYTES")
    sync_max_attempts: int = Field(default=5, ge=1, alias="SYNC_MAX_ATTEMPTS")
    backoff_base_s: float = Field(default=1.0, gt=0, alias="SYNC_BACKOFF_BASE_S")
    backoff_max_s: float = Field(default=60.0, gt=0, alias="SYNC_BACKOFF_MAX_S")

    # ------------------------------------------------------------------ self-limits / logging
    cpu_budget_percent: float = Field(default=3.0, gt=0, alias="AGENT_CPU_BUDGET_PERCENT")
    lane_hung_after_s: float = Field(default=180.0, ge=30, alias="AGENT_LANE_HUNG_AFTER_S")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_max_mb: int = Field(default=5, ge=1, alias="AGENT_LOG_MAX_MB")
    log_backups: int = Field(default=5, ge=1, alias="AGENT_LOG_BACKUPS")
    config_poll_interval_s: float = Field(default=15.0, ge=0, alias="CONFIG_POLL_INTERVAL_S")

    # Kept for backward compatibility (in-memory buffer of the previous design; now the SQLite queue).
    max_buffered_batches: int = Field(default=120, ge=1, alias="AGENT_MAX_BUFFERED_BATCHES")

    @field_validator("service_allowlist", "remediation_actions", "restartable_applications", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _level(cls, value: str) -> str:
        level = value.upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return level

    def interval(self, override: int | None) -> int:
        return override or self.telemetry_interval_ms

    @property
    def effective_cpu_interval_ms(self) -> int:
        return self.interval(self.cpu_interval_ms)

    def resolved_data_dir(self, run_mode: RunMode) -> Path:
        return self.data_dir or default_data_dir(run_mode is RunMode.SERVICE)
