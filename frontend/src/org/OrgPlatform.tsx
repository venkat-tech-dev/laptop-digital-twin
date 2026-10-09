import { useState } from 'react'

import { useApi } from '../hooks/useApi'
import { orgApi } from '../services/orgApi'
import type { Organization } from '../types/org'
import { Button, DataTable, Panel, PanelHeading } from '../ui/primitives'
import { AuditTable } from './OrgAudit'
import { Notice, StatusChip } from './orgUi'
import { useAction, when } from './orgUtils'

/** Platform administrators: organizations, their status and quotas, and the cross-organization security audit. */
export function OrgPlatform() {
  const orgs = useApi(() => orgApi.organizations(), [])
  const { notice, run } = useAction()
  const [form, setForm] = useState({ org_id: '', name: '', owner: '' })
  const [selected, setSelected] = useState<string | null>(null)
  const sel = orgs.data?.items.find((o) => o.org_id === selected) ?? null
  return (
    <>
      <Panel>
        <PanelHeading eyebrow="PLATFORM" title="Organizations" right={<p className="panel-meta">{orgs.data ? `${orgs.data.items.length} organizations` : 'Loading…'}</p>} />
        <DataTable rowKey={(o) => o.org_id} rows={orgs.data?.items ?? []} empty={orgs.error ?? 'No organizations'} selected={selected} onSelect={(o) => setSelected(o.org_id)}
          columns={[
            { key: 'i', header: 'ID', width: 130, kind: 'mono', render: (o) => o.org_id },
            { key: 'n', header: 'NAME', grow: true, kind: 'primary', render: (o) => o.name },
            { key: 'd', header: 'DEVICES', width: 80, kind: 'mono', render: (o) => `${o.devices ?? 0}/${o.quotas.max_devices?.limit ?? '—'}` },
            { key: 'm', header: 'MEMBERS', width: 80, kind: 'mono', render: (o) => `${o.members ?? 0}/${o.quotas.max_users?.limit ?? '—'}` },
            { key: 's', header: 'STATUS', width: 100, render: (o) => <StatusChip value={o.status} /> },
            { key: 'c', header: 'CREATED', width: 120, render: (o) => when(o.created_at) },
          ]} />
        <form className="form-row" onSubmit={(e) => {
          e.preventDefault()
          void run(`Organization ${form.org_id} created.`, () => orgApi.createOrganization(form.org_id.trim(), form.name.trim(), form.owner.trim()), orgs.reload)
            .then((ok) => ok && setForm({ org_id: '', name: '', owner: '' }))
        }}>
          <input className="form-input" placeholder="Id (lowercase, e.g. acme)" value={form.org_id} onChange={(e) => setForm({ ...form, org_id: e.target.value.toLowerCase() })} aria-label="Organization id" />
          <input className="form-input" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} aria-label="Organization name" />
          <input className="form-input" placeholder="Owner (existing account, optional)" value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} aria-label="Owner" />
          <Button type="submit" disabled={form.org_id.trim().length < 2 || !form.name.trim()}>Create</Button>
        </form>
        <Notice notice={notice} />
      </Panel>
      {sel ? <OrgQuotas key={sel.org_id} org={sel} onSaved={orgs.reload} /> : null}
      <Panel>
        <PanelHeading eyebrow="PLATFORM" title="Security audit (all organizations)" right={<p className="panel-meta">Includes cross-organization probes, which tenants never see</p>} />
        <AuditTable load={(q) => orgApi.platformAudit(q)} extraFilters />
      </Panel>
    </>
  )
}

function OrgQuotas({ org, onSaved }: { org: Organization; onSaved: () => void }) {
  const { notice, run } = useAction()
  const [quotas, setQuotas] = useState(() => Object.fromEntries(Object.entries(org.quotas).map(([k, q]) => [k, { limit: q.limit, mode: q.mode }])))
  const isDefault = org.org_id === 'default'
  return (
    <Panel>
      <PanelHeading eyebrow={org.org_id} title={`${org.name}: status and quotas`} right={!isDefault ? (
        <span className="form-row">
          {org.status === 'ACTIVE' ? (
            <Button className="btn--danger" onClick={() => { if (confirm(`Suspend ${org.name}? All its members are signed out and its devices stop being accepted.`)) void run('Organization suspended.', () => orgApi.patchOrganization(org.org_id, { status: 'SUSPENDED' }), onSaved) }}>Suspend</Button>
          ) : (
            <Button onClick={() => void run('Organization re-activated.', () => orgApi.patchOrganization(org.org_id, { status: 'ACTIVE' }), onSaved)}>Re-activate</Button>
          )}
        </span>
      ) : null} />
      <DataTable rowKey={([k]) => k} rows={Object.entries(quotas)} empty="No quotas"
        columns={[
          { key: 'n', header: 'QUOTA', grow: true, kind: 'primary', render: ([k]) => k.replace(/_/g, ' ') },
          { key: 'l', header: 'LIMIT', width: 120, render: ([k, q]) => (
            <input className="form-input" type="number" min={0} value={q.limit} aria-label={`${k} limit`}
              onChange={(e) => setQuotas({ ...quotas, [k]: { ...q, limit: Number(e.target.value) } })} />
          ) },
          { key: 'm', header: 'WHEN EXCEEDED', width: 140, render: ([k, q]) => (
            <select className="form-select" value={q.mode} aria-label={`${k} mode`} onChange={(e) => setQuotas({ ...quotas, [k]: { ...q, mode: e.target.value as 'THROTTLE' | 'REJECT' } })}>
              <option value="THROTTLE">Throttle (429)</option>
              <option value="REJECT">Reject</option>
            </select>
          ) },
        ]} />
      <Button onClick={() => void run('Quotas saved.', () => orgApi.patchOrganization(org.org_id, { quotas }), onSaved)}>Save quotas</Button>
      <Notice notice={notice} />
    </Panel>
  )
}
