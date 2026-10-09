"""Evidence collection, temporal analysis and ranking (deterministic, every item traceable).

Temporal reasoning per signal (1-minute means of the recent window):
    elevated   current above the device's usual upper bound (Phase 4 baseline p95) - or, without a
               baseline, above the twin's warning threshold
    onset      start of the current continuous run above that bound -> sustained duration
    pattern    sudden (one step carries >= 60 % of the rise), gradual (rise spread over >= 10 min),
               recurring (>= 3 separate runs above the bound in the window), recent
    order      which signal rose first (CPU before temperature -> consistent with workload heating;
               a process that started just before the CPU rise -> consistent with it contributing)
Correlation is Pearson r over common minutes; it is evidence of association, never of causation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.domain.diagnosis.context import DiagnosticContext, SeriesSummary
from app.domain.diagnosis.models import Evidence, EvidenceType

KEY_SIGNALS = ("cpu", "memory", "disk_active", "net_latency", "temperature")


@dataclass
class Temporal:
    elevated: bool
    bound: float | None
    onset: float | None  # epoch s of the start of the current run above the bound
    sustained_s: float
    pattern: str  # sudden | gradual | recurring | recent | none
    runs: int
    slope_per_min: float | None


@dataclass
class EvidenceIndex:
    items: list[Evidence] = field(default_factory=list)
    by_signal: dict[str, dict[str, Evidence]] = field(default_factory=dict)  # signal -> kind -> evidence
    anomalies: dict[str, list[Evidence]] = field(default_factory=dict)  # signal -> anomaly evidence
    predictions: dict[str, Evidence] = field(default_factory=dict)  # target -> evidence
    processes: list[Evidence] = field(default_factory=list)  # ranked by CPU share
    memory_processes: list[Evidence] = field(default_factory=list)
    correlations: dict[tuple[str, str], Evidence] = field(default_factory=dict)
    order: dict[tuple[str, str], Evidence] = field(default_factory=dict)  # (first, second) -> evidence
    security: list[Evidence] = field(default_factory=list)
    events: list[Evidence] = field(default_factory=list)
    temporal: dict[str, Temporal] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def add(self, e: Evidence) -> Evidence:
        self.items.append(e)
        return e

    def get(self, signal: str, kind: str) -> Evidence | None:
        return self.by_signal.get(signal, {}).get(kind)

    def ids(self) -> set[str]:
        return {e.evidence_id for e in self.items}


def _hhmm(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M UTC") if ts else "?"


def _dur(s: float) -> str:
    return f"{s / 60:.0f} min" if s >= 90 else f"{s:.0f} s"


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "?"
    if unit == "%":
        return f"{v:.0f}%"
    if unit in ("°C", "C"):
        return f"{v:.0f} °C"
    return f"{v:.1f} {unit}"


def _slope(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 5:
        return None
    t0 = points[0][0]
    xs = [(t - t0) / 60 for t, _ in points]
    ys = [v for _, v in points]
    slopes = sorted(
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] > xs[i]
    )
    return slopes[len(slopes) // 2] if slopes else None


def temporal(s: SeriesSummary) -> Temporal:
    pts = s.points
    bound = s.baseline_high if s.baseline_high is not None else s.warning
    if not pts or bound is None:
        return Temporal(False, bound, None, 0.0, "none", 0, _slope(pts))
    up = s.direction == "up"

    def above(v: float) -> bool:
        return v > bound if up else v < bound

    runs = 0
    prev = False
    for _, v in pts:
        cur = above(v)
        if cur and not prev:
            runs += 1
        prev = cur
    elevated = above(pts[-1][1])
    onset = None
    if elevated:
        i = len(pts) - 1
        while i > 0 and above(pts[i - 1][1]):
            i -= 1
        onset = pts[i][0]
        before = pts[i - 1][1] if i > 0 else pts[i][1]
        rise = abs(pts[-1][1] - before)
        # two-minute changes: a step that straddles a minute boundary is still one sudden step
        steps = [abs(pts[k][1] - pts[k - 2][1]) for k in range(max(2, i), min(len(pts), i + 3))]
        jump = max(steps, default=0.0)
        first_jump = abs(pts[i][1] - before) if i > 0 else 0.0
        if runs >= 3:
            pattern = "recurring"
        elif rise > 0 and max(jump, first_jump) >= 0.7 * rise:
            pattern = "sudden"
        elif pts[-1][0] - onset >= 600:
            pattern = "gradual"
        else:
            pattern = "recent"
    else:
        pattern = "recurring" if runs >= 3 else "none"
    sustained = (pts[-1][0] - onset + 60) if onset is not None else 0.0
    return Temporal(elevated, bound, onset, sustained, pattern, runs, _slope(pts[-30:]))


def pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 6:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / (sa * sb)


def collect(ctx: DiagnosticContext) -> EvidenceIndex:
    idx = EvidenceIndex()
    counter = iter(range(1, 10_000))

    def new(**kw: Any) -> Evidence:
        return idx.add(Evidence(evidence_id=f"E{next(counter)}", **kw))

    now_iso = datetime.fromtimestamp(ctx.generated_at, UTC).isoformat()
    # ---------------------------------------------------------------- per-signal observations
    for key, s in ctx.series.items():
        tmp = temporal(s)
        idx.temporal[key] = tmp
        slot = idx.by_signal.setdefault(key, {})
        cur = s.current
        if cur is None:
            idx.missing.append(f"no recent {s.label.lower()} samples")
            continue
        window = {"start": datetime.fromtimestamp(s.points[0][0], UTC).isoformat(), "end": now_iso}
        usual = (
            f"typical up to {_fmt(s.baseline_high, s.unit)} earlier in this hour; no learned baseline yet"
            if s.baseline_high is not None and s.baseline_source == "window"
            else f"usual up to {_fmt(s.baseline_high, s.unit)}"
            if s.baseline_high is not None and s.direction == "up"
            else f"usual down to {_fmt(s.baseline_high, s.unit)}"
            if s.baseline_high is not None
            else f"warning level {_fmt(s.warning, s.unit)}"
            if s.warning is not None
            else "no baseline yet"
        )
        ref = {"kind": "metric", "id": key}
        if tmp.elevated:
            over = abs(cur - (tmp.bound or cur)) / max(abs(tmp.bound or 1.0), 1.0)
            strength = 0.4 + 0.5 * min(1.0, over / 0.25)  # marginal overshoots are weak evidence
            if s.critical is not None and (
                (cur >= s.critical) if s.direction == "up" else (cur <= s.critical)
            ):
                strength = max(strength, 0.9)
            slot["observation"] = new(
                type=EvidenceType.OBSERVATION,
                source="twin",
                signal=key,
                metric=s.label,
                observed=round(cur, 2),
                baseline=s.baseline_high if s.baseline_high is not None else s.warning,
                unit=s.unit,
                deviation=round(cur - (tmp.bound or cur), 2),
                timestamp=now_iso,
                window=window,
                ref=ref,
                strength=round(strength, 2),
                statement=f"{s.label} is {_fmt(cur, s.unit)} ({usual}).",
            )
            if tmp.onset is not None and tmp.sustained_s >= 120:
                slot["temporal"] = new(
                    type=EvidenceType.TEMPORAL,
                    source="telemetry_history",
                    signal=key,
                    metric=s.label,
                    observed=round(tmp.sustained_s),
                    unit="s",
                    timestamp=_iso(tmp.onset),
                    window=window,
                    ref=ref,
                    strength=round(min(0.9, 0.45 + tmp.sustained_s / 3600), 2),
                    statement=f"{s.label} has stayed above its usual range for {_dur(tmp.sustained_s)} "
                    f"({tmp.pattern} onset around {_hhmm(tmp.onset)}).",
                )
        elif key in KEY_SIGNALS:
            slot["absence"] = new(
                type=EvidenceType.ABSENCE_OF_EXPECTED_SIGNAL,
                source="twin",
                signal=key,
                metric=s.label,
                observed=round(cur, 2),
                baseline=tmp.bound,
                unit=s.unit,
                timestamp=now_iso,
                window=window,
                ref=ref,
                strength=0.6,
                statement=f"{s.label} is within its usual range ({_fmt(cur, s.unit)}, {usual}).",
            )
        if tmp.pattern == "recurring" and not tmp.elevated:
            slot["recurring"] = new(
                type=EvidenceType.TEMPORAL,
                source="telemetry_history",
                signal=key,
                metric=s.label,
                observed=float(tmp.runs),
                unit="episodes",
                window=window,
                ref=ref,
                strength=0.5,
                statement=f"{s.label} exceeded its usual range {tmp.runs} times in the window "
                "(recurring spikes).",
            )
        if (
            tmp.pattern in ("gradual", "none")  # a step change is not a trend
            and tmp.slope_per_min is not None
            and abs(tmp.slope_per_min) * 30 >= max(5.0, abs(cur) * 0.1)
            and len(s.points) >= 10
        ):
            slot["trend"] = new(
                type=EvidenceType.TREND,
                source="telemetry_history",
                signal=key,
                metric=s.label,
                observed=round(tmp.slope_per_min, 3),
                unit=f"{s.unit}/min",
                window=window,
                ref=ref,
                strength=0.55,
                statement=f"{s.label} has been {'rising' if tmp.slope_per_min > 0 else 'falling'} by about "
                f"{abs(tmp.slope_per_min):.2f} {s.unit}/min over the last {len(s.points)} min.",
            )
    # ---------------------------------------------------------------- correlation + order
    for a, b in (("cpu", "temperature"), ("memory", "disk_active"), ("cpu", "memory")):
        sa, sb = ctx.series.get(a), ctx.series.get(b)
        ta, tb = idx.temporal.get(a), idx.temporal.get(b)
        if not sa or not sb or not ta or not tb:
            continue
        common = sorted(set(int(t // 60) for t, _ in sa.points) & set(int(t // 60) for t, _ in sb.points))
        va = {int(t // 60): v for t, v in sa.points}
        vb = {int(t // 60): v for t, v in sb.points}
        r = pearson([va[m] for m in common], [vb[m] for m in common])
        if r is not None and abs(r) >= 0.6 and (ta.elevated or tb.elevated):
            idx.correlations[(a, b)] = new(
                type=EvidenceType.CORRELATION,
                source="telemetry_history",
                signal=f"{a}+{b}",
                observed=round(r, 2),
                unit="r",
                ref={"kind": "correlation", "id": f"{a}~{b}"},
                strength=round(abs(r) * 0.8, 2),
                window={"minutes": len(common)},
                statement=f"{sa.label} and {sb.label.lower()} moved together over {len(common)} min "
                f"(r = {r:+.2f}); "
                "this is an association, not proof of cause.",
            )
        if ta.onset is not None and tb.onset is not None and ta.elevated and tb.elevated:
            first, second = (a, b) if ta.onset <= tb.onset else (b, a)
            gap = abs(tb.onset - ta.onset)
            idx.order[(first, second)] = new(
                type=EvidenceType.TEMPORAL,
                source="telemetry_history",
                signal=f"{first}->{second}",
                observed=round(gap),
                unit="s",
                ref={"kind": "order", "id": f"{first}>{second}"},
                strength=0.6,
                statement=f"{ctx.series[first].label} rose first; "
                f"{ctx.series[second].label.lower()} followed "
                f"about {_dur(gap)} later.",
            )
    # ---------------------------------------------------------------- signals from other phases
    trig = ctx.trigger
    trig_sig = trig.get("signal")
    covered = {a.get("id") for a in ctx.anomalies}
    if trig.get("kind") == "alert" and trig_sig and trig.get("anomaly_id") not in covered:
        # the alert itself is a fact: the platform raised it (its values come from Phase 4/5, not the model)
        obs, exp = trig.get("observed"), trig.get("expected")
        idx.anomalies.setdefault(str(trig_sig), []).append(new(
            type=EvidenceType.ANOMALY, source="alerting", signal=trig_sig, metric=trig.get("metric"),
            observed=obs, baseline=exp, timestamp=trig.get("started_at"),
            ref={"kind": "alert", "id": trig.get("id")},
            strength={"CRITICAL": 0.85, "HIGH": 0.75, "MEDIUM": 0.6}.get(str(trig.get("severity")), 0.45),
            statement=f"Alert ({trig.get('severity')}): {trig.get('title')}"
            + (f" - observed {obs:g} vs usual {exp:g}" if isinstance(obs, (int, float))
               and isinstance(exp, (int, float)) else "") + ".",
        ))  # fmt: skip
    for an in ctx.anomalies:
        sig = an.get("signal") or "other"
        e = new(
            type=EvidenceType.ANOMALY,
            source="anomaly_engine",
            signal=sig,
            metric=an.get("metric"),
            observed=an.get("observed"),
            baseline=an.get("expected"),
            unit=an.get("unit"),
            timestamp=an.get("started_at"),
            ref={"kind": "anomaly", "id": an.get("id")},
            strength=round(min(0.95, 0.4 + 0.5 * float(an.get("confidence") or 0.5)), 2),
            statement=f"Anomaly ({an.get('level')}): {an.get('title')}"
            + (
                f" - observed {an.get('observed')} vs usual {an.get('expected')}"
                if an.get("observed") is not None and an.get("expected") is not None
                else ""
            )
            + ".",
        )
        idx.anomalies.setdefault(sig, []).append(e)
    for p in ctx.predictions:
        idx.predictions[str(p.get("target"))] = new(
            type=EvidenceType.PREDICTION,
            source="forecaster",
            signal=p.get("target"),
            metric=p.get("metric"),
            observed=p.get("current"),
            baseline=p.get("threshold"),
            timestamp=p.get("created_at"),
            ref={"kind": "prediction", "id": p.get("id")},
            strength=0.6 if p.get("confidence_band") == "HIGH" else 0.45,
            statement=f"Forecast: {p.get('statement')}",
        )
    total_cpu = sum(pf.cpu_during for pf in ctx.processes) or 0.0
    for rank, pf in enumerate(sorted(ctx.processes, key=lambda x: -x.cpu_during)[:3]):
        share = pf.cpu_during / total_cpu if total_cpu else 0.0
        started = ""
        if pf.started_at:
            started = f"; started {_hhmm(pf.started_at)}"
        idx.processes.append(
            new(
                type=EvidenceType.PROCESS,
                source="process_snapshots",
                signal="cpu",
                metric="process CPU",
                process=pf.name,
                observed=round(pf.cpu_during, 1),
                baseline=None if pf.cpu_before is None else round(pf.cpu_before, 1),
                unit="% CPU",
                window=ctx.process_window,
                ref={"kind": "process", "id": pf.name, "rank": rank + 1},
                strength=round(min(0.9, 0.3 + share), 2),
                deviation=round(share, 3),
                statement=f"{pf.name} averaged {pf.cpu_during:.1f}% CPU during the window"
                + (f" (before: {pf.cpu_before:.1f}%)" if pf.cpu_before is not None else "")
                + f"{started}.",
            )
        )
    for pf in sorted(ctx.processes, key=lambda x: -x.mem_during_mb)[:2]:
        idx.memory_processes.append(
            new(
                type=EvidenceType.PROCESS,
                source="process_snapshots",
                signal="memory",
                metric="process memory",
                process=pf.name,
                observed=round(pf.mem_during_mb),
                baseline=None if pf.mem_before_mb is None else round(pf.mem_before_mb),
                unit="MB",
                window=ctx.process_window,
                ref={"kind": "process", "id": pf.name},
                strength=0.5,
                deviation=None if pf.mem_before_mb is None else round(pf.mem_during_mb - pf.mem_before_mb),
                statement=f"{pf.name} used about {pf.mem_during_mb:.0f} MB"
                + (f" (before: {pf.mem_before_mb:.0f} MB)" if pf.mem_before_mb is not None else "")
                + ".",
            )
        )
    if not ctx.processes:
        idx.missing.append(
            "no process snapshots for this window"
            if not ctx.process_window.get("snapshots")
            else "process details are not available"
        )
    for finding in ctx.security.get("findings") or []:
        idx.security.append(
            new(
                type=EvidenceType.OBSERVATION,
                source="twin",
                signal="security",
                metric="security posture",
                ref={"kind": "health_reason", "id": "security"},
                strength=0.8,
                timestamp=now_iso,
                statement=f"Security finding: {finding}.",
            )
        )
    for ev in ctx.timeline:
        if ev.get("severity") in ("warning", "error", "critical") and not str(ev.get("type", "")).startswith(
            "alert."
        ):
            idx.events.append(
                new(
                    type=EvidenceType.EVENT,
                    source="timeline",
                    signal=None,
                    timestamp=ev.get("time"),
                    ref={"kind": "event", "id": ev.get("event_id") or ev.get("type")},
                    strength=0.4,
                    statement=f"Event at {str(ev.get('time', ''))[11:16]} UTC: {ev.get('message')}",
                )
            )
    for key in ("cpu", "memory", "temperature"):
        if key not in ctx.series:
            idx.missing.append(f"{key} telemetry is not available")
    return idx


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def rank(items: list[Evidence]) -> list[Evidence]:
    """Strongest first; anomalies and sustained observations ahead of context."""
    order = {
        EvidenceType.ANOMALY: 0,
        EvidenceType.OBSERVATION: 1,
        EvidenceType.TEMPORAL: 2,
        EvidenceType.PROCESS: 3,
        EvidenceType.CORRELATION: 4,
        EvidenceType.TREND: 5,
        EvidenceType.PREDICTION: 6,
        EvidenceType.EVENT: 7,
        EvidenceType.ABSENCE_OF_EXPECTED_SIGNAL: 8,
        EvidenceType.BASELINE: 9,
    }
    return sorted(items, key=lambda e: (-e.strength, order.get(e.type, 9)))
