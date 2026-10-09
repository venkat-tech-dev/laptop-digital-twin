/** Phase-4 anomaly intelligence types (mirror of app/domain/anomalies/models.py Anomaly.to_dict). */

export type AnomalyLevel = 'INFO' | 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
export type AnomalyType = 'threshold_anomaly' | 'behavioral_anomaly' | 'volatility_anomaly' | 'multivariate_anomaly'
export type Lifecycle = 'DETECTED' | 'ONGOING' | 'ACKNOWLEDGED' | 'RESOLVED' | 'SUPPRESSED' | 'EXPIRED'

export const LEVELS: AnomalyLevel[] = ['INFO', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL']
export const TYPES: { id: AnomalyType; label: string }[] = [
  { id: 'threshold_anomaly', label: 'Safety threshold' },
  { id: 'behavioral_anomaly', label: 'Unusual for this device' },
  { id: 'volatility_anomaly', label: 'Unusually unstable' },
  { id: 'multivariate_anomaly', label: 'Unusual combination' },
]
export const typeLabel = (t: string): string => TYPES.find((x) => x.id === t)?.label ?? t

export interface SeverityFactor { factor: string; points: number; reason: string }
export interface RelatedSignal { signal_id: string; title: string; value: number; unit: string; expected: number; z: number; abnormal: boolean }

export interface AnomalyEvidence {
  summary?: string
  observed?: { value?: number; latest?: number; unit?: string; window_s?: number; samples?: number; score?: number }
  expected?: { median?: number; p05?: number; p95?: number; upper_trigger?: number; context?: string; threshold?: number | string | null; meaning?: string; rule?: string }
  deviation?: { robust_z?: number; ewma_z?: number; delta?: number; volatility_ratio?: number | null; trend_per_min?: number | null; margin?: number }
  duration_s?: number
  baseline?: { status?: string; version?: string; source?: string; context?: string; context_samples?: number; sample_count?: number; model_id?: string; model_version?: number; trained_samples?: number; trained_until?: string }
  methods?: { id: string; text: string }[]
  related?: RelatedSignal[]
  contributions?: { signal_id: string; title: string; robust_deviation: number }[]
  data_quality?: { coverage: number; dropped_impossible: number }
  severity?: { level: AnomalyLevel; points: number; breakdown: SeverityFactor[] }
  confidence?: number
  confidence_band?: string
  confidence_factors?: Record<string, number>
  process_context?: { wording: string; processes: { name: string | null; cpu_percent: number | null; memory_percent: number | null }[] }
  incident?: { title: string; signals: string[]; wording?: string } | null
  note?: string
  suppressed_because?: string
  closed_because?: string
}

export interface AnomalyRecord {
  anomaly_id: string
  device_id: string
  detector: string
  rule_id: string
  component_id: string
  metric_key: string
  severity: 'info' | 'warning' | 'critical'
  title: string
  message: string
  value: number | string | null
  threshold: number | string | null
  started_at: string
  last_seen_at: string
  resolved_at: string | null
  status: 'active' | 'resolved'
  anomaly_type: AnomalyType
  category: string | null
  level: AnomalyLevel
  confidence: number | null
  confidence_band?: string | null
  lifecycle: Lifecycle
  signal_id: string | null
  model_version: string | null
  baseline_version: string | null
  expected_value: number | null
  expected_min: number | null
  expected_max: number | null
  deviation_score: number | null
  persistence_s: number
  evidence: AnomalyEvidence
  related: RelatedSignal[]
  correlation_key: string | null
  occurrences: number
  updated_at: string | null
  feedback: { verdict: string; by: string; note: string | null; at: string } | null
  acknowledgement?: { acknowledged_by: string; acknowledged_at: string; note: string | null } | null
}

/** Compact entry of the twin document (alerts.active / alerts.recent). */
export interface TwinAlert {
  anomaly_id: string
  severity: string
  level: AnomalyLevel
  type: AnomalyType
  confidence: number | null
  title: string
  since: string
  resolved_at: string | null
  lifecycle: Lifecycle
  metric_key: string
  signal_id: string | null
  correlation_key: string | null
}

export interface AnomalyHistoryPage { device_id: string; items: AnomalyRecord[]; limit: number; offset: number; source: string }

export interface AnomalySummary {
  device_id: string
  active_count: number
  highest_severity: AnomalyLevel | null
  by_level: Record<AnomalyLevel, number>
  by_type: Record<AnomalyType, number>
  detection: { mode: string; reason: string }
  baseline_status: Record<string, string>
  data_quality: Record<string, string>
  last_evaluated: string | null
}

export interface BaselineContext { context: string; sample_count: number; median: number; mad: number; p05: number; p95: number; p99: number }
export interface DeviceBaseline {
  device_id: string
  detection: { mode: string; reason: string }
  last_training: { at: string; duration_s: number; model: Record<string, unknown> } | null
  signals: {
    signal_id: string
    title: string
    unit: string
    status: string
    source: string | null
    data_quality: string | null
    baseline: { trained_from: string | null; trained_until: string | null; sample_count: number; excluded_count: number; contexts: Record<string, BaselineContext> } | null
  }[]
  models: { model_id: string; version: number; n_train: number; active: boolean; created_at: string | null; features: string[] }[]
}

export interface AnomalyFilters {
  status?: 'active' | 'resolved'
  level?: AnomalyLevel[]
  type?: AnomalyType[]
  since?: string
  until?: string
  min_confidence?: number
  limit?: number
  offset?: number
}
