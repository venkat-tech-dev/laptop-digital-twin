"""Alert policy: configuration that decides whether an event becomes an alert, and how it is routed.

Nothing here is hard-coded inside providers; every rule is data (``AlertPolicy.public()`` /
``merged()``), editable by administrators through the API and validated.

Defaults (documented in docs/alerting.md):

    rule                  sources                       min severity  min confidence  persistence
    critical-immediate    any                           CRITICAL      -               0 s
    high-immediate        any                           HIGH          0.5             0 s
    medium-persistent     anomaly, prediction           MEDIUM        0.5             300 s
    connectivity          connectivity                  MEDIUM        -               0 s
    device-health         device_health, security       MEDIUM        -               0 s
    low-grouped           anomaly, prediction           LOW           0.5             0 s  (grouped)
    INFO events never become alerts.

    prediction time bands  <= 15 min -> HIGH, <= 1 h -> MEDIUM, <= 24 h -> LOW (raises, never lowers,
                           the forecast's own severity; a LOW-confidence forecast is not raised)
    cooldown               a recurrence of the same condition within 15 min creates a new alert that
                           is visible in-app but does not notify again (unless more severe)
    escalation             HIGH/CRITICAL unacknowledged for 30 min -> operators; 60 min -> admins
    quiet hours            per user: LOW/MEDIUM deferred to the end of quiet hours, HIGH configurable,
                           CRITICAL always immediate
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from app.domain.alerting.models import SEVERITIES

SOURCES = ("anomaly", "prediction", "connectivity", "device_health", "security", "remediation")
CATEGORIES = ("anomaly", "prediction", "connectivity", "security", "system")
CHANNELS = ("in_app", "browser", "windows", "email", "webhook")
FREQUENCIES = ("immediate", "grouped", "digest")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    sources: tuple[str, ...]
    min_severity: str
    min_confidence: float | None = None
    persistence_s: float = 0.0
    group: bool = False  # notifications grouped (never for CRITICAL)
    enabled: bool = True

    def matches(self, source: str, severity: str, confidence: float | None) -> bool:
        if not self.enabled or source not in self.sources:
            return False
        if SEVERITIES.index(severity) < SEVERITIES.index(self.min_severity):
            return False
        return self.min_confidence is None or confidence is None or confidence >= self.min_confidence


@dataclass(frozen=True)
class EscalationStep:
    after_s: float
    roles: tuple[str, ...]  # operator | admin (device owners are notified at level 0)


@dataclass(frozen=True)
class AlertPolicy:
    rules: tuple[Rule, ...] = (
        Rule("critical-immediate", SOURCES, "CRITICAL"),
        Rule("high-immediate", SOURCES, "HIGH", min_confidence=0.5),
        Rule(
            "medium-persistent", ("anomaly", "prediction"), "MEDIUM", min_confidence=0.5, persistence_s=300.0
        ),
        Rule("connectivity", ("connectivity",), "MEDIUM"),
        Rule("device-health", ("device_health", "security"), "MEDIUM"),
        Rule("remediation", ("remediation",), "MEDIUM"),  # approvals needed, failures, circuit breakers
        Rule("low-grouped", ("anomaly", "prediction"), "LOW", min_confidence=0.5, group=True),
    )
    prediction_bands: tuple[tuple[float, str], ...] = ((900.0, "HIGH"), (3600.0, "MEDIUM"), (86400.0, "LOW"))
    cooldown_s: float = 900.0
    correlation_window_s: float = 600.0
    expire_after_s: float = 86400.0  # an alert whose source never reports again
    escalation_severities: tuple[str, ...] = ("HIGH", "CRITICAL")
    escalation: tuple[EscalationStep, ...] = (
        EscalationStep(1800.0, ("operator",)),
        EscalationStep(3600.0, ("admin",)),
    )
    notify_on_resolution: bool = True  # in-app only, to users who were notified
    group_window_s: float = 600.0
    max_notifications_per_user_hour: int = 30  # fatigue guard (CRITICAL is never throttled)
    mandatory_critical_in_app: bool = True  # enterprise safeguard: CRITICAL always reaches the in-app inbox

    def rule_for(self, source: str, severity: str, confidence: float | None) -> Rule | None:
        return next((r for r in self.rules if r.matches(source, severity, confidence)), None)

    def prediction_severity(
        self, source_severity: str, eta_s: float | None, confidence_band: str | None
    ) -> str:
        if eta_s is None or confidence_band == "LOW":
            return source_severity
        band = next((sev for limit, sev in self.prediction_bands if eta_s <= limit), None)
        if band is None:
            return source_severity
        return band if SEVERITIES.index(band) > SEVERITIES.index(source_severity) else source_severity

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["prediction_bands"] = [{"max_eta_s": a, "severity": b} for a, b in self.prediction_bands]
        return d

    def merged(self, changes: dict[str, Any]) -> AlertPolicy:
        kw: dict[str, Any] = {}
        for key, value in changes.items():
            if key == "rules":
                kw["rules"] = tuple(
                    Rule(
                        r["rule_id"],
                        tuple(r["sources"]),
                        r["min_severity"],
                        r.get("min_confidence"),
                        float(r.get("persistence_s", 0)),
                        bool(r.get("group", False)),
                        bool(r.get("enabled", True)),
                    )
                    for r in value
                )
            elif key == "escalation":
                kw["escalation"] = tuple(
                    EscalationStep(float(s["after_s"]), tuple(s["roles"])) for s in value
                )
            elif key == "prediction_bands":
                kw["prediction_bands"] = tuple((float(b["max_eta_s"]), str(b["severity"])) for b in value)
            elif key == "escalation_severities":
                kw[key] = tuple(value)
            elif key in {f for f in self.__dataclass_fields__}:
                kw[key] = value
        return replace(self, **kw)


@dataclass(frozen=True)
class QuietHours:
    start: str  # "22:00" local time
    end: str  # "07:00" (may cross midnight)
    high: str = "immediate"  # immediate | defer
    medium: str = "defer"

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Preferences:
    """Per-user notification preferences (validated by the API)."""

    channels: tuple[str, ...] = ("in_app", "browser")
    severities: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    categories: tuple[str, ...] = CATEGORIES
    frequency: str = "immediate"  # immediate | grouped | digest
    digest_hour: int = 8  # local hour of the daily digest
    timezone: str = "UTC"
    quiet_hours: QuietHours | None = None
    email: str | None = None

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["quiet_hours"] = self.quiet_hours.public() if self.quiet_hours else None
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Preferences:
        qh = d.get("quiet_hours")
        return Preferences(
            channels=tuple(d.get("channels") or ("in_app", "browser")),
            severities=tuple(d.get("severities") or ("LOW", "MEDIUM", "HIGH", "CRITICAL")),
            categories=tuple(d.get("categories") or CATEGORIES),
            frequency=d.get("frequency") or "immediate",
            digest_hour=int(d.get("digest_hour", 8)),
            timezone=d.get("timezone") or "UTC",
            quiet_hours=QuietHours(**qh) if qh else None,
            email=d.get("email"),
        )


@dataclass(frozen=True)
class WebhookTarget:
    """An organisation integration endpoint (secret comes from the environment, never stored)."""

    name: str
    url: str
    min_severity: str = "HIGH"
    categories: tuple[str, ...] = CATEGORIES
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)
