"""Phase 4 - BehaviorEngine lifecycle: persistence, hysteresis, cooldown, dedupe, suppression,
expiry, correlation and evidence (deterministic: injected clock and ids)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.domain.anomalies.baseline import BaselineStatus, ContextStats, SignalBaseline
from app.domain.anomalies.behavior import BehaviorEngine, Transition
from app.domain.anomalies.models import AnomalyType, Lifecycle
from app.domain.anomalies.observation import Observation
from app.domain.anomalies.policy import AnomalyPolicy
from app.domain.anomalies.signals import SIGNALS_BY_ID
from app.domain.anomalies.stats import Summary

T0 = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
DEV = "dev-1"


def _baseline(
    sid: str, values: list[float], status: BaselineStatus = BaselineStatus.STABLE
) -> SignalBaseline:
    return SignalBaseline(
        sid,
        status,
        f"{sid}-v1",
        T0 - timedelta(days=8),
        T0,
        12_000,
        0,
        {"all": ContextStats("all", Summary.of(values))},
    )


BASE = {
    "cpu": _baseline("cpu", [10.0 + (i % 21) for i in range(500)]),  # median 20, MAD ~5
    "temperature": _baseline("temperature", [45.0 + (i % 9) for i in range(500)]),  # median 49
    "memory": _baseline("memory", [55.0 + (i % 5) * 0.5 for i in range(500)]),
}


def _obs(sid: str, value: float, ts: datetime, coverage: float = 1.0) -> Observation:
    t = ts.timestamp()
    pts = tuple((t - 115 + 5 * i, value) for i in range(24))
    return Observation(SIGNALS_BY_ID[sid], value, value, 24, coverage, t, pts)


class Clock:
    def __init__(self, engine: BehaviorEngine) -> None:
        self.engine = engine
        self.now = T0
        self.log: list[tuple[float, Transition]] = []

    def run(
        self, seconds: float, values: Callable[[datetime], dict[str, float | None]], **kw
    ) -> list[Transition]:
        out: list[Transition] = []
        end = self.now + timedelta(seconds=seconds)
        while self.now < end:
            self.now += timedelta(seconds=10)
            obs = {
                sid: (_obs(sid, v, self.now) if v is not None else None)
                for sid, v in values(self.now).items()
            }
            trs = self.engine.evaluate(DEV, self.now, obs, BASE, **kw)
            self.log += [((self.now - T0).total_seconds(), t) for t in trs]
            out += trs
        return out


def _engine(**policy) -> tuple[BehaviorEngine, Clock]:
    ids = iter(range(1, 10_000))
    eng = BehaviorEngine(AnomalyPolicy(**policy), id_factory=lambda: f"a{next(ids)}")
    return eng, Clock(eng)


def kinds(trs: list[Transition]) -> list[str]:
    return [t.kind for t in trs]


def test_persistence_window_short_spike_is_not_reported() -> None:
    eng, clk = _engine()
    assert clk.run(60, lambda _t: {"cpu": 90.0}) == []  # abnormal, but only for 60 s (< 180 s)
    assert clk.run(300, lambda _t: {"cpu": 20.0}) == []
    assert eng.active(DEV) == []


def test_sustained_deviation_opens_with_evidence_then_resolves_with_hysteresis() -> None:
    eng, clk = _engine()
    trs = clk.run(400, lambda _t: {"cpu": 85.0})
    detected = [t for t in trs if t.kind == "detected"]
    assert len(detected) == 1
    a = detected[0].anomaly
    assert (
        a.anomaly_type is AnomalyType.BEHAVIORAL
        and a.signal_id == "cpu"
        and a.lifecycle in (Lifecycle.DETECTED, Lifecycle.ONGOING)
    )
    ev = a.evidence
    assert {"observed", "expected", "deviation", "duration_s", "baseline", "methods", "confidence"} <= set(ev)
    assert ev["expected"]["median"] == 20.0 and ev["baseline"]["status"] == "STABLE"
    assert (
        a.expected_min is not None
        and a.expected_max is not None
        and a.deviation_score
        and a.deviation_score > 3.5
    )
    assert 0.7 <= (a.confidence or 0) <= 1.0
    # between the recovery and the trigger bar: still active (no flapping)
    assert "resolved" not in kinds(clk.run(300, lambda _t: {"cpu": 36.0}))  # z ~2.2, delta 16
    assert eng.active(DEV)
    # clearly normal, but only for 60 s (< recovery_s 120): still active
    assert "resolved" not in kinds(clk.run(60, lambda _t: {"cpu": 20.0}))
    trs2 = clk.run(200, lambda _t: {"cpu": 20.0})
    assert "resolved" in kinds(trs2) and eng.active(DEV) == []
    closed = next(t.anomaly for t in trs2 if t.kind == "resolved")
    assert closed.lifecycle is Lifecycle.RESOLVED and closed.resolved_at is not None


def test_one_active_anomaly_per_key_and_rate_limited_updates() -> None:
    eng, clk = _engine()
    trs = clk.run(1200, lambda _t: {"cpu": 85.0})
    assert kinds(trs).count("detected") == 1
    assert len(eng.active(DEV)) == 1
    assert kinds(trs).count("updated") <= 1200 / 60 + 3  # not one event per evaluation


def test_recurrence_within_cooldown_reopens_the_same_anomaly() -> None:
    eng, clk = _engine()
    first = next(t for t in clk.run(300, lambda _t: {"cpu": 85.0}) if t.kind == "detected").anomaly
    clk.run(200, lambda _t: {"cpu": 20.0})  # resolved
    assert eng.active(DEV) == []
    trs = clk.run(300, lambda _t: {"cpu": 85.0})
    reopened = [t for t in trs if t.kind == "updated" and "reopened" in t.changed]
    assert reopened and reopened[0].anomaly.anomaly_id == first.anomaly_id
    assert reopened[0].anomaly.occurrences == 2 and "detected" not in kinds(trs)
    # after the cooldown a recurrence is a new anomaly
    clk.run(200, lambda _t: {"cpu": 20.0})
    clk.run(1000, lambda _t: {"cpu": 20.0})
    trs2 = clk.run(300, lambda _t: {"cpu": 85.0})
    assert "detected" in kinds(trs2)


def test_suppression_when_a_safety_threshold_already_covers_the_signal() -> None:
    eng, clk = _engine()
    trs = clk.run(400, lambda _t: {"cpu": 96.0}, threshold_signals=frozenset({"cpu"}))
    assert "detected" not in kinds(trs)
    sup = [t for t in trs if t.kind == "suppressed"]
    assert len(sup) == 1  # recorded once, then only counted
    assert sup[0].anomaly.lifecycle is Lifecycle.SUPPRESSED
    assert "safety-threshold" in sup[0].anomaly.evidence["suppressed_because"]
    assert eng.stats["suppressed"] > 1


def test_false_positive_feedback_mutes_the_key() -> None:
    _eng, clk = _engine()
    trs = clk.run(400, lambda _t: {"cpu": 85.0}, muted_keys=frozenset({"behavior.cpu"}))
    assert "detected" not in kinds(trs) and "suppressed" in kinds(trs)


def test_missing_data_expires_instead_of_resolving_or_inventing_values() -> None:
    eng, clk = _engine()
    clk.run(300, lambda _t: {"cpu": 85.0})
    assert eng.active(DEV)
    assert clk.run(500, lambda _t: {"cpu": None}) == []  # no data yet for expire_after_s (600 s)
    trs = clk.run(200, lambda _t: {"cpu": None})
    assert kinds(trs) == ["expired"]
    a = trs[0].anomaly
    assert (
        a.lifecycle is Lifecycle.EXPIRED and a.evidence["closed_because"] == "no current data to confirm it"
    )


def test_correlated_signals_share_an_incident_key() -> None:
    eng, clk = _engine()
    trs = clk.run(400, lambda _t: {"cpu": 85.0, "temperature": 75.0, "memory": 56.0})
    active = eng.active(DEV)
    assert {a.signal_id for a in active} == {"cpu", "temperature"}
    keys = {a.correlation_key for a in active}
    assert len(keys) == 1 and None not in keys
    inc = active[0].evidence["incident"]
    assert set(inc["signals"]) == {"cpu", "temperature"} and "not necessarily causal" in inc["wording"]
    cpu = next(a for a in active if a.signal_id == "cpu")
    assert any(r["signal_id"] == "temperature" and r["abnormal"] for r in cpu.related)
    assert any(t.kind == "detected" for t in trs)


def test_cold_baseline_never_yields_confident_anomalies() -> None:
    eng, _clk = _engine()
    cold = {"cpu": _baseline("cpu", [10.0 + (i % 21) for i in range(500)], BaselineStatus.COLD)}
    out: list[Transition] = []
    now = T0
    for _ in range(60):
        now += timedelta(seconds=10)
        out += eng.evaluate(DEV, now, {"cpu": _obs("cpu", 95.0, now)}, cold)
    det = [t.anomaly for t in out if t.kind == "detected"]
    assert det and all((a.confidence or 0) <= 0.45 for a in det)
    assert det[0].evidence.get("note", "").startswith("No device baseline yet")


def test_acknowledged_state_survives_updates_and_process_context_wording() -> None:
    eng, clk = _engine()
    procs = [{"name": "build.exe", "cpu_percent": 70.0, "memory_percent": 3.0}]
    a = next(
        t for t in clk.run(300, lambda _t: {"cpu": 85.0}, processes=procs) if t.kind == "detected"
    ).anomaly
    assert "not established as the cause" in a.evidence["process_context"]["wording"]
    eng.acknowledge(DEV, a.anomaly_id)
    clk.run(300, lambda _t: {"cpu": 95.0}, processes=procs)
    assert eng.active(DEV)[0].lifecycle is Lifecycle.ACKNOWLEDGED


def test_process_context_can_be_disabled_by_policy() -> None:
    _eng, clk = _engine(process_context=False)
    procs = [{"name": "secret-tool.exe", "cpu_percent": 70.0, "memory_percent": 3.0}]
    a = next(
        t for t in clk.run(300, lambda _t: {"cpu": 85.0}, processes=procs) if t.kind == "detected"
    ).anomaly
    assert "process_context" not in a.evidence
    assert "secret-tool" not in str(a.to_dict())


def test_replay_of_the_same_input_is_deterministic() -> None:
    def run() -> list[tuple[float, str, str, float | None]]:
        _e, clk = _engine()
        clk.run(
            1500, lambda t: {"cpu": 85.0 if t < T0 + timedelta(minutes=12) else 20.0, "temperature": 50.0}
        )
        return [(s, t.kind, t.anomaly.anomaly_id, t.anomaly.confidence) for s, t in clk.log]

    assert run() == run()
