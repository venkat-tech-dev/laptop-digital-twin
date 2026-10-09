/** Phase 9: organizations, members, devices, policies, audit and identity (shapes of /api/v1/org/*). */

export interface OrgRef {
  org_id: string
  name: string
  role: string
  role_label: string
}

export interface QuotaSetting {
  limit: number
  mode: 'THROTTLE' | 'REJECT'
  kind?: string
}

export interface Organization {
  org_id: string
  name: string
  status: 'ACTIVE' | 'SUSPENDED' | 'ARCHIVED'
  created_at: string
  quotas: Record<string, QuotaSetting>
  devices?: number
  members?: number
}

export interface RoleInfo {
  role: string
  label: string
  permissions: string[]
}

export interface OrgCurrent {
  organization: Organization
  role: string | null
  platform_admin: boolean
  permissions: string[]
  roles: RoleInfo[]
}

export interface OrgUnit {
  unit_id: string
  org_id: string
  kind: 'business_unit' | 'department' | 'team'
  name: string
  parent_id: string | null
  status: string
}

export interface DeviceGroup {
  group_id: string
  org_id: string
  name: string
  kind: string
  unit_id: string | null
  priority: number
  tags: string[]
  status: string
  devices?: string[]
  device_count?: number
}

export interface Member {
  org_id: string
  username: string
  role: string
  role_label?: string
  status: 'ACTIVE' | 'DISABLED'
  group_scope: string[]
  source: string
  created_at: string | null
  last_login_at?: string | null
  mfa_enrolled?: boolean
}

export type Lifecycle = 'PENDING' | 'ACTIVE' | 'DISABLED' | 'QUARANTINED' | 'REVOKED' | 'RETIRED' | 'DECOMMISSIONED'
export type ComplianceStatus = 'COMPLIANT' | 'PARTIALLY_COMPLIANT' | 'NON_COMPLIANT' | 'UNKNOWN' | 'EXEMPT'

export interface FleetDevice {
  device_id: string
  lifecycle: Lifecycle | 'UNKNOWN'
  lifecycle_display: string
  presence: string
  health: string | null
  model: string | null
  os: string | null
  agent_version: string | null
  groups: string[]
  group_names: string[]
  enrolled_at: string | null
  enrollment: 'legacy key' | 'token' | null
  compliance: ComplianceStatus
  compliance_reasons: string[]
  security_posture: string | null
  last_seen: string | null
}

export interface ComplianceCheck {
  check: string
  state: 'PASS' | 'FAIL' | 'UNKNOWN' | 'NOT_SUPPORTED'
  required: boolean
  detail: string
}

export interface ComplianceResult {
  device_id?: string
  status: ComplianceStatus
  reasons: string[]
  checks: ComplianceCheck[]
  evaluated_at?: string
}

export interface EnrollmentToken {
  token_id: string
  org_id: string
  created_by: string
  created_at: string
  expires_at: string
  max_uses: number
  uses: number
  group_id: string | null
  label: string
  status: string
  revoked_at: string | null
  token?: string
  note?: string
}

export interface PolicyField {
  type: string
  default: unknown
  min: number | null
  max: number | null
  choices: string[]
  doc: string
}

export interface PolicySchema {
  kinds: Record<string, Record<string, PolicyField>>
  scopes: string[]
}

export interface Policy {
  policy_id: string
  org_id: string
  scope_type: string
  scope_id: string
  kind: string
  version: number
  status: 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
  body: Record<string, unknown>
  locked: string[]
  created_by: string | null
  created_at: string | null
  updated_by: string | null
  updated_at: string | null
  effective_from: string | null
  effective_until: string | null
  note: string
}

export interface PolicyValidation {
  policy: Policy
  errors: string[]
  warnings: string[]
  preview: {
    affected_devices: number
    sample_device: string | null
    changes: Record<string, { from: unknown; to: unknown }>
    devices_on_blocked_versions?: number
  }
}

export interface EffectivePolicy {
  kind: string
  device_id: string | null
  values: Record<string, unknown>
  source: Record<string, string>
}

export interface AuditEvent {
  id: number
  event_id: string
  at: string
  org_id: string | null
  actor_id: string
  actor_type: string
  action: string
  category: string
  resource_type: string | null
  resource_id: string | null
  result: 'SUCCESS' | 'FAILURE' | 'DENIED'
  reason: string | null
  severity: 'INFO' | 'WARNING' | 'HIGH'
  source: string
  request_id: string | null
  ip: string | null
  metadata: Record<string, unknown>
  hash: string
}

export interface AuditPage {
  items: AuditEvent[]
  next_before_id: number | null
}

export interface IdentityProvider {
  provider_id: string
  org_id: string
  kind: 'oidc' | 'saml'
  name: string
  status: 'ACTIVE' | 'DISABLED'
  config: Record<string, unknown>
  created_by: string
  created_at: string
}

export interface ScimToken {
  token_id: string
  label: string
  created_by: string
  created_at: string
  revoked_at: string | null
}

export interface UserSession {
  session_id: string
  username: string
  org_id: string
  created_at: string | null
  expires_at: string | null
  last_seen_at: string | null
  revoked_at: string | null
  revoked_reason: string | null
  auth_method: string
  mfa: boolean
  ip: string | null
  user_agent: string | null
  current: boolean
}

export interface OrgDashboard {
  devices_total: number
  by_presence: Record<string, number>
  by_health: Record<string, number>
  by_lifecycle: Record<string, number>
  by_compliance: Record<string, number>
  by_agent_version: Record<string, number>
  by_security_posture: Record<string, number>
  active_alerts: number
  alerts_by_severity: Record<string, number>
  active_predictions: number
  remediation_by_status: Record<string, number>
  generated_at: string
}

export interface SecurityDashboard {
  window_hours: number
  authentication_failures: number
  authorization_denials: number
  cross_tenant_attempts: number
  failed_enrollments: number
  audit_events: number
  members: number
  mfa_adoption: { with_mfa: number; members: number }
  inactive_users_30d: number
  devices_legacy_enrolled: number
  devices_outdated_agent: number
  devices_non_compliant: number
  devices_revoked: number
  security_alerts_open: number
  measured: string
}

export interface QuotaUsage {
  used: number
  limit: number
  mode: 'THROTTLE' | 'REJECT'
  rejected_total?: number
}

export interface UsageDashboard {
  users: number
  devices: number
  telemetry_batches_since_start: number
  api_requests_since_start: number
  websocket_connections: number
  diagnoses_in_memory: number | null
  remediations_recent: number
  storage: string
  quotas: Record<string, QuotaUsage>
  since: string
}
