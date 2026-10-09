import { useState } from 'react'

import { navigate } from '../app/routes'
import { useApi } from '../hooks/useApi'
import { RemediationDetail, RemediationList } from '../remediation/RemediationViews'
import { riskTone } from '../remediation/remediationFormat'
import { api } from '../services/api'
import { useRemediationEvents } from '../stores/remediationStore'
import { useSession } from '../stores/sessionStore'
import { CENTER_TABS } from '../types/remediation'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, Panel, PanelHeading } from '../ui/primitives'

const PAGE = 25

function Center({ onSelect }: { onSelect: (id: string) => void }) {
  const [tab, setTab] = useState('pending')
  const [device, setDevice] = useState('')
  const [risk, setRisk] = useState('')
  const [action, setAction] = useState('')
  const [by, setBy] = useState('')
  const [since, setSince] = useState('')
  const revision = useRemediationEvents((s) => s.revision)
  const statuses = CENTER_TABS.find((t) => t.id === tab)?.statuses ?? []
  const list = useApi(() => api.remediations({ status: statuses, device_id: device || undefined, risk: risk || undefined,
    action_type: action || undefined, requested_by: by || undefined, since: since ? new Date(since).toISOString() : undefined, limit: PAGE }),
  [tab, device, risk, action, by, since, revision], 20_000)
  const items = list.data?.items ?? []
  return (
    <Panel className="side-panel" style={{ flex: '424 0 0' }}>
      <PanelHeading title="Remediation Center" right={<p className="panel-meta">{items.length}{items.length === PAGE ? '+' : ''}</p>} />
      <div className="seg" role="tablist" aria-label="Remediation status">
        {CENTER_TABS.map((t) => (
          <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} className={`seg__btn ${tab === t.id ? 'is-on' : ''}`} onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </div>
      <div className="anomaly-filters">
        <input aria-label="Device" placeholder="Device" value={device} onChange={(e) => setDevice(e.target.value.trim())} />
        <select aria-label="Risk" value={risk} onChange={(e) => setRisk(e.target.value)}>
          <option value="">All risks</option>{['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <select aria-label="Action" value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">All actions</option>{['REFRESH_TELEMETRY', 'REQUEST_SYSTEM_RESCAN', 'RECONNECT_AGENT', 'RESTART_KNOWN_APPLICATION'].map((a) => <option key={a} value={a}>{a.replace(/_/g, ' ').toLowerCase()}</option>)}
        </select>
        <input aria-label="Requested by" placeholder="Requested by" value={by} onChange={(e) => setBy(e.target.value.trim())} />
        <input aria-label="Since" type="date" value={since} onChange={(e) => setSince(e.target.value)} />
      </div>
      {list.error ? <p className="note">Remediation unavailable: {list.error}</p> : null}
      {list.data && !items.length ? <p className="note">Nothing here.</p> : null}
      <RemediationList items={items} onOpen={onSelect} />
    </Panel>
  )
}

function Catalog() {
  const q = useApi(() => api.actionCatalog(), [])
  if (!q.data) return <Panel><PanelHeading title="Action catalog" /><p className="note">{q.error ?? 'Loading…'}</p></Panel>
  return (
    <Panel>
      <PanelHeading title="Action catalog" right={<p className="panel-meta">Only these actions exist. There is no command, script or file operation.</p>} />
      <table className="rem-catalog">
        <thead><tr><th>Action</th><th>Risk</th><th>Changes the device</th><th>Reversible</th><th>Verification</th><th>Status</th></tr></thead>
        <tbody>
          {q.data.actions.map((a) => (
            <tr key={a.action_id} className={a.enabled ? '' : 'is-disabled'}>
              <td><b>{a.name}</b><br /><span className="note">{a.description}</span></td>
              <td><Chip tone={riskTone(a.risk_level)}>{a.risk_level}</Chip></td>
              <td>{a.changes_state ? 'Yes' : 'No'}</td>
              <td>{a.reversible ? 'Yes' : a.rollback === 'NOT_NEEDED' ? 'Nothing to undo' : 'No'}</td>
              <td className="note">{a.verification.description}</td>
              <td>{a.enabled ? 'Available' : <span title={a.disabled_reason ?? ''}>Disabled: {a.disabled_reason}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="note note--muted">Approved applications: {q.data.applications.map((a) => a.name).join(', ')}. Each device must also allow an application locally before it can be restarted there.</p>
    </Panel>
  )
}

function Safety() {
  const me = useSession((s) => s.me)
  const status = useApi(() => api.remediationStatus(), [], 15_000)
  const policy = useApi(() => api.remediationPolicy(), [], 0)
  const [msg, setMsg] = useState<string | null>(null)
  const isAdmin = Boolean(me?.can_admin)
  const s = status.data
  const toggle = async () => {
    if (!policy.data) return
    const on = !policy.data.kill_switches.global
    if (on && !window.confirm('Stop all remediation? No new execution may begin until it is turned off.')) return
    try {
      await api.setKillSwitch('global', on)
      policy.reload()
      status.reload()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }
  const audit = async () => {
    try {
      const r = await api.verifyRemediationAudit()
      setMsg(r.ok ? `Audit trail intact (${r.rows} entries).` : `Audit trail BROKEN at entry ${r.first_bad}.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <Panel>
      <PanelHeading title="Safety controls" />
      {s ? (
        <div className="anomaly-detail__grid">
          <div><p className="eyebrow">KILL SWITCH</p><Chip tone={policy.data?.kill_switches.global || s.kill_switch_env ? 'critical' : 'accent'}>{policy.data?.kill_switches.global || s.kill_switch_env ? 'ALL REMEDIATION STOPPED' : 'OFF'}</Chip></div>
          <div><p className="eyebrow">AUTOMATIC REMEDIATION</p><Chip tone={s.auto_remediation_enabled ? 'amber' : 'muted'}>{s.auto_remediation_enabled ? 'ENABLED (LOW RISK ONLY)' : 'DISABLED'}</Chip></div>
          <div><p className="eyebrow">SIGNING</p><Chip tone={s.signing_configured ? 'accent' : 'critical'}>{s.signing_configured ? `KEY ${s.key_id}` : 'NOT CONFIGURED'}</Chip></div>
          <div><p className="eyebrow">IN FLIGHT</p><p>{s.in_flight} / {s.fleet_max_concurrent}</p></div>
          <div><p className="eyebrow">FOUR-EYES FROM</p><p>{policy.data?.four_eyes_min_risk ?? '—'} risk</p></div>
          <div><p className="eyebrow">APPROVALS EXPIRE AFTER</p><p>{policy.data ? `${Math.round(policy.data.approval_ttl_s / 60)} min` : '—'}</p></div>
        </div>
      ) : <p className="note">{status.error ?? 'Loading…'}</p>}
      {s?.open_circuits.length ? <p className="note">Stopped after repeated failures: {s.open_circuits.map((c) => `${c.action} on ${c.device_id} (until ${new Date(c.until).toLocaleString()})`).join('; ')}</p> : null}
      {isAdmin ? (
        <div className="recommendation__actions">
          <Button className={policy.data?.kill_switches.global ? '' : 'btn--danger'} onClick={() => void toggle()}>{policy.data?.kill_switches.global ? 'Resume remediation' : 'Stop all remediation'}</Button>
          <Button onClick={() => void audit()}>Verify audit trail</Button>
        </div>
      ) : <p className="note note--muted">Administrators manage kill switches and policy.</p>}
      {msg ? <p className="note">{msg}</p> : null}
    </Panel>
  )
}

export function RemediationPage({ param }: { param: string | null }) {
  const [view, setView] = useState<'center' | 'catalog' | 'safety'>('center')
  const [selected, setSelected] = useState<string | null>(param)
  const open = (id: string) => {
    setSelected(id)
    navigate('remediation', id)
  }
  return (
    <>
      <PageHeading title="Remediation" subtitle="Allowlisted actions only. AI can recommend; policy authorises; people approve; the device validates; telemetry verifies." />
      <div className="seg seg--tabs" role="tablist">
        {([['center', 'Remediation Center'], ['catalog', 'Action catalog'], ['safety', 'Safety controls']] as const).map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={view === id} className={`seg__btn ${view === id ? 'is-on' : ''}`} onClick={() => setView(id)}>{label}</button>
        ))}
      </div>
      {view === 'center' ? (
        <div className="split split--rev">
          <Center onSelect={open} />
          {selected ? <RemediationDetail key={selected} id={selected} /> : (
            <Panel style={{ flex: '868 0 0' }}><PanelHeading title="No remediation selected" /><p className="note">Select a remediation to see what is wrong, what would happen, the risk, how success is verified and whether it can be undone.</p></Panel>
          )}
        </div>
      ) : null}
      {view === 'catalog' ? <Catalog /> : null}
      {view === 'safety' ? <Safety /> : null}
    </>
  )
}
