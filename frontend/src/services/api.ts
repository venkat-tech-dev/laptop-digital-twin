import type {
  Anomaly,
  HealthEvent,
  DeviceInfo,
  HistoryResponse,
  PredictionsResponse,
  ProcessSnapshot,
  RecentResponse,
  ScenarioInfo,
  SimulationResult,
  SystemInfo,
  TwinSnapshot,
} from '../types/telemetry'
import type {
  AgentSettings,
  AnomalyAnalysis,
  AuthConfig,
  EndpointState,
  Me,
  SessionOut,
  SyncStatus,
  UserAccount,
  WorkspaceInfo,
  DeviceSummary,
  PipelineStats,
} from '../types/admin'
import type { Geometry } from '../types/telemetry'
import type { AlertRecord, NotificationPage, NotificationPreferences, NotificationRecord, PreferencesResponse } from '../types/alerting'
import type { DevicePredictions, ForecastCurve, PredictionRecord } from '../types/prediction'
import type { DiagnosisDetail, DiagnosisJob, DiagnosisSummary, TriggerKind, Verdict } from '../types/diagnosis'
import type { CatalogAction, DryRunPlan, Remediation } from '../types/remediation'
import type { AnomalyFilters, AnomalyHistoryPage, AnomalyRecord, AnomalySummary, DeviceBaseline } from '../types/anomaly'
import type { ExplainOut, FleetPage, FleetSummary, HistorySeries, TwinEventItem, TwinSnapshotMsg } from '../types/twinDoc'
import { auth } from './auth'
import { scopePath } from './deviceScope'

export class ApiError extends Error {
  readonly status: number
  /** Stable machine-readable code from the backend (e.g. MFA_CODE_REQUIRED, PERMISSION_DENIED). */
  readonly code: string | null
  readonly requestId: string | null

