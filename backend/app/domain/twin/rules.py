"""Deterministic, documented rules of the digital twin (no ML, no hidden scoring).

Severity (visual state of a metric):   normal < elevated < warning < critical
    A value enters a level when it crosses the level's threshold; it only drops back to a lower
    level once it is ``hysteresis`` below that threshold (no flapping at a boundary).

Freshness of a metric (from its own collection interval, not a global constant):
    wait     = TWIN_PUBLISH_WAIT_S (agent batching) + TWIN_FRESHNESS_GRACE_S (transport, processing)
    LIVE     age <= interval + wait
    RECENT   age <= max(4 x interval, 60 s) + wait
    STALE    older
    OFFLINE  the device is offline (overrides the above)
    UNKNOWN  never reported            UNSUPPORTED  the agent reports the sensor as unavailable
    Static values (capacity, model...) do not age: they follow the device connectivity.

Connectivity of the device (heartbeat presence + telemetry):
    ONLINE    agent heartbeats and telemetry arrives on time
    DEGRADED  agent heartbeats, but telemetry is late or >= 3 collectors are failing
    STALE     heartbeats missed (PRESENCE_STALE_AFTER_S)
    OFFLINE   no contact for PRESENCE_OFFLINE_AFTER_S
    UNKNOWN   never contacted since the backend started and no telemetry

Health of the device: the worst of the rules in ``HEALTH_RULES`` (each documented below); UNKNOWN
when the device is OFFLINE/UNKNOWN (the last known health is kept as ``last_known``) or when no rule
has any data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    WARNING = "warning"
    CRITICAL = "critical"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


SEVERITY_RANK = {
    Severity.UNKNOWN: -1,
    Severity.OFFLINE: -1,
    Severity.NORMAL: 0,
    Severity.ELEVATED: 1,
    Severity.WARNING: 2,
    Severity.CRITICAL: 3,
}


class Freshness(StrEnum):
    LIVE = "LIVE"
    RECENT = "RECENT"
    STALE = "STALE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class Connectivity(StrEnum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class Health(StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


HEALTH_RANK = {Health.UNKNOWN: -1, Health.HEALTHY: 0, Health.WARNING: 1, Health.CRITICAL: 2}


@dataclass(frozen=True, slots=True)
class Thresholds:
    elevated: float | None
    warning: float | None
    critical: float | None
    higher_is_worse: bool = True
    hysteresis: float = 3.0

    def classify(self, value: float, previous: Severity | None) -> Severity:
        levels = [
            (Severity.CRITICAL, self.critical),
            (Severity.WARNING, self.warning),
            (Severity.ELEVATED, self.elevated),
        ]
        raw = Severity.NORMAL
        for sev, limit in levels:
            if limit is not None and self._beyond(value, limit, 0.0):
                raw = sev
                break
        if previous is None or SEVERITY_RANK.get(previous, -1) <= SEVERITY_RANK[raw]:
            return raw
        # dropping to a lower level: stay at the higher level until clearly past its threshold
        limit = dict(levels).get(previous)
        if limit is not None and self._beyond(value, limit, -self.hysteresis):
            return previous
        return raw

    def _beyond(self, value: float, limit: float, slack: float) -> bool:
        return value >= limit + slack if self.higher_is_worse else value <= limit - slack


#: Visual thresholds per twin field (the same numbers feed the health rules, so the UI and the
#: health verdict never disagree).
THRESHOLDS: dict[str, Thresholds] = {
    "performance.cpu.usage_percent": Thresholds(60, 90, 95),
    "performance.cpu.temperature_c": Thresholds(80, 90, 98, hysteresis=2.0),
    "performance.memory.usage_percent": Thresholds(70, 90, 95),
    "performance.disk.usage_percent": Thresholds(80, 90, 95, hysteresis=1.0),
    "performance.disk.active_time_percent": Thresholds(50, 90, 98),
    "performance.gpu.usage_percent": Thresholds(60, 90, 95),
    "thermal.temperature_c": Thresholds(80, 90, 98, hysteresis=2.0),
    "battery.charge_percent": Thresholds(30, 15, 5, higher_is_worse=False, hysteresis=2.0),
    "battery.health_percent": Thresholds(80, 60, 40, higher_is_worse=False, hysteresis=1.0),
    "network.gateway_latency_ms": Thresholds(50, 150, 500, hysteresis=10.0),
    "network.packet_loss_percent": Thresholds(1, 5, 20, hysteresis=0.5),
    "storage.wear_percent": Thresholds(70, 90, 100, hysteresis=0.0),
    "security.signature_age_days": Thresholds(3, 7, 30, hysteresis=0.0),
}

#: Boolean fields whose ``False`` (or listed value) is a warning/critical condition.
BOOLEAN_ALERTS: dict[str, tuple[object, Severity]] = {
    "network.internet_connected": (False, Severity.WARNING),
    "network.device_connected": (False, Severity.WARNING),
    "security.realtime_protection": (False, Severity.CRITICAL),
    "security.antivirus_enabled": (False, Severity.CRITICAL),
    "security.firewall_enabled": (False, Severity.WARNING),
    "security.secure_boot": (False, Severity.WARNING),
    "storage.smart_critical_warning": (True, Severity.CRITICAL),
    "storage.health_ok": (False, Severity.CRITICAL),
    "thermal.throttling": (True, Severity.ELEVATED),
    "operating_system.reboot_required": (True, Severity.ELEVATED),
}

#: Which fields make up each visual section of the twin (the 3D/2D visualization reads these).
SECTIONS: dict[str, tuple[str, ...]] = {
    "cpu": ("performance.cpu.usage_percent", "performance.cpu.temperature_c"),
    "memory": ("performance.memory.usage_percent",),
    "storage": (
        "performance.disk.usage_percent",
        "performance.disk.active_time_percent",
        "storage.wear_percent",
        "storage.smart_critical_warning",
        "storage.health_ok",
    ),
    "gpu": ("performance.gpu.usage_percent",),
    "network": (
        "network.internet_connected",
        "network.device_connected",
        "network.gateway_latency_ms",
        "network.packet_loss_percent",
    ),
    "battery": ("battery.charge_percent", "battery.health_percent"),
    "thermal": ("thermal.temperature_c", "thermal.throttling"),
    "security": (
        "security.realtime_protection",
        "security.antivirus_enabled",
        "security.firewall_enabled",
        "security.secure_boot",
        "security.signature_age_days",
    ),
}


@dataclass(frozen=True, slots=True)
class HealthRule:
    rule_id: str
    description: str
    fields: tuple[str, ...]
    #: worst field severity -> health contribution
    warning_at: Severity = Severity.WARNING
    critical_at: Severity = Severity.CRITICAL


HEALTH_RULES: tuple[HealthRule, ...] = (
    HealthRule(
        "thermal", "CPU-area temperature >= 90 C warning, >= 98 C critical", ("thermal.temperature_c",)
    ),
    HealthRule(
        "memory", "Memory in use >= 90 % warning, >= 95 % critical", ("performance.memory.usage_percent",)
    ),
    HealthRule(
        "disk_space",
        "System volume >= 90 % full warning, >= 95 % critical",
        ("performance.disk.usage_percent",),
    ),
    HealthRule(
        "drive",
        "Drive SMART critical warning or Windows health != Healthy (critical); wear >= 90 % (warning)",
        ("storage.smart_critical_warning", "storage.health_ok", "storage.wear_percent"),
    ),
    HealthRule(
        "battery",
        "On battery: charge <= 15 % warning, <= 5 % critical; battery health < 60 % warning, < 40 % critical",
        ("battery.charge_percent", "battery.health_percent"),
    ),
    HealthRule(
        "network",
        "No network connection or no internet access (warning)",
        ("network.device_connected", "network.internet_connected"),
    ),
    HealthRule(
        "security",
        "Protection/antivirus off (critical); firewall or Secure Boot off, signatures > 7 days (warning)",
        (
            "security.realtime_protection",
            "security.antivirus_enabled",
            "security.firewall_enabled",
            "security.secure_boot",
            "security.signature_age_days",
        ),
    ),
)

#: CPU is only a health problem when sustained (a busy CPU is normal): mean >= 90 % for 120 s.
CPU_SUSTAINED_WINDOW_S = 120.0
CPU_SUSTAINED_PERCENT = 90.0
#: Agent-reported problems that degrade health.
AGENT_FAILING_COLLECTORS_WARN = 3
AGENT_QUEUE_WARN = 1000
