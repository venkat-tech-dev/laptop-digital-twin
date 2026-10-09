"""Replay engine + evaluation metrics (offline; never part of the live path).

    historical (or synthetic) telemetry --> replay (same observe + BehaviorEngine code as live)
        --> detected anomalies --> precision / recall / false-positive rate / latency / alert volume

The replay drives the *exact* live code: ``observe`` (data-quality gates) and ``BehaviorEngine``
with an injected clock, so an evaluation result describes production behavior, not a model of it.

Synthetic scenarios exist for evaluation and tests only. They are labelled ``synthetic`` in every
output and never reach the twin, the database or the UI (the product's NO FAKE DATA rule).
"""

from __future__ import annotations

import bisect
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.domain.anomalies.baseline import (
    BaselineStatus,
    SignalBaseline,
    build_signal_baseline,
    fleet_baseline,
)
from app.domain.anomalies.behavior import BehaviorEngine, Transition
from app.domain.anomalies.iforest import MultivariateModel, select_features, train_model
from app.domain.anomalies.models import Anomaly
from app.domain.anomalies.observation import Observation, observe
from app.domain.anomalies.policy import AnomalyPolicy
from app.domain.anomalies.signals import RELATIONS, SIGNALS_BY_ID

Series = dict[str, list[tuple[float, float]]]  # signal id -> [(epoch_s, value)] sorted


@dataclass(frozen=True, slots=True)
class Incident:
    """Ground truth: an injected abnormal period and the signals it affects."""

    start: float
    end: float
    signals: frozenset[str]
    label: str


@dataclass
class ReplayResult:
    transitions: list[tuple[float, Transition]] = field(default_factory=list)
    anomalies: dict[str, Anomaly] = field(default_factory=dict)
    first_detected_at: dict[str, float] = field(default_factory=dict)
    evaluations: int = 0

    def reported(self) -> list[Anomaly]:
        """Anomalies that reached an operator (suppressed candidates are excluded)."""
        return [a for a in self.anomalies.values() if a.lifecycle.value != "SUPPRESSED"]


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


