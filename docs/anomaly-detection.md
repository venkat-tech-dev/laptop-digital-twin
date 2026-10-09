# Intelligent anomaly detection (Phase 4)

Device-specific, context-aware and explainable detection of *unusual behavior for this laptop*,
alongside the deterministic safety thresholds of Phases 1–3. No forecasting, no LLM, no
recommendations, no remediation, no notifications (later phases).

## 1. What it answers

| Question | Answered by |
|---|---|
| Is a safety limit crossed? (memory ≥ 90 %, CPU ≥ 95 °C …) | `threshold_anomaly` – V1 rules, unchanged, confidence 0.95 (a fact) |
| Is this value unusual **for this device at this time**? | `behavioral_anomaly` – robust z / p99 / EWMA level shift on a learned baseline |
| Is it unusually erratic? | `volatility_anomaly` – robust minute-to-minute volatility vs the baseline spread |
| Is the **combination** unusual (each value normal alone)? | `multivariate_anomaly` – per-device Extended Isolation Forest + learned signal relations |

## 2. Data flow

```text
agent → ingest (Phase 2) → twin (Phase 3) ─┬─ V1 threshold rules (every applied reading)
                                           └─ IntelligenceService (every 10 s per online device)
                                                twin.window samples
                                                  → observe(): data-quality gates
                                                  → BehaviorEngine: detectors → lifecycle → correlation → evidence
                                                  → anomalies table (durable upsert)
                                                  → anomaly.detected / .updated / .resolved (WebSocket, device-scoped)
                                                  → twin alerts.* fields → twin.state.patch
scheduled (hourly) training: TimescaleDB 1-minute history (7 days) − confident anomaly intervals
                                                  → device_baselines (+ daily Isolation Forest → anomaly_models)
```

Raw telemetry is never modified: anomalies are separate records and the twin only gains
`alerts.*` fields.

Code: `backend/app/domain/anomalies/` is pure (no clock, DB or network – the caller passes `now`), so a
replay produces exactly the live result. `app/services/intelligence.py` wires it to the twin,
storage, events and metrics.

| Module | Role |
|---|---|
| `stats.py` | median, MAD, quantiles, robust z, EWMA, slope |
| `signals.py` | the 10 behavioral signals (twin field, unit, family, practical minimum delta, safety levels) and the learned relations (`temperature ~ cpu`) |
| `policy.py` | every tunable (`AnomalyPolicy`) |
| `baseline.py` | contextual baselines, COLD / DEVELOPING / STABLE / DEGRADED, fleet baseline |
| `observation.py` | data-quality gates |
| `detectors.py` | univariate + multivariate assessments |
| `iforest.py` | (Extended) Isolation Forest, robust scaler, relations, JSON (de)serialisation |
| `scoring.py` | severity points and evidence-based confidence |
| `behavior.py` | `BehaviorEngine`: lifecycle, dedupe/suppression, correlation, evidence |
| `replay.py` | replay engine, metrics, synthetic scenarios A–G |

## 3. Baselines

* Learned on **1-minute means** from persisted history (`baseline_history_days`, default 7), the same
  time scale as the observations (2-minute means), so a short spike is not compared with long averages.
* **Contexts** (first one with ≥ `min_context_samples` minutes wins): `how:wd:10` (weekday, 10:00–10:59) →
  `dt:wd` (all weekday minutes) → `all`.
* Per context: count, median, MAD, mean, std, p05/p25/p50/p75/p90/p95/p99 (`device_baselines`, one row per
  device × signal × context, with the concrete series key it was learned from).
* **Status**: COLD (< 60 minutes – fleet baseline, stricter trigger, confidence capped at 0.45), DEVELOPING,
  STABLE (≥ 7 days span and ≥ 2,880 minutes), DEGRADED (> 30 % of history excluded as incidents).
* **No contamination**: intervals of anomalies with confidence ≥ 0.7 are cut out before training; an
  interval an operator marked *false positive* stays in (it was normal behavior). Only data before the
  training time is used.
* **Retraining**: baselines hourly (`ANOMALY_RETRAIN_INTERVAL_S`), Isolation Forest daily
  (`ANOMALY_MODEL_RETRAIN_INTERVAL_S`), one device at a time in a background worker. Never on a telemetry
  event, never from the browser.

## 4. Detectors

Every trigger also needs a **practically relevant** deviation (`Signal.min_delta`, e.g. CPU 15 points,
memory 8 points, temperature 8 °C), so a very stable device does not alarm on meaningless changes.

| Detector | Trigger | Recovery (hysteresis) |
|---|---|---|
| robust z | `(x − median) / max(1.4826·MAD, floor) ≥ 3.5` (COLD: 5.0) | z ≤ 2.0 and EWMA below shift and ≤ p95, or deviation < ½ min_delta |
| quantile | x > p99 of the context (and z ≥ 2) | ≤ p95 |
| EWMA level shift | EWMA of observations ≥ 3 robust σ (sustained shift below the z trigger) | as above |
| volatility | median \|Δ\| of successive 1-minute means / 0.954 ≥ 3 × baseline σ (a single step does not count) | ratio < 1.8 |
| multivariate | Extended Isolation Forest score > learned threshold (99.5th percentile of clean training minutes) + 0.01 | score ≤ threshold − 0.02 |

