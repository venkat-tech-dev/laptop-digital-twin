import { useEffect, useState } from 'react'

import { useCoverage, useStreamStats } from '../app/derive'
import { AgentHealthPanel, PipelinePanel } from '../app/EndpointPanels'
import { useApi } from '../hooks/useApi'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { ApiError, api, wsUrl } from '../services/api'
import { auth } from '../services/auth'
import { usePrefs } from '../stores/prefsStore'
import { canAdmin, useSession } from '../stores/sessionStore'
import { useTwinStore } from '../stores/twinStore'
import type { Role } from '../types/admin'
import { formatBytes } from '../utils/format'
import { num, readingOf } from '../utils/twin'
import { setTheme, themeChoice, type ThemeChoice } from '../services/theme'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, DataTable, Panel, PanelHeading, Preference, Spec, Switch } from '../ui/primitives'

const FREQS = [
  { label: '250 ms', ms: 250 },
  { label: '500 ms', ms: 500 },
  { label: '1 second', ms: 1000 },
  { label: '2 seconds', ms: 2000 },
]
const PROCESS_FREQS = [1000, 3000, 5000, 10000]
const ROLES: Role[] = ['admin', 'operator', 'viewer', 'employee']

const errText = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e))
const when = (iso: string | null | undefined) =>
  iso ? `${new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })} UTC` : '—'

