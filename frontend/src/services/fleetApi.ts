/** Phase 10 fleet intelligence and operations API (tenant-scoped on the server). */
import { qs, request } from './api'

export interface Deduction { factor: string; points: number; detail: string }
export interface FleetHealth {
  version: string
  status: 'OK' | 'INSUFFICIENT_DATA'
  score: number | null
  band: 'HEALTHY' | 'WATCH' | 'DEGRADED' | 'POOR' | null
  devices: number
  scored: number
  coverage: number
  confidence?: 'HIGH' | 'MEDIUM' | 'LOW'
  unknown_devices: string[]
  critical_conditions: { device_id: string; reasons: string[] }[]
  contributors: { factor: string; avg_points_deducted: number; devices: number }[]
  worst_devices: { device_id: string; score: number; band: string; deductions: Deduction[]; groups: string[] }[]
  weights: Record<string, number>
  formula?: string
  generated_at: string
  history: { at: string; score: number; coverage: number; devices: number; version: string }[]
}

export interface Association {
  dimension: string
  value: string
  devices_in_burst: number
  of_burst: number
  devices_in_fleet: number
  of_fleet: number
  lift: number
  p_value: number
  p_adjusted: number
  strength: 'STRONG' | 'MODERATE' | 'WEAK'
}
export interface FleetInsight {
  insight_id: string
  signal: string
  observed_fact: string
  devices: string[]
  window: { start: string; end: string }
  statistical_associations: Association[]
  possible_explanation: string
  causation: 'NOT_ESTABLISHED'
  method: string
}
export interface RecurringIssue {
  alert_type: string
  occurrences: number
  devices_affected: number
  devices_with_repeats: number
  worst_severity: string
  last_7_days: number
  previous_7_days: number
  trend: 'RISING' | 'FALLING' | 'STABLE'
  last_seen: string
}
export interface RemediationOutcome { action_type: string; completed: number; by_status: Record<string, number>; success_rate: number | null; note: string | null }
export interface FleetInsights {
  generated_at: string
  scope: { organization: string; devices: number; anomalies_considered: number; since_hours: number }
  correlation: { status: 'OK' | 'INSUFFICIENT_DATA'; reason?: string; insights: FleetInsight[]; window_minutes?: number; min_devices?: number }
  recurring_issues: RecurringIssue[]
  remediation_outcomes: RemediationOutcome[]
  legend: Record<string, string>
}

export interface Projection {
  status: 'OK' | 'INSUFFICIENT_DATA'
  metric?: string
  points?: number
  needed?: number
  current: number | null
  growth_per_day?: number
  growth_per_day_range?: [number, number]
  limit?: number
  utilization?: number | null
  days_to_limit?: number | string | null
  days_to_limit_earliest?: number | null
  assumptions?: string
  review_by?: string
}
export interface FleetCapacity {
  generated_at: string
  devices: Projection
  alert_volume?: Projection
  storage?: { status?: string; reason?: string; database_bytes?: number; uncompressed_chunk_bytes_per_day?: { day: string; bytes: number }[]; daily_volume_trend?: Projection; note?: string }
}

export interface FleetOperations {
  generated_at: string
  devices: number
  presence: Record<string, number>
  health: Record<string, number>
  critical_devices: string[]
  warning_devices: string[]
  active_anomalies: number
  upcoming_crossings_24h: { device_id: string; target: string; crossing_at: string | null; confidence: string; statement: string }[]
  agents: { recommended_version: string | null; by_version: Record<string, number>; outdated: string[]; legacy_enrolled: string[] }
  compliance: Record<string, number>
  remediation_outcomes: RemediationOutcome[]
  pipeline?: { persist_queue_depth: number; persist_oldest_age_s: number | null; ingest_inflight: number; background: { status: string; tasks: number; not_running: Record<string, { state: string; restarts: number; last_error: string | null }> } }
  notification_backlog?: { due: number; oldest_due_s: number | null }
}

export interface ModelGovernance {
  generated_at: string
  window_days: number
  anomaly_detection: { detectors: { detector: string; detected: number; labelled: number; label_coverage: number | null; false_positive_rate: number | null; status: string }[]; config_version: number | null; detection_delay: string; note: string }
  prediction?: { overall: Record<string, number | null>; by_target?: Record<string, Record<string, number | null>>; models_used: Record<string, number> }
  diagnosis?: { provider: Record<string, unknown>; prompt_version: string; stats: Record<string, number>; queue_depth: number }
}

export interface SloRow {
  slo_id: string
  journey: string
  sli: string
  target: number
  kind: 'ratio_good' | 'max_value'
  evaluation_window: string
  alert: string
  target_status: 'PROPOSED'
  current: number | null
  state: 'MEETING' | 'NOT_MEETING' | 'NO_DATA'
}
export interface SloReport { generated_at: string; window: string; process_uptime_s: number; note: string; slos: SloRow[] }

export const fleetApi = {
  health: () => request<FleetHealth>('/api/v1/fleet/health'),
  insights: (windowMinutes = 30, sinceHours = 24) => request<FleetInsights>(`/api/v1/fleet/insights${qs({ window_minutes: windowMinutes, since_hours: sinceHours })}`),
  capacity: () => request<FleetCapacity>('/api/v1/fleet/capacity'),
  operations: () => request<FleetOperations>('/api/v1/fleet/operations'),
  models: (days = 30) => request<ModelGovernance>(`/api/v1/fleet/models${qs({ days })}`),
  slo: () => request<SloReport>('/api/v1/ops/slo'),
}
