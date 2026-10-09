/**
 * Phase 9 organization administration API. The organization is always the one of the signed-in session
 * (switch with /auth/switch-organization); nothing here sends an organization id the server would trust.
 */
import type {
  AuditPage,
  ComplianceResult,
  DeviceGroup,
  EffectivePolicy,
  EnrollmentToken,
  FleetDevice,
  IdentityProvider,
  Member,
  OrgCurrent,
  OrgDashboard,
  OrgUnit,
  Organization,
  Policy,
  PolicySchema,
  PolicyValidation,
  ScimToken,
  SecurityDashboard,
  UsageDashboard,
  UserSession,
} from '../types/org'
import { download, qs, request, send } from './api'

const enc = encodeURIComponent

export interface AuditQuery {
  actor?: string
  action?: string
  category?: string
  result?: string
  severity?: string
  since?: string
  until?: string
  limit?: number
  before_id?: number
}

export const orgApi = {
  current: () => request<OrgCurrent>('/api/v1/org'),
  rename: (name: string) => send<Organization>('/api/v1/org', 'PATCH', { name }),
  // structure
  units: () => request<{ items: OrgUnit[] }>('/api/v1/org/units'),
  createUnit: (kind: OrgUnit['kind'], name: string, parentId: string | null) =>
    send<OrgUnit>('/api/v1/org/units', 'POST', { kind, name, parent_id: parentId }),
  archiveUnit: (id: string) => send<OrgUnit>(`/api/v1/org/units/${enc(id)}/archive`, 'POST'),
  groups: () => request<{ items: DeviceGroup[] }>('/api/v1/org/groups'),
  saveGroup: (g: { name: string; kind: string; unit_id: string | null; priority: number; tags: string[]; status: string }, id?: string) =>
    id ? send<DeviceGroup>(`/api/v1/org/groups/${enc(id)}`, 'PATCH', g) : send<DeviceGroup>('/api/v1/org/groups', 'POST', g),
  groupMembers: (id: string, add: string[], remove: string[]) =>
    send<DeviceGroup>(`/api/v1/org/groups/${enc(id)}/members`, 'POST', { add, remove }),
  // members
  members: () => request<{ items: Member[] }>('/api/v1/org/members'),
  addMember: (m: { username: string; password?: string; role: string; group_scope: string[] }) =>
    send<Member>('/api/v1/org/members', 'POST', { ...m, password: m.password || undefined }),
  updateMember: (username: string, m: { role: string; group_scope: string[]; status: 'ACTIVE' | 'DISABLED' }) =>
    send<Member>(`/api/v1/org/members/${enc(username)}`, 'PUT', m),
  revokeMemberSessions: (username: string) => send<{ revoked: number }>(`/api/v1/org/members/${enc(username)}/revoke-sessions`, 'POST'),
  // devices
  devices: (f: { group_id?: string; unit_id?: string; lifecycle?: string; compliance?: string; health?: string; agent_version?: string; os?: string } = {}) =>
    request<{ items: FleetDevice[]; count: number }>(`/api/v1/org/devices${qs(f)}`),
  compliance: (deviceId: string) => request<ComplianceResult>(`/api/v1/org/devices/${enc(deviceId)}/compliance`),
  lifecycle: (deviceId: string, to: string, reason?: string) =>
    send<Record<string, unknown>>(`/api/v1/org/devices/${enc(deviceId)}/lifecycle`, 'POST', { to, reason: reason || null }),
  deleteDeviceData: (deviceId: string, confirm: string, reason: string) =>
    send<{ job_id: string; status: string }>(`/api/v1/org/devices/${enc(deviceId)}/data-deletion`, 'POST', { confirm_device_id: confirm, reason }),
  tokens: () => request<{ items: EnrollmentToken[] }>('/api/v1/org/enrollment-tokens'),
  createToken: (t: { ttl_hours: number; max_uses: number; group_id: string | null; label: string }) =>
    send<EnrollmentToken>('/api/v1/org/enrollment-tokens', 'POST', t),
  revokeToken: (id: string) => send<EnrollmentToken>(`/api/v1/org/enrollment-tokens/${enc(id)}/revoke`, 'POST'),
  // policies
  policySchema: () => request<PolicySchema>('/api/v1/org/policies/schema'),
  policies: (kind?: string) => request<{ items: Policy[] }>(`/api/v1/org/policies${qs({ kind })}`),
  saveDraft: (p: { kind: string; scope_type: string; scope_id?: string | null; body: Record<string, unknown>; locked: string[]; note: string }) =>
    send<Policy>('/api/v1/org/policies', 'POST', p),
  validatePolicy: (id: string, version?: number) => send<PolicyValidation>(`/api/v1/org/policies/${enc(id)}/validate${qs({ version })}`, 'POST'),
  publishPolicy: (id: string, version: number) => send<Policy>(`/api/v1/org/policies/${enc(id)}/publish`, 'POST', { version }),
  rollbackPolicy: (id: string, toVersion: number) => send<Policy>(`/api/v1/org/policies/${enc(id)}/rollback`, 'POST', { to_version: toVersion }),
  archivePolicy: (id: string) => send<Policy>(`/api/v1/org/policies/${enc(id)}/archive`, 'POST'),
  effective: (kind: string, deviceId?: string) => request<EffectivePolicy>(`/api/v1/org/policies/effective${qs({ kind, device_id: deviceId })}`),
  // audit
  audit: (q: AuditQuery = {}) => request<AuditPage>(`/api/v1/org/audit${qs({ ...q })}`),
  exportAudit: (fmt: 'csv' | 'json', q: { action?: string; category?: string; since?: string; until?: string }) =>
    download(`/api/v1/org/audit/export${qs({ fmt, ...q })}`, `audit.${fmt}`),
  verifyAudit: () => request<{ ok: boolean; rows: number; first_bad: number | null }>('/api/v1/org/audit/verify'),
  // identity
  providers: () => request<{ items: IdentityProvider[]; mfa_available: boolean; redirect_uri_hint: string }>('/api/v1/org/identity-providers'),
  addProvider: (p: { kind: 'oidc' | 'saml'; name: string; config: Record<string, unknown> }) =>
    send<IdentityProvider>('/api/v1/org/identity-providers', 'POST', p),
  providerStatus: (id: string, status: 'ACTIVE' | 'DISABLED') =>
    send<IdentityProvider>(`/api/v1/org/identity-providers/${enc(id)}/status`, 'POST', { status }),
  scimTokens: () => request<{ items: ScimToken[] }>('/api/v1/org/scim-tokens'),
  createScimToken: (label: string) => send<{ token: string; token_id: string; label: string; note: string }>('/api/v1/org/scim-tokens', 'POST', { label }),
  revokeScimToken: (id: string) => send<{ ok: boolean }>(`/api/v1/org/scim-tokens/${enc(id)}/revoke`, 'POST'),
  // my account
  sessions: () => request<{ items: UserSession[] }>('/api/v1/auth/sessions'),
  revokeSession: (id: string) => send<{ ok: boolean }>(`/api/v1/auth/sessions/${enc(id)}/revoke`, 'POST'),
  totpSetup: () => send<{ secret: string; otpauth_uri: string; note: string }>('/api/v1/auth/mfa/totp/setup', 'POST'),
  totpActivate: (code: string) => send<{ ok: boolean; sign_in_again: boolean }>('/api/v1/auth/mfa/totp/activate', 'POST', { code }),
  // dashboards
  dashboard: () => request<OrgDashboard>('/api/v1/org/dashboard'),
  security: () => request<SecurityDashboard>('/api/v1/org/security'),
  usage: () => request<UsageDashboard>('/api/v1/org/usage'),
  // platform administrators
  organizations: () => request<{ items: Organization[]; quota_names: string[]; permissions: string[] }>('/api/v1/platform/organizations'),
  createOrganization: (org_id: string, name: string, owner?: string) =>
    send<Organization>('/api/v1/platform/organizations', 'POST', { org_id, name, owner: owner || undefined }),
  patchOrganization: (orgId: string, changes: { name?: string; status?: string; quotas?: Record<string, { limit: number; mode: string }> }) =>
    send<Organization>(`/api/v1/platform/organizations/${enc(orgId)}`, 'PATCH', changes),
  platformAudit: (q: { org_id?: string; action?: string; category?: string; limit?: number; before_id?: number } = {}) =>
    request<AuditPage>(`/api/v1/platform/audit${qs({ ...q })}`),
}
