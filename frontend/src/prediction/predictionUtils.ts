import type { ChipTone } from '../ui/primitives'

/** Duration in the unit a person would use ("~9 days", "~27 min"). */
export function fmtDuration(s: number): string {
  if (!Number.isFinite(s)) return '—'
  if (s < 90) return `${Math.round(s)} s`
  if (s < 5400) return `${Math.round(s / 60)} min`
  if (s < 2 * 86400) return `${(s / 3600).toFixed(1)} h`
  return `${Math.round(s / 86400)} days`
}

export function severityTone(sev: string | null | undefined): ChipTone {
  if (sev === 'CRITICAL') return 'critical'
  if (sev === 'HIGH' || sev === 'MEDIUM') return 'amber'
  return 'muted'
}

export function healthTone(h: string): ChipTone {
  if (h === 'GOOD') return 'accent'
  if (h === 'FAIR') return 'amber'
  return 'muted'
}
