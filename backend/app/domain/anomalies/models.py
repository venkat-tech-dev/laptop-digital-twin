from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """Legacy 3-level severity (Phase 1-3 API/UI). Derived from ``Level`` for new anomalies."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Level(StrEnum):
    """Phase-4 severity (deterministic, explainable points: app/domain/anomalies/scoring.py)."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"].index(self.value)

    @property
    def legacy(self) -> Severity:
        return {
            "INFO": Severity.INFO,
            "LOW": Severity.INFO,
            "MEDIUM": Severity.WARNING,
            "HIGH": Severity.WARNING,
            "CRITICAL": Severity.CRITICAL,
        }[self.value]


class Detector(StrEnum):
    RULE = "rule"
    STATISTICAL = "statistical"  # Phase-2/3 EWMA detector (records kept; superseded by BEHAVIORAL)
    BEHAVIORAL = "behavioral"  # Phase 4: device baseline (robust z / quantile / EWMA shift)
    MULTIVARIATE = "multivariate"  # Phase 4: per-device Isolation Forest


class AnomalyType(StrEnum):
    THRESHOLD = "threshold_anomaly"  # safety limit (a fact, not a learned judgement)
    BEHAVIORAL = "behavioral_anomaly"  # unusual for this device
    VOLATILITY = "volatility_anomaly"  # unusually erratic for this device
    MULTIVARIATE = "multivariate_anomaly"  # unusual combination of signals


class Lifecycle(StrEnum):
    DETECTED = "DETECTED"
    ONGOING = "ONGOING"
    ACKNOWLEDGED = "ACKNOWLEDGED"  # derived from the acknowledgement record
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"
    EXPIRED = "EXPIRED"  # data stopped (device offline/stale): cannot be confirmed any more


LEGACY_LEVEL = {Severity.INFO: Level.LOW, Severity.WARNING: Level.MEDIUM, Severity.CRITICAL: Level.CRITICAL}


@dataclass(slots=True)
class Anomaly:
    anomaly_id: str
    device_id: str
    detector: Detector
    rule_id: str
    component_id: str
    metric_key: str
    severity: Severity
    title: str
    message: str
    value: float | str | None
    threshold: float | str | None
    started_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None = None
    context: dict[str, Any] = field(default_factory=dict)
    # ---- Phase 4 (optional: legacy records have the defaults)
    anomaly_type: AnomalyType = AnomalyType.THRESHOLD
    category: str | None = None
    level: Level | None = None
    confidence: float | None = None
    lifecycle: Lifecycle = Lifecycle.DETECTED
    signal_id: str | None = None
    model_version: str | None = None
    baseline_version: str | None = None
    expected_value: float | None = None
    expected_min: float | None = None
    expected_max: float | None = None
    deviation_score: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    related: list[dict[str, Any]] = field(default_factory=list)
    correlation_key: str | None = None
    occurrences: int = 1
    updated_at: datetime | None = None
    feedback: dict[str, Any] | None = None  # operator verdict (true/false positive), not detector output

    @property
    def status(self) -> str:
        return "resolved" if self.resolved_at else "active"

    @property
    def effective_level(self) -> Level:
        return self.level or LEGACY_LEVEL[self.severity]

    @property
    def persistence_s(self) -> float:
        end = self.resolved_at or self.last_seen_at
        return max(0.0, (end - self.started_at).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        return {
            "anomaly_id": self.anomaly_id,
            "device_id": self.device_id,
            "detector": self.detector.value,
            "rule_id": self.rule_id,
            "component_id": self.component_id,
            "metric_key": self.metric_key,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "started_at": self.started_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "status": self.status,
            "context": self.context,
            "anomaly_type": self.anomaly_type.value,
            "category": self.category,
            "level": self.effective_level.value,
            "confidence": self.confidence,
            "lifecycle": self.lifecycle.value,
            "signal_id": self.signal_id,
            "model_version": self.model_version,
            "baseline_version": self.baseline_version,
            "expected_value": self.expected_value,
            "expected_min": self.expected_min,
            "expected_max": self.expected_max,
            "deviation_score": self.deviation_score,
            "persistence_s": round(self.persistence_s, 1),
            "evidence": self.evidence,
            "related": self.related,
            "correlation_key": self.correlation_key,
            "occurrences": self.occurrences,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "feedback": self.feedback,
        }
