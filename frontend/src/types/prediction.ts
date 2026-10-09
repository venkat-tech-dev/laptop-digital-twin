/** Phase-5 forecasting types (mirror of app/domain/prediction/engine.py and services/forecasting.py). */

export type ForecastStatus = 'AVAILABLE' | 'INSUFFICIENT_HISTORY' | 'LOW_CONFIDENCE' | 'UNSTABLE' | 'NOT_APPLICABLE' | 'STALE_DATA'
export type PredictionStatus = 'ACTIVE' | 'UPDATED' | 'LOW_CONFIDENCE' | 'INVALIDATED' | 'EXPIRED' | 'CONFIRMED' | 'CANCELLED'
export type ForecastHealth = 'GOOD' | 'FAIR' | 'LOW CONFIDENCE' | 'UNAVAILABLE'
export type TargetId = 'disk' | 'memory' | 'battery' | 'temperature' | 'cpu'

export interface ForecastRange { horizon_s: number; expected: number | null; lower: number | null; upper: number | null }

export interface PredictionRecord {
  prediction_id: string
  device_id: string
  correlation_key: string
  target_id: TargetId
  prediction_type: string
  metric: string
  unit: string
  direction: 'up' | 'down'
  status: PredictionStatus
  severity: string | null
  current_value: number | null
  threshold: number
  forecast_value: number | null
  forecast_at: string | null
  time_to_threshold_s: number | null
  crossing_at: string | null
  crossing_earliest: string | null
  crossing_latest: string | null
  lower_bound: number | null
  upper_bound: number | null
  confidence: number
  confidence_band: string
  model_type: string
  model_version: string
  feature_version: string
  history_start: string | null
  history_end: string | null
  statement: string
  evidence: {
    summary?: string
    forecast?: ForecastRange | null
    confidence?: { value: number; band: string; factors: Record<string, number> } | null
    model?: { type: string; version: string; feature_version: string } | null
    context?: Record<string, unknown>
    impact?: string
    wording?: string
  }
  created_at: string
  updated_at: string
  expires_at: string | null
  closed_at: string | null
  reason: string | null
  revisions: number
  actual_crossing_at: string | null
  timing_error_s: number | null
  lead_time_s: number | null
}

/** Per-target state from GET /devices/{id}/predictions. */
export interface TargetForecast {
  target_id: TargetId
  title: string
  unit?: string
  prediction_type?: string
  status: ForecastStatus
  reason: string
  current_value?: number | null
  threshold?: number
  time_to_threshold_s?: number | null
  earliest_s?: number | null
  latest_s?: number | null
  crossing_at?: string | null
  forecast?: ForecastRange | null
  confidence?: number | null
  confidence_band?: string | null
  severity?: string | null
  model?: string | null
  model_version?: string | null
  health: ForecastHealth
  prediction: PredictionRecord | null
  source_key?: string | null
}

export interface DevicePredictions { device_id: string; generated_at: string; targets: TargetForecast[]; wording: string }

/** Compact entry of the twin document (predictions.<target>). */
export interface TwinPrediction {
  status: ForecastStatus
  health: ForecastHealth
  reason: string
  title: string
  unit: string
  current_value: number | null
  forecast: ForecastRange | null
  model: string | null
  prediction_id?: string
  prediction_status?: PredictionStatus
  threshold?: number
  crossing_at?: string | null
  crossing_earliest?: string | null
  crossing_latest?: string | null
  confidence?: number
  confidence_band?: string
  severity?: string | null
  statement?: string
}

export interface ForecastCurve {
  device_id: string
  target_id: TargetId
  history: [number, number][]
  forecast: { t: number; mean: number; lower: number; upper: number }[]
  context: Record<string, unknown>
}

export const TARGET_ORDER: TargetId[] = ['disk', 'memory', 'battery', 'temperature', 'cpu']
