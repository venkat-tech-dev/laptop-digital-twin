/** Phase-3 digital twin document (server: app/services/twin_engine.py, rules: app/domain/twin/rules.py). */

export type Freshness = 'LIVE' | 'RECENT' | 'STALE' | 'OFFLINE' | 'UNKNOWN' | 'UNSUPPORTED'
export type Visual = 'normal' | 'elevated' | 'warning' | 'critical' | 'offline' | 'unknown'
export type Connectivity = 'ONLINE' | 'DEGRADED' | 'STALE' | 'OFFLINE' | 'UNKNOWN'
export type TwinHealth = 'HEALTHY' | 'WARNING' | 'CRITICAL' | 'UNKNOWN'

export interface TwinSource {
  metric_key: string
  origin: string
  batch_id: string | null
  sequence: number | null
  received_at: string | null
}

/** One measured field: value + severity + freshness + provenance. ``value: null`` = unknown. */
export interface TwinField {
  value: unknown
  unit: string
  label: string | null
  reason: string | null
  timestamp: string | null
  interval_s: number | null
  source: TwinSource | null
  status: Visual
  freshness: Freshness
}

export interface TwinSection {
  visual: Visual
  because?: string[]
  freshness?: Freshness
}

export interface HealthReason {
  rule: string
  state: TwinHealth
  field: string | null
  message: string
  rule_description: string
}

export interface TwinHealthState {
  state: TwinHealth
  last_known?: TwinHealth | null
  reasons: HealthReason[]
  evaluated_at: string | null
  note?: string
}

/** Flat state: the same keys the server uses in patches (``performance.cpu.usage_percent``...). */
export type TwinFlat = Record<string, unknown>

export interface TwinSnapshotMsg {
  twin_id: string
  device_id: string
  twin_version: number
  epoch: string
  projected_at: string | null
  restored_from_cache: boolean
  format: 'flat' | 'nested'
  state: TwinFlat
}

export interface TwinEventItem {
  event_id: string
  time: string
  type: string
  kind: string
  severity: 'info' | 'warning' | 'error' | 'critical'
  message: string
  data: Record<string, unknown>
}

export interface FleetRow {
  device_id: string
  hostname: string | null
  manufacturer: string | null
  model: string | null
  owner: string | null
  owner_username: string | null
  department: string | null
  connectivity: Connectivity
  presence: string
  health: TwinHealth
  visual: Visual
  active_alerts: number
  last_telemetry_at: string | null
  last_contact_at: string | null
  twin_version: number
  primary: boolean
  cpu: number | null
  memory: number | null
  disk: number | null
  battery: number | null
  temperature: number | null
  internet: boolean | null
  cpu_status: Visual | null
  memory_status: Visual | null
  disk_status: Visual | null
  battery_status: Visual | null
  temperature_status: Visual | null
}

export interface FleetPage {
  total: number
  page: number
  page_size: number
  items: FleetRow[]
  counts: { total: number; by_health: Record<string, number>; by_connectivity: Record<string, number> }
}

export interface FleetSummary {
  organization: string
  total: number
  by_health: Record<string, number>
  by_connectivity: Record<string, number>
  departments: { name: string; total: number; by_health: Record<string, number>; by_connectivity: Record<string, number> }[]
}

export interface ExplainOut {
  path: string
  current: TwinField
  twin_version: number
  source_reading: null | { metric_key: string; value: unknown; unit: string; collected_at: string; source: string; quality: string; available: boolean; reason: string | null; interval_s: number | null }
  severity_rule: null | { elevated: number | null; warning: number | null; critical: number | null; higher_is_worse: boolean; hysteresis: number }
  boolean_rule: null | { alert_when: unknown; severity: string }
  freshness_limits_s: { live: number; recent: number }
}

export interface HistorySeries {
  device_id: string
  range: string
  bucket_seconds: number
  series: Record<string, { t: string; avg: number; min: number; max: number }[]>
}
