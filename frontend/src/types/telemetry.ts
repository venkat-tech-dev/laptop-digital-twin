/** Typed contract with the backend (mirrors backend/app/schemas/twin.py and the WS protocol). */

export type MetricValue = number | boolean | string | null

export type Quality = 'GOOD' | 'DEGRADED' | 'STALE' | 'UNAVAILABLE' | 'ERROR'
export type MetricKind = 'measured' | 'derived' | 'static'
export type DeviceStatus = 'LIVE' | 'DEGRADED' | 'STALE' | 'OFFLINE'
export type HealthStatus = 'healthy' | 'warning' | 'critical' | 'unknown'
export type Severity = 'info' | 'warning' | 'critical'

export interface MetricReading {
  key: string
  metric: string
  value: MetricValue
  unit: string
  timestamp: string
  source: string
  quality: Quality
  availability: 'available' | 'unavailable'
  kind: MetricKind
  reason: string | null
  labels: Record<string, string>
}

export interface HealthReason {
  severity: 'ok' | 'info' | 'warning' | 'critical'
  message: string
  impact: number
  metric: string | null
  value: number | string | null
}

export interface ComponentHealth {
  score: number | null
  status: HealthStatus
  reasons: HealthReason[]
}

export type ComponentType =
  | 'laptop' | 'chassis' | 'display' | 'motherboard' | 'cpu' | 'gpu' | 'memory' | 'vrm' | 'storage' | 'disk'
  | 'battery' | 'cooling' | 'fan' | 'thermal_sensors' | 'network' | 'network_adapter' | 'power'
  | 'operating_system' | 'telemetry_agent'

export interface TwinComponent {
  component_id: string
  component_type: ComponentType
  name: string
  parent_id: string | null
  manufacturer: string | null
  model: string | null
  properties: Record<string, unknown>
  current_state: string
  health: ComponentHealth
  availability: 'available' | 'partial' | 'unavailable' | 'no_telemetry'
  last_updated: string | null
  telemetry: Record<string, MetricReading>
}

export interface Anomaly {
  anomaly_id: string
  device_id: string
  detector: 'rule' | 'statistical'
  rule_id: string
  component_id: string
  metric_key: string
  severity: Severity
  title: string
  message: string
  value: number | string | null
  threshold: number | string | null
  started_at: string
  last_seen_at: string
  resolved_at: string | null
  status: 'active' | 'resolved'
  context: Record<string, unknown>
  /** Present on REST responses (not on WebSocket events). */
  acknowledgement?: { acknowledged_by: string; acknowledged_at: string; note: string | null } | null
  confidence?: { value: number; method: string; factors: Record<string, number> } | null
}

export interface ProcessInfo {
  pid: number
  name: string
  status: string
  cpu_percent: number | null
  memory_rss_bytes: number | null
  memory_percent: number | null
  num_threads: number | null
  gpu_percent: number | null
  io_read_bytes_per_sec: number | null
  io_write_bytes_per_sec: number | null
  handle_count?: number | null
  started_at?: string | null
  tcp_established?: number | null
  tcp_listening?: number | null
  udp_endpoints?: number | null
  /** Opt-in fields (agent configuration); profile name redacted from paths. */
  path?: string | null
  user?: string | null
  publisher?: string | null
}

export interface ProcessSnapshot {
  timestamp: string
  source: string
  total_processes: number
  unavailable_fields: Record<string, string>
  details_collected?: boolean
  processes: ProcessInfo[]
}

export interface ThermalSummary {
  cpu_area_temperature_c: number | null
  sensor: string | null
  metric_key: string | null
  source: string | null
  band: 'normal' | 'elevated' | 'hot' | 'critical' | 'unknown'
  is_cpu_package_sensor: boolean
}

export interface TwinSnapshot {
  device_id: string
  device_status: DeviceStatus
  last_seen: string | null
  generated_at: string
  mode: 'live'
  data_source: string
  components: TwinComponent[]
  health: { overall: ComponentHealth; components: Record<string, ComponentHealth> }
  thermal: ThermalSummary
  active_anomalies: Anomaly[]
  processes: ProcessSnapshot | null
}

export type PortKind = 'usb_c' | 'usb_c_tb' | 'usb_a' | 'hdmi' | 'rj45' | 'audio' | 'lock' | 'smartcard'

/** Published physical design of a specific laptop model (reference data, not telemetry). */
export interface DeviceProfile {
  id: string
  name: string
  source: string
  chassis_mm: { width: number; depth: number; height: number }
  lid_mm: number
  colour: { name: string; hex: string }
  display: { diagonal_in: number; aspect: string; bezel_mm: { side: number; top: number; bottom: number } }
  keyboard: { layout: string; trackpoint: boolean; trackpad_buttons: number; numpad: boolean; fingerprint: string | null }
  webcam: { privacy_shutter: boolean }
  ports: { left: { kind: PortKind; label: string }[]; right: { kind: PortKind; label: string }[] }
  internals: { fans: number; heat_pipes: number; sodimm_slots: number; ssd: string; wlan: string; battery_cells: number; speakers: number }
  accuracy: string
}

export interface Geometry {
  kind: 'generic' | 'profile' | 'matched' | 'exact'
  label: string
  url: string | null
  detected: string
  note?: string | null
  attribution?: string | null
  panel?: { width_cm: number; height_cm: number; diagonal_in: number } | null
  resolution?: string | null
  profile?: DeviceProfile | null
  photo_url?: string | null
  photo_attribution?: string | null
}

export interface DeviceInfo {
  device_id: string
  manufacturer: string | null
  model: string | null
  model_number: string | null
  os_name: string | null
  status: DeviceStatus
  last_seen: string | null
  agent_version: string
  data_source: string
  mode: string
  telemetry: string
  sensor_provider: string | null
  geometry: Geometry
}

