/** Phase-8 remediation types (mirror of app/domain/remediation and app/api/v1/remediation.py). */

export type RemediationStatus =
  | 'PROPOSED' | 'PENDING_APPROVAL' | 'APPROVED' | 'REJECTED' | 'QUEUED' | 'VALIDATING' | 'EXECUTING' | 'VERIFYING'
  | 'SUCCEEDED' | 'PARTIALLY_SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'EXPIRED' | 'ROLLED_BACK' | 'ROLLBACK_FAILED'
export type Risk = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'

export interface AuditEntry { at: string; actor: string; action: string; from_status: string | null; to_status: string | null; detail: Record<string, unknown> }
export interface Check { check: string; ok?: boolean; state?: 'PASS' | 'FAIL' | 'PENDING'; detail: string; kind?: string; hard?: boolean }

export interface Remediation {
  id: string
  remediation_id: string
  device_id: string
  alert_id: string | null
  diagnosis_id: string | null
  action_type: string
  action_name: string
  description: string
  risk_level: Risk
  requires_approval: boolean
  approval_policy: string
  parameters: Record<string, string>
  status: RemediationStatus
  requested_by: string
  approved_by: string | null
  approval_at: string | null
  approval_expires_at: string | null
  rejected_by: string | null
  created_at: string
  updated_at: string
  scheduled_at: string | null
  mode: 'IMMEDIATE' | 'SCHEDULED' | 'MAINTENANCE_WINDOW'
  started_at: string | null
  completed_at: string | null
  failed_at: string | null
  result: string | null
  failure_reason: string | null
  correlation_id: string
  execution_id: string
  reason: string
  diagnosis_confidence: number | null
  action_confidence: number | null
  expected_success_probability: number | null
  recommendation_source: 'rules' | 'ai' | 'user'
  rollback_strategy: string
  dry_run: boolean
  action?: { risk_level: Risk; impact: string; rollback: string; reversible: boolean; changes_state: boolean; estimated_duration_s: number; timeouts_s: { validation: number; execution: number; verification: number } }
  allowed?: { approve: boolean; reject: boolean; cancel: boolean }
  // full view only
  preconditions?: string[]
  verification_rules?: { checks: string[]; timeout_s: number; target_signal: string | null; target_below: number | null; description: string; application?: { name: string; executables: string[]; impact: string } }
  evidence?: { evidence_id: string; statement: string }[]
  execution?: { dispatched_at?: string; picked_up_at?: string | null; issues?: number; envelope_digest?: string; waiting_on?: string; preconditions?: Check[]; report?: { phase: string; detail: string; at: string; data: Record<string, unknown> } }
  verification?: { baseline?: number | null; after?: number | null; outcome?: string; checks?: Check[] }
  audit?: AuditEntry[]
}

export interface CatalogAction {
  action_id: string
  name: string
  description: string
  risk_level: Risk
  changes_state: boolean
  impact: string
  rollback: string
  reversible: boolean
  enabled: boolean
  disabled_reason: string | null
  estimated_duration_s: number
  auto_eligible: boolean
  cooldown_s: number
  max_per_day: number
  parameter_schema: { properties?: Record<string, unknown> }
  verification: { description: string }
}

export interface DryRunPlan {
  dry_run: true
  expected_effect: string
  estimated_duration_s: number
  preconditions: Check[]
  would_execute_now: boolean
  rollback: string
  note: string
}

export const STATUS_TEXT: Record<RemediationStatus, string> = {
  PROPOSED: 'Proposed', PENDING_APPROVAL: 'Pending approval', APPROVED: 'Approved', REJECTED: 'Rejected', QUEUED: 'Queued',
  VALIDATING: 'Sent to device', EXECUTING: 'Executing', VERIFYING: 'Verifying', SUCCEEDED: 'Succeeded',
  PARTIALLY_SUCCEEDED: 'Partially succeeded', FAILED: 'Failed', CANCELLED: 'Cancelled', EXPIRED: 'Expired',
  ROLLED_BACK: 'Rolled back', ROLLBACK_FAILED: 'Rollback failed',
}

export const CENTER_TABS: { id: string; label: string; statuses: RemediationStatus[] }[] = [
  { id: 'pending', label: 'Pending approval', statuses: ['PROPOSED', 'PENDING_APPROVAL'] },
  { id: 'scheduled', label: 'Scheduled', statuses: ['APPROVED', 'QUEUED'] },
  { id: 'running', label: 'Running', statuses: ['VALIDATING', 'EXECUTING', 'VERIFYING'] },
  { id: 'completed', label: 'Completed', statuses: ['SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'ROLLED_BACK'] },
  { id: 'failed', label: 'Failed', statuses: ['FAILED', 'ROLLBACK_FAILED'] },
  { id: 'cancelled', label: 'Cancelled', statuses: ['CANCELLED', 'REJECTED', 'EXPIRED'] },
]
