import { useState } from 'react'

import { fmtDuration, timeAgo } from '../alerting/alertingFormat'
import { SeverityBadge } from '../alerting/alertingUi'
import { navigate } from '../app/routes'
import { DiagnosisPanel } from '../diagnosis/DiagnosisPanel'
import { useApi } from '../hooks/useApi'
import { useNow } from '../hooks/useNow'
import { api } from '../services/api'
import { permission, requestPermission, type PermissionState } from '../services/browserNotify'
import { useNotifications } from '../stores/notificationStore'
import { canOperate, useSession } from '../stores/sessionStore'
import {
  CATEGORY_LABEL,
  STATUS_LABEL,
  type AlertCategory,
  type AlertRecord,
  type AlertSeverity,
  type Channel,
  type NotificationPreferences,
} from '../types/alerting'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, Panel, PanelHeading, Spec } from '../ui/primitives'

type Tab = 'inbox' | 'alerts' | 'preferences'
const INBOX_FILTERS: { id: string; label: string; params: { unread?: boolean; severity?: string[]; category?: string[] } }[] = [
  { id: 'all', label: 'All', params: {} },
  { id: 'unread', label: 'Unread', params: { unread: true } },
  { id: 'critical', label: 'Critical', params: { severity: ['CRITICAL'] } },
  { id: 'high', label: 'High', params: { severity: ['HIGH'] } },
  { id: 'medium', label: 'Medium', params: { severity: ['MEDIUM'] } },
  { id: 'low', label: 'Low', params: { severity: ['LOW'] } },
  { id: 'anomaly', label: 'Anomalies', params: { category: ['anomaly'] } },
  { id: 'prediction', label: 'Predictions', params: { category: ['prediction'] } },
  { id: 'system', label: 'System', params: { category: ['system', 'connectivity', 'security'] } },
]
const PAGE = 20