The multivariate model learns each device's own **relation** `temperature ≈ a + b·cpu` and uses the
residual as a feature, so "busy CPU at idle-level temperature" is detected although neither value is
unusual alone (an axis-parallel Isolation Forest cannot see this; measured in scenario G). Features with
too little history are dropped first rather than blocking the model. The model is JSON (never pickle) and
validated on load (structure, dimensions, child indices).

## 5. Lifecycle and false-positive control

```text
normal → pending (abnormal) → after persistence_s (180 s; multivariate 2×) → DETECTED → ONGOING
pending → recovered → normal (nothing reported: transient)
active → recovered for recovery_s (120 s) → RESOLVED
active → no trustworthy data for expire_after_s (600 s) → EXPIRED (never "resolved" without evidence)
RESOLVED → abnormal again within cooldown_s (900 s) → same anomaly re-opens, occurrences + 1
operator acknowledgement → ACKNOWLEDGED (kept across updates)
```

* one active anomaly per device × detector key; updates are rate-limited (60 s, immediately on escalation)
* **SUPPRESSED** (recorded once, then counted): a behavioral candidate on a signal with an active safety
  threshold; a multivariate candidate whose contributing signals all have their own anomaly; a key an
  operator marked false positive (muted ≥ 1 h)
* level and confidence keep their **peak** while an anomaly fades; current values are in `evidence.current`

## 6. Severity (deterministic points)

| Factor | Points |
|---|---|
| deviation | robust z < 5: 1, 5–8: 2, ≥ 8: 3 (multivariate: margin over threshold) |
| persistence | ≥ 5 min: +1, ≥ 15 min: +2 |
| correlation | +1 per other abnormal signal of the family (max +2) |
| safety proximity | ≥ the twin's warning level: +1, ≥ critical level: +2 (forces ≥ HIGH) |
| impact | memory/temperature 2, CPU/latency/drive activity 1 |

Points → INFO < 2 ≤ LOW < 3 ≤ MEDIUM < 5 ≤ HIGH < 7 ≤ CRITICAL. **CRITICAL requires safety proximity**:
unusual but within safe limits is capped at HIGH. The breakdown is stored in `evidence.severity`.

## 7. Confidence (evidence-based)

`confidence = evidence^0.40 × persistence^0.20 × baseline^0.25 × data_quality^0.15`

| Factor | Meaning |
|---|---|
| evidence | logistic in how far the deviation exceeds the trigger (at the trigger: 0.5) |
| persistence | held / (2 × persistence window), capped at 1 |
| baseline | STABLE 0.95, DEVELOPING 0.6–0.9 by sample count, DEGRADED 0.5, COLD 0.3 (+ cap 0.45) |
| data quality | share of expected samples present in the observation window |

Bands: < 0.4 LOW, 0.4–0.7 MODERATE, 0.7–0.9 HIGH, ≥ 0.9 VERY HIGH.

## 8. Data quality and time alignment

An observation (2-minute mean) is produced only when: the device is connected (ONLINE presence); the
newest sample is fresh (3 × its own interval + publish wait + grace); ≥ 50 % of the expected samples are
present; impossible values (NaN, % outside 0–100, negative rates) are dropped and counted. Duplicate and
out-of-order samples never reach the window (Phase 2/3). Correlation uses the same evaluation instant for
every signal; anomalies of one family that start within `correlation_window_s` (120 s) form one incident
(`correlation_key`). Process context (top 3 by CPU) is worded "associated with (observed at the same
time; not established as the cause)" and is off when `ANOMALY_PROCESS_CONTEXT=false` or when the
enterprise policy hides process names (`TWIN_SHOW_PROCESS_NAMES=false`).

## 9. Graceful degradation

| Situation | Detection mode (visible in `/anomaly-summary`) |
|---|---|
| device baselines + model | `multivariate` |
| model unavailable / too little joint history / disabled | `statistical` |
| no device baseline (new device) | `fleet_baseline` (stricter, confidence ≤ 0.45) |
| nothing learned | `thresholds` (V1 safety rules always run on the ingest path) |

An evaluation or training error is logged and counted (`detector_error_total`) and never stops
telemetry, the twin or the threshold rules.

## 10. APIs (all server-side authorized; employees see only assigned devices, other ids answer 404)

| Method | Path | Role |
|---|---|---|
| GET | `/api/v1/devices/{id}/anomalies?status=&level=&type=&since=&until=&min_confidence=&signal_id=&limit=&offset=` | reader |
| GET | `/api/v1/devices/{id}/anomalies/active` | reader |
| GET | `/api/v1/devices/{id}/anomaly-summary` | reader |
| GET | `/api/v1/devices/{id}/baseline` | reader |
| GET | `/api/v1/anomalies/{id}` | reader (device-checked) |
| POST | `/api/v1/anomalies/{id}/feedback` `{verdict: true_positive\|false_positive\|unsure, note}` | operator |
| GET / PUT | `/api/v1/anomaly-config` (validated ranges, versioned, audited) | admin |