export function SettingsPage({ section }: { section?: string | null }) {
  const device = useTwinStore((s) => s.device)
  const components = useTwinStore((s) => s.components)
  const { status } = useLiveStatus()
  const stream = useStreamStats()
  const coverage = useCoverage()
  const info = useApi(() => api.systemInfo(), [], 30_000)
  const agent = useApi(() => api.agentSettings(), [], 5_000)
  const sync = useApi(() => api.sync(), [], 5_000)
  const prefs = usePrefs()
  const me = useSession((s) => s.me)
  const admin = canAdmin(me)
  const [test, setTest] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [anonymizeBundle, setAnonymizeBundle] = useState(true)

  useEffect(() => {
    if (section) document.getElementById(`settings-${section}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [section])

  const requested = agent.data?.requested
  const applied = agent.data?.applied
  const cadence = stream.cadenceMs
  const agentCpu = num(readingOf(components.agent, 'agent.cpu_percent'))
  const agentRss = num(readingOf(components.agent, 'agent.memory_rss_bytes'))
  const connected = status === 'LIVE' || status === 'DEGRADED'
  const ws = wsUrl().replace(/\?token=.*$/, '?token=•••')
  const dirty = JSON.stringify({ ...prefs.draft, savedAt: null }) !== JSON.stringify({ ...prefs.saved, savedAt: null })
  const pending = requested?.configured && applied?.config_version !== undefined && applied.config_version < requested.version

  const run = async (label: string, fn: () => Promise<unknown>, reload?: () => void) => {
    setNotice(null)
    try {
      await fn()
      reload?.()
      setNotice(label)
    } catch (e) {
      setNotice(`Failed: ${errText(e)}`)
    }
  }
  const setAgent = (changes: Record<string, unknown>) => run('Agent configuration saved — the agent applies it within ~15 s.', () => api.setAgentSettings(changes), agent.reload)

  const runTest = async () => {
    setTesting(true)
    try {
      const r = await api.ready()
      const checks = r.body.checks as Record<string, { status: string }> | undefined
      setTest(`${r.ok ? 'Ready' : 'Not ready'} · ${r.ms.toFixed(0)} ms · ${Object.entries(checks ?? {}).map(([k, v]) => `${k}: ${v.status}`).join(', ')}`)
    } catch (e) {
      setTest(`Unreachable: ${errText(e)}`)
    } finally {
      setTesting(false)
    }
  }

  return (
    <>
      <PageHeading title="Settings" subtitle="Control collection, provenance and connection policy for this device."
        action={<Button icon="arrowRight" onClick={prefs.save} disabled={!dirty}>Save changes</Button>} />
      {notice ? <p className="toast" role="status">{notice}</p> : null}

      <div className="split split--even">
        <div className="side-stack side-stack--grow">
          <Panel>
            <PanelHeading eyebrow="COLLECTION" title="Telemetry frequency"
              right={<Chip tone={pending ? 'amber' : 'accent'}>{cadence ? `${(1000 / cadence).toFixed(1)} HZ MEASURED` : '— HZ'}</Chip>} />
            <p className="note">Balance sensor resolution against agent overhead. The agent polls this setting and applies it within ~15 seconds; the measured rate is taken from the live stream.</p>
            <div className="controls-row__group">
              {FREQS.map((f) => {
                const active = (requested?.telemetry_interval_ms ?? 1000) === f.ms
                return (
                  <Button key={f.ms} active={active} disabled={!admin || active}
                    title={admin ? `Ask the agent to sample every ${f.label}` : 'Administrator role required'}
                    onClick={() => setAgent({ telemetry_interval_ms: f.ms })}>{f.label}</Button>
                )
              })}
            </div>
            <div>
              <Spec label="Requested interval" value={requested ? `${requested.telemetry_interval_ms} ms${requested.configured ? ` · v${requested.version} by ${requested.updated_by}` : ' · agent default'}` : '—'} />
              <Spec label="Applied by agent" tone={pending ? 'amber' : 'default'}
                value={applied?.telemetry_interval_ms !== undefined ? `${applied.telemetry_interval_ms} ms${pending ? ' · update pending' : ''}` : 'Waiting for agent report'} />
              <div className="spec">
                <p className="spec__label">Process attribution cadence</p>
                <select className="form-select" disabled={!admin} value={requested?.process_interval_ms ?? 3000} aria-label="Process attribution cadence"
                  onChange={(e) => setAgent({ process_interval_ms: Number(e.target.value) })}>
                  {PROCESS_FREQS.map((ms) => <option key={ms} value={ms}>{ms / 1000} s</option>)}
                </select>
              </div>
              <Spec label="Historical aggregation" value={info.data ? `${info.data.persist_sample_interval_s} s samples · ${info.data.history_aggregation}` : '—'} />
            </div>
            <p className="eyebrow">MEASURED AGENT CPU {agentCpu !== null ? `${agentCpu.toFixed(2)}%` : '—'} / {agentRss !== null ? formatBytes(agentRss) : '—'} WORKING SET</p>
          </Panel>

          <Panel>
            <PanelHeading eyebrow="PROVENANCE" title="Sensor providers" right={<p className="panel-meta">{coverage.available} OF {coverage.total} SENSORS</p>} />
            <DataTable rowKey={(r) => r.id} rows={coverage.providers}
              columns={[
                { key: 'p', header: 'PROVIDER', width: 242, kind: 'primary', render: (r) => r.label },
                { key: 'v', header: 'VERSION', width: 84, render: (r) => r.version },
                { key: 'c', header: 'COVERAGE', width: 124, render: (r) => `${r.available} / ${r.total}` },
                { key: 's', header: 'STATE', grow: true, render: (r) => (r.available === 0 ? 'Unavailable' : r.available < r.total ? 'Partial' : 'Available'), cellKind: (r) => (r.available === r.total ? 'accent' : 'amber') },
              ]} />
            <p className="note--muted note">LibreHardwareMonitor (run as administrator) adds CPU package temperature, power, PL1/PL2, core voltage, GPU temperature/clock/power and fan RPM. See scripts/install-lhm.ps1.</p>
          </Panel>

          <Panel>
            <PanelHeading eyebrow="STORAGE POLICY" title="Data retention" />
            <div>
              <Spec label="Storage backend" value={info.data ? `${info.data.persistence}${info.data.timescaledb ? ' + TimescaleDB' : ''}` : '—'} />
              <Spec label="Retention period" value={info.data?.retention_days ? `${info.data.retention_days} days` : '—'} />
              <Spec label="Retention mechanism" value={info.data?.retention_mechanism ?? '—'} />
              <Spec label="Persisted sample interval" value={info.data ? `${info.data.persist_sample_interval_s} s` : '—'} />
              <Spec label="Persisted samples (this session)" value={info.data ? info.data.persisted_samples.toLocaleString() : '—'} />
              <Spec label="Persistence queue" value={info.data ? `${info.data.persist_queue_depth} pending` : '—'} />
            </div>
            <p className="note" style={{ fontSize: 10 }}>Retention is set on the backend (RETENTION_DAYS). Excluded from persistence: {info.data?.persist_excluded_prefixes?.join(', ') || '—'}.</p>
          </Panel>

          <WorkspacesPanel admin={admin} onNotice={setNotice} />
          <UsersPanel admin={admin} onNotice={setNotice} />
        </div>

        <div className="side-stack side-stack--grow">
          <Panel>
            <PanelHeading eyebrow="DEVICE AGENT" title="Connection" right={<Chip tone={connected ? 'accent' : 'critical'}>{connected ? 'LIVE STREAM ACTIVE' : status}</Chip>} />
            <div>
              <Spec label="Environment" value={`Live / ${info.data?.app_env ?? '—'}`} />
              <Spec label="Physical device" value={device ? `${connected ? 'Connected' : 'Not reporting'} · ${device.model ?? device.device_id}` : 'Not connected'} tone={connected ? 'default' : 'amber'} />
              <Spec label="Stream endpoint" value={ws} />
              <Spec label="Transport security" value={ws.startsWith('wss') ? 'TLS / WebSocket' : 'Local-only (no TLS)'} tone={ws.startsWith('wss') ? 'accent' : 'amber'} />
              <Spec label="Agent version" value={device?.agent_version ?? '—'} />
              <Spec label="Auth mode" value={info.data?.auth_mode ?? '—'} />
            </div>
            <Preference title="Reconnect automatically" description="Retry with backoff after an interrupted stream." checked={prefs.draft.reconnect} onChange={(v) => prefs.set({ reconnect: v })} />
            <div className="controls-row__group">
              <Button primary icon="plug" onClick={() => location.reload()}>{connected ? 'Reconnect stream' : 'Connect physical device'}</Button>
              <Button icon="radio" onClick={runTest} disabled={testing}>{testing ? 'Testing…' : 'Test endpoint'}</Button>
            </div>
            {test ? <p className="note" style={{ fontSize: 11 }}>{test}</p> : null}
          </Panel>

          <AgentHealthPanel />

          {me?.role !== 'employee' ? <PipelinePanel /> : null}

          <AppearancePanel />

          <Panel>
            <PanelHeading eyebrow="LOCAL-FIRST BY DEFAULT" title="Privacy & permissions" />
            <Preference title="Keep telemetry on this device"
              description={sync.data?.active ? `Sync to ${sync.data.target_url} is ON — batches leave this machine.` : 'No outbound sync is active, so no sensor data leaves this machine.'}
              checked={!sync.data?.active} disabled onChange={() => undefined} />
            <Preference title="Anonymize device identifiers" description="Mask device IDs and model numbers in exported reports." checked={prefs.draft.anonymizeExports} onChange={(v) => prefs.set({ anonymizeExports: v })} />
            <Preference title="Include process names in exports" description="Local process names remain visible; exported names are excluded when off." checked={prefs.draft.includeProcessNamesInExports} onChange={(v) => prefs.set({ includeProcessNamesInExports: v })} />
            <Preference title="Collect process details"
              description={`Image path (account name redacted), owner and publisher of the listed top processes. ${admin ? 'Applied by the agent within ~15 s.' : 'Administrator role required.'}`}
              checked={requested?.collect_process_details ?? false} disabled={!admin}
              onChange={(v) => setAgent({ collect_process_details: v })} />
          </Panel>

          <Panel>
            <PanelHeading eyebrow="SUPPORT" title="Diagnostics bundle" right={<Chip tone="muted">LOCAL DOWNLOAD</Chip>} />
            <p className="note">A ZIP with configuration (secrets redacted), component availability and why sensors are unavailable, anomalies and health events. It is downloaded to this computer — share it yourself if you need help. Nothing is uploaded.</p>
            <div className="preference">
              <div className="preference__text">
                <p className="preference__title">Anonymize identifiers</p>
                <p className="preference__desc">Replace the device ID with a hash and drop serial numbers.</p>
              </div>
              <Switch checked={anonymizeBundle} onChange={setAnonymizeBundle} label="Anonymize diagnostics" />
            </div>
            <div className="controls-row__group">
              <Button icon="download" onClick={() => run('Diagnostics bundle downloaded.', () => api.downloadDiagnostics(anonymizeBundle))}>Download diagnostics</Button>
            </div>
          </Panel>

          <SyncPanel admin={admin} data={sync.data} reload={sync.reload} onNotice={setNotice} />

          <div className="confirm-card">
            <div className="confirm-card__head">
              <p>{dirty ? 'Unsaved changes' : 'Device policy is up to date'}</p>
              <p className={dirty ? 'confirm-card__state is-dirty' : 'confirm-card__state'}>{dirty ? 'PENDING' : 'SAVED'}</p>
            </div>
            <p className="note">{prefs.saved.savedAt ? `Browser preferences last saved / ${when(prefs.saved.savedAt)}` : 'Defaults in use (not saved yet)'}. Agent, sync and account changes above are saved immediately on the backend.</p>
            <div className="controls-row__group">
              <Button primary icon="check" onClick={prefs.save} disabled={!dirty}>Save changes</Button>
              <Button icon="rotateCcw" onClick={prefs.restoreDefaults}>Restore defaults</Button>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

// ----------------------------------------------------------------------------- workspaces
function WorkspacesPanel({ admin, onNotice }: { admin: boolean; onNotice: (m: string) => void }) {
  const { workspaces, workspaceId, setWorkspaces, selectWorkspace } = useSession()
  const device = useTwinStore((s) => s.device)
  const [name, setName] = useState('')
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null)
  const reload = async () => setWorkspaces(await api.workspaces())
  const act = async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn()
      await reload()
      onNotice(label)
    } catch (e) {
      onNotice(`Failed: ${errText(e)}`)
    }
  }
  return (
    <section className="panel" id="settings-workspaces">
      <PanelHeading eyebrow="ORGANISATION" title="Workspaces" right={<p className="panel-meta">{workspaces.length} WORKSPACE{workspaces.length === 1 ? '' : 'S'}</p>} />
      <p className="note">Group devices for operators. Each device belongs to one workspace; every agent connected to this backend appears here.</p>
      <DataTable rowKey={(w) => w.workspace_id} rows={workspaces} selected={workspaceId} onSelect={(w) => selectWorkspace(w.workspace_id)}
        columns={[
          { key: 'n', header: 'WORKSPACE', width: 170, kind: 'primary', render: (w) => (renaming?.id === w.workspace_id ? (
            <input className="form-input" value={renaming.name} autoFocus aria-label="Workspace name" onClick={(e) => e.stopPropagation()}
              onChange={(e) => setRenaming({ id: w.workspace_id, name: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void act('Workspace renamed.', () => api.updateWorkspace(w.workspace_id, renaming.name)).then(() => setRenaming(null))
                if (e.key === 'Escape') setRenaming(null)
              }} />
          ) : w.name) },
          { key: 'd', header: 'DEVICES', grow: true, render: (w) => (w.devices.length ? w.devices.map((d) => `${d.name} (${d.status})`).join(', ') : '—') },
          { key: 'a', header: '', width: 190, render: (w) => (admin ? (
            <span className="form-row" onClick={(e) => e.stopPropagation()}>
              {device && !w.device_ids.includes(device.device_id) ? (
                <button type="button" className="link" onClick={() => act(`Device moved to ${w.name}.`, () => api.updateWorkspace(w.workspace_id, w.name, [...w.device_ids, device.device_id]))}>Move device here</button>
              ) : null}
              <button type="button" className="link" onClick={() => setRenaming({ id: w.workspace_id, name: w.name })}>Rename</button>
              {workspaces.length > 1 ? <button type="button" className="link" onClick={() => act('Workspace deleted.', () => api.deleteWorkspace(w.workspace_id))}>Delete</button> : null}
            </span>
          ) : null) },
        ]} />
      {admin ? (
        <form className="form-row" onSubmit={(e) => { e.preventDefault(); if (name.trim()) void act('Workspace created.', () => api.createWorkspace(name.trim())).then(() => setName('')) }}>
          <input className="form-input" placeholder="New workspace name" value={name} onChange={(e) => setName(e.target.value)} aria-label="New workspace name" />
          <Button type="submit" icon="arrowRight" disabled={!name.trim()}>Create workspace</Button>
        </form>
      ) : <p className="note note--muted">Administrator role required to change workspaces.</p>}
    </section>
  )
}

// ----------------------------------------------------------------------------- users
function UsersPanel({ admin, onNotice }: { admin: boolean; onNotice: (m: string) => void }) {
  const accounts = auth.mode === 'accounts'
  const users = useApi(() => (accounts && admin ? api.users() : Promise.resolve([])), [accounts, admin])
  const me = useSession((s) => s.me)
  const [form, setForm] = useState({ username: '', password: '', role: 'viewer' as Role })
  const act = async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn()
      users.reload()
      onNotice(label)
    } catch (e) {
      onNotice(`Failed: ${errText(e)}`)
    }
  }
  return (
    <section className="panel" id="settings-users">
      <PanelHeading eyebrow="ACCESS" title="User accounts" right={<Chip tone={accounts ? 'accent' : 'muted'}>{accounts ? 'ACCOUNTS ENABLED' : `AUTH: ${auth.mode.toUpperCase()}`}</Chip>} />
      {!accounts ? (
        <p className="note">User accounts are off. Set <span className="caption-mono">AUTH_MODE=accounts</span> and a <span className="caption-mono">JWT_SECRET</span> (≥ 32 characters) in .env and restart the backend; the first visit then asks you to create the administrator.</p>
      ) : !admin ? (
        <p className="note">Signed in as {me?.username} ({me?.role}). Administrators manage accounts.</p>
      ) : (
        <>
          <DataTable rowKey={(u) => u.user_id} rows={users.data ?? []} empty="No accounts"
            columns={[
              { key: 'u', header: 'USER', width: 130, kind: 'primary', render: (u) => `${u.username}${u.username === me?.username ? ' (you)' : ''}` },
              { key: 'r', header: 'ROLE', width: 110, render: (u) => (
                <select className="form-select" value={u.role} aria-label={`Role of ${u.username}`} disabled={u.username === me?.username}
                  onChange={(e) => act('Role updated.', () => api.updateUser(u.user_id, { role: e.target.value }))}>
                  {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              ) },
              { key: 'l', header: 'LAST SIGN-IN', width: 120, render: (u) => when(u.last_login_at) },
              { key: 'a', header: '', grow: true, render: (u) => (u.username === me?.username ? null : (
                <span className="form-row">
                  <button type="button" className="link" onClick={() => act(u.disabled ? 'Account enabled.' : 'Account disabled.', () => api.updateUser(u.user_id, { disabled: !u.disabled }))}>{u.disabled ? 'Enable' : 'Disable'}</button>
                  <button type="button" className="link" onClick={() => { if (confirm(`Delete ${u.username}?`)) void act('Account deleted.', () => api.deleteUser(u.user_id)) }}>Delete</button>
                </span>
              )), cellKind: (u) => (u.disabled ? 'amber' : 'mono') },
            ]} />
          <form className="form-row" onSubmit={(e) => {
            e.preventDefault()
            void act(`Account ${form.username} created.`, () => api.createUser(form.username.trim(), form.password, form.role)).then(() => setForm({ username: '', password: '', role: 'viewer' }))
          }}>
            <input className="form-input" placeholder="Username" autoComplete="off" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} aria-label="New username" />
            <input className="form-input" type="password" placeholder="Initial password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} aria-label="Initial password" />
            <select className="form-select" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })} aria-label="Role">
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            <Button type="submit" icon="arrowRight" disabled={!form.username || !form.password}>Add user</Button>
          </form>
          <p className="note note--muted">Admin: users, workspaces, agent and sync settings, model uploads. Operator: acknowledge anomalies. Viewer: read-only.</p>
        </>
      )}
    </section>
  )
}

// ----------------------------------------------------------------------------- hub sync
function SyncPanel({ admin, data, reload, onNotice }: {
  admin: boolean
  data: Awaited<ReturnType<typeof api.sync>> | null
  reload: () => void
  onNotice: (m: string) => void
}) {
  const [url, setUrl] = useState<string | null>(null)
  const target = url ?? data?.target_url ?? ''
  const save = async (changes: Record<string, unknown>, label: string) => {
    try {
      await api.setSync(changes)
      reload()
      setUrl(null)
      onNotice(label)
    } catch (e) {
      onNotice(`Failed: ${errText(e)}`)
    }
  }
  const state = !data ? '—' : data.active ? 'SYNCING' : data.enabled ? (data.key_configured ? 'ENABLED' : 'KEY MISSING') : 'OFF'
  return (
    <Panel>
      <PanelHeading eyebrow="OPTIONAL · OFF BY DEFAULT" title="Hub sync" right={<Chip tone={data?.active ? 'amber' : 'muted'}>{state}</Chip>} />
      <p className="note">Forward this device's validated telemetry to another Laptop Digital Twin backend (for example a team hub). Requires the hub's ingest key in <span className="caption-mono">SYNC_TARGET_KEY</span> on this backend. Process lists stay local unless included.</p>
      <div className="form-row">
        <input className="form-input" placeholder="https://hub.example.com" value={target} disabled={!admin} onChange={(e) => setUrl(e.target.value)} aria-label="Hub URL" />
        <Button disabled={!admin || target === (data?.target_url ?? '')} onClick={() => save({ target_url: target || null }, 'Hub URL saved.')}>Save URL</Button>
      </div>
      <Preference title="Enable sync" description={data?.key_configured ? 'Hub ingest key is configured.' : 'SYNC_TARGET_KEY is not set: sync cannot start.'}
        checked={data?.enabled ?? false} disabled={!admin || !data?.target_url} onChange={(v) => save({ enabled: v }, v ? 'Sync enabled.' : 'Sync disabled.')} />
      <Preference title="Include process lists" description="Send top-process snapshots to the hub as well." checked={data?.include_processes ?? false}
        disabled={!admin} onChange={(v) => save({ include_processes: v }, 'Sync preference saved.')} />
      <div>
        <Spec label="Sent / queued / dropped" value={data ? `${data.sent_batches} / ${data.queued_batches} / ${data.dropped_batches}` : '—'} />
        <Spec label="Last success" value={when(data?.last_success_at)} />
        <Spec label="Last error" value={data?.last_error ?? 'None'} tone={data?.last_error ? 'amber' : 'default'} />
      </div>
      {!admin ? <p className="note note--muted">Administrator role required.</p> : null}
    </Panel>
  )
}

function AppearancePanel() {
  const [choice, setChoice] = useState<ThemeChoice>(themeChoice())
  return (
    <Panel>
      <PanelHeading eyebrow="APPEARANCE" title="Theme" />
      <div className="controls-row__group" role="group" aria-label="Theme">
        {(['dark', 'light', 'system'] as ThemeChoice[]).map((c) => (
          <Button key={c} active={choice === c} onClick={() => { setTheme(c); setChoice(c) }}>
            {c === 'system' ? 'Follow system' : c.charAt(0).toUpperCase() + c.slice(1)}
          </Button>
        ))}
      </div>
      <p className="note note--muted">Stored in this browser only. The 3D stage keeps its dark studio lighting in both themes.</p>
    </Panel>
  )
}
