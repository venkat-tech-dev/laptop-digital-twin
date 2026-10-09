# Predictive analytics & failure forecasting (Phase 5)

Answers *"what is likely to happen next, and approximately when?"* — with a range, a confidence, the
evidence and the model behind every estimate. Deterministic statistics only: no LLM, no deep learning,
no notifications (Phase 6), no remediation.

## 1. Data flow

```text
twin.window (live samples) ──┐
TimescaleDB (once per series)┴─> BucketSeries (bounded, per device × target)
   -> prepare: quality gates, resampling, regime trimming
   -> assess: trend gates, level model, threshold crossing, confidence, severity, exceedance
   -> PredictionTracker: lifecycle (created/updated/invalidated/expired/confirmed/cancelled)
   -> predictions table · prediction.* WebSocket events · twin fields predictions.<target>
```

One background loop (`ForecastService`, every 15 s) evaluates each target on its own cadence. Nothing
runs inside an API request or per telemetry event; the browser cannot start training or run a model.
Forecasts are a separate concept in the twin (`predictions.*`): observed values (`performance.*`),
anomalies (`alerts.*`) and predictions are never mixed.

Code: `backend/app/domain/prediction/` (pure, injected clock): `targets.py`, `preprocess.py`,
`forecasters.py`, `scoring.py`, `engine.py`, `backtest.py`; service `app/services/forecasting.py`;
API `app/api/v1/predictions.py`; storage `app/repositories/predictions.py`, migration 0008.

## 2. What is forecast (audit of the recorded telemetry of this laptop)

| Target | Why | Resampling | History used / minimum | Horizon | Live model |
|---|---|---|---|---|---|
| System drive (C:) usage | capacity exhaustion stops updates and saves | 15 min, last | 14 days / 24 h, 24 points | 365 days (≤ 10× history) | robust trend; naive when flat |
| Memory utilization | exhaustion → paging, failures | 1 min, mean | 60 min / 20 min | 2 h (≤ 2× history) | trend if gates pass, else EWMA; exceedance probability |
| Battery charge (discharging only) | shutdown at critical charge | 1 min, last | 30 min since unplug / 8 min | 2 h (≤ 3× history) | trend on the current discharge |
| CPU-area temperature | throttling / shutdown | 30 s, mean | 15 min / 6 min | 30 min (≤ 1× history) | conservative trend, EWMA otherwise; exceedance |
| CPU utilization | sustained pressure | 1 min, mean | 30 min / 15 min | expected range in 15 min | EWMA range (trend only in words); exceedance |

Not forecast: gateway latency / packet loss (30 s sampling, spiky, 0/100 % loss bursts — no stable
trend); GPU temperature (not reported by this agent); battery *wear* stays in the legacy
`/analytics/predictions` endpoint (cycle-based, unchanged for backward compatibility).

Sampling on the real device: CPU / memory / temperature / battery every 5 s, gateway latency 30 s,
disk usage 60 s; 17–18 agent outages in 30 h (data gaps are handled, never interpolated).

## 3. Data quality

`BucketSeries` folds samples into buckets as they arrive (bounded memory, no rescans) and drops:
duplicates and out-of-order samples, samples from the future (clock skew > 5 s), non-finite values,
sentinels (Windows' 4294967295 "unknown" battery time) and physically impossible values. `prepare` then:

* `STALE_DATA` if the newest sample is older than the target's limit (e.g. temperature 60 s) — never a
  forecast from old data;
* `INSUFFICIENT_HISTORY` below the minimum span / points;
* regime change: a jump larger than the target's limit between consecutive buckets (disk cleanup,
  large file, workload switch) — only data after the last break is used; battery uses the last
  unplug time as its regime start and is `NOT_APPLICABLE` on AC power / charging.

## 4. Models (`forecasters.py`)

| Model | Version | Algorithm | Role |
|---|---|---|---|
| naive | naive-v1 | last value; random-walk interval | baseline every model must beat |
| ewma | ewma-v1 | simple exponential smoothing, alpha on one-step errors | level forecast for noisy metrics |
| trend | theilsen-v1 | Theil–Sen slope (median of pairwise slopes), Sen's 90 % slope interval, ±10 % structural floor | crossing forecasts |
| holt | holt-damped-v1 | damped additive trend (phi 0.98) | evaluated only: worst on every metric (see §9) |

**Trend gates** (all required before any time-to-threshold is claimed): slope ≥ the target's practical
minimum; the slope interval excludes zero; ≥ 60 % of pairwise slopes agree in sign; a real net change
between the first and last fifth of the window (≥ 2σ and ≥ half of slope × span); for saturating
metrics (temperature, memory) the recent half is not decelerating below half of the earlier slope.

**Selection.** The live model per metric was chosen by the walk-forward evaluation (§9): the
simplest model within 10 % of the best error. A rolling-origin comparison trend vs naive inside each
window (no future data) feeds the confidence's *skill* factor.

