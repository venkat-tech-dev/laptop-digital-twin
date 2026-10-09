import type { AlertSeverity } from '../types/alerting'

const ICON: Record<string, string> = { CRITICAL: '■', HIGH: '▲', MEDIUM: '●', LOW: '○', INFO: '·' }

/** Severity never by colour alone: a glyph and the word as well. */
export function SeverityBadge({ severity }: { severity: AlertSeverity | string }) {
  return (
    <span className={`sev-badge sev-badge--${severity.toLowerCase()}`}>
      <span aria-hidden>{ICON[severity] ?? '·'}</span> {severity}
    </span>
  )
}
