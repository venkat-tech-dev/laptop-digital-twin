import type { RemediationStatus, Risk } from '../types/remediation'
import type { ChipTone } from '../ui/primitives'

export function riskTone(risk: Risk | string | null | undefined): ChipTone {
  return risk === 'LOW' ? 'accent' : risk === 'MEDIUM' ? 'amber' : risk ? 'critical' : 'muted'
}

export function statusTone(status: RemediationStatus | string): ChipTone {
  if (status === 'SUCCEEDED') return 'accent'
  if (['FAILED', 'ROLLBACK_FAILED'].includes(status)) return 'critical'
  if (['PENDING_APPROVAL', 'PARTIALLY_SUCCEEDED', 'EXECUTING', 'VALIDATING', 'VERIFYING'].includes(status)) return 'amber'
  return 'muted'
}

export function duration(fromIso: string | null | undefined, toIso: string | null | undefined): string {
  if (!fromIso || !toIso) return '—'
  const s = Math.max(0, (Date.parse(toIso) - Date.parse(fromIso)) / 1000)
  return s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`
}

export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`
}

export function rollbackText(rollback: string | undefined): string {
  if (rollback === 'NOT_NEEDED') return 'Nothing on the device changes, so there is nothing to undo.'
  if (rollback === 'NOT_AVAILABLE') return 'This action cannot be undone.'
  return rollback ? `Rollback: ${rollback}` : '—'
}

export const CHECK_TEXT: Record<string, string> = {
  agent_completed: 'Device executed the action',
  fresh_telemetry: 'Fresh telemetry received',
  application_running: 'Application running again',
  target_improved: 'Condition improved',
}