## 5. Threshold crossing, uncertainty, confidence, severity (`scoring.py`)

* **Crossing**: first time the mean path reaches the threshold within the reach (horizon, capped at
  the target's multiple of the usable history); `None` otherwise — a crossing time is never invented.
  Earliest / latest from the pessimistic / optimistic 80 % bounds ("~15 days, likely 13–17").
* **Confidence** = history^0.15 × quality^0.15 × skill^0.20 × stability^0.25 × horizon^0.15 ×
  precision^0.10 (× 0.85 when Phase 4 flags the signal as unusually volatile). Bands ≥ 0.75 HIGH,
  ≥ 0.5 MEDIUM, else LOW. Published only ≥ 0.5; kept while ≥ 0.4.
* **Severity** from time to threshold per target bands (disk 180 d / 30 d / 7 d / 1 d; memory & battery
  2 h / 1 h / 30 min / 10 min; temperature 30 / 15 / 8 / 3 min): INFO, LOW, MEDIUM, HIGH; CRITICAL only
  for the critical threshold, within the HIGH band, at confidence ≥ 0.75; LOW confidence lowers it one
  step.
* **Exceedance probability** (memory, CPU, temperature): share of recent windows of the horizon length
  whose values exceeded the threshold — for metrics that cross thresholds by recurring spikes rather
  than trends (this laptop's RAM sits at 85–90 % with short excursions above 90 %).

## 6. Lifecycle (`engine.py`)

```text
created (ACTIVE) -> updated (UPDATED / LOW_CONFIDENCE, only on material change) -> CONFIRMED | INVALIDATED | EXPIRED | CANCELLED
```

* one record per correlation key `device:target:type:threshold` — updates, not duplicates;
* stability: ETA smoothed against the previous estimate; an update is published only for a ≥ 20 % ETA
  change, a severity or confidence-status change, at most every 3 × the target's update interval;
* INVALIDATED (with reason) after 3 consecutive evaluations without a supporting forecast, immediately
  on a regime change or when not applicable (charger connected);
* EXPIRED when the latest likely crossing (+25 % grace) passes, or after a long period without data;
* CONFIRMED when the observed value reaches the threshold; stores the actual crossing time, the timing
  error of the first and the latest estimate and the lead time; no new prediction for the same key
  until the value falls back below the threshold − re-arm margin.

## 7. APIs (device authorization as in Phase 3/4; no training/execution endpoints exist)

| Method | Path | Role |
|---|---|---|
| GET | `/api/v1/devices/{id}/predictions` | reader — every target: status, range, confidence, active prediction |
| GET | `/api/v1/devices/{id}/predictions/history?active=&status=&target=&since=&until=&limit=&offset=` | reader |
| GET | `/api/v1/devices/{id}/predictions/{target}/forecast` | reader — actual history + forecast path |
| GET | `/api/v1/predictions/{id}` | reader (device-checked) |
| GET | `/api/v1/prediction-accuracy?device_id=&since=` | staff — calibration of closed predictions |
| GET / PUT | `/api/v1/prediction-config` | admin — validated policy, thresholds, horizons, bands; versioned |

WebSocket (device topic, existing infrastructure): `prediction.created`, `prediction.updated`,
`prediction.invalidated`, `prediction.expired`, `prediction.confirmed`, `prediction.cancelled`. Twin
fields: `predictions.<target>`, `predictions.active_count`, `predictions.highest_severity`.

**Phase 6 contract.** Each event carries a self-contained record: `prediction_type`, `severity`,
`metric`, `current_value`, `threshold`, `crossing_at` / `crossing_earliest` / `crossing_latest`,
`time_to_threshold_s`, `confidence` / `confidence_band`, `model_type` / `model_version`, `statement`,
`status` and `reason`. Delivery (email, Teams, Slack) is deliberately not implemented here.

## 8. Storage (migration 0008, new table only)

`predictions` with lifecycle, forecast, uncertainty, model/feature versions, history window, evidence
(JSONB) and calibration columns; indexes on (device, status), (device, target, created),
(correlation key, created), (type, status), updated_at, expires_at.

## 9. Evaluation

`scripts/prediction_eval.py` walks forward through recorded telemetry (read-only) and labelled synthetic
scenarios; results in `docs/prediction-eval-results.json`. See the final report for the numbers.

## 10. Configuration

`PREDICTION_ENABLED`; everything else per metric through `PUT /prediction-config` (thresholds,
window, minimum history, horizon, update interval, staleness, minimum slope, severity bands, enabled)
and policy (publish / keep confidence, invalidation count, material change, publish interval).

## 11. Known limitations

* Trends cannot foresee step changes (application launch, sudden file copy): no lead time is possible;
  recurring spikes are reported as an exceedance probability instead.
* Battery discharge is load-dependent; long-range battery estimates are weaker than short ones.
* Disk forecasts need at least a day of history and extrapolate at most 10× the usable history.
* Thermal forecasts are deliberately conservative (saturation gate, 1× extrapolation): few warnings.