Legacy `/api/v1/anomalies`, `anomaly_detected` / `anomaly_resolved` events and old rows keep working.
There is deliberately **no** endpoint to train, run a detector, upload a model or execute anything.

WebSocket (device topic, existing infrastructure): `anomaly.detected`, `anomaly.updated` (with
`changed`), `anomaly.resolved` (also for EXPIRED) carrying the full record.

Twin document: `alerts.active` (≤ 10, compact), `alerts.recent` (≤ 5 closed), `alerts.active_count`,
`alerts.anomaly_count`, `alerts.highest_severity`. Health: safety thresholds drive health directly;
behavioral anomalies only when HIGH/CRITICAL **and** confidence ≥ 0.7 (WARNING).

## 11. Storage (migration 0007, non-destructive)

* `anomalies` + nullable columns: anomaly_type, category, level, confidence, lifecycle, signal_id,
  model_version, baseline_version, expected_value/min/max, deviation_score, evidence, related,
  correlation_key, occurrences, updated_at, feedback (legacy rows remain valid)
* `device_baselines` (device, signal, context) and `anomaly_models` (versioned JSON artifacts; one active
  per device)

## 12. Configuration

`ANOMALY_INTELLIGENCE_ENABLED`, `ANOMALY_EVAL_INTERVAL_S`, `ANOMALY_RETRAIN_INTERVAL_S`,
`ANOMALY_MODEL_RETRAIN_INTERVAL_S`, `ANOMALY_BASELINE_HISTORY_DAYS`, `ANOMALY_Z_TRIGGER`,
`ANOMALY_PERSISTENCE_S`, `ANOMALY_COOLDOWN_S`, `ANOMALY_IFOREST_ENABLED`, `ANOMALY_PROCESS_CONTEXT`.
Admins can change a validated subset at runtime (`PUT /anomaly-config`): trigger/recovery z, persistence,
recovery, cooldown, shift/volatility ratios, correlation window, expiry, contamination confidence,
Isolation Forest threshold quantile, enabling detectors, process context.

## 13. Observability

Prometheus: `anomalies_detected_total{type,level}`, `anomalies_resolved_total{type,how}`,
`anomalies_suppressed_total{type}`, `false_positive_feedback_total{type}`, `detector_latency_ms`,
`baseline_training_duration_seconds`, `model_training_duration_seconds`, `detector_error_total{stage}`,
`active_anomalies{type,level}`. Structured logs: `anomaly_detected`, `anomaly_closed`,
`anomaly_training_done/failed`, `anomaly_config_changed`, `anomaly_feedback`.

## 14. Evaluation

`scripts/anomaly_eval.py` replays telemetry through the live code (synthetic scenarios, and
`--device <id>` for recorded history, read-only). Results: `docs/anomaly-eval-results.json`.

| Scenario | Expected | Result |
|---|---|---|
| A CPU 10–30 % → 80–90 % for 20 min | anomaly | detected after 220 s; CPU + temperature as one incident; 0 FP |
| B CPU 95 % for 2 s | no behavioral anomaly | none |
| C CPU ↑ and temperature ↑ | correlated anomaly | one `correlation_key` for both |
| D new device | cold start, no confident anomalies | all COLD; none raised |
| E memory leak +1 %/min | level shift | detected after 670 s |
| F CPU alternating 10/60 % | volatility | volatility anomalies (CPU, temperature) |
| G busy CPU, idle temperature | multivariate only | detected by the model only (390 s) |
| normal 24 h | no alerts | 0 alerts; model drift 0.42 % above threshold (expected 0.5 %) |

Performance: `scripts/anomaly_bench.py` (in-process, full evaluation path incl. model scoring).

| Devices | Round | Per device | Share of one core (10 s interval) | Process RSS |
|---|---|---|---|---|
| 1 | 1 ms | 1.2 ms | ~0 % | 164 MB |
| 100 | 0.12 s | 1.2 ms | 1.2 % | 165 MB |
| 500 | 0.48 s | 1.0 ms | 5 % | 184 MB |
| 1,000 | 1.15 s | 1.2 ms | 11.7 % | 213 MB |

End-to-end A/B on the isolated stack (100 simulated agents, 90 s, fresh database each,
`docs/loadtest-ab-phase4.json`): intelligence off vs on – end-to-end p50 65 / 73 ms, p95 341 / 332 ms,
DB insert 934 / 964 ms, 0 drops and 0 errors in both: the difference is within run-to-run noise.

Training per device (8 days of 6 signals): baselines 0.7 s, Isolation Forest 3.7 s → hourly baselines +
daily models ≈ 24 % of one core at 1,000 devices. Beyond that, partition devices across backend replicas
(evaluation is per device and stateless apart from the engine memory).

## 15. Privacy

Only operational telemetry is learned (CPU, memory, GPU, temperature, drive activity/throughput, gateway
latency, network throughput). Process names appear only as optional "associated with" context, never in
baselines or models, and are disabled by policy flags. Baselines and models contain statistics only.