// ---------------------------------------------------------------------------------- inbox
function Inbox() {
  const [filter, setFilter] = useState('all')
  const [offset, setOffset] = useState(0)
  const revision = useNotifications((s) => s.revision)
  const params = INBOX_FILTERS.find((f) => f.id === filter)?.params ?? {}
  const page = useApi(() => api.notifications({ ...params, limit: PAGE, offset }), [filter, offset, revision], 60_000)
  const items = page.data?.items ?? []
  const open = async (id: string, alertId: string | null, read: boolean) => {
    if (!read) {
      const n = await api.readNotification(id).catch(() => null)
      if (n) useNotifications.getState().upsert(n)
    }
    if (alertId) navigate('alerts', `alert:${alertId}`)
  }
  return (
    <Panel>
      <PanelHeading title="Inbox" right={
        <Button onClick={async () => { await api.readAllNotifications().catch(() => undefined); page.reload(); useNotifications.getState().bump() }}
          disabled={!page.data?.unread}>Mark all as read</Button>} />
      <div className="seg" role="tablist" aria-label="Filter notifications">
        {INBOX_FILTERS.map((f) => (
          <button key={f.id} type="button" role="tab" aria-selected={filter === f.id} className={`seg__btn ${filter === f.id ? 'is-on' : ''}`}
            onClick={() => { setFilter(f.id); setOffset(0) }}>{f.label}</button>
        ))}
      </div>
      {page.error ? <p className="note">Notifications unavailable: {page.error}</p> : null}
      {page.loading && !page.data ? <p className="note">Loading…</p> : null}
      {page.data && items.length === 0 ? <p className="note">No notifications{filter !== 'all' ? ' match this filter' : ''}.</p> : null}
      <ul className="inbox">
        {items.map((n) => (
          <li key={n.notification_id}>
            <button type="button" className={`inbox__item ${n.read_at ? '' : 'is-unread'}`}
              onClick={() => void open(n.notification_id, n.alert_id, Boolean(n.read_at))}>
              <SeverityBadge severity={n.severity} />
              <span className="inbox__main">
                <span className="inbox__title">{n.title}</span>
                <span className="inbox__body">{n.body}</span>
                <span className="inbox__meta">{CATEGORY_LABEL[n.category] ?? n.category} · {n.device_id ?? 'all devices'} · {new Date(n.created_at).toLocaleString()}</span>
              </span>
              <span className="inbox__state">{n.read_at ? 'Read' : 'Unread'}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="pager">
        <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Newer</Button>
        <span className="note">{items.length ? `${offset + 1}–${offset + items.length}` : ''}</span>
        <Button disabled={items.length < PAGE} onClick={() => setOffset(offset + PAGE)}>Older</Button>
      </div>
    </Panel>
  )
}

// ---------------------------------------------------------------------------------- alerts
function AlertList({ selected, onSelect }: { selected: string | null; onSelect: (id: string) => void }) {
  const [status, setStatus] = useState<'open' | 'closed' | 'all'>('open')
  const [severity, setSeverity] = useState<AlertSeverity | ''>('')
  const [category, setCategory] = useState<AlertCategory | ''>('')
  const [offset, setOffset] = useState(0)
  const revision = useNotifications((s) => s.revision)
  const statuses = status === 'open' ? ['OPEN', 'ONGOING', 'ACKNOWLEDGED', 'SUPPRESSED'] : status === 'closed' ? ['RESOLVED', 'EXPIRED'] : []
  const list = useApi(() => api.alerts({ status: statuses, severity: severity ? [severity] : [], category: category ? [category] : [], limit: PAGE, offset }),
    [status, severity, category, offset, revision], 30_000)
  const items = list.data?.items ?? []
  return (
    <Panel className="side-panel" style={{ flex: '424 0 0' }}>
      <PanelHeading title="Alerts" right={<p className="panel-meta">{items.length}{items.length === PAGE ? '+' : ''}</p>} />
      <div className="anomaly-filters">
        <select aria-label="Status" value={status} onChange={(e) => { setStatus(e.target.value as 'open'); setOffset(0) }}>
          <option value="open">Open</option><option value="closed">Resolved / expired</option><option value="all">All</option>
        </select>
        <select aria-label="Severity" value={severity} onChange={(e) => { setSeverity(e.target.value as AlertSeverity | ''); setOffset(0) }}>
          <option value="">All severities</option>
          {(['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] as const).map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select aria-label="Category" value={category} onChange={(e) => { setCategory(e.target.value as AlertCategory | ''); setOffset(0) }}>
          <option value="">All categories</option>
          {(Object.keys(CATEGORY_LABEL) as AlertCategory[]).map((c) => <option key={c} value={c}>{CATEGORY_LABEL[c]}</option>)}
        </select>
      </div>
      {list.error ? <p className="note">Alerts unavailable: {list.error}</p> : null}
      {list.data && items.length === 0 ? <p className="note">{status === 'open' ? 'No active alerts.' : 'No alerts match.'}</p> : null}
      <div className="anomaly-list">
        {items.map((a) => (
          <button key={a.alert_id} type="button" className={`alert-row alert-row--${a.severity.toLowerCase()} ${selected === a.alert_id ? 'is-selected' : ''}`}
            onClick={() => onSelect(a.alert_id)}>
            <SeverityBadge severity={a.severity} />
            <span className="alert-row__title">{a.title}</span>
            <span className="alert-row__meta">{STATUS_LABEL[a.status]} · {a.device_id} · {timeAgo(a.first_detected_at)}{a.occurrences > 1 ? ` · ${a.occurrences} updates` : ''}{a.correlation_key ? ' · correlated' : ''}</span>
          </button>
        ))}
      </div>
      <div className="pager">
        <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Newer</Button>
        <Button disabled={items.length < PAGE} onClick={() => setOffset(offset + PAGE)}>Older</Button>
      </div>
    </Panel>
  )
}

function fmtNum(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${Math.round(v * 10) / 10}`
}

function AlertDetail({ id }: { id: string }) {
  const revision = useNotifications((s) => s.revision)
  const a = useApi(() => api.alert(id), [id, revision], 30_000)
  const me = useSession((s) => s.me)
  const now = useNow(30_000)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const alert: AlertRecord | null = a.data
  if (!alert) {
    return <Panel style={{ flex: '868 0 0' }}><PanelHeading title={a.error ? 'Alert unavailable' : 'Loading…'} />{a.error ? <p className="note">{a.error}</p> : null}</Panel>
  }
  const ev = (alert.metadata.evidence ?? {}) as Record<string, unknown>
  const open = ['OPEN', 'ONGOING', 'ACKNOWLEDGED', 'SUPPRESSED'].includes(alert.status)
  const act = async (action: 'acknowledge' | 'resolve' | 'suppress') => {
    setBusy(true)
    setMsg(null)
    try {
      await api.alertAction(alert.alert_id, action, undefined, action === 'suppress' ? 24 : undefined)
      a.reload()
      useNotifications.getState().bump()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  const eta = typeof ev.time_to_threshold_s === 'number' ? ev.time_to_threshold_s : null
  return (
    <Panel style={{ flex: '868 0 0' }} className="anomaly-detail">
      <PanelHeading eyebrow={`${CATEGORY_LABEL[alert.category].toUpperCase()} / ${alert.source_type.toUpperCase()}`} title={alert.title}
        right={<div className="anomaly-detail__chips"><SeverityBadge severity={alert.severity} /><Chip tone={open ? 'amber' : 'muted'}>{STATUS_LABEL[alert.status]}</Chip></div>} />
      <p className="anomaly-detail__summary">{alert.summary}</p>
      <div className="anomaly-detail__grid">
        <Spec label="Device" value={<button type="button" className="link-btn" onClick={() => navigate('twin')}>{alert.device_id}</button>} />
        <Spec label="First detected" value={new Date(alert.first_detected_at).toLocaleString()} />
        <Spec label="Duration" value={fmtDuration(((alert.resolved_at ? Date.parse(alert.resolved_at) : now) - Date.parse(alert.first_detected_at)) / 1000)} />
        <Spec label="Confidence" value={alert.confidence !== null ? `${Math.round(alert.confidence * 100)}%` : 'Deterministic'} />
        <Spec label="Metric" value={alert.metadata.metric ?? '—'} />
        <Spec label="Observed" value={fmtNum(alert.metadata.observed)} />
        <Spec label="Expected" value={fmtNum(alert.metadata.expected)} />
        <Spec label="Threshold" value={fmtNum(alert.metadata.threshold)} />
        {eta !== null ? <Spec label="Estimated time" value={`~${fmtDuration(eta)}`} /> : null}
        <Spec label="Escalation" value={alert.escalation_level ? `level ${alert.escalation_level}` : 'none'} />
      </div>
      {ev.summary ? <p className="note">Evidence: {String(ev.summary)}</p> : null}
      <DiagnosisPanel kind="alert" id={alert.alert_id} />
      {alert.source_type === 'anomaly' && ev.anomaly_id ? <Button onClick={() => navigate('health', String(ev.anomaly_id))}>Open the anomaly</Button> : null}
      {alert.source_type === 'prediction' ? <Button onClick={() => navigate('analytics', `forecast:${String(alert.alert_type === 'resource_exhaustion' ? (alert.metadata.metric?.includes('disk') ? 'disk' : 'memory') : '')}`)}>Open the forecast</Button> : null}
      {open ? (
        <div className="recommendation">
          <p className="recommendation__label">ACTIONS (NOTIFICATION ONLY - NOTHING IS CHANGED ON THE DEVICE)</p>
          <div className="recommendation__actions">
            <Button icon="check" disabled={busy || alert.status === 'ACKNOWLEDGED' || alert.status === 'SUPPRESSED'} onClick={() => void act('acknowledge')}>Acknowledge</Button>
            <Button disabled={busy || !canOperate(me)} onClick={() => void act('resolve')} title={canOperate(me) ? undefined : 'Operator role required'}>Resolve</Button>
            <Button disabled={busy || !canOperate(me) || alert.status === 'SUPPRESSED'} onClick={() => void act('suppress')} title="Silence notifications for 24 h">Suppress 24 h</Button>
          </div>
          {msg ? <p className="note">{msg}</p> : null}
        </div>
      ) : null}
      <div className="anomaly-detail__columns">
        <div>
          <p className="eyebrow">ALERT HISTORY (AUDIT)</p>
          <ol className="audit">
            {(alert.audit ?? []).map((e, i) => (
              <li key={`${e.at}-${i}`}><span className="audit__time">{new Date(e.at).toLocaleTimeString()}</span> <b>{e.action}</b> by {e.actor}{e.to_status && e.to_status !== e.from_status ? ` → ${e.to_status}` : ''}{e.detail ? ` — ${e.detail}` : ''}</li>
            ))}
          </ol>
        </div>
        <div>
          <p className="eyebrow">NOTIFICATION DELIVERY</p>
          {(alert.deliveries ?? []).length === 0 ? <p className="note">No notifications were sent for this alert (policy, preferences or cooldown).</p> : null}
          <ul className="audit">
            {(alert.deliveries ?? []).map((d) => (
              <li key={d.notification_id}>{d.channel} → {d.user_id}: <b>{d.status}</b>{d.attempt_count > 1 ? ` after ${d.attempt_count} attempts` : ''}{d.failure_reason ? ` (${d.failure_reason})` : ''}{d.escalation_level ? ` · escalation ${d.escalation_level}` : ''}</li>
            ))}
          </ul>
        </div>
      </div>
    </Panel>
  )
}

// ---------------------------------------------------------------------------- preferences
const ZONES: string[] = (() => {
  try {
    return (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone') ?? []
  } catch {
    return []
  }
})()

const ALL_CHANNELS: { id: Channel; label: string }[] = [
  { id: 'in_app', label: 'In-app' }, { id: 'browser', label: 'Browser' }, { id: 'windows', label: 'Windows (agent toast)' }, { id: 'email', label: 'E-mail' },
]

function Preferences() {
  const prefs = useApi(() => api.notificationPreferences(), [])
  const [edited, setDraft] = useState<NotificationPreferences | null>(null)
  const draft = edited ?? prefs.data?.preferences ?? null
  const [perm, setPerm] = useState<PermissionState>(permission())
  const [msg, setMsg] = useState<string | null>(null)
  if (!prefs.data || !draft) return <Panel><PanelHeading title="Preferences" /><p className="note">{prefs.error ?? 'Loading…'}</p></Panel>
  const ch = prefs.data.channels
  const toggle = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v])
  const save = async () => {
    setMsg(null)
    try {
      const r = await api.setNotificationPreferences(draft)
      setDraft(r.preferences)
      setMsg('Saved.')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <Panel>
      <PanelHeading title="Notification preferences" right={<Button primary onClick={() => void save()}>Save</Button>} />
      {prefs.data.safeguards.critical_always_in_app ? <p className="note note--muted">Critical alerts always reach your in-app inbox (organisation policy).</p> : null}
      <div className="prefs">
        <fieldset>
          <legend>Channels</legend>
          {ALL_CHANNELS.map((c) => (
            <label key={c.id} className="prefs__opt" title={ch[c.id]?.reason ?? undefined}>
              <input type="checkbox" checked={draft.channels.includes(c.id)} onChange={() => setDraft({ ...draft, channels: toggle(draft.channels, c.id) })} />
              {c.label}{ch[c.id] && !ch[c.id].available ? <span className="note"> (unavailable: {ch[c.id].reason})</span> : null}
            </label>
          ))}
          {draft.channels.includes('browser') ? (
            <p className="note">Browser permission: <b>{perm}</b>{perm === 'default' ? <> · <button type="button" className="link-btn" onClick={async () => setPerm(await requestPermission())}>Allow browser notifications</button></> : null}{perm === 'denied' ? ' · blocked in the browser settings; in-app notifications still work' : null}</p>
          ) : null}
          {draft.channels.includes('email') ? (
            <label className="prefs__field">E-mail address <input type="email" value={draft.email ?? ''} onChange={(e) => setDraft({ ...draft, email: e.target.value || null })} /></label>
          ) : null}
        </fieldset>
        <fieldset>
          <legend>Severities</legend>
          {(['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] as AlertSeverity[]).map((s) => (
            <label key={s} className="prefs__opt"><input type="checkbox" checked={draft.severities.includes(s)} disabled={s === 'CRITICAL'}
              onChange={() => setDraft({ ...draft, severities: toggle(draft.severities, s) })} /> {s}</label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Categories</legend>
          {(Object.keys(CATEGORY_LABEL) as AlertCategory[]).map((c) => (
            <label key={c} className="prefs__opt"><input type="checkbox" checked={draft.categories.includes(c)} onChange={() => setDraft({ ...draft, categories: toggle(draft.categories, c) })} /> {CATEGORY_LABEL[c]}</label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Frequency</legend>
          {(['immediate', 'grouped', 'digest'] as const).map((f) => (
            <label key={f} className="prefs__opt"><input type="radio" name="freq" checked={draft.frequency === f} onChange={() => setDraft({ ...draft, frequency: f })} /> {f === 'digest' ? `Daily digest at ${draft.digest_hour}:00` : f[0].toUpperCase() + f.slice(1)}</label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Quiet hours</legend>
          <label className="prefs__opt"><input type="checkbox" checked={Boolean(draft.quiet_hours)}
            onChange={() => setDraft({ ...draft, quiet_hours: draft.quiet_hours ? null : { start: '22:00', end: '07:00', high: 'immediate', medium: 'defer' } })} /> Enabled (critical alerts are never delayed)</label>
          {draft.quiet_hours ? (
            <div className="prefs__row">
              <label className="prefs__field">From <input type="time" value={draft.quiet_hours.start} onChange={(e) => setDraft({ ...draft, quiet_hours: { ...draft.quiet_hours!, start: e.target.value } })} /></label>
              <label className="prefs__field">To <input type="time" value={draft.quiet_hours.end} onChange={(e) => setDraft({ ...draft, quiet_hours: { ...draft.quiet_hours!, end: e.target.value } })} /></label>
              <label className="prefs__field">High <select value={draft.quiet_hours.high} onChange={(e) => setDraft({ ...draft, quiet_hours: { ...draft.quiet_hours!, high: e.target.value as 'immediate' } })}><option value="immediate">Immediately</option><option value="defer">After quiet hours</option></select></label>
              <label className="prefs__field">Medium <select value={draft.quiet_hours.medium} onChange={(e) => setDraft({ ...draft, quiet_hours: { ...draft.quiet_hours!, medium: e.target.value as 'defer' } })}><option value="defer">After quiet hours</option><option value="immediate">Immediately</option></select></label>
            </div>
          ) : null}
          <label className="prefs__field">Time zone
            <select value={draft.timezone} onChange={(e) => setDraft({ ...draft, timezone: e.target.value })}>
              {[draft.timezone, Intl.DateTimeFormat().resolvedOptions().timeZone, ...ZONES].filter((z, i, all) => z && all.indexOf(z) === i).map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
          </label>
        </fieldset>
      </div>
      {msg ? <p className="note">{msg}</p> : null}
    </Panel>
  )
}

// ----------------------------------------------------------------------------------- page
export function AlertsPage({ param }: { param: string | null }) {
  const initialAlert = param?.startsWith('alert:') ? param.slice(6) : null
  const [tab, setTab] = useState<Tab>(initialAlert ? 'alerts' : 'inbox')
  const [selected, setSelected] = useState<string | null>(initialAlert)
  const unread = useNotifications((s) => s.unread)
  return (
    <>
      <PageHeading title="Alerts & Notifications" subtitle="The right alert, to the right person, with the evidence to decide. Real events only." />
      <div className="seg seg--tabs" role="tablist">
        {([['inbox', `Inbox${unread ? ` (${unread})` : ''}`], ['alerts', 'Alerts'], ['preferences', 'Preferences']] as const).map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={`seg__btn ${tab === id ? 'is-on' : ''}`} onClick={() => setTab(id)}>{label}</button>
        ))}
      </div>
      {tab === 'inbox' ? <Inbox /> : null}
      {tab === 'alerts' ? (
        <div className="split split--rev">
          <AlertList selected={selected} onSelect={setSelected} />
          {selected ? <AlertDetail key={selected} id={selected} /> : (
            <Panel style={{ flex: '868 0 0' }}><PanelHeading title="No alert selected" /><p className="note">Select an alert to see what happened, the evidence, its history and who was notified.</p></Panel>
          )}
        </div>
      ) : null}
      {tab === 'preferences' ? <Preferences /> : null}
    </>
  )
}
