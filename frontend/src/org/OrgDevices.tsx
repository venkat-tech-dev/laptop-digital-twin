import { useState } from 'react'

import { navigate } from '../app/routes'
import { useApi } from '../hooks/useApi'
import { deviceScope } from '../services/deviceScope'
import { orgApi } from '../services/orgApi'
import type { EnrollmentToken, FleetDevice } from '../types/org'
import { Button, DataTable, Panel, PanelHeading, Spec } from '../ui/primitives'
import { Notice, OneTimeSecret, StatusChip } from './orgUi'
import { useAction, useCan, when } from './orgUtils'

const LIFECYCLES = ['ACTIVE', 'STALE', 'DISABLED', 'QUARANTINED', 'REVOKED', 'RETIRED', 'DECOMMISSIONED']
const COMPLIANCE = ['COMPLIANT', 'PARTIALLY_COMPLIANT', 'NON_COMPLIANT', 'UNKNOWN', 'EXEMPT']

/** What each lifecycle action means, shown before it is taken. */
const TRANSITIONS: { to: string; label: string; needs: string; from: string[]; explain: string }[] = [
  { to: 'ACTIVE', label: 'Re-activate', needs: 'device.manage', from: ['DISABLED', 'QUARANTINED'], explain: 'Telemetry and actions are accepted again.' },
  { to: 'DISABLED', label: 'Disable', needs: 'device.manage', from: ['ACTIVE', 'QUARANTINED'], explain: 'Telemetry from the device is refused until re-activated. Its credential is kept.' },
  { to: 'QUARANTINED', label: 'Quarantine', needs: 'device.manage', from: ['ACTIVE', 'DISABLED'], explain: 'Telemetry keeps flowing for investigation; no remediation action can run on it.' },
  { to: 'REVOKED', label: 'Revoke', needs: 'device.remove', from: ['ACTIVE', 'DISABLED', 'QUARANTINED'], explain: 'The credential stops working immediately. The device must be enrolled again with a new token.' },
  { to: 'RETIRED', label: 'Retire', needs: 'device.remove', from: ['ACTIVE', 'DISABLED', 'QUARANTINED', 'REVOKED'], explain: 'The device leaves service; its credential is revoked. History is kept until data deletion.' },
]

