"""Platform confidence: computed from evidence, never taken from a model's self-reported probability.

    support        noisy-OR of supporting evidence strengths      1 - prod(1 - s_i)
    contradiction  noisy-OR of contradicting strengths; base = support * (1 - 0.6 * contradiction)
    data_quality   0.6 + 0.4 * coverage; halved when telemetry is stale
    history        1.0 with a learned baseline for the trigger signal, 0.85 without
    temporal       +5 % when the order of events fits the hypothesis, -15 % when it contradicts it
    missing        x0.93 per piece of missing evidence (max 3 counted)
    trigger        x0.75 for hypotheses outside the trigger's category
    complexity     x0.9 for the leader when the runner-up is within 0.08 (ambiguous)
    minimum        fewer than 2 supporting items -> capped below LOW (INSUFFICIENT)
    model          optional bounded agreement adjustment from the local model: at most +-0.05

Bands: HIGH >= 0.75, MEDIUM >= 0.5, LOW >= 0.3, otherwise INSUFFICIENT.
"""

from __future__ import annotations

from app.domain.diagnosis.context import DiagnosticContext
from app.domain.diagnosis.evidence import EvidenceIndex
from app.domain.diagnosis.models import DiagnosisType, Hypothesis
from app.domain.diagnosis.rules import SIGNAL_CATEGORY

MIN_EVIDENCE = 2
MODEL_ADJUST_MAX = 0.05


def band(c: float) -> str:
    if c >= 0.75:
        return "HIGH"
    if c >= 0.5:
        return "MEDIUM"
    if c >= 0.3:
        return "LOW"
    return "INSUFFICIENT"


def _noisy_or(strengths: list[float]) -> float:
    p = 1.0
    for s in strengths:
        p *= 1.0 - max(0.0, min(0.99, s))
    return 1.0 - p


def trigger_category(ctx: DiagnosticContext) -> DiagnosisType | None:
    sig = str(ctx.trigger.get("signal") or "")
    return SIGNAL_CATEGORY.get(sig) or SIGNAL_CATEGORY.get(sig.split(".")[0])


def score(ctx: DiagnosticContext, ix: EvidenceIndex, hyps: list[Hypothesis]) -> list[Hypothesis]:
    """Fill confidence, level and factors in place; return hypotheses ranked best first."""
    strength = {e.evidence_id: e.strength for e in ix.items}
    dq = ctx.data_quality
    coverage = float(dq.get("coverage", 1.0))
    quality = (0.6 + 0.4 * max(0.0, min(1.0, coverage))) * (0.5 if dq.get("stale") else 1.0)
    trig_cat = trigger_category(ctx)
    trig_sig = str(ctx.trigger.get("signal") or "")
    ts = ctx.series.get(trig_sig)
    has_baseline = bool(ts and ts.baseline_high is not None and ts.baseline_source == "device")
    for h in hyps:
        support = _noisy_or([strength.get(i, 0.0) for i in h.supporting])
        contra = _noisy_or([strength.get(i, 0.0) for i in h.contradicting])
        f = {
            "support": round(support, 3),
            "contradiction": round(contra, 3),
            "data_quality": round(quality, 3),
            "history": 1.0 if has_baseline else 0.85,
            "temporal": 1.0,
            "missing": round(0.93 ** min(3, len(h.missing)), 3),
            "trigger_match": 1.0 if trig_cat is None or h.category == trig_cat else 0.75,
        }
        order = h.factors.get("temporal_order", 0.0)
        if order > 0:
            f["temporal"] = 1.05
        elif order < 0:
            f["temporal"] = 0.85
        if h.code == "cpu.process" and h.factors.get("process_rose") == 0.0:
            f["temporal"] = min(f["temporal"], 0.9)  # the process was already this busy before the episode
        c = support * (1 - 0.6 * contra)
        for k in ("data_quality", "history", "temporal", "missing", "trigger_match"):
            c *= f[k]
        if h.category == DiagnosisType.UNKNOWN:
            c = min(c, 0.35)
        if len(h.supporting) < MIN_EVIDENCE:
            c = min(c, 0.29)
            f["minimum_evidence"] = 0.0
        h.factors.update(f)
        h.confidence = round(max(0.0, min(0.97, c)), 3)
    ranked = sorted(hyps, key=lambda h: (h.category == DiagnosisType.UNKNOWN, -h.confidence))
    if len(ranked) > 1 and ranked[0].category != DiagnosisType.UNKNOWN:
        lead, second = ranked[0], ranked[1]
        if second.category != DiagnosisType.UNKNOWN and lead.confidence - second.confidence < 0.08:
            lead.confidence = round(max(second.confidence, lead.confidence * 0.9), 3)  # never flips the order
            lead.factors["complexity"] = 0.9
    # UNKNOWN leads when nothing reaches LOW
    if ranked and ranked[0].category != DiagnosisType.UNKNOWN and ranked[0].confidence < 0.3:
        ranked.sort(key=lambda h: (h.category != DiagnosisType.UNKNOWN, -h.confidence))
    for h in ranked:
        h.confidence_level = band(h.confidence)
    return ranked


def model_adjustment(h: Hypothesis, agrees: bool | None) -> None:
    """Small, bounded nudge when the local model agrees / disagrees with a rule hypothesis."""
    if agrees is None:
        return
    delta = MODEL_ADJUST_MAX if agrees else -MODEL_ADJUST_MAX
    h.confidence = round(max(0.0, min(0.97, h.confidence + delta)), 3)
    h.factors["model_agreement"] = delta
    h.confidence_level = band(h.confidence)
