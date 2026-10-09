# ruff: noqa: E501  (the SLO table is data: one SLO per line)
"""Service-level objectives (Phase 10): definitions and current SLI values.

The SLO table below is the single source used by ``GET /api/v1/ops/slo`` and documented in
``docs/operations/slo.md``. Targets are **proposed** (status ``PROPOSED``): none of them has been
measured over its evaluation window yet. The endpoint reports *current* SLI values computed from this
process's Prometheus registry, cumulative since the process started (``window: process_lifetime``).
Windowed error budgets and burn-rate alerts come from Prometheus (``infrastructure/monitoring/slo-rules.yml``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.metrics import REGISTRY


@dataclass(frozen=True)
class Slo:
    slo_id: str
    journey: str
    sli: str
    target: float  # fraction (0.995) or threshold, see ``kind``
    kind: str  # ratio_good | max_value
    window: str
    alert: str
    dependencies: str
    measurement: str


SLOS: tuple[Slo, ...] = (
    Slo("api-availability", "Authenticated users can use the API", "share of API requests (excluding health, metrics, unmatched) not answered 5xx",
        0.995, "ratio_good", "30 days", "burn rate 14.4x over 1 h and 5 min, or 6x over 6 h and 30 min", "backend, PostgreSQL", "ldt_http_request_duration_seconds_count by status"),
    Slo("device-state-latency", "Authorized users retrieve device state quickly", "share of device-state reads (/devices/{id}/twin, /device, /twin) served within 250 ms",
        0.95, "ratio_good", "30 days", "below target over 1 h", "backend memory state", "ldt_http_request_duration_seconds_bucket{le=0.25}"),
    Slo("ingest-success", "Telemetry is accepted", "share of telemetry batches accepted or deduplicated (not rejected by the server or shed)",
        0.999, "ratio_good", "30 days", "burn rate 14.4x over 1 h", "backend, quotas", "ldt_ingest_batches_total by result + ldt_ingest_rejected_total{reason=overloaded}"),
    Slo("ingest-processing", "Telemetry is processed quickly", "share of batches processed (validation → twin) within 250 ms",
        0.99, "ratio_good", "30 days", "below target over 1 h", "backend CPU", "ldt_pipeline_latency_ms{stage=server_processing}"),
    Slo("persistence-lag", "Accepted telemetry becomes durable", "oldest sample waiting to be written (seconds)",
        60.0, "max_value", "continuous", "> 60 s for 5 min, or any ldt_persist_dropped_total increase", "PostgreSQL", "ldt_persist_oldest_queued_age_seconds, ldt_persist_dropped_total"),
    Slo("alert-delivery", "Critical alerts are not silently lost", "age of the oldest notification due for delivery (seconds)",
        120.0, "max_value", "continuous", "> 120 s for 5 min; any FAILED notification", "PostgreSQL, providers", "ldt_notification_oldest_due_seconds, notifications_total{kind=failed}"),
    Slo("background-health", "Background failures are observable and recover", "critical background loops running",
        1.0, "ratio_good", "continuous", "any critical loop crash_looping (readiness 503)", "backend", "ldt_background_task_up, ldt_background_task_restarts_total"),
    Slo("remediation-traceability", "Approved remediation has a traceable result", "in-flight remediations older than 1 hour (stuck)",
        0.0, "max_value", "continuous", "any stuck remediation", "agent, backend", "remediation service state"),
    Slo("tenant-isolation", "Tenant isolation holds under load", "cross-tenant exposures found by the isolation suite",
        0.0, "max_value", "per release", "any failing isolation test blocks the release", "code", "tests/unit/test_tenant_isolation.py (CI gate); ldt_cross_tenant_access_attempts_total counts attempts, not exposures"),
)  # fmt: skip


def _samples(name: str) -> list[tuple[str, dict[str, str], float]]:
    out: list[tuple[str, dict[str, str], float]] = []
    for metric in REGISTRY.collect():
        if metric.name == name or metric.name == name.removesuffix("_total"):
            out += [(s.name, s.labels, s.value) for s in metric.samples]
    return out


def _ratio(good: float, total: float) -> float | None:
    return round(good / total, 5) if total else None


def _http(routes: tuple[str, ...] | None, le: str | None) -> tuple[float, float]:
    """(good, total) over HTTP requests; good = non-5xx (le None) or within ``le`` seconds."""
    good = total = 0.0
    for name, labels, value in _samples("ldt_http_request_duration_seconds"):
        route = labels.get("route", "")
        if route in ("unmatched", "/metrics") or route.startswith("/health"):
            continue
        if routes is not None and route not in routes:
            continue
        if le is None and name.endswith("_count"):
            total += value
            if not labels.get("status", "").startswith("5"):
                good += value
        elif le is not None and name.endswith("_bucket") and labels.get("le") == le:
            good += value
        elif le is not None and name.endswith("_count"):
            total += value
    return good, total


def _pipeline(stage: str, le_ms: str) -> tuple[float, float]:
    good = total = 0.0
    for name, labels, value in _samples("ldt_pipeline_latency_ms"):
        if labels.get("stage") != stage:
            continue
        if name.endswith("_bucket") and labels.get("le") == le_ms:
            good = value
        elif name.endswith("_count"):
            total = value
    return good, total


def _counter(name: str, label: str | None = None) -> dict[str, float]:
    out: dict[str, float] = {}
    for sample, labels, value in _samples(name):
        if sample.endswith("_created"):
            continue
        out[labels.get(label, "") if label else ""] = (
            out.get(labels.get(label, "") if label else "", 0.0) + value
        )
    return out


STATE_ROUTES = ("/api/v1/devices/{device_id}/twin", "/api/v1/device", "/api/v1/twin")


def evaluate(container: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    values: dict[str, Any] = {}
    g, t = _http(None, None)
    values["api-availability"] = (_ratio(g, t), {"requests": int(t), "errors_5xx": int(t - g)})
    g, t = _http(STATE_ROUTES, "0.25")
    values["device-state-latency"] = (_ratio(g, t), {"reads": int(t)})
    batches = _counter("ldt_ingest_batches_total", "result")
    rejected = _counter("ldt_ingest_rejected_total", "reason")
    ok = batches.get("accepted", 0) + batches.get("duplicate", 0)
    # server-side failures only: client errors (validation, auth) and deferred replays (the agent keeps the
    # data and retries by design) are not counted against the SLO
    shed = rejected.get("overloaded", 0.0)
    values["ingest-success"] = (
        _ratio(ok, ok + shed),
        {"accepted_or_duplicate": int(ok), "shed": int(shed), "rejected_by_reason": rejected},
    )
    g, t = _pipeline("server_processing", "250.0")
    values["ingest-processing"] = (_ratio(g, t), {"batches": int(t)})
    p = container.persister
    values["persistence-lag"] = (
        p.oldest_age_s() or 0.0,
        {"depth": p.depth, "dropped_total": int(sum(_counter("ldt_persist_dropped_total").values()))},
    )
    oldest = None
    backlog = None
    alerts = getattr(container, "alerts", None)
    for _, _labels, value in _samples("ldt_notification_oldest_due_seconds"):
        oldest = value
    for _, _labels, value in _samples("ldt_notification_backlog"):
        backlog = value
    values["alert-delivery"] = (oldest if alerts is not None else None, {"backlog": backlog})
    sup = container.supervisor
    crit = [s for s in sup.tasks.values() if s.critical and s.state != "finished"]  # finished = feature off
    up = sum(1 for s in crit if s.state == "running")
    values["background-health"] = (_ratio(up, len(crit)), sup.status())
    rem = getattr(container, "remediation", None)
    stuck = None
    if rem is not None:
        from app.domain.remediation.models import IN_FLIGHT

        stuck = sum(
            1 for r in rem.items.values() if r.status in IN_FLIGHT and r.updated_at < now - timedelta(hours=1)
        )
    values["remediation-traceability"] = (stuck, {})
    values["tenant-isolation"] = (None, {"note": "verified per release by the isolation test suite"})
    out = []
    for slo in SLOS:
        current, detail = values.get(slo.slo_id, (None, {}))
        if current is None:
            state = "NO_DATA"
        elif slo.kind == "ratio_good":
            state = "MEETING" if current >= slo.target else "NOT_MEETING"
        else:
            state = "MEETING" if current <= slo.target else "NOT_MEETING"
        out.append({
            "slo_id": slo.slo_id, "journey": slo.journey, "sli": slo.sli, "target": slo.target, "kind": slo.kind,
            "evaluation_window": slo.window, "alert": slo.alert, "dependencies": slo.dependencies,
            "measurement": slo.measurement, "target_status": "PROPOSED", "current": current, "state": state,
            "detail": detail,
        })  # fmt: skip
    return {
        "generated_at": now.isoformat(),
        "window": "process_lifetime",
        "process_uptime_s": round(time.monotonic() - container.started_monotonic),
        "note": "Current values are cumulative since this process started; they are not an achieved "
        "reliability over the evaluation window. Windowed error budgets come from Prometheus.",
        "slos": out,
    }
