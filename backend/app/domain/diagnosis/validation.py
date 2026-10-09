"""Validation of local-model output (hallucination control).

A model statement survives only if it:
  * parses into the schema (pydantic, bounded lengths);
  * cites evidence ids that exist;
  * mentions only numbers present in the cited evidence (+-5 % or +-1) and only known process names;
  * contains no destructive / command-like recommendation.
Everything else is recorded in ``rejected`` (reason UNSUPPORTED / UNKNOWN_NUMBER / UNKNOWN_PROCESS /
FORBIDDEN_ACTION / INVALID) and hidden from the user. Over-certain causal wording is softened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.domain.diagnosis.evidence import EvidenceIndex
from app.domain.diagnosis.models import Evidence
from app.domain.diagnosis.rules import FORBIDDEN_ACTIONS


class Claim(BaseModel):
    text: str = Field(max_length=400)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class ExtraHypothesis(BaseModel):
    cause: str = Field(max_length=200)
    supporting_evidence: list[str] = Field(default_factory=list, max_length=12)
    contradicting_evidence: list[str] = Field(default_factory=list, max_length=12)


class SuggestedAction(BaseModel):
    """Untrusted: only structure is checked here; Phase 8 validates it against the action catalog."""

    action_type: str = Field(max_length=64, pattern=r"^[A-Z_]{3,64}$")
    parameters: dict[str, str] = Field(default_factory=dict, max_length=4)


class ModelOutput(BaseModel):
    summary: str = Field(default="", max_length=600)
    ranking: list[str] = Field(default_factory=list, max_length=20)
    claims: list[Claim] = Field(default_factory=list, max_length=12)
    investigate: list[Claim] = Field(default_factory=list, max_length=8)
    additional_hypothesis: ExtraHypothesis | None = None
    suggested_action: SuggestedAction | None = None


@dataclass
class Validated:
    summary: str | None
    ranking: list[str]
    claims: list[dict[str, Any]] = field(default_factory=list)
    investigate: list[dict[str, Any]] = field(default_factory=list)
    extra: ExtraHypothesis | None = None
    rejected: list[dict[str, Any]] = field(default_factory=list)
    suggested_action: dict[str, Any] | None = None


_NUM = re.compile(r"(?<![A-Za-z#])-?\d+(?:\.\d+)?")
_EID = re.compile(r"\bE\d{1,4}\b")
_CERTAIN = [
    (
        re.compile(r"\b(definitely|certainly|undoubtedly|clearly)\s+(caused|causes|is causing)\b", re.I),
        "likely contributed to",
    ),
    (re.compile(r"\b(is|was) the (root )?cause of\b", re.I), "is a likely contributor to"),
    (re.compile(r"\bproves?\b", re.I), "suggests"),
]


def _numbers(e: Evidence) -> list[float]:
    vals = [v for v in (e.observed, e.baseline, e.deviation) if v is not None]
    vals += [float(x) for x in _NUM.findall(e.statement)]
    if e.unit == "s" and e.observed is not None:
        vals += [e.observed / 60]  # durations are worded in minutes
    return vals


def _known_number(x: float, pool: list[float]) -> bool:
    if abs(x) <= 1 or x in (100.0,):  # trivial numbers ("1 application", "100%")
        return True
    return any(abs(x - v) <= max(1.0, abs(v) * 0.05) for v in pool)


def soften(text: str) -> str:
    for rx, rep in _CERTAIN:
        text = rx.sub(rep, text)
    return text


_FORBIDDEN = re.compile("|".join(FORBIDDEN_ACTIONS), re.I)


def forbidden(text: str) -> bool:
    return "`" in text or _FORBIDDEN.search(text) is not None


def parse(raw: str | dict[str, Any]) -> ModelOutput:
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`").removeprefix("json").strip()
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in model output")
        raw = json.loads(s[start : end + 1])
    return ModelOutput.model_validate(raw)


def validate(
    raw: str | dict[str, Any], ix: EvidenceIndex, candidate_codes: list[str], process_names: list[str]
) -> Validated:
    try:
        out = parse(raw)
    except (ValueError, ValidationError) as exc:
        return Validated(None, [], rejected=[{"reason": "INVALID", "detail": str(exc)[:300]}])
    by_id = {e.evidence_id: e for e in ix.items}
    names = {n.lower() for n in process_names if n}
    all_nums = [n for e in ix.items for n in _numbers(e)]
    rejected: list[dict[str, Any]] = []

    def check(text: str, ids: list[str], require_ids: bool, kind: str) -> str | None:
        text = soften(text.strip())
        cited = [i for i in dict.fromkeys(ids + _EID.findall(text))]
        unknown = [i for i in cited if i not in by_id]
        if unknown or (require_ids and not cited):
            rejected.append(
                {
                    "kind": kind,
                    "text": text[:200],
                    "reason": "UNSUPPORTED",
                    "detail": f"unknown evidence {unknown}" if unknown else "no evidence cited",
                }
            )
            return None
        if forbidden(text):
            rejected.append({"kind": kind, "text": text[:200], "reason": "FORBIDDEN_ACTION"})
            return None
        pool = [n for i in cited for n in _numbers(by_id[i])] if cited else all_nums
        bad = [x for x in (float(v) for v in _NUM.findall(_EID.sub("", text))) if not _known_number(x, pool)]
        if bad:
            rejected.append(
                {"kind": kind, "text": text[:200], "reason": "UNKNOWN_NUMBER", "detail": str(bad[:3])}
            )
            return None
        for token in re.findall(r"\b[\w.-]+\.exe\b", text, re.I):
            if token.lower() not in names:
                rejected.append(
                    {"kind": kind, "text": text[:200], "reason": "UNKNOWN_PROCESS", "detail": token}
                )
                return None
        return text

    claims = []
    for c in out.claims:
        t = check(c.text, c.evidence_ids, True, "claim")
        if t:
            claims.append({"text": t, "evidence_ids": [i for i in c.evidence_ids if i in by_id]})
    investigate = []
    for c in out.investigate:
        t = check(c.text, c.evidence_ids, False, "recommendation")
        if t:
            investigate.append({"text": t, "evidence_ids": [i for i in c.evidence_ids if i in by_id]})
    summary = check(out.summary, [], False, "summary") if out.summary else None
    ranking = [code for code in dict.fromkeys(out.ranking) if code in candidate_codes]
    for code in out.ranking:
        if code not in candidate_codes:
            rejected.append(
                {"kind": "ranking", "text": code[:80], "reason": "UNSUPPORTED", "detail": "unknown candidate"}
            )
    extra = out.additional_hypothesis
    if extra is not None:
        sup = [i for i in extra.supporting_evidence if i in by_id]
        cause = check(extra.cause, sup, True, "hypothesis") if len(sup) >= 2 else None
        if cause is None:
            if len(sup) < 2:
                rejected.append(
                    {
                        "kind": "hypothesis",
                        "text": extra.cause[:200],
                        "reason": "UNSUPPORTED",
                        "detail": "fewer than 2 valid evidence ids",
                    }
                )
            extra = None
        else:
            extra = ExtraHypothesis(
                cause=cause,
                supporting_evidence=sup,
                contradicting_evidence=[i for i in extra.contradicting_evidence if i in by_id],
            )
    suggested = out.suggested_action.model_dump() if out.suggested_action is not None else None
    if suggested and any(len(v) > 64 or forbidden(v) for v in suggested["parameters"].values()):
        rejected.append(
            {"kind": "suggested_action", "text": str(suggested)[:200], "reason": "FORBIDDEN_ACTION"}
        )
        suggested = None
    return Validated(summary, ranking, claims, investigate, extra, rejected, suggested)
