# Phase 7 — Local AI diagnosis & explainability

> AI may explain the endpoint. AI may recommend what a human should investigate.
> AI must not change the endpoint.

Nothing in Phase 7 can act on a device. The pipeline has no commands, process control, configuration
changes or tools. A model only produces text, and that text is validated before anyone sees it.

## Pipeline

```
trigger: HIGH/CRITICAL alert (automatic), or POST /alerts|anomalies|predictions/{id}/diagnose
  -> bounded job queue (DIAGNOSIS_QUEUE_MAX; duplicates share a job; 300 s cooldown per trigger for
     automatic runs; 429 + Retry-After when full)
  -> DiagnosticContext: minimised and sanitised
  -> fingerprint cache (same material facts within the TTL -> the existing diagnosis, status CACHED)
  -> evidence (typed, ranked, each with an id E1..En and a reference to its source record)
  -> rule hypotheses (supporting / contradicting / missing evidence; safe recommendations)
  -> platform confidence (computed from evidence; never the model's self-reported probability)
  -> [optional] local model: ranks and words the explanation -> validation
  -> versioned diagnosis (the previous version becomes SUPERSEDED; nothing is overwritten)
  -> diagnosis.* WebSocket events, twin "diagnoses.*" (DIAGNOSED), timeline entry, metrics
```

FACT → SIGNAL → EVIDENCE → HYPOTHESIS → DIAGNOSIS → RECOMMENDATION.

| Layer | Code |
|---|---|
| Context: sanitising, fingerprint | `backend/app/domain/diagnosis/context.py` |
| Evidence, temporal analysis, ranking | `backend/app/domain/diagnosis/evidence.py` |
| Rule hypotheses, recommendation catalogue, forbidden actions | `backend/app/domain/diagnosis/rules.py` |
| Platform confidence | `backend/app/domain/diagnosis/confidence.py` |
| Prompt (system/data boundary, output schema) | `backend/app/domain/diagnosis/prompts.py` |
| Validation (hallucination control) | `backend/app/domain/diagnosis/validation.py` |
| Assembly of the explanation | `backend/app/domain/diagnosis/composer.py` |
| Providers: rules, Ollama, mock; endpoint policy | `backend/app/services/diagnosis_providers.py` |
| Service: queue, context builder, versioning, events | `backend/app/services/diagnosis.py` |
| Storage (migration 0010: `diagnoses`, `diagnosis_feedback`) | `backend/app/repositories/diagnoses.py` |
| API | `backend/app/api/v1/diagnosis.py` |
| UI | `frontend/src/diagnosis/DiagnosisPanel.tsx` (alert detail, anomaly detail, forecast detail, twin page) |

Everything is reused, not duplicated:
- the twin, the Phase 4 baselines (`IntelligenceService.baseline_bounds`) and anomalies;
- the Phase 5 predictions and the Phase 6 alerts;
- process snapshots (`ProcessHistory`, `attribute_processes`) and the twin's security findings.

There is no second twin, anomaly engine or alert engine.

## Context (what can reach a model)

**Included:**
- 60 minutes of 1-minute means for CPU, memory, temperature, drive activity, drive space, gateway latency, packet loss, battery and GPU;
- usual ranges: the learned baseline (p95 for this hour and day type). On a device whose baseline is still cold, the typical level earlier in the hour plus a margin, labelled as such;
- active and recent anomalies, active predictions and open alerts;
- process names with CPU and memory during the episode and before it;
- security findings (for example "Secure Boot: off");
- recent agent events;
- device model.

**Never included:** command lines, paths, user names, host names, IP or MAC addresses, serial numbers, window titles, documents, clipboard contents, keystrokes, credentials or tokens. The platform does not collect most of these.

**Process names follow the enterprise policy.** With `TWIN_SHOW_PROCESS_NAMES=false` they become "process #n".