  constructor(status: number, message: string, code: string | null = null, requestId: string | null = null) {
    super(message)
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

/** Error bodies: {code, message, request_id, detail} (Phase 9) or {detail: string | {code, message}} (earlier). */
export function parseError(status: number, fallback: string, body: unknown): ApiError {
  if (!body || typeof body !== 'object') return new ApiError(status, fallback)
  const b = body as { detail?: unknown; code?: unknown; message?: unknown; request_id?: unknown }
  const d = b.detail
  let message = typeof b.message === 'string' ? b.message : null
  let code = typeof b.code === 'string' ? b.code : null
  if (!message && typeof d === 'string') message = d
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const o = d as { message?: unknown; code?: unknown }
    if (!message && typeof o.message === 'string') message = o.message
    if (!code && typeof o.code === 'string') code = o.code
  }
  if (!message && Array.isArray(d)) message = 'The request was not valid' // FastAPI validation list
  return new ApiError(status, message ?? fallback, code, typeof b.request_id === 'string' ? b.request_id : null)
}

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

function headers(): Record<string, string> {
  const h: Record<string, string> = { Accept: 'application/json' }
  const jwt = auth.jwt()
  if (jwt) h.Authorization = `Bearer ${jwt}`
  else if (auth.mode === 'api_key' && auth.apiKey()) h['X-API-Key'] = auth.apiKey() as string
  return h
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  path = scopePath(path, init?.method ?? 'GET')
  const res = await fetch(`${BASE}${path}`, { ...init, headers: { ...headers(), ...(init?.headers ?? {}) } })
  if (!res.ok) {
    if (res.status === 401 && auth.mode === 'accounts' && auth.jwt()) {
      window.dispatchEvent(new Event('ldt:unauthorized')) // session expired or account disabled
    }
    let body: unknown = null
    try {
      body = await res.json()
    } catch {
      /* non-JSON error body */
    }
    throw parseError(res.status, res.statusText, body)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

/** Download a binary response (diagnostics bundle) as a file. */
export async function download(path: string, fallbackName: string): Promise<void> {
  const res = await fetch(`${BASE}${path}`, { headers: headers() })
  if (!res.ok) throw parseError(res.status, res.statusText, await res.json().catch(() => null))
  const blob = await res.blob()
  const name = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') ?? '')?.[1] ?? fallbackName
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}

export function qs(params: Record<string, string | number | readonly string[] | undefined>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined) continue
    if (Array.isArray(v)) v.forEach((item) => sp.append(k, item))
    else sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export const api = {
  authConfig: () => request<AuthConfig>('/api/v1/auth/config'),
  me: () => request<Me>('/api/v1/auth/me'),
  login: (username: string, password: string, otp?: string, organizationId?: string) =>
    send<SessionOut>('/api/v1/auth/login', 'POST', { username, password, otp: otp || undefined, organization_id: organizationId || undefined }),
  logout: () => send<{ ok: boolean }>('/api/v1/auth/logout', 'POST'),
  switchOrganization: (organizationId: string) => send<SessionOut>('/api/v1/auth/switch-organization', 'POST', { organization_id: organizationId }),
  signInProviders: (organization: string) =>
    request<{ items: { provider_id: string; name: string; kind: 'oidc' | 'saml' }[] }>(`/api/v1/auth/providers${qs({ organization })}`),
  setup: (username: string, password: string, setupToken?: string) =>
    send<SessionOut>('/api/v1/auth/setup', 'POST', { username, password, setup_token: setupToken || undefined }),
  users: () => request<UserAccount[]>('/api/v1/users'),
  createUser: (username: string, password: string, role: string) => send<UserAccount>('/api/v1/users', 'POST', { username, password, role }),
  updateUser: (id: string, changes: Record<string, unknown>) => send<UserAccount>(`/api/v1/users/${id}`, 'PATCH', changes),
  deleteUser: (id: string) => send<void>(`/api/v1/users/${id}`, 'DELETE'),
  workspaces: () => request<WorkspaceInfo[]>('/api/v1/workspaces'),
  createWorkspace: (name: string, deviceIds?: string[]) => send<WorkspaceInfo>('/api/v1/workspaces', 'POST', { name, device_ids: deviceIds }),
  updateWorkspace: (id: string, name: string, deviceIds?: string[]) =>
    send<WorkspaceInfo>(`/api/v1/workspaces/${id}`, 'PATCH', { name, device_ids: deviceIds }),
  deleteWorkspace: (id: string) => send<void>(`/api/v1/workspaces/${id}`, 'DELETE'),
  endpoint: (limit = 50) => request<EndpointState>(`/api/v1/endpoint${qs({ limit })}`),
  devices: () => request<FleetPage>('/api/v1/devices', { method: 'GET' }).then((p) => p.items as unknown as DeviceSummary[]),
  devicesPage: (params: { q?: string; connectivity?: string[]; health?: string[]; department?: string; sort?: string; order?: 'asc' | 'desc'; page?: number; page_size?: number }) =>
    request<FleetPage>(`/api/v1/devices${qs(params)}`),
  fleetSummary: () => request<FleetSummary>('/api/v1/fleet/summary'),
  deviceTwin: (id: string) => request<TwinSnapshotMsg>(`/api/v1/devices/${encodeURIComponent(id)}/twin${qs({ format: 'flat' })}`),
  deviceTimeline: (id: string, limit = 100) => request<{ items: TwinEventItem[]; source: string }>(`/api/v1/devices/${encodeURIComponent(id)}/timeline${qs({ limit })}`),
  deviceHistory: (id: string, fields: string[], range: '15m' | '1h' | '6h' | '24h') =>
    request<HistorySeries>(`/api/v1/devices/${encodeURIComponent(id)}/history${qs({ fields, range })}`),
  explainField: (id: string, path: string) => request<ExplainOut>(`/api/v1/devices/${encodeURIComponent(id)}/twin/explain${qs({ path })}`),
  assignDevice: (id: string, username: string | null, employeeName: string | null) =>
    send<Record<string, unknown>>(`/api/v1/devices/${encodeURIComponent(id)}/assignment`, 'PUT', { username, employee_name: employeeName }),
  pipelineStats: () => request<PipelineStats>('/api/v1/pipeline/stats'),
  revokeDevice: (deviceId: string) => send<{ revoked: boolean }>(`/api/v1/endpoint/credentials/${deviceId}`, 'DELETE'),
  agentSettings: () => request<AgentSettings>('/api/v1/settings/agent'),
  setAgentSettings: (changes: Record<string, unknown>) => send<AgentSettings>('/api/v1/settings/agent', 'PUT', changes),
  sync: () => request<SyncStatus>('/api/v1/settings/sync'),
  setSync: (changes: Record<string, unknown>) => send<SyncStatus>('/api/v1/settings/sync', 'PUT', changes),
  acknowledge: (id: string, note?: string) => send<Anomaly>(`/api/v1/anomalies/${id}/acknowledge`, 'POST', { note: note || null }),
  unacknowledge: (id: string) => send<Anomaly>(`/api/v1/anomalies/${id}/acknowledge`, 'DELETE'),
  anomalyAnalysis: (id: string) => request<AnomalyAnalysis>(`/api/v1/anomalies/${id}/analysis`),
  // Phase 4: anomaly intelligence (read side + operator feedback; no training or execution endpoints exist)
  deviceAnomalies: (id: string, f: AnomalyFilters = {}) =>
    request<AnomalyHistoryPage>(`/api/v1/devices/${encodeURIComponent(id)}/anomalies${qs({ ...f, min_confidence: f.min_confidence, level: f.level, type: f.type })}`),
  activeAnomalies: (id: string) => request<{ device_id: string; items: AnomalyRecord[] }>(`/api/v1/devices/${encodeURIComponent(id)}/anomalies/active`),
  anomalySummary: (id: string) => request<AnomalySummary>(`/api/v1/devices/${encodeURIComponent(id)}/anomaly-summary`),
  deviceBaseline: (id: string) => request<DeviceBaseline>(`/api/v1/devices/${encodeURIComponent(id)}/baseline`),
  anomaly: (id: string) => request<AnomalyRecord>(`/api/v1/anomalies/${encodeURIComponent(id)}`),
  // Phase 5: forecasting (read side only; no training or model endpoints exist)
  devicePredictions: (id: string) => request<DevicePredictions>(`/api/v1/devices/${encodeURIComponent(id)}/predictions`),
  predictionHistory: (id: string, params: { active?: boolean; target?: string[]; status?: string[]; limit?: number } = {}) =>
    request<{ device_id: string; items: PredictionRecord[] }>(
      `/api/v1/devices/${encodeURIComponent(id)}/predictions/history${qs({ active: params.active === undefined ? undefined : String(params.active), target: params.target, status: params.status, limit: params.limit })}`,
    ),
  forecastCurve: (id: string, target: string) =>
    request<ForecastCurve>(`/api/v1/devices/${encodeURIComponent(id)}/predictions/${encodeURIComponent(target)}/forecast`),
  prediction: (id: string) => request<PredictionRecord>(`/api/v1/predictions/${encodeURIComponent(id)}`),
  // Phase 6: alerts and notifications (the user id always comes from the session, never the request)
  alerts: (params: { device_id?: string; severity?: string[]; category?: string[]; status?: string[]; source_type?: string; limit?: number; offset?: number } = {}) =>
    request<{ items: AlertRecord[]; limit: number; offset: number }>(`/api/v1/alerts${qs(params)}`),
  alert: (id: string) => request<AlertRecord>(`/api/v1/alerts/${encodeURIComponent(id)}`),
  alertAction: (id: string, action: 'acknowledge' | 'resolve' | 'suppress', note?: string, hours?: number) =>
    send<AlertRecord>(`/api/v1/alerts/${encodeURIComponent(id)}/${action}`, 'POST', { note: note || null, ...(hours ? { hours } : {}) }),
  // Phase 7: diagnosis (asynchronous: a request returns a job; nothing is changed on the device)
  deviceDiagnoses: (id: string, params: { current?: boolean; limit?: number } = {}) =>
    request<{ items: DiagnosisSummary[] }>(`/api/v1/devices/${encodeURIComponent(id)}/diagnoses${qs({ current: params.current === undefined ? undefined : String(params.current), limit: params.limit })}`),
  diagnosesFor: (kind: TriggerKind, id: string, current = true) =>
    request<{ device_id: string; items: DiagnosisSummary[] }>(`/api/v1/diagnoses${qs({ [`${kind}_id`]: id, current: String(current) })}`),
  diagnosis: (id: string) => request<DiagnosisDetail>(`/api/v1/diagnoses/${encodeURIComponent(id)}`),
  requestDiagnosis: (kind: TriggerKind, id: string, force = false) =>
    send<{ job: DiagnosisJob }>(`/api/v1/${kind === 'alert' ? 'alerts' : kind === 'anomaly' ? 'anomalies' : 'predictions'}/${encodeURIComponent(id)}/diagnose`, 'POST', { force }),
  diagnosisJob: (id: string) => request<DiagnosisJob>(`/api/v1/diagnosis-jobs/${encodeURIComponent(id)}`),
  diagnosisFeedback: (id: string, verdict: Verdict, actualCause?: string, note?: string) =>
    send<{ verdict: Verdict }>(`/api/v1/diagnoses/${encodeURIComponent(id)}/feedback`, 'POST', { verdict, actual_cause: actualCause || null, note: note || null }),
  // Phase 8: remediation (every decision is authorised and audited server-side)
  remediations: (params: { device_id?: string; status?: string[]; action_type?: string; risk?: string; requested_by?: string; diagnosis_id?: string; since?: string; limit?: number; offset?: number } = {}) =>
    request<{ items: Remediation[]; permissions: string[] }>(`/api/v1/remediations${qs(params)}`),
  remediation: (id: string) => request<Remediation>(`/api/v1/remediations/${encodeURIComponent(id)}`),
  requestRemediation: (body: { device_id: string; action_type: string; parameters?: Record<string, string>; diagnosis_id?: string; mode?: string; scheduled_at?: string; dry_run?: boolean }) =>
    send<Remediation>('/api/v1/remediations', 'POST', body),
  remediationDecision: (id: string, decision: 'approve' | 'reject' | 'cancel', note?: string) =>
    send<Remediation>(`/api/v1/remediations/${encodeURIComponent(id)}/${decision}`, 'POST', { note: note || null }),
  remediationDryRun: (id: string) => send<DryRunPlan>(`/api/v1/remediations/${encodeURIComponent(id)}/dry-run`, 'POST'),
  actionCatalog: () => request<{ actions: CatalogAction[]; applications: { application_id: string; name: string }[] }>('/api/v1/action-catalog'),
  remediationStatus: () => request<{ signing_configured: boolean; key_id: string | null; kill_switch_env: boolean; auto_remediation_enabled: boolean; policy_version: number; in_flight: number; fleet_max_concurrent: number; by_status: Record<string, number>; open_circuits: { device_id: string; action: string; until: string }[] }>('/api/v1/remediation-admin/status'),
  remediationPolicy: () => request<{ kill_switches: { global: boolean; actions: string[]; devices: string[] }; four_eyes_min_risk: string; approval_ttl_s: number; auto_remediation_enabled: boolean; version: number }>('/api/v1/remediation-policy'),
  setKillSwitch: (scope: 'global' | 'action' | 'device', enabled: boolean, target?: string) =>
    send<Record<string, unknown>>('/api/v1/remediation-policy/kill-switch', 'POST', { scope, enabled, target: target ?? null }),
  verifyRemediationAudit: () => request<{ ok: boolean; rows: number; first_bad: number | null }>('/api/v1/remediation-admin/audit/verify'),
  notifications: (params: { unread?: boolean; severity?: string[]; category?: string[]; limit?: number; offset?: number } = {}) =>
    request<NotificationPage>(`/api/v1/notifications${qs({ ...params, unread: params.unread === undefined ? undefined : String(params.unread) })}`),
  unreadCount: () => request<{ unread: number; by_severity: Record<string, number> }>('/api/v1/notifications/unread-count'),
  readNotification: (id: string) => send<NotificationRecord>(`/api/v1/notifications/${encodeURIComponent(id)}/read`, 'POST'),
  readAllNotifications: () => send<{ marked: number }>('/api/v1/notifications/read-all', 'POST'),
  notificationPreferences: () => request<PreferencesResponse>('/api/v1/notification-preferences'),
  setNotificationPreferences: (p: NotificationPreferences) => send<PreferencesResponse>('/api/v1/notification-preferences', 'PUT', p),
  anomalyFeedback: (id: string, verdict: 'true_positive' | 'false_positive' | 'unsure', note?: string) =>
    send<{ anomaly_id: string; feedback: AnomalyRecord['feedback'] }>(`/api/v1/anomalies/${encodeURIComponent(id)}/feedback`, 'POST', { verdict, note: note || null }),
  downloadDiagnostics: (anonymize: boolean) => download(`/api/v1/system/diagnostics${qs({ anonymize: String(anonymize) })}`, 'ldt-diagnostics.zip'),
  uploadAsset: (kind: 'mesh' | 'photo', file: Blob, params: { exact?: boolean; attribution?: string }) =>
    request<Geometry>(`/api/v1/models/${kind}${qs({ exact: params.exact === undefined ? undefined : String(params.exact), attribution: params.attribution || undefined })}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    }),
  deleteAsset: (kind: 'mesh' | 'photo') => send<Geometry>(`/api/v1/models/${kind}`, 'DELETE'),
  token: (apiKey: string) =>
    request<{ access_token: string; expires_at: string }>('/api/v1/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey }),
    }),
  device: () => request<DeviceInfo>('/api/v1/device'),
  hardware: () => request<{ device_id: string; discovered_at: string; inventory: Record<string, unknown> }>('/api/v1/hardware'),
  twin: () => request<TwinSnapshot>('/api/v1/twin'),
  anomalies: (status?: 'active' | 'resolved', limit = 50) => request<Anomaly[]>(`/api/v1/anomalies${qs({ status, limit })}`),
  history: (keys: string[], minutes: number, bucketSeconds?: number, window?: { start: string; end: string }) =>
    request<HistoryResponse>(`/api/v1/telemetry/history${qs({ keys, minutes, bucket_seconds: bucketSeconds, start: window?.start, end: window?.end })}`),
  recent: (seconds: number) => request<RecentResponse>(`/api/v1/telemetry/recent${qs({ seconds })}`),
  processes: (sortBy: string, limit = 15) =>
    request<ProcessSnapshot & { sort_by: string }>(`/api/v1/system/processes${qs({ sort_by: sortBy, limit })}`),
  predictions: () => request<PredictionsResponse>('/api/v1/analytics/predictions'),
  thermal: (minutes: number, end?: string) => request<Record<string, unknown>>(`/api/v1/analytics/thermal${qs({ minutes, end })}`),
  scenarios: () => request<ScenarioInfo[]>('/api/v1/simulation/scenarios'),
  simulate: (body: Record<string, unknown>) =>
    request<SimulationResult>('/api/v1/simulation/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  systemInfo: () => request<SystemInfo>('/api/v1/system/info'),
  healthEvents: (limit = 200) => request<HealthEvent[]>(`/api/v1/health/events${qs({ limit })}`),
  performance: (minutes: number, end?: string) =>
    request<{ window_end?: string; metrics: Record<string, { samples: number; mean: number | null; max: number | null; min: number | null }> }>(
      `/api/v1/analytics/performance${qs({ minutes, end })}`,
    ),
  ready: async () => {
    const t0 = performance.now()
    const res = await fetch(`${BASE}/health/ready`)
    return { ok: res.ok, ms: performance.now() - t0, body: (await res.json()) as Record<string, unknown> }
  },
}

/** WebSocket URL with credentials as a query parameter (browsers cannot set WS headers). */
export function wsUrl(): string {
  const configured = import.meta.env.VITE_WS_URL as string | undefined
  const base = configured ?? `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/twin`
  const token = auth.jwt() ?? (auth.mode === 'api_key' ? auth.apiKey() : null)
  return token ? `${base}?token=${encodeURIComponent(token)}` : base
}