export interface ComponentDelta {
  current_state: string
  availability: TwinComponent['availability']
  health: ComponentHealth
  last_updated: string | null
  telemetry: Record<string, MetricReading>
}

// ----------------------------------------------------------------------------- WebSocket events
interface WsBase {
  timestamp: string
  device_id?: string | null
}

/** Pipeline timestamps (ISO 8601 UTC). collected_at is the device clock. */
export interface PipelineTiming {
  collected_at: string
  sent_at: string
  server_received_at: string
  published_at: string
  replay: boolean
}
export interface TelemetryUpdateEvent extends WsBase {
  event: 'telemetry_update'
  sequence: number
  device_status: DeviceStatus
  components: Record<string, ComponentDelta>
  processes?: ProcessSnapshot
  timing?: PipelineTiming
}
export type Presence = 'ONLINE' | 'STALE' | 'OFFLINE' | 'UNKNOWN'
export interface PresenceEvent extends WsBase {
  event: 'device_presence_changed'
  presence: Presence
  previous_presence: Presence
  last_contact_at: string | null
}
export interface SubscribedEvent extends WsBase {
  event: 'subscribed' | 'unsubscribed'
  topics: string[]
  accepted?: string[]
  rejected?: { topic: string; reason: string }[]
  devices?: string[]
}
export interface SnapshotEvent extends WsBase {
  event: 'twin_snapshot'
  twin: TwinSnapshot | null
}
export interface ConnectionStatusEvent extends WsBase {
  event: 'connection_status'
  status: string
  heartbeat_interval_s: number
  client_id: string
  primary_device_id?: string | null
}
export interface HeartbeatEvent extends WsBase {
  event: 'heartbeat'
  server_time: string
  device_status: DeviceStatus
  last_seen: string | null
}
export interface ComponentStateEvent extends WsBase {
  event: 'component_state_changed'
  domain_event: string
  component_id: string
  previous_state: string
  current_state: string
}
export interface HealthChangedEvent extends WsBase {
  event: 'health_changed'
  component_id: string
  score: number | null
  status: HealthStatus
  previous_score: number | null
  previous_status: HealthStatus
  reasons: HealthReason[]
}
export interface AnomalyEvent extends WsBase {
  event: 'anomaly_detected' | 'anomaly_resolved'
  anomaly: Anomaly
}
export interface DeviceStatusEvent extends WsBase {
  event: 'device_status_changed'
  status: DeviceStatus
  previous_status: DeviceStatus
  last_seen_at: string | null
}
export interface SystemEventMsg extends WsBase {
  event: 'system_event'
  event_type: string
  severity: string
  message: string
  data: Record<string, unknown>
}
export interface PongEvent extends WsBase {
  event: 'pong'
  server_time?: string
}

export type ServerEvent =
  | TelemetryUpdateEvent
  | SnapshotEvent
  | ConnectionStatusEvent
  | HeartbeatEvent
  | ComponentStateEvent
  | HealthChangedEvent
  | AnomalyEvent
  | DeviceStatusEvent
  | SystemEventMsg
  | PongEvent
  | PresenceEvent
  | SubscribedEvent

// ----------------------------------------------------------------------------- REST payloads
export interface HistoryPoint { t: string; avg: number; min: number; max: number; n: number }
export interface HistoryResponse {
  device_id: string
  start: string
  end: string
  bucket_seconds: number
  series: Record<string, HistoryPoint[]>
}
export interface RecentResponse { device_id: string; seconds: number; points: [number, Record<string, number>][] }

export interface Prediction {
  prediction_id: string
  title: string
  method: string
  kind: 'PREDICTED'
  status: string
  statement: string
  confidence: 'insufficient' | 'low' | 'medium' | 'high'
  confidence_score: number
  supporting_metrics: Record<string, unknown>
  assumptions: string[]
}
export interface PredictionsResponse { device_id: string; generated_at: string; disclaimer: string; predictions: Prediction[] }

export interface ScenarioInfo {
  scenario: string
  cpu_load: number
  gpu_load: number
  ram_gb: number
  on_battery: boolean
  description: string
}

export interface SimulationResult {
  mode: 'SIMULATION'
  label: string
  generated_at: string
  device_id: string
  baseline_captured_at: string | null
  baseline_device_status: DeviceStatus
  scenario: string
  workload: Omit<ScenarioInfo, 'scenario'>
  duration_s: number
  current: Record<string, number | string | boolean | null>
  predicted: Record<string, number | string | boolean | null>
  difference: Record<string, number | null>
  assumptions: string[]
  confidence: string
  confidence_score: number
  warnings: string[]
  trajectory: SimulationPoint[]
}
export interface SimulationPoint {
  t_s: number
  cpu_usage: number
  gpu_usage: number
  temperature_c?: number
  memory_percent?: number
  battery_percent?: number
  package_power_w: number
  temperature_low_c?: number
  temperature_high_c?: number
  fan_duty_percent_est?: number
  charge_power_w?: number
}

export interface SystemInfo {
  app_env: string
  version: string
  persistence: string
  timescaledb: boolean
  redis: string
  auth_mode: string
  websocket_clients: number
  persisted_samples: number
  persist_queue_depth: number
  uptime_s: number
  retention_days?: number
  retention_mechanism?: string
  persist_sample_interval_s?: number
  history_aggregation?: string
  persist_excluded_prefixes?: string[]
}

export interface HealthEvent {
  device_id: string
  component_id: string
  time: string
  previous_score: number | null
  score: number | null
  previous_status: string
  status: string
  reasons: HealthReason[]
}