export function OrgDevices() {
  const can = useCan()
  const [filters, setFilters] = useState<{ lifecycle?: string; compliance?: string; group_id?: string; os?: string }>({})
  const fleet = useApi(() => orgApi.devices(filters), [JSON.stringify(filters)], 15_000)
  const groups = useApi(() => (can('group.view') ? orgApi.groups() : Promise.resolve({ items: [] })), [can])
  const [selected, setSelected] = useState<string | null>(null)
  const sel = fleet.data?.items.find((d) => d.device_id === selected) ?? null
  const set = (k: keyof typeof filters, v: string) => setFilters({ ...filters, [k]: v || undefined })

  return (
    <>
      <Panel>
        <PanelHeading eyebrow="FLEET" title="Devices" right={<p className="panel-meta">{fleet.data ? `${fleet.data.count} shown` : 'Loading…'}</p>} />
        <div className="form-row">
          <select className="form-select" value={filters.lifecycle ?? ''} onChange={(e) => set('lifecycle', e.target.value)} aria-label="Lifecycle filter">
            <option value="">Any lifecycle</option>
            {LIFECYCLES.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
          <select className="form-select" value={filters.compliance ?? ''} onChange={(e) => set('compliance', e.target.value)} aria-label="Compliance filter">
            <option value="">Any compliance</option>
            {COMPLIANCE.map((c) => <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>)}
          </select>
          <select className="form-select" value={filters.group_id ?? ''} onChange={(e) => set('group_id', e.target.value)} aria-label="Group filter">
            <option value="">Any group</option>
            {(groups.data?.items ?? []).map((g) => <option key={g.group_id} value={g.group_id}>{g.name}</option>)}
          </select>
          <input className="form-input" placeholder="Operating system contains…" value={filters.os ?? ''} onChange={(e) => set('os', e.target.value)} aria-label="OS filter" />
        </div>
        <DataTable rowKey={(d) => d.device_id} rows={fleet.data?.items ?? []} empty={fleet.error ?? 'No devices match'} selected={selected} onSelect={(d) => setSelected(d.device_id)}
          columns={[
            { key: 'd', header: 'DEVICE', width: 170, kind: 'primary', render: (d) => d.device_id },
            { key: 'm', header: 'MODEL', width: 140, render: (d) => d.model ?? '—' },
            { key: 'l', header: 'LIFECYCLE', width: 110, render: (d) => <StatusChip value={d.lifecycle_display} /> },
            { key: 'p', header: 'LINK', width: 90, render: (d) => <StatusChip value={d.presence} /> },
            { key: 'c', header: 'COMPLIANCE', width: 150, render: (d) => <StatusChip value={d.compliance} title={d.compliance_reasons.join('; ')} /> },
            { key: 'v', header: 'AGENT', width: 70, kind: 'mono', render: (d) => d.agent_version ?? '—' },
            { key: 'g', header: 'GROUPS', grow: true, render: (d) => d.group_names.join(', ') || '—' },
            { key: 'e', header: 'ENROLLED', width: 90, render: (d) => d.enrollment ?? '—', cellKind: (d) => (d.enrollment === 'legacy key' ? 'amber' : 'mono') },
          ]} />
      </Panel>
      {sel ? <DeviceDetail key={sel.device_id} device={sel} onChange={fleet.reload} /> : null}
      {can('device.enroll') ? <EnrollmentTokens groups={groups.data?.items ?? []} /> : null}
    </>
  )
}

function DeviceDetail({ device, onChange }: { device: FleetDevice; onChange: () => void }) {
  const can = useCan()
  const compliance = useApi(() => orgApi.compliance(device.device_id), [device.device_id, device.lifecycle])
  const { notice, run } = useAction()
  const [pending, setPending] = useState<(typeof TRANSITIONS)[number] | null>(null)
  const [reason, setReason] = useState('')
  const [confirmId, setConfirmId] = useState('')
  const [delReason, setDelReason] = useState('')
  const options = TRANSITIONS.filter((t) => t.from.includes(device.lifecycle) && can(t.needs))

  return (
    <div className="split split--even">
      <Panel>
        <PanelHeading eyebrow="DEVICE" title={device.device_id} right={
          <button type="button" className="link" onClick={() => { deviceScope.set(device.device_id); navigate('twin') }}>Open twin</button>
        } />
        <Spec label="Lifecycle" value={<StatusChip value={device.lifecycle} />} />
        <Spec label="Operating system" value={device.os ?? 'Not reported'} />
        <Spec label="Agent" value={device.agent_version ?? 'Not reported'} />
        <Spec label="Enrolled" value={`${when(device.enrolled_at)} · ${device.enrollment ?? '—'}`} />
        <Spec label="Last seen" value={when(device.last_seen)} />
        {options.length ? (
          <div className="form-row" style={{ flexWrap: 'wrap' }}>
            {options.map((t) => (
              <Button key={t.to} className={['REVOKED', 'RETIRED'].includes(t.to) ? 'btn--danger' : ''} onClick={() => { setPending(t); setReason('') }}>{t.label}</Button>
            ))}
          </div>
        ) : null}
        {pending ? (
          <form className="panel" onSubmit={(e) => {
            e.preventDefault()
            void run(`${device.device_id}: ${pending.to.toLowerCase()}.`, () => orgApi.lifecycle(device.device_id, pending.to, reason), onChange).then(() => setPending(null))
          }}>
            <p className="note"><strong>{pending.label}.</strong> {pending.explain}</p>
            <input className="form-input" placeholder="Reason (recorded in the audit trail)" value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Reason" />
            <div className="form-row">
              <Button type="submit" className={['REVOKED', 'RETIRED'].includes(pending.to) ? 'btn--danger' : ''}>Confirm {pending.label.toLowerCase()}</Button>
              <Button type="button" onClick={() => setPending(null)}>Cancel</Button>
            </div>
          </form>
        ) : null}
        {device.lifecycle === 'RETIRED' && can('device.remove') && can('retention.manage') ? (
          <form className="panel" onSubmit={(e) => {
            e.preventDefault()
            void run('Data deletion started. Audit and governance records are kept.', () => orgApi.deleteDeviceData(device.device_id, confirmId, delReason), onChange)
          }}>
            <p className="note"><strong>Delete this device's data.</strong> Telemetry, twin state, anomalies, predictions, alerts and diagnoses are deleted permanently. Audit records stay. Type the device id to confirm.</p>
            <input className="form-input" placeholder={device.device_id} value={confirmId} onChange={(e) => setConfirmId(e.target.value)} aria-label="Confirm device id" />
            <input className="form-input" placeholder="Reason (e.g. request reference)" value={delReason} onChange={(e) => setDelReason(e.target.value)} aria-label="Deletion reason" />
            <Button type="submit" className="btn--danger" disabled={confirmId !== device.device_id || delReason.trim().length < 3}>Delete data</Button>
          </form>
        ) : null}
        <Notice notice={notice} />
      </Panel>
      <Panel>
        <PanelHeading eyebrow="COMPLIANCE" title={compliance.data ? <StatusChip value={compliance.data.status} /> : 'Compliance'} />
        {compliance.data ? (
          <>
            {compliance.data.reasons.map((r) => <p key={r} className="note">{r}</p>)}
            <DataTable rowKey={(c) => c.check} rows={compliance.data.checks} empty="No checks"
              columns={[
                { key: 'c', header: 'CHECK', width: 170, kind: 'primary', render: (c) => c.check.replace(/_/g, ' ') },
                { key: 's', header: 'RESULT', width: 120, render: (c) => <StatusChip value={c.state} /> },
                { key: 'r', header: 'REQUIRED', width: 70, render: (c) => (c.required ? 'Yes' : 'No') },
                { key: 'd', header: 'DETAIL', grow: true, render: (c) => c.detail },
              ]} />
            <p className="note note--muted">Not reported means unknown, never pass. Checks the agent cannot measure are shown as not supported.</p>
          </>
        ) : <p className="note">{compliance.error ?? 'Loading…'}</p>}
      </Panel>
    </div>
  )
}

function EnrollmentTokens({ groups }: { groups: { group_id: string; name: string }[] }) {
  const tokens = useApi(() => orgApi.tokens(), [])
  const { notice, run } = useAction()
  const [form, setForm] = useState({ ttl: 24, label: '', group: '' })
  const [created, setCreated] = useState<EnrollmentToken | null>(null)
  const groupName = (id: string | null) => (id ? groups.find((g) => g.group_id === id)?.name ?? id : '—')
  return (
    <Panel>
      <PanelHeading eyebrow="ENROLLMENT" title="Enrollment tokens" right={<p className="panel-meta">Single-use, expiring; the device gets its own credential</p>} />
      {created?.token ? (
        <OneTimeSecret label="ENROLLMENT TOKEN" secret={created.token} onDismiss={() => setCreated(null)}>
          <p className="note">On the device, set <span className="caption-mono">AGENT_ENROLLMENT_TOKEN</span> to this value in the agent's .env and start the agent. It expires {when(created.expires_at)}.</p>
        </OneTimeSecret>
      ) : null}
      <form className="form-row" onSubmit={(e) => {
        e.preventDefault()
        void run('Token created.', async () => setCreated(await orgApi.createToken({ ttl_hours: form.ttl, max_uses: 1, group_id: form.group || null, label: form.label })), tokens.reload)
      }}>
        <input className="form-input" placeholder="Label (e.g. laptop asset tag)" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} aria-label="Token label" />
        <select className="form-select" value={form.ttl} onChange={(e) => setForm({ ...form, ttl: Number(e.target.value) })} aria-label="Valid for">
          {[1, 8, 24, 72, 168].map((h) => <option key={h} value={h}>Valid {h < 24 ? `${h} h` : `${h / 24} d`}</option>)}
        </select>
        <select className="form-select" value={form.group} onChange={(e) => setForm({ ...form, group: e.target.value })} aria-label="Join group">
          <option value="">No group</option>
          {groups.map((g) => <option key={g.group_id} value={g.group_id}>Join {g.name}</option>)}
        </select>
        <Button type="submit" icon="arrowRight">Create token</Button>
      </form>
      <DataTable rowKey={(t) => t.token_id} rows={tokens.data?.items ?? []} empty={tokens.error ?? 'No tokens'}
        columns={[
          { key: 'l', header: 'LABEL', grow: true, kind: 'primary', render: (t) => t.label || t.token_id.slice(0, 8) },
          { key: 's', header: 'STATUS', width: 100, render: (t) => <StatusChip value={t.status} /> },
          { key: 'u', header: 'USES', width: 60, kind: 'mono', render: (t) => `${t.uses}/${t.max_uses}` },
          { key: 'g', header: 'GROUP', width: 110, render: (t) => groupName(t.group_id) },
          { key: 'b', header: 'CREATED BY', width: 110, render: (t) => t.created_by },
          { key: 'e', header: 'EXPIRES', width: 130, render: (t) => when(t.expires_at) },
          { key: 'a', header: '', width: 70, render: (t) => (t.status === 'ACTIVE' ? (
            <button type="button" className="link" onClick={() => void run('Token revoked.', () => orgApi.revokeToken(t.token_id), tokens.reload)}>Revoke</button>
          ) : null) },
        ]} />
      <Notice notice={notice} />
    </Panel>
  )
}
