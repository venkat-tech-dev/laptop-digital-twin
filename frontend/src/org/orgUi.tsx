import { useState, type ReactNode } from 'react'

import { Chip, type ChipTone } from '../ui/primitives'

export function Notice({ notice }: { notice: { ok: boolean; text: string } | null }) {
  if (!notice) return null
  return <p className="note" role="status" style={{ color: notice.ok ? 'var(--accent)' : 'var(--critical)' }}>{notice.text}</p>
}

const TONES: Record<string, ChipTone> = {
  ACTIVE: 'accent', COMPLIANT: 'accent', PASS: 'accent', SUCCESS: 'accent', PUBLISHED: 'accent', ONLINE: 'accent',
  PARTIALLY_COMPLIANT: 'amber', DISABLED: 'amber', QUARANTINED: 'amber', STALE: 'amber', DRAFT: 'amber', WARNING: 'amber', PENDING: 'amber', UNKNOWN: 'muted',
  NOT_SUPPORTED: 'muted', EXEMPT: 'muted', ARCHIVED: 'muted', RETIRED: 'muted', DECOMMISSIONED: 'muted', OFFLINE: 'muted', USED: 'muted', EXPIRED: 'muted',
  NON_COMPLIANT: 'critical', FAIL: 'critical', FAILURE: 'critical', DENIED: 'critical', REVOKED: 'critical', HIGH: 'critical', SUSPENDED: 'critical',
}

export function StatusChip({ value, title }: { value: string | null | undefined; title?: string }) {
  const v = value ?? 'UNKNOWN'
  return <Chip tone={TONES[v] ?? 'muted'} title={title}>{v.replace(/_/g, ' ')}</Chip>
}

/** Secret shown exactly once (enrollment / SCIM tokens): never stored by the UI. */
export function OneTimeSecret({ label, secret, onDismiss, children }: { label: string; secret: string; onDismiss: () => void; children?: ReactNode }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="panel" role="alert" style={{ borderColor: 'var(--amber)' }}>
      <p className="eyebrow">{label} · SHOWN ONCE</p>
      <p className="caption-mono" style={{ wordBreak: 'break-all', userSelect: 'all' }}>{secret}</p>
      {children}
      <div className="form-row">
        <button type="button" className="btn" onClick={() => void navigator.clipboard?.writeText(secret).then(() => setCopied(true))}>{copied ? 'Copied' : 'Copy'}</button>
        <button type="button" className="btn" onClick={onDismiss}>I have stored it</button>
      </div>
      <p className="note note--muted">Only a hash is kept on the server; it cannot be shown again.</p>
    </div>
  )
}

export function Counts({ data, empty = 'None' }: { data: Record<string, number> | undefined; empty?: string }) {
  const entries = Object.entries(data ?? {}).sort((a, b) => b[1] - a[1])
  if (!entries.length) return <span className="note note--muted">{empty}</span>
  return (
    <span className="form-row" style={{ flexWrap: 'wrap', gap: 6 }}>
      {entries.map(([k, v]) => <span key={k}><StatusChip value={k === 'null' || k === 'None' ? 'UNKNOWN' : k} /> <span className="caption-mono">{v}</span></span>)}
    </span>
  )
}
