"""Diagnosis domain model: evidence, hypotheses, diagnosis (versioned)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class DiagnosisType(StrEnum):
    CPU_PRESSURE = "CPU_PRESSURE"
    MEMORY_PRESSURE = "MEMORY_PRESSURE"
    DISK_PRESSURE = "DISK_PRESSURE"
    THERMAL_ISSUE = "THERMAL_ISSUE"
    BATTERY_DEGRADATION = "BATTERY_DEGRADATION"
    NETWORK_DEGRADATION = "NETWORK_DEGRADATION"
    APPLICATION_INSTABILITY = "APPLICATION_INSTABILITY"
    SYSTEM_INSTABILITY = "SYSTEM_INSTABILITY"
    STARTUP_PERFORMANCE = "STARTUP_PERFORMANCE"
    SECURITY_STATE = "SECURITY_STATE"
    RESOURCE_CONTENTION = "RESOURCE_CONTENTION"
    UNKNOWN = "UNKNOWN"


class DiagnosisStatus(StrEnum):
    GENERATING = "GENERATING"
    AVAILABLE = "AVAILABLE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class EvidenceType(StrEnum):
    OBSERVATION = "OBSERVATION"
    TREND = "TREND"
    ANOMALY = "ANOMALY"
    PREDICTION = "PREDICTION"
    PROCESS = "PROCESS"
    EVENT = "EVENT"
    CORRELATION = "CORRELATION"
    BASELINE = "BASELINE"
    ABSENCE_OF_EXPECTED_SIGNAL = "ABSENCE_OF_EXPECTED_SIGNAL"
    TEMPORAL = "TEMPORAL"


@dataclass(slots=True)
class Evidence:
    """One traceable fact. ``ref`` points at the record it came from (metric series, anomaly id,
    prediction id, alert id, process snapshot window, timeline event)."""

    evidence_id: str  # E1, E2 ... (stable within one diagnosis)
    type: EvidenceType
    source: str  # twin | telemetry_history | anomaly_engine | forecaster | process_snapshots | timeline
    statement: str  # human wording, built from the values below (never free text from the model)
    signal: str | None = None  # cpu, memory, temperature ...
    metric: str | None = None
    observed: float | None = None
    baseline: float | None = None
    unit: str | None = None
    deviation: float | None = None
    timestamp: str | None = None
    window: dict[str, Any] | None = None  # {"start", "end"} the statement covers
    process: str | None = None
    ref: dict[str, Any] = field(default_factory=dict)  # {"kind": "anomaly", "id": "..."} etc.
    strength: float = 0.5  # 0..1 how strongly it indicates *something* (not which hypothesis)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d


@dataclass(slots=True)
class Hypothesis:
    code: str  # e.g. cpu.process
    category: DiagnosisType
    cause: str  # "Sustained CPU usage by one application"
    supporting: list[str] = field(default_factory=list)  # evidence ids
    contradicting: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # what would confirm / refute it
    recommendations: list[str] = field(default_factory=list)  # safe human investigation steps
    confidence: float = 0.0
    confidence_level: str = "INSUFFICIENT"
    factors: dict[str, float] = field(default_factory=dict)
    origin: str = "rules"  # rules | model

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        return d


@dataclass(slots=True)
class Diagnosis:
    diagnosis_id: str
    series_id: str  # all versions of one diagnosis share it
    version: int
    tenant_id: str
    device_id: str
    trigger_kind: str  # alert | anomaly | prediction | manual
    trigger_id: str | None
    alert_id: str | None
    anomaly_id: str | None
    prediction_id: str | None
    status: DiagnosisStatus
    diagnosis_type: DiagnosisType
    category: str
    severity: str | None
    summary: str
    likely_cause: str | None
    confidence: float
    confidence_level: str
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    explanation: dict[str, Any]  # what happened / why / supports / contradicts / missing / investigate
    related_processes: list[str]
    related_events: list[str]
    reasoning_model: str  # rules | ollama:<model>
    model_version: str
    prompt_version: str | None
    context_fingerprint: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    notices: list[str] = field(
        default_factory=list
    )  # e.g. "AI reasoning unavailable: deterministic evidence"
    rejected_claims: list[dict[str, Any]] = field(default_factory=list)  # model output that failed validation
    feedback: list[dict[str, Any]] = field(default_factory=list)
    supersedes: str | None = None
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        out: dict[str, Any] = {
            "diagnosis_id": self.diagnosis_id,
            "series_id": self.series_id,
            "version": self.version,
            "tenant_id": self.tenant_id,
            "device_id": self.device_id,
            "trigger_kind": self.trigger_kind,
            "trigger_id": self.trigger_id,
            "alert_id": self.alert_id,
            "anomaly_id": self.anomaly_id,
            "prediction_id": self.prediction_id,
            "status": self.status.value,
            "diagnosis_type": self.diagnosis_type.value,
            "category": self.category,
            "severity": self.severity,
            "summary": self.summary,
            "likely_cause": self.likely_cause,
            "confidence": self.confidence,
            "confidence_level": self.confidence_level,
            "reasoning_model": self.reasoning_model,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "expires_at": iso(self.expires_at),
            "notices": self.notices,
            "supersedes": self.supersedes,
            "alternative_causes": [h.cause for h in self.hypotheses[1:4]],
        }
        if full:
            out.update(
                {
                    "hypotheses": [h.public() for h in self.hypotheses],
                    "evidence": [e.public() for e in self.evidence],
                    "explanation": self.explanation,
                    "related_processes": self.related_processes,
                    "related_events": self.related_events,
                    "rejected_claims": self.rejected_claims,
                    "feedback": self.feedback,
                    "timings": self.timings,
                    "context_fingerprint": self.context_fingerprint,
                }
            )
        return out


def evidence_from(d: dict[str, Any]) -> Evidence:
    names = Evidence.__dataclass_fields__
    kw = {k: v for k, v in d.items() if k in names}
    kw["type"] = EvidenceType(d["type"])
    return Evidence(**kw)


def hypothesis_from(d: dict[str, Any]) -> Hypothesis:
    names = Hypothesis.__dataclass_fields__
    kw = {k: v for k, v in d.items() if k in names}
    kw["category"] = DiagnosisType(d["category"])
    return Hypothesis(**kw)


SCALAR_KEYS = (
    "series_id", "version", "tenant_id", "device_id", "trigger_kind", "trigger_id", "alert_id",
    "anomaly_id", "prediction_id", "status", "diagnosis_type", "category", "severity", "summary",
    "likely_cause", "confidence", "confidence_level", "reasoning_model", "model_version",
    "prompt_version", "context_fingerprint", "supersedes", "created_at", "updated_at", "expires_at",
)  # fmt: skip


def diagnosis_body(d: Diagnosis) -> dict[str, Any]:
    """Everything that is not a column (stored as JSON)."""
    return {
        "hypotheses": [h.public() for h in d.hypotheses],
        "evidence": [e.public() for e in d.evidence],
        "explanation": d.explanation,
        "related_processes": d.related_processes,
        "related_events": d.related_events,
        "notices": d.notices,
        "rejected_claims": d.rejected_claims,
        "feedback": d.feedback,
        "timings": d.timings,
    }


def diagnosis_from(diagnosis_id: str, scalars: dict[str, Any], body: dict[str, Any]) -> Diagnosis:
    kw: dict[str, Any] = {k: scalars.get(k) for k in SCALAR_KEYS}
    kw["status"] = DiagnosisStatus(kw["status"])
    kw["diagnosis_type"] = DiagnosisType(kw["diagnosis_type"])
    return Diagnosis(
        diagnosis_id=diagnosis_id,
        hypotheses=[hypothesis_from(h) for h in body.get("hypotheses") or []],
        evidence=[evidence_from(e) for e in body.get("evidence") or []],
        explanation=body.get("explanation") or {},
        related_processes=list(body.get("related_processes") or []),
        related_events=list(body.get("related_events") or []),
        notices=list(body.get("notices") or []),
        rejected_claims=list(body.get("rejected_claims") or []),
        feedback=list(body.get("feedback") or []),
        timings=dict(body.get("timings") or {}),
        **kw,
    )