# --------------------------------------------------------------------------- training helpers
def minute_means(points: Sequence[tuple[float, float]]) -> list[tuple[datetime, float]]:
    buckets: dict[int, list[float]] = {}
    for t, v in points:
        buckets.setdefault(int(t) // 60, []).append(v)
    return [(_dt(m * 60.0), sum(vs) / len(vs)) for m, vs in sorted(buckets.items())]


def train_baselines(
    history: Series, policy: AnomalyPolicy, now: datetime, version: str = "replay"
) -> dict[str, SignalBaseline]:
    return {
        sid: build_signal_baseline(sid, minute_means(pts), [], policy, f"{sid}-{version}", now)
        for sid, pts in history.items()
    }


def train_replay_model(
    device_id: str, history: Series, baselines: dict[str, SignalBaseline], policy: AnomalyPolicy
) -> MultivariateModel | None:
    features = sorted(
        sid
        for sid, b in baselines.items()
        if b.status is not BaselineStatus.COLD and SIGNALS_BY_ID[sid].multivariate
    )
    if len(features) < 2:
        return None
    per = {f: {int(t.timestamp()) // 60: v for t, v in minute_means(history[f])} for f in features}
    features, common = select_features(per, policy.iforest_min_train_samples)
    if not common:
        return None
    rows = [[per[f][m] for f in features] for m in common]
    return train_model(
        device_id,
        1,
        features,
        rows,
        [SIGNALS_BY_ID[f].min_scale for f in features],
        _dt(common[0] * 60).isoformat(),
        _dt(common[-1] * 60).isoformat(),
        policy.iforest_trees,
        policy.iforest_sample_size,
        policy.iforest_threshold_quantile,
        policy.iforest_seed,
        relations=RELATIONS,
    )


# ------------------------------------------------------------------------------------ replay
def replay(
    live: Series,
    baselines: dict[str, SignalBaseline],
    policy: AnomalyPolicy,
    start: float,
    end: float,
    *,
    model: MultivariateModel | None = None,
    interval_s: float = 5.0,
    device_id: str = "replay-device",
    connected: Callable[[float], bool] = lambda _t: True,
) -> ReplayResult:
    counter = iter(range(1, 10**9))
    engine = BehaviorEngine(policy, id_factory=lambda: f"replay-{next(counter):06d}")
    times = {sid: [t for t, _ in pts] for sid, pts in live.items()}
    out = ReplayResult()
    t = start + policy.observation_window_s
    live_limit = interval_s * 3 + 10.0
    while t <= end:
        observations: dict[str, Observation | None] = {}
        for sid, pts in live.items():
            sig = SIGNALS_BY_ID[sid]
            lo = bisect.bisect_left(times[sid], t - policy.observation_window_s)
            hi = bisect.bisect_right(times[sid], t)
            obs, _ = observe(
                sig,
                pts[lo:hi],
                t,
                policy.observation_window_s,
                interval_s,
                live_limit,
                policy.min_window_coverage,
                connected(t),
            )
            observations[sid] = obs
        for tr in engine.evaluate(device_id, _dt(t), observations, baselines, model):
            out.transitions.append((t, tr))
            out.anomalies[tr.anomaly.anomaly_id] = tr.anomaly
            if tr.kind in ("detected", "suppressed"):
                out.first_detected_at.setdefault(tr.anomaly.anomaly_id, t)
        out.evaluations += 1
        t += policy.eval_interval_s
    return out


# ----------------------------------------------------------------------------------- metrics
def evaluate(
    result: ReplayResult, incidents: Sequence[Incident], span_s: float, margin_s: float = 300.0
) -> dict[str, Any]:
    reported = result.reported()

    def matches(a: Anomaly, inc: Incident) -> bool:
        det = result.first_detected_at.get(a.anomaly_id, a.started_at.timestamp())
        in_time = inc.start <= det <= inc.end + margin_s
        related = a.signal_id is None or a.signal_id in inc.signals  # multivariate: any signal
        return in_time and related

    tp = [a for a in reported if any(matches(a, i) for i in incidents)]
    fp = [a for a in reported if a not in tp]
    found: dict[str, float] = {}
    resolution_errors: list[float] = []
    for inc in incidents:
        hits = [a for a in tp if matches(a, inc)]
        if hits:
            found[inc.label] = min(result.first_detected_at.get(a.anomaly_id, 0.0) for a in hits) - inc.start
            for a in hits:
                if a.resolved_at is not None:
                    resolution_errors.append(a.resolved_at.timestamp() - inc.end)
    hours = max(span_s / 3600.0, 1e-9)
    return {
        "anomalies_reported": len(reported),
        "true_positives": len(tp),
        "false_positives": len(fp),
        "precision": round(len(tp) / len(reported), 3) if reported else (1.0 if not incidents else 0.0),
        "recall": round(len(found) / len(incidents), 3) if incidents else None,
        "false_positives_per_hour": round(len(fp) / hours, 3),
        "alert_volume_per_day": round(len(reported) / hours * 24, 2),
        "detection_latency_s": {k: round(v, 1) for k, v in found.items()},
        "resolution_delay_s": [round(x, 1) for x in resolution_errors],
        "max_confidence_fp": max((a.confidence or 0.0 for a in fp), default=None),
        "levels": sorted({a.effective_level.value for a in reported}),
        "correlated": sorted({a.correlation_key for a in reported if a.correlation_key}),
        "evaluations": result.evaluations,
    }


def baseline_stability(history: Series, policy: AnomalyPolicy, now: datetime) -> dict[str, float]:
    """Relative change of each signal's median/scale between the two halves of the history
    (small = stable baseline; large = drifting behavior, retraining matters)."""
    out: dict[str, float] = {}
    for sid, pts in history.items():
        if len(pts) < 4:
            continue
        mid = pts[len(pts) // 2][0]
        a = build_signal_baseline(sid, minute_means([p for p in pts if p[0] < mid]), [], policy, "h1", now)
        b = build_signal_baseline(sid, minute_means([p for p in pts if p[0] >= mid]), [], policy, "h2", now)
        ca, cb = a.contexts.get("all"), b.contexts.get("all")
        if ca is None or cb is None:
            continue
        scale = max(ca.stats.mad * 1.4826, SIGNALS_BY_ID[sid].min_scale)
        out[sid] = round(abs(cb.stats.median - ca.stats.median) / scale, 3)
    return out


def model_drift(model: MultivariateModel, recent: Series) -> dict[str, Any]:
    """Share of recent (assumed normal) minutes above the model threshold: ~1 - threshold_quantile
    when the model still fits; much higher means the device's behavior moved (retrain)."""
    per = {
        f: {int(t.timestamp()) // 60: v for t, v in minute_means(recent.get(f, []))} for f in model.features
    }
    common = sorted(set.intersection(*(set(m) for m in per.values()))) if per else []
    if not common:
        return {"minutes": 0, "above_threshold": None}
    above = sum(1 for m in common if model.score([per[f][m] for f in model.features]) > model.threshold)
    return {
        "minutes": len(common),
        "above_threshold": round(above / len(common), 4),
        "expected": round(1 - float(model.params["threshold_quantile"]), 4),
    }


# ------------------------------------------------------------------------- synthetic scenarios
@dataclass
class Scenario:
    name: str
    description: str
    expected: str
    history: Series
    live: Series
    incidents: list[Incident]
    start: float
    end: float
    cold: bool = False
    fleet: dict[str, SignalBaseline] | None = None


DAY = 86400.0


def _workload(rng: random.Random, t: float, level: float) -> float:
    hour = (t % DAY) / 3600.0
    weekday = int(t // DAY + 4) % 7 < 5  # 1970-01-01 was a Thursday
    office = 9 <= hour < 18 and weekday
    return level * (1.0 if office else 0.55)


def generate(
    seed: int,
    start: float,
    duration_s: float,
    interval_s: float = 5.0,
    cpu_level: float = 20.0,
    overrides: Callable[[float, dict[str, float]], None] | None = None,
) -> Series:
    """Plausible laptop telemetry: minute-scale workload (AR(1)) + sample noise; temperature follows
    CPU; memory drifts slowly. Deterministic for a seed."""
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data, not security
    out: Series = {s: [] for s in ("cpu", "memory", "temperature", "disk_active", "net_latency", "gpu")}
    load = cpu_level
    mem = 55.0
    t = start
    minute = -1
    while t < start + duration_s:
        m = int(t // 60)
        if m != minute:
            minute = m
            target = _workload(rng, t, cpu_level)
            load = target + 0.8 * (load - target) + rng.gauss(0, 2.5)
            mem = 55.0 + 0.995 * (mem - 55.0) + rng.gauss(0, 0.4)
        v = {
            "cpu": load + rng.gauss(0, 3.0),
            "memory": mem + rng.gauss(0, 0.3),
            "disk_active": 4.0 + abs(rng.gauss(0, 2.0)),
            "net_latency": 5.0 + abs(rng.gauss(0, 1.5)),
            "gpu": 4.0 + abs(rng.gauss(0, 1.5)),
        }
        if overrides is not None:
            overrides(t, v)
        temp = 42.0 + 0.35 * v["cpu"] + rng.gauss(0, 0.8)
        v["temperature"] = v.get("temperature", temp)
        for k, x in v.items():
            if k == "cpu":
                x = min(100.0, max(0.0, x))
            out[k].append((t, x))
        t += interval_s
    return out


def scenarios(seed: int = 7) -> list[Scenario]:
    """Scenarios A-D of the Phase-4 specification (+ E: memory leak)."""
    history_days = 8
    t0 = 1_760_000_000.0 - (1_760_000_000.0 % DAY) + 10 * 3600  # a day boundary + 10:00 UTC
    hist_start = t0 - history_days * DAY
    history = generate(seed, hist_start, history_days * DAY)
    duration = 3 * 3600.0
    out: list[Scenario] = []

    # A: CPU 10-30 % -> 80-90 % for 20 minutes
    a_start, a_end = t0 + 3600, t0 + 3600 + 20 * 60

    def a(t: float, v: dict[str, float]) -> None:
        if a_start <= t < a_end:
            v["cpu"] = 85.0 + random.Random(int(t)).uniform(-5, 5)  # noqa: S311

    out.append(
        Scenario(
            "A_sustained_cpu",
            "Normal CPU 10-30 %, then 80-90 % for 20 minutes",
            "anomaly detected",
            history,
            generate(seed + 1, t0, duration, overrides=a),
            [Incident(a_start, a_end, frozenset({"cpu", "temperature"}), "cpu_80_90_20min")],
            t0,
            t0 + duration,
        )
    )

    # B: a 2-second spike to 95 % (1-second samples around it)
    b_at = t0 + 3600

    def b(t: float, v: dict[str, float]) -> None:
        if b_at <= t < b_at + 2:
            v["cpu"] = 95.0

    out.append(
        Scenario(
            "B_short_spike",
            "CPU = 95 % for 2 seconds",
            "no behavioral anomaly",
            history,
            generate(seed + 2, t0, duration, interval_s=1.0, overrides=b),
            [],
            t0,
            t0 + duration,
        )
    )

    # C: CPU up and temperature up together (correlated incident)
    c_start, c_end = t0 + 3600, t0 + 3600 + 15 * 60

    def c(t: float, v: dict[str, float]) -> None:
        if c_start <= t < c_end:
            v["cpu"] = 70.0 + random.Random(int(t)).uniform(-4, 4)  # noqa: S311

    out.append(
        Scenario(
            "C_thermal_correlation",
            "CPU and temperature rise together",
            "correlated performance/thermal anomaly",
            history,
            generate(seed + 3, t0, duration, overrides=c),
            [Incident(c_start, c_end, frozenset({"cpu", "temperature"}), "cpu_and_temperature")],
            t0,
            t0 + duration,
        )
    )

    # D: new device (no history): only a fleet baseline from other devices; heavier normal workload
    fleet_devices = [
        train_baselines(
            generate(seed + 10 + i, hist_start, 2 * DAY, cpu_level=18 + 4 * i), AnomalyPolicy(), _dt(t0)
        )
        for i in range(3)
    ]
    fleet = {
        sid: fb
        for sid in history
        if (fb := fleet_baseline(sid, [d[sid] for d in fleet_devices], "fleet", _dt(t0))) is not None
    }
    out.append(
        Scenario(
            "D_new_device",
            "New device: no own history, normal but busier workload",
            "cold start, no confident anomalies",
            {},
            generate(seed + 4, t0, duration, cpu_level=30.0),
            [],
            t0,
            t0 + duration,
            cold=True,
            fleet=fleet,
        )
    )

    # E: slow memory leak (+1 %/min for 25 min, then stays high)
    e_start = t0 + 3600

    def e(t: float, v: dict[str, float]) -> None:
        if t >= e_start:
            v["memory"] += min(25.0, (t - e_start) / 60.0)

    out.append(
        Scenario(
            "E_memory_leak",
            "Memory climbs 1 %/minute and stays high",
            "anomaly detected (level shift)",
            history,
            generate(seed + 5, t0, duration, overrides=e),
            [Incident(e_start, t0 + duration, frozenset({"memory"}), "memory_leak")],
            t0,
            t0 + duration,
        )
    )

    # F: erratic CPU (alternating 10 % / 60 % every minute for 30 minutes): volatility, not level
    f_start, f_end = t0 + 3600, t0 + 3600 + 30 * 60

    def f(t: float, v: dict[str, float]) -> None:
        if f_start <= t < f_end:
            v["cpu"] = 60.0 if int(t // 60) % 2 else 10.0

    out.append(
        Scenario(
            "F_erratic_cpu",
            "CPU alternates 10 % / 60 % every minute for 30 minutes",
            "volatility anomaly",
            history,
            generate(seed + 6, t0, duration, overrides=f),
            [Incident(f_start, f_end, frozenset({"cpu", "temperature"}), "erratic_cpu")],
            t0,
            t0 + duration,
        )
    )

    # G: each value normal on its own, the combination is not (busy CPU, idle-level temperature)
    g_start, g_end = t0 + 3600, t0 + 3600 + 20 * 60

    def g(t: float, v: dict[str, float]) -> None:
        if g_start <= t < g_end:
            v["cpu"] = 28.0 + random.Random(int(t)).uniform(-2, 2)  # noqa: S311
            v["temperature"] = 45.0 + random.Random(int(t) + 1).uniform(-0.5, 0.5)  # noqa: S311

    out.append(
        Scenario(
            "G_unusual_combination",
            "CPU busy (28 %) while temperature stays at idle level (45 C)",
            "multivariate anomaly",
            history,
            generate(seed + 7, t0, duration, overrides=g),
            [Incident(g_start, g_end, frozenset({"cpu", "temperature"}), "cpu_temperature_mismatch")],
            t0,
            t0 + duration,
        )
    )
    return out


def run_scenario(sc: Scenario, policy: AnomalyPolicy | None = None, use_model: bool = True) -> dict[str, Any]:
    policy = policy or AnomalyPolicy()
    now = _dt(sc.start)
    if sc.cold:
        baselines = dict(sc.fleet or {})
        model = None
    else:
        baselines = train_baselines(sc.history, policy, now)
        model = train_replay_model("replay-device", sc.history, baselines, policy) if use_model else None
    interval = sc.live["cpu"][1][0] - sc.live["cpu"][0][0]
    result = replay(sc.live, baselines, policy, sc.start, sc.end, model=model, interval_s=interval)
    metrics = evaluate(result, sc.incidents, sc.end - sc.start)
    return {
        "scenario": sc.name,
        "data": "synthetic",
        "description": sc.description,
        "expected": sc.expected,
        "baseline_status": {k: b.status.value for k, b in baselines.items()},
        "model": model.model_id if model else None,
        "metrics": metrics,
        "anomalies": [
            {
                "title": a.title,
                "type": a.anomaly_type.value,
                "level": a.effective_level.value,
                "confidence": a.confidence,
                "lifecycle": a.lifecycle.value,
                "signal_id": a.signal_id,
                "correlation_key": a.correlation_key,
                "detected_after_s": round(result.first_detected_at.get(a.anomaly_id, sc.start) - sc.start, 1),
            }
            for a in result.reported()
        ],
    }


__all__ = [
    "Incident",
    "ReplayResult",
    "Scenario",
    "baseline_stability",
    "evaluate",
    "generate",
    "minute_means",
    "model_drift",
    "replay",
    "run_scenario",
    "scenarios",
    "train_baselines",
    "train_replay_model",
]