**Every endpoint string is untrusted.** `clean()` strips control characters and markup sequences (``` and < >) and caps the length. The model receives the data as a JSON document in the user message, introduced as data. The system prompt never contains telemetry.

## Evidence and temporal reasoning

**Evidence types:**

| Type | Example |
|---|---|
| OBSERVATION | "CPU is 90% (usual up to 45%)" |
| TEMPORAL | "above its usual range for 9 min, sudden onset around 11:19 UTC"; "CPU rose first; temperature followed 2 min later" |
| TREND | only for gradual rises (a step change is not a trend) |
| ANOMALY | Phase 4 anomaly, or the triggering alert |
| PREDICTION | Phase 5 forecast |
| PROCESS | "builder.exe averaged 62% CPU (before: 1%)" |
| CORRELATION | Pearson r, always worded "an association, not proof of cause" |
| EVENT | agent event |
| ABSENCE_OF_EXPECTED_SIGNAL | "Memory is within its usual range" |

**Onset patterns:**
- `sudden`: one two-minute change carries at least 70 % of the rise;
- `gradual`: elevated for 10 minutes or more without a step;
- `recurring`: three or more separate runs above the usual range;
- `recent`: elevated, but too short to be either.

The explanation includes a before / during / next sequence.

## Hypotheses

Rule-based hypotheses for the trigger and related signals:

| Area | Hypotheses |
|---|---|
| CPU | `cpu.process` (one process is at least 35 % of the CPU in the window and rose relative to before), `cpu.background` |
| Memory | `memory.process_growth` (≥ 300 MB growth), `memory.leak_like` (steady climb), `memory.overall_load` |
| Thermal | `thermal.workload` (CPU elevated or correlated, CPU first), `thermal.cooling` (hot without load; CPU normal contradicts the workload hypothesis) |
| Disk | `disk.capacity`, `disk.activity` (with paging when correlated with memory) |
| Other | `network.degradation`, `battery.drain`, `security.posture`, `resource_contention`, `unknown` |

Wording is hedged ("likely contributor", "consistent with"). Alternatives are always kept and shown.

Recommendations come from a fixed catalogue of human investigation steps. Destructive or command-like wording is blocked by word-bounded patterns (`FORBIDDEN_ACTIONS`): kill, terminate, delete, registry, firewall, PowerShell, cmd, uninstall, disable, `Set-*`, shutdown and similar.

## Platform confidence

```
support        = noisy-OR of supporting evidence strengths
base           = support × (1 − 0.6 × noisy-OR of contradicting strengths)
× data quality   (0.6 + 0.4 × coverage; × 0.5 when telemetry is stale)
× history        (1.0 with a learned device baseline, 0.85 without)
× temporal       (+5 % when the order of events fits, −15 % when it contradicts)
× missing        (0.93 per missing item, at most 3)
× trigger match  (0.75 outside the trigger's category)
× ambiguity      (leader × 0.9, never below the runner-up, when the top two are within 0.08)
minimum evidence: fewer than 2 supporting items -> capped below LOW
model agreement: at most ±0.05
```

**Bands:** HIGH ≥ 0.75, MEDIUM ≥ 0.5, LOW ≥ 0.3, otherwise INSUFFICIENT.

**Statuses:**
- AVAILABLE: MEDIUM or higher.
- LOW_CONFIDENCE: LOW.
- INSUFFICIENT_EVIDENCE: the unknown hypothesis leads, or fewer than 2 supporting items.
- FAILED: the pipeline failed.
- GENERATING: in progress.
- SUPERSEDED / EXPIRED: kept as history.

## Local model (optional)

The default is **rules only**: no model is configured. To enable a local model:

```
DIAGNOSIS_MODE=LOCAL_ONLY                     # LOCAL_ONLY | CENTRAL_PRIVATE | DISABLED
DIAGNOSIS_LLM_URL=http://host.docker.internal:11434   # Ollama
DIAGNOSIS_MODELS=qwen2.5:3b,qwen2.5:1.5b,gemma2:2b    # preference order; nothing is hard-coded
DIAGNOSIS_TIMEOUT_S=90
DIAGNOSIS_MEMORY_GATE_PERCENT=90
DIAGNOSIS_MODEL_HOST_DEVICE_ID=               # endpoint that also runs the model (empty = assume any)
DIAGNOSIS_MODEL_KEEP_ALIVE=5m                 # how long Ollama keeps the model loaded after a diagnosis
```

**Endpoint policy** (checked at start-up and before every request):
- `LOCAL_ONLY`: loopback, `host.docker.internal` or an `ollama` sidecar only.
- `CENTRAL_PRIVATE`: private (RFC 1918) addresses also allowed.
- Public addresses, credentials in the URL and non-HTTP schemes are always refused. A refused endpoint falls back to rules.

Nothing is sent to an external AI API.

**Model selection:**
- The provider reads `/api/tags` and picks the first configured model that is installed and fits the free memory (model size × 1.2 + 512 MB).
- Free memory is that of the machine running the model. With `DIAGNOSIS_MODEL_HOST_DEVICE_ID` set, it comes from that device's own telemetry (memory in use × total). This matters when the backend runs in Docker and Ollama on the Windows host: the container's `/proc/meminfo` describes the Docker VM, not the host. If the host's memory is unknown, no model is used (fail closed).
- It never assumes a GPU.
- When the device hosting the model is at or above `DIAGNOSIS_MEMORY_GATE_PERCENT` memory, inference is deferred and rules are used, with a notice.
- A short `DIAGNOSIS_MODEL_KEEP_ALIVE` (for example `30s`) gives the memory back right after a diagnosis, at the cost of reloading the model next time (a few seconds from disk).

**This laptop (2026-10-09):** Ollama 0.40.1 with `qwen2.5:3b` (1.9 GB) and `qwen2.5:1.5b` (1 GB), keep-alive 30 s, model host = this laptop. Its memory use is usually 85–98 % of 15.7 GB, so most diagnoses run on rules alone. A model is used only while about 2.7 GB (3B) or 1.7 GB (1.5B) is free.

**Robustness:**
- Requests are made with `format: json`, temperature 0.1 and no tools.
- Each request has a timeout, and jobs can be cancelled.
- After 3 consecutive failures the model cools down for 300 s.
- Every failure falls back to the deterministic result, with the notice **"AI reasoning unavailable. Showing deterministic evidence."**

**What the model may and may not do:**
- It ranks the candidate hypotheses, writes a summary, adds claims and investigation steps, and may propose at most one extra hypothesis.
- It cannot choose the primary cause. Its ranking only nudges confidence by ±0.05.
- An extra hypothesis needs 2 or more valid evidence ids. The platform scores it, and its confidence is capped at 0.49.

**Validation (hallucination control).** Each model statement must:
- parse into the schema;
- cite evidence ids that exist;
- use only numbers present in the cited evidence (within ±5 % or ±1);
- name only known processes;
- contain no forbidden action.

Over-certain wording ("definitely caused") is softened. A summary that names a process that does not support the likely cause is rejected. Rejected statements are stored in `rejected_claims` and hidden; the UI shows only their count.

## API

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/devices/{id}/diagnoses?current=&status=&type=` | anyone who can see the device |
| GET | `/api/v1/diagnoses/{id}` (evidence, hypotheses, versions, feedback) | same |
| GET | `/api/v1/diagnoses?alert_id=|anomaly_id=|prediction_id=` | same |
| POST | `/api/v1/alerts/{id}/diagnose`, `/anomalies/{id}/diagnose`, `/predictions/{id}/diagnose` → 202 `{job}` | operator, or the employee the device is assigned to; `force` (bypass cache) operator only; viewers read-only |
| GET / POST | `/api/v1/diagnosis-jobs/{id}`, `/diagnosis-jobs/{id}/cancel` | as above |
| POST | `/api/v1/diagnoses/{id}/feedback` `{verdict, actual_cause?, note?}` | anyone who can see the device |
| GET | `/api/v1/diagnosis-config/status` (mode, model health, queue, statistics, feedback totals) | staff |

A diagnosis, job or trigger the caller may not see answers 404, like an unknown id.

Feedback verdicts are HELPFUL, NOT_HELPFUL, CORRECT, PARTIALLY_CORRECT and INCORRECT. Feedback is used for evaluation only; no model is retrained from it.

There is no endpoint that accepts a prompt, runs a model synchronously or changes a device.

**WebSocket** (device topic): `diagnosis.started`, `diagnosis.updated`, `diagnosis.available`, `diagnosis.failed` and `diagnosis.expired`, each carrying the summary form. Clients fetch the details over REST.

**Twin:** `diagnoses.latest`, `diagnoses.active_count` and `diagnoses.generating` form a DIAGNOSED section, separate from the observed, anomalous and predicted fields. Timeline entries use `diagnosis.available`, `diagnosis.failed` and `diagnosis.expired`.

## Observability

Metrics:
- `ldt_diagnoses_total{status,reasoning}`
- `ldt_diagnosis_seconds{reasoning}`
- `ldt_diagnosis_queue_depth`
- `ldt_diagnosis_model_failures_total{reason}`
- `ldt_diagnosis_rejected_claims_total{reason}`
- `ldt_diagnosis_cache_hits_total`
- `ldt_diagnosis_jobs_dropped_total{reason}`

Logs carry ids, type, confidence, model and duration. Free text (feedback notes, telemetry strings) is never logged.

## Retention

- A diagnosis expires after `DIAGNOSIS_TTL_S` (6 h by default); it is kept as EXPIRED.
- Rows older than `DIAGNOSIS_RETENTION_DAYS` (90 by default) are purged. Feedback is removed with its diagnosis (cascade).

## Performance

From `scripts/diagnosis_bench.py`, 2,000 synthetic contexts; results are in `docs/diagnosis-bench-results.json`:

| Measure | Result |
|---|---|
| Deterministic pipeline | 0.75 ms p50 / 2.0 ms p95, about 1,000 diagnoses/s on one core |
| Validation of a model answer | 0.17 ms p50 |
| Prompt size | ~1,600 tokens p50, ~2,300 max (fits a 4k context) |

Model inference time is dominated by the local model. On CPU, a 1.5–3 B model takes tens of seconds, which is why work runs one job at a time from a queue and never inside a request.
