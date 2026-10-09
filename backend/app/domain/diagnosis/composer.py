"""Assemble a diagnosis: evidence -> rule hypotheses -> platform confidence -> (optional) validated
model explanation -> explanation sections. Pure; the service adds identity, storage and events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.diagnosis import confidence, evidence, rules
from app.domain.diagnosis.context import DiagnosticContext
from app.domain.diagnosis.evidence import EvidenceIndex
from app.domain.diagnosis.models import DiagnosisStatus, DiagnosisType, Evidence, Hypothesis
from app.domain.diagnosis.validation import Validated

FALLBACK_NOTICE = "AI reasoning unavailable. Showing deterministic evidence."


@dataclass
class Draft:
    index: EvidenceIndex
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    status: DiagnosisStatus = DiagnosisStatus.AVAILABLE
    diagnosis_type: DiagnosisType = DiagnosisType.UNKNOWN
    summary: str = ""
    likely_cause: str | None = None
    confidence: float = 0.0
    confidence_level: str = "INSUFFICIENT"
    explanation: dict[str, Any] = field(default_factory=dict)
    related_processes: list[str] = field(default_factory=list)
    related_events: list[str] = field(default_factory=list)
    rejected_claims: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def prepare(ctx: DiagnosticContext) -> Draft:
    """Deterministic part: evidence + ranked rule hypotheses with platform confidence."""
    ix = evidence.collect(ctx)
    hyps = confidence.score(ctx, ix, rules.hypotheses(ctx, ix))
    return Draft(index=ix, hypotheses=hyps, evidence=evidence.rank(ix.items))


def _statements(ix: EvidenceIndex, ids: list[str]) -> list[dict[str, str]]:
    by = {e.evidence_id: e for e in ix.items}
    return [{"evidence_id": i, "statement": by[i].statement} for i in ids if i in by]


def _timeline(ctx: DiagnosticContext, ix: EvidenceIndex) -> dict[str, list[str]]:
    """Before / during / after relative to the trigger's start."""
    sig = str(ctx.trigger.get("signal") or "")
    t = ix.temporal.get(sig)
    before: list[str] = []
    during: list[str] = []
    after: list[str] = []
    for (first, _second), e in ix.order.items():
        (before if first != sig else during).append(e.statement)
    if t is not None and t.onset is not None:
        during.append(
            f"Onset pattern: {t.pattern}; above its usual range for {round(t.sustained_s / 60)} min so far."
        )
    for p in ix.processes[:2]:
        if p.baseline is not None and (p.observed or 0) - p.baseline >= 5:
            before.append(
                f"{p.process} was at {p.baseline:.1f}% CPU before the window and "
                f"{(p.observed or 0):.1f}% during it."
            )
    for e in ix.predictions.values():
        after.append(e.statement)
    return {"before": before, "during": during, "after": after}


def finish(ctx: DiagnosticContext, d: Draft, model: Validated | None, reasoning: str) -> Draft:
    ix, hyps = d.index, d.hypotheses
    if model is not None:
        d.rejected_claims = model.rejected
        ranked = model.ranking
        if ranked and hyps:
            lead = hyps[0]
            if lead.category != DiagnosisType.UNKNOWN:
                confidence.model_adjustment(lead, ranked[0] == lead.code if lead.code in ranked else None)
            for h in hyps[1:]:
                if ranked and ranked[0] == h.code:
                    confidence.model_adjustment(h, True)
        if model.extra is not None:
            extra = Hypothesis(
                code="model.extra",
                category=DiagnosisType.UNKNOWN,
                cause=model.extra.cause,
                supporting=model.extra.supporting_evidence,
                contradicting=model.extra.contradicting_evidence,
                origin="model",
                recommendations=[],
            )
            confidence.score(ctx, ix, [extra])  # same platform scoring as rule hypotheses
            extra.confidence = round(
                min(extra.confidence, 0.49), 3
            )  # model-only ideas never exceed LOW/MEDIUM edge
            extra.confidence_level = confidence.band(extra.confidence)
            hyps.append(extra)
        hyps.sort(key=lambda h: (h.category == DiagnosisType.UNKNOWN and h.origin == "rules", -h.confidence))
    primary = hyps[0] if hyps else None
    d.hypotheses = hyps
    if primary is None:
        d.status = DiagnosisStatus.INSUFFICIENT_EVIDENCE
        d.summary = "Not enough evidence to explain this event."
        return d
    d.diagnosis_type = primary.category
    d.confidence = primary.confidence
    d.confidence_level = primary.confidence_level
    if primary.category == DiagnosisType.UNKNOWN or len(primary.supporting) < confidence.MIN_EVIDENCE:
        d.status = DiagnosisStatus.INSUFFICIENT_EVIDENCE
    elif primary.confidence_level in ("LOW", "INSUFFICIENT"):
        d.status = DiagnosisStatus.LOW_CONFIDENCE
    else:
        d.status = DiagnosisStatus.AVAILABLE
    d.likely_cause = primary.cause if d.status != DiagnosisStatus.INSUFFICIENT_EVIDENCE else None
    observed = [e for e in d.evidence if e.type.value in ("ANOMALY", "OBSERVATION")][:2]
    what = " ".join(e.statement for e in observed) or str(ctx.trigger.get("title") or "")
    rule_summary = (
        f"{primary.cause} ({primary.confidence_level.lower()} confidence)."
        if d.likely_cause
        else "The available evidence does not point to a clear cause yet."
    )
    d.summary = rule_summary
    if model is not None and model.summary:
        by_id = {e.evidence_id: e for e in ix.items}
        backing = {(by_id[i].process or "").lower() for i in primary.supporting if i in by_id}
        named = [p.lower() for p in (e.process for e in ix.items) if p and p.lower() in model.summary.lower()]
        stray = [p for p in named if p not in backing]
        if stray and d.likely_cause:
            d.rejected_claims.append(
                {
                    "kind": "summary",
                    "text": model.summary[:200],
                    "reason": "UNSUPPORTED",
                    "detail": f"names {stray[0]} which does not support the likely cause",
                }
            )
        else:
            d.summary = model.summary
    recs = list(primary.recommendations)
    if model is not None:
        recs += [c["text"] for c in model.investigate if c["text"] not in recs]
    d.related_processes = [e.process for e in ix.processes if e.process][:3]
    d.related_events = [e.evidence_id for e in ix.events][:5]
    d.explanation = {
        "what_happened": what,
        "likely_cause": d.likely_cause,
        "why": _statements(ix, primary.supporting[:5]),
        "supporting": _statements(ix, primary.supporting),
        "contradicting": _statements(ix, primary.contradicting),
        "missing": primary.missing + [m for m in ix.missing if m not in primary.missing],
        "investigate": recs[:6],
        "uncertainty": (
            f"Platform confidence is {primary.confidence_level} ({primary.confidence:.0%}), "
            "computed from the evidence, not from the AI model. Correlation does not prove cause."
        ),
        "timeline": _timeline(ctx, ix),
        "model_claims": model.claims if model is not None else [],
        "suggested_action": model.suggested_action if model is not None else None,
        "reasoning": reasoning,
    }
    return d
