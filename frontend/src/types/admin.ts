export type Role = 'admin' | 'operator' | 'viewer' | 'employee'

export interface AuthConfig {
  mode: 'none' | 'api_key' | 'jwt' | 'accounts'
  setup_required?: boolean
  setup_token_required?: boolean
}

export interface Me {
  username: string
  display_name: string | null
  role: Role
  method: 'anonymous' | 'api_key' | 'jwt' | 'account'
  auth_mode: AuthConfig['mode']
  can_operate: boolean
  can_admin: boolean
  // Phase 9 (accounts mode): current organization, role and permissions; authorization stays server-side
  organization?: { org_id: string; name: string }
  org_role?: string | null
  org_role_label?: string
  platform_admin?: boolean
  permissions?: string[]
  organizations?: import('./org').OrgRef[]
  mfa?: boolean
  mfa_enrolled?: boolean
  mfa_setup_required?: boolean
  session_id?: string | null
}

export interface UserAccount {
  user_id: string
  username: string
  role: Role
  created_at: string
  disabled: boolean
  last_login_at: string | null
}

export interface SessionOut {
  user: UserAccount
  access_token: string
  expires_at: string
  organization_id?: string
  mfa?: boolean
  mfa_setup_required?: boolean
}

export interface WorkspaceDevice {
  device_id: string
  name: string
  status: string
}

export interface WorkspaceInfo {
  workspace_id: string
  name: string
  created_at: string
  device_ids: string[]
  devices: WorkspaceDevice[]
}

export interface AgentConfig {
  configured: boolean
  telemetry_interval_ms: number
  process_interval_ms: number
  top_process_count: number
  collect_process_details: boolean
  version: number
  updated_at: string | null
  updated_by: string | null
}

export interface AgentSettings {
  requested: AgentConfig
  applied: Partial<{
    telemetry_interval_ms: number
    process_interval_ms: number
    collect_process_details: boolean
    config_version: number
  }>
}

export interface SyncStatus {
  enabled: boolean
  target_url: string | null
  include_processes: boolean
  key_configured: boolean
  updated_at: string | null
  updated_by: string | null
  active: boolean
  queued_batches: number
  sent_batches: number
  dropped_batches: number
  last_success_at: string | null
  last_attempt_at: string | null
  last_error: string | null
}

export interface Confidence {
  value: number
  method: string
  factors: Record<string, number>
}

export interface CorrelatedSignal {
  metric_key: string
  label: string
  r: number
  samples: number
}

export interface ProcessContribution {
  process: string
  cpu_percent_during: number
  cpu_percent_before: number | null
  memory_bytes_during: number
  memory_bytes_before: number | null
}

export interface AnomalyAnalysis {
  anomaly_id: string
  metric_key: string
  window: { start: string; end: string; bucket_seconds: number }
  confidence: Confidence
  correlated_signals: CorrelatedSignal[]
  signal_source: string
  target_samples: number
  process_attribution: {
    ranked_by: 'cpu' | 'memory'
    snapshots_during: number
    snapshots_before: number
    history_since: string | null
    history_snapshots: number
    processes: ProcessContribution[]
    note: string | null
  }
  summary: string
}

export type HealthState = 'HEALTHY' | 'WARNING' | 'CRITICAL' | 'UNKNOWN'

export interface DeviceEventInfo {
  event_id: string
  type: string
  severity: 'info' | 'warning' | 'error' | 'critical'
  timestamp: string
  source: string
  message: string
  data: Record<string, string | number | boolean | null>
}

export interface CollectorHealthInfo {
  name: string
  lane: string
  interval_ms: number
  last_success_at: string | null
  last_error: string | null
  consecutive_failures: number
  total_failures: number
  last_duration_ms: number | null
}

export interface EndpointState {
  device_id: string
  device_health: { state: HealthState; reasons: string[]; checks: Record<string, HealthState>; evaluated_at: string } | null
  agent_health: {
    agent_version: string
    run_mode: string
    started_at: string
    uptime_s: number
    cpu_percent: number | null
    memory_rss_bytes: number | null
    queue_depth: number
    queue_bytes: number
    queue_dropped_total: number
    last_collection_at: string | null
    last_sync_at: string | null
    sync_failures_consecutive: number
    sync_failures_total: number
    lanes_replaced_total: number
    collectors: CollectorHealthInfo[]
    received_at?: string
  } | null
  events: DeviceEventInfo[]
  credential: { registered_at: string; last_used_at: string | null; revoked: boolean } | null
  presence?: PresenceInfo | null
  sequence?: SequenceInfo | null
}

export type PresenceState = 'ONLINE' | 'STALE' | 'OFFLINE' | 'UNKNOWN'

export interface PresenceInfo {
  device_id: string
  presence: PresenceState
  last_contact_at: string | null
  last_heartbeat_at: string | null
  last_batch_at: string | null
  contact_age_s: number | null
  heartbeat: { queue_depth?: number; api_latency_ms?: number | null; sync_online?: boolean; agent_version?: string } | null
}

export interface SequenceInfo {
  last_sequence: number | null
  received: number
  in_order: number
  out_of_order: number
  duplicates: number
  resets: number
  gaps_detected: number
  missing: number
  clock_drift_s: number | null
  max_abs_drift_s: number
}

export interface DeviceSummary {
  device_id: string
  manufacturer: string | null
  model: string | null
  agent_version: string | null
  telemetry_status: string
  presence: PresenceState
  last_contact_at: string | null
  last_sequence: number | null
  missing_batches: number
  clock_drift_s: number | null
  device_health: string | null
  queue_depth: number | null
  primary: boolean
}

export interface LatencySummary {
  count: number
  p50: number | null
  p95: number | null
  p99: number | null
  max: number | null
}

export interface PipelineStats {
  generated_at: string
  uptime_s: number
  ingest: {
    counters: { requests: number; accepted: number; duplicates: number; rejected: number; rate_limited: number; samples: number; events: number }
    rejections: Record<string, number>
    latency: Record<string, LatencySummary>
    receipts: { pending: number; written: number; dropped: number; last_error: string | null }
    rate_limit: { per_device_per_min: number; burst: number; limited_total: number }
    dedupe_cache_size: number
  }
  presence: { summary: Record<PresenceState, number>; stale_after_s: number; offline_after_s: number }
  sequences: Record<string, SequenceInfo>
  clock_drift_warnings: string[]
  persistence: {
    mode: string
    timescaledb: boolean
    aggregate_5m: boolean
    queue_depth: number
    written_total: number
    last_write_at: string | null
    last_error: string | null
    events_dropped: number
    events_failed: number
  }
  retention: { raw_days: number; aggregate_days: number; event_days: number; receipt_hours: number }
  websocket: {
    clients: number
    legacy_clients: number
    subscriptions_by_kind: Record<string, number>
    queued_messages: number
    sent_total: number
    slow_consumers_dropped_total: number
  }
}
