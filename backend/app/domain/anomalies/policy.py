"""Every anomaly-engine tunable in one place (filled from settings; admins can change a subset)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any


@dataclass(frozen=True)
class AnomalyPolicy:
    # ---- evaluation / data quality
    eval_interval_s: float = 10.0  # how often each device is evaluated
    observation_window_s: float = 120.0  # observation = mean of the samples in this window
    min_window_coverage: float = 0.5  # fraction of expected samples present, else "insufficient data"
    # ---- baselines
    baseline_history_days: float = 7.0
    training_bucket_s: int = 60  # baselines are learned on 1-minute means (matches observations)
    retrain_interval_s: float = 3600.0  # baselines
    model_retrain_interval_s: float = 86400.0  # Isolation Forest (~5x the cost of all baselines)
    cold_min_samples: int = 60  # < 1 hour of minutes: COLD (fleet baseline only)
    stable_min_span_days: float = 7.0  # >= 7 days: STABLE (contextual baselines)
    stable_min_samples: int = 2880
    min_context_samples: int = 120  # an hour/day-type context needs this many minutes, else fallback
    degraded_excluded_fraction: float = 0.3  # too much history excluded as incidents -> DEGRADED
    contamination_min_confidence: float = 0.7  # anomalies at/above this confidence are cut out of training
    # ---- univariate detection
    z_trigger: float = 3.5
    z_recover: float = 2.0
    z_trigger_cold: float = 5.0  # fleet baseline: much stricter (and low confidence)
    quantile_trigger: str = "p99"
    quantile_recover: str = "p95"
    persistence_s: float = 180.0  # abnormal for this long before an anomaly opens
    recovery_s: float = 120.0  # normal for this long before it resolves (hysteresis)
    cooldown_s: float = 900.0  # a recurrence within this window re-opens the same anomaly
    ewma_alpha: float = 0.3
    shift_z: float = 3.0  # EWMA level-shift threshold (robust sigmas)
    volatility_ratio: float = 3.0  # recent spread vs baseline spread
    # ---- multivariate (Isolation Forest)
    iforest_enabled: bool = True
    iforest_trees: int = 64
    iforest_sample_size: int = 128
    iforest_min_train_samples: int = 720  # 12 hours of clean minutes
    iforest_threshold_quantile: float = 0.995
    iforest_seed: int = 7
    iforest_margin: float = 0.01  # score must exceed the learned threshold by this much
    iforest_persistence_factor: float = 2.0  # least explainable detector: needs to persist longer
    # ---- correlation / lifecycle
    correlation_window_s: float = 120.0
    expire_after_s: float = 600.0  # no current data for this long (device offline/stale): EXPIRED
    update_event_min_interval_s: float = 60.0
    # ---- scoring
    confidence_bands: tuple[float, float, float] = (0.4, 0.7, 0.9)  # moderate, high, very high
    severity_bands: tuple[int, int, int, int] = (2, 3, 5, 7)  # LOW, MEDIUM, HIGH, CRITICAL (points)
    # ---- privacy
    process_context: bool = True
    enabled_detectors: tuple[str, ...] = field(default=("robust_z", "quantile", "ewma_shift", "iforest"))

    def public(self) -> dict[str, Any]:
        return asdict(self)

    def merged(self, changes: dict[str, Any]) -> AnomalyPolicy:
        """Apply admin changes (validated by the API schema); unknown keys are ignored."""
        allowed = {f.name for f in fields(self)}
        clean = {k: (tuple(v) if isinstance(v, list) else v) for k, v in changes.items() if k in allowed}
        return replace(self, **clean)  # type: ignore[arg-type]
