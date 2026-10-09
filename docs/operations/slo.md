# Service-level objectives

All targets are **proposed** (status `PROPOSED`). No target below has been measured over its evaluation
window yet, so none is a claim of achieved reliability. The definitions live in code
(`backend/app/services/slo.py`) and are served with their current values by
`GET /api/v1/ops/slo` (platform scope). Windowed error budgets and alerts come from Prometheus
(`infrastructure/monitoring/slo-rules.yml`).

| SLO | User journey | SLI (good events / valid events) | Target | Window | Alert | Dependencies |
|---|---|---|---|---|---|---|
| api-availability | Authenticated users can use the API | API requests not answered 5xx (excluding `/health`, `/metrics`, unmatched paths) | 99.5 % | 30 days | burn 14.4× over 1 h and 5 min (page); 6× over 6 h and 30 min (ticket) | backend, PostgreSQL |
| device-state-latency | Authorized users retrieve device state | device-state reads (`/devices/{id}/twin`, `/device`, `/twin`) within 250 ms | 95 % | 30 days | < 95 % over 1 h for 15 min | backend memory |
| ingest-success | Telemetry is accepted | batches accepted or deduplicated ÷ (that + batches shed as `overloaded`) | 99.9 % | 30 days | shed ratio > 14.4 × 0.1 % over 1 h | backend, quotas |
| ingest-processing | Telemetry is processed quickly | batches processed (validation → twin projection) within 250 ms | 99 % | 30 days | ratio below target over 1 h | backend CPU |
| persistence-lag | Accepted telemetry becomes durable | oldest sample waiting to be written | ≤ 60 s | continuous | > 60 s for 5 min; **any** dropped sample | PostgreSQL |
| alert-delivery | Critical alerts are not silently lost | oldest notification due for delivery | ≤ 120 s | continuous | > 120 s for 5 min | PostgreSQL, providers |
| background-health | Background failures are observable and recover | critical loops running (persister, receipts, audit, alerting, remediation, liveness) | 100 % | continuous | a loop crash-looping (readiness 503) | backend |
| remediation-traceability | Approved remediation has a traceable result | in-flight remediations older than 1 h | 0 | continuous | any stuck item | agent, backend |
| tenant-isolation | Isolation holds | cross-tenant exposures found by `test_tenant_isolation.py` | 0 | per release | failing test blocks release | code |

**Not SLIs by design:** client errors (4xx), deferred replays (the agent keeps the data and retries)
and validation rejections. They are visible in `ldt_ingest_rejected_total` but do not consume the
budget.

**Excluded journeys, with the reason:**
* *Digital Twin recovery after reconnect.* It is verified by tests (`frontend/src/services/twinSocket.test.ts`,
  `backend/tests/unit/test_twin_engine.py`: snapshot after resubscribe, version-gap resync). There is no
  runtime SLI yet; `ldt_pipeline_latency_ms{stage="websocket_delivery"}` is client-reported.
* *WebSocket delivery delay.* It is only measured when browsers report pings, so it is not reliable
  enough for an SLO.

## Error budgets

A 30-day 99.5 % availability target allows about 3 h 36 min of 5xx-equivalent failure. When more than
50 % of a budget is spent, prioritise reliability work over features in the next release. When it is
exhausted, freeze changes except reliability fixes. The burn-rate thresholds above follow the common
multi-window pattern (fast: 2 % of the budget per hour; slow: 5 % per 6 h).

## Current values

`GET /api/v1/ops/slo` reports each SLI computed from the running process's metrics, **cumulative since
that process started** (`window: process_lifetime`). An SLI with no events reports `NO_DATA`, never a
made-up 100 %. Example from the development laptop on 2026-10-09 (one device, minutes of uptime) is not
a reliability result and is not quoted.

## Validation status

* `slo-rules.yml` is valid YAML and uses metric and label names verified against `/metrics`
  (including `le="250.0"`). The PromQL has **not** been validated with `promtool` (no Prometheus binary
  on this machine). Run `promtool check rules infrastructure/monitoring/slo-rules.yml` in CI.
* Tested: `tests/unit/test_reliability_phase10.py` (endpoint content, `NO_DATA`, platform scope).
