import { useEffect, useState } from 'react'

import { orgApi, type AuditQuery } from '../services/orgApi'
import type { AuditEvent, AuditPage } from '../types/org'
import { Button, DataTable, Panel, PanelHeading } from '../ui/primitives'
import { Notice, StatusChip } from './orgUi'
import { errText, useAction, useCan, when } from './orgUtils'

const CATEGORIES = ['authentication', 'user', 'organization', 'device', 'policy', 'remediation', 'identity', 'data', 'security']

/** Searchable, tamper-evident audit trail (hash chain) with CSV / JSON export. Shared by org and platform views. */
export function AuditTable({ load, extraFilters }: { load: (q: AuditQuery & { org_id?: string }) => Promise<AuditPage>; extraFilters?: boolean }) {
  const [q, setQ] = useState<AuditQuery & { org_id?: string }>({ limit: 100 })
  const [items, setItems] = useState<AuditEvent[]>([])
  const [next, setNext] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<number | null>(null)

  const fetchPage = async (before?: number) => {
    setError(null)
    try {
      const page = await load({ ...q, before_id: before })
      setItems(before ? [...items, ...page.items] : page.items)
      setNext(page.next_before_id)
    } catch (e) {
      setError(errText(e))
    }
  }
  useEffect(() => {
    void fetchPage()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(q)])
  const set = (k: keyof typeof q, v: string) => setQ({ ...q, [k]: v || undefined })
  const sel = items.find((e) => e.id === open)

  return (
    <>
      <div className="form-row">
        {extraFilters ? <input className="form-input" placeholder="Organization id" value={q.org_id ?? ''} onChange={(e) => set('org_id', e.target.value)} aria-label="Organization filter" /> : null}
        <input className="form-input" placeholder="Action starts with (e.g. auth.)" value={q.action ?? ''} onChange={(e) => set('action', e.target.value)} aria-label="Action filter" />
        <select className="form-select" value={q.category ?? ''} onChange={(e) => set('category', e.target.value)} aria-label="Category filter">
          <option value="">Any category</option>
          {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        {!extraFilters ? (
          <>
            <input className="form-input" placeholder="Actor" value={q.actor ?? ''} onChange={(e) => set('actor', e.target.value)} aria-label="Actor filter" />
            <select className="form-select" value={q.result ?? ''} onChange={(e) => set('result', e.target.value)} aria-label="Result filter">
              <option value="">Any result</option>
              {['SUCCESS', 'FAILURE', 'DENIED'].map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </>
        ) : null}
      </div>
      {error ? <p className="note" style={{ color: 'var(--critical)' }}>{error}</p> : null}
      <DataTable rowKey={(e) => e.id} rows={items} empty="No events" selected={open} onSelect={(e) => setOpen(e.id === open ? null : e.id)}
        columns={[
          { key: 't', header: 'WHEN', width: 130, render: (e) => when(e.at) },
          ...(extraFilters ? [{ key: 'o', header: 'ORG', width: 90, kind: 'mono' as const, render: (e: AuditEvent) => e.org_id ?? 'platform' }] : []),
          { key: 'a', header: 'ACTION', width: 210, kind: 'primary', render: (e) => e.action },
          { key: 'u', header: 'ACTOR', width: 130, render: (e) => `${e.actor_id}${e.actor_type !== 'user' ? ` (${e.actor_type})` : ''}` },
          { key: 'r', header: 'RESOURCE', grow: true, kind: 'mono', render: (e) => (e.resource_type ? `${e.resource_type}:${e.resource_id ?? ''}` : '—') },
          { key: 's', header: 'RESULT', width: 100, render: (e) => <StatusChip value={e.result} title={e.reason ?? undefined} /> },
          { key: 'v', header: 'SEV', width: 90, render: (e) => (e.severity === 'INFO' ? 'info' : <StatusChip value={e.severity} />) },
        ]} />
      {sel ? (
        <pre className="caption-mono" style={{ whiteSpace: 'pre-wrap', maxHeight: 260, overflow: 'auto' }}>
          {JSON.stringify({ ...sel, hash: `${sel.hash.slice(0, 16)}…` }, null, 2)}
        </pre>
      ) : null}
      {next ? <Button onClick={() => void fetchPage(next)}>Load older events</Button> : null}
    </>
  )
}

export function OrgAudit() {
  const can = useCan()
  const { notice, run } = useAction()
  return (
    <Panel>
      <PanelHeading eyebrow="GOVERNANCE" title="Audit trail" right={
        <span className="form-row">
          <Button onClick={() => void run('Hash chain verified: no audit record was changed or removed.', async () => {
            const r = await orgApi.verifyAudit()
            if (!r.ok) throw new Error(`Hash chain broken at row ${r.first_bad} of ${r.rows}: the audit table was modified outside the application.`)
          })}>Verify integrity</Button>
          {can('audit.export') ? (
            <>
              <Button icon="download" onClick={() => void run('CSV export downloaded (audited).', () => orgApi.exportAudit('csv', {}))}>CSV</Button>
              <Button icon="download" onClick={() => void run('JSON export downloaded (audited).', () => orgApi.exportAudit('json', {}))}>JSON</Button>
            </>
          ) : null}
        </span>
      } />
      <Notice notice={notice} />
      <AuditTable load={(q) => orgApi.audit(q)} />
      <p className="note note--muted">Append-only and hash-chained; the database refuses updates and deletes of audit rows. Secrets are never recorded. Exports are limited to 50,000 rows and are themselves audited.</p>
    </Panel>
  )
}
