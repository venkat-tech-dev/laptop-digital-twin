import { memo, useEffect, useMemo, useRef, useState } from 'react'

import { navigate } from '../app/routes'
import { useNow } from '../hooks/useNow'
import { api } from '../services/api'
import { useTwinDoc, useTwinValue } from '../stores/twinDocStore'
import type { Connectivity, ExplainOut, TwinField, TwinHealthState, TwinSection } from '../types/twinDoc'
import { Button, Chip, Panel, PanelHeading } from '../ui/primitives'
import { ageText, CONNECTIVITY_LABEL, CONNECTIVITY_TONE, formatField, FRESHNESS_TEXT, HEALTH_TONE, VISUAL_TONE } from './format'

type Range = '15m' | '1h' | '6h' | '24h'

/** Re-renders only itself once per second ("updated 4 s ago"); the card around it stays put. */
function Ago({ iso, prefix = '' }: { iso: string | null | undefined; prefix?: string }) {
  const now = useNow(1000)
  return <>{prefix}{ageText(iso, now)}</>
}

// ------------------------------------------------------------------------------------ header

export function TwinHeader() {
  const status = useTwinDoc((s) => s.status)
  const error = useTwinDoc((s) => s.error)
  const version = useTwinDoc((s) => s.version)
  const restored = useTwinValue<boolean>('twin.restored') ?? false
  const hostname = useTwinValue<string>('identity.hostname')
  const model = useTwinValue<string>('identity.model')
  const manufacturer = useTwinValue<string>('identity.manufacturer')
  const owner = useTwinValue<string>('identity.owner')
  const department = useTwinValue<string>('identity.department')
  const os = useTwinValue<string>('operating_system.name')
  const osVersion = useTwinValue<string>('operating_system.version')
  const connectivity = (useTwinValue<Connectivity>('connectivity.status') ?? 'UNKNOWN') as Connectivity
  const lastTelemetry = useTwinValue<string>('connectivity.last_telemetry_at')
  const health = useTwinValue<TwinHealthState>('health')
  const twinId = useTwinValue<string>('identity.twin_id')

  if (status === 'loading' && version === 0) return <Panel className="twin-header"><p className="note">Loading device state…</p></Panel>
  if (status === 'missing') return <Panel className="twin-header"><p className="note">No telemetry received yet for this device. {error}</p></Panel>
  if (status === 'error') return <Panel className="twin-header"><p className="note" style={{ color: 'var(--critical)' }}>Device state unavailable: {error}</p></Panel>
  if (status === 'idle') return <Panel className="twin-header"><p className="note">No device has connected yet.</p></Panel>

  const statusLine =
    connectivity === 'OFFLINE' ? <>Device offline · <Ago iso={lastTelemetry} prefix="last seen " /></>
    : connectivity === 'STALE' ? <>Telemetry delayed · <Ago iso={lastTelemetry} prefix="last update " /></>
    : connectivity === 'UNKNOWN' ? <>Status unknown · no contact since the backend started</>
    : <><Ago iso={lastTelemetry} prefix="Last update " /></>
  return (
    <Panel className="twin-header">
      <div className="twin-header__identity">
        <p className="eyebrow">DIGITAL TWIN · {department ?? 'NO DEPARTMENT'}</p>
        <p className="twin-header__name">{hostname ?? model ?? 'Unnamed device'}</p>
        <p className="twin-header__sub">
          {[manufacturer, model].filter(Boolean).join(' ')} · {os ?? 'OS unknown'} {osVersion ?? ''} · {owner ? `Assigned to ${owner}` : 'Not assigned'}
        </p>
      </div>
      <div className="twin-header__state">
        <span className={`chip ${CONNECTIVITY_TONE[connectivity] === 'accent' ? '' : `chip--${CONNECTIVITY_TONE[connectivity]}`} ${connectivity === 'ONLINE' ? 'chip--live' : ''}`} role="status" aria-live="polite">
          <span className="chip__dot" />● {CONNECTIVITY_LABEL[connectivity]}
        </span>
        <p className="caption-mono">{statusLine}</p>
        <Chip tone={HEALTH_TONE[health?.state ?? 'UNKNOWN']} title={health?.note ?? health?.reasons.map((r) => r.message).join(' · ')}>
          HEALTH {health?.state ?? 'UNKNOWN'}
          {health?.state === 'UNKNOWN' && health.last_known ? ` (last known ${health.last_known})` : ''}
        </Chip>
        <p className="caption-mono" title={`twin ${twinId ?? ''}`}>v{version}{restored ? ' · last known state (restored)' : ''}</p>
      </div>
    </Panel>
  )
}

// ------------------------------------------------------------------------------------ metric cards

export interface CardSpec {
  title: string
  path: string
  section: string
  secondary?: { label: string; path: string }[]
}

export const CARDS: CardSpec[] = [
  { title: 'CPU', path: 'performance.cpu.usage_percent', section: 'cpu',
    secondary: [{ label: 'Clock', path: 'performance.cpu.frequency_mhz' }, { label: 'Temperature', path: 'performance.cpu.temperature_c' }] },
  { title: 'Memory', path: 'performance.memory.usage_percent', section: 'memory',
    secondary: [{ label: 'In use', path: 'performance.memory.used_bytes' }, { label: 'Installed', path: 'performance.memory.total_bytes' }] },
  { title: 'Storage', path: 'performance.disk.usage_percent', section: 'storage',
    secondary: [{ label: 'Free', path: 'performance.disk.free_bytes' }, { label: 'Activity', path: 'performance.disk.active_time_percent' }, { label: 'Drive health', path: 'storage.health_ok' }] },
  { title: 'Network', path: 'network.internet_connected', section: 'network',
    secondary: [{ label: 'Download', path: 'network.rx_bytes_per_sec' }, { label: 'Upload', path: 'network.tx_bytes_per_sec' }, { label: 'Gateway latency', path: 'network.gateway_latency_ms' }, { label: 'Connection', path: 'network.connection_type' }] },
  { title: 'Battery', path: 'battery.charge_percent', section: 'battery',
    secondary: [{ label: 'State', path: 'battery.charging_state' }, { label: 'Health', path: 'battery.health_percent' }, { label: 'Power source', path: 'battery.power_source' }] },
  { title: 'Thermal', path: 'thermal.temperature_c', section: 'thermal',
    secondary: [{ label: 'Throttling', path: 'thermal.throttling' }, { label: 'Fan', path: 'thermal.fan_rpm' }] },
  { title: 'Security', path: 'security.realtime_protection', section: 'security',
    secondary: [{ label: 'Firewall', path: 'security.firewall_enabled' }, { label: 'Antivirus', path: 'security.antivirus_enabled' }, { label: 'Secure Boot', path: 'security.secure_boot' }, { label: 'Signatures', path: 'security.signature_age_days' }] },
  { title: 'GPU', path: 'performance.gpu.usage_percent', section: 'gpu' },
]

const TREND_PATHS = CARDS.map((c) => c.path).filter((p) => !p.startsWith('network.internet') && !p.startsWith('security.'))

function Sparkline({ points, field }: { points: { t: number; v: number }[]; field?: TwinField }) {
  if (points.length < 2) return <p className="sparkline__empty">{field?.value === null ? 'NO DATA' : 'COLLECTING HISTORY'}</p>
  const w = 160
  const h = 36
  const t0 = points[0].t
  const t1 = points[points.length - 1].t || t0 + 1
  const vs = points.map((p) => p.v)
  let lo = Math.min(...vs)
  let hi = Math.max(...vs)
  if (field?.unit === '%') {
    lo = Math.min(lo, 0)
    hi = Math.max(hi, 100)
  }
  if (hi === lo) hi = lo + 1
  const x = (t: number) => ((t - t0) / Math.max(1, t1 - t0)) * w
  const y = (v: number) => h - ((v - lo) / (hi - lo)) * (h - 4) - 2
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join('')
  return (
    <svg className="sparkline" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="trend from measured history">
      <path d={d} fill="none" strokeWidth="1.5" />
    </svg>
  )
}

function Secondary({ label, path }: { label: string; path: string }) {
  const f = useTwinValue<TwinField>(path)
  const fv = formatField(f, path)
  return (
    <div className="twin-card__row">
      <span>{label}</span>
      <span className={!fv ? 'is-na' : f?.status === 'warning' || f?.status === 'critical' ? `is-${f.status}` : ''}
        title={f?.reason ?? (f ? FRESHNESS_TEXT[f.freshness] : 'No data yet')}>
        {fv ? `${fv.value}${fv.unit ? ` ${fv.unit}` : ''}` : f?.freshness === 'UNSUPPORTED' ? 'Unsupported' : 'No data'}
      </span>
    </div>
  )
}

const TwinCard = memo(function TwinCard({ spec, trend, deviceId }: { spec: CardSpec; trend: { t: number; v: number }[]; deviceId: string }) {
  const f = useTwinValue<TwinField>(spec.path)
  const section = useTwinValue<TwinSection>(`sections.${spec.section}`)
  const [explain, setExplain] = useState<ExplainOut | null>(null)
  const live = useRef<{ t: number; v: number }[]>([])
  // append live values from patches (no polling) to the history loaded once
  useEffect(() => {
    if (f && typeof f.value === 'number' && f.timestamp) {
      const t = Date.parse(f.timestamp)
      const last = live.current[live.current.length - 1]
      if (!last || last.t < t) live.current = [...live.current.slice(-120), { t, v: f.value }]
    }
  }, [f])
  const points = useMemo(() => {
    const merged = [...trend, ...live.current.filter((p) => !trend.length || p.t > trend[trend.length - 1].t)]
    return merged
  }, [trend, f]) // eslint-disable-line react-hooks/exhaustive-deps
  const delta = useMemo(() => {
    if (!f || typeof f.value !== 'number' || points.length < 2) return null
    const target = Date.parse(f.timestamp ?? '') - 5 * 60_000
    const past = points.find((p) => p.t >= target)
    if (!past || Date.parse(f.timestamp ?? '') - past.t < 120_000) return null
    return f.value - past.v
  }, [f, points])
  const visual = section?.visual ?? 'unknown'
  const fv = formatField(f, spec.path)
  const freshness = f?.freshness ?? 'UNKNOWN'
  const display = fv
  return (
    <div className={`twin-card twin-card--${visual}`} data-visual={visual} data-field={spec.path}>
      <div className="twin-card__head">
        <p className="eyebrow">{spec.title.toUpperCase()}</p>
        <Chip tone={VISUAL_TONE[visual]}>{visual.toUpperCase()}</Chip>
      </div>
      <div className={`reading ${!display ? 'reading--na' : ''}`}>
        <p className="reading__value" data-testid={`value-${spec.path}`}>
          {display ? display.value : freshness === 'UNSUPPORTED' ? 'UNSUPPORTED' : 'NO DATA'}
        </p>
        {display?.unit ? <p className="reading__unit">{display.unit}</p> : null}
      </div>
      <p className="twin-card__meta">
        <span className={`freshness freshness--${freshness.toLowerCase()}`}>{FRESHNESS_TEXT[freshness]}</span>
        {f?.timestamp ? <span> · <Ago iso={f.timestamp} prefix="updated " /></span> : null}
        {delta !== null && Math.abs(delta) >= 1 ? <span> · {delta > 0 ? '↑' : '↓'} {Math.abs(delta).toFixed(0)}{f?.unit === '%' ? ' pts' : ` ${f?.unit}`} vs 5 min ago</span> : null}
      </p>
      {f?.reason && !display ? <p className="note note--muted">{f.reason}</p> : null}
      {f?.unit && f.unit !== 'bool' ? <Sparkline points={points} field={f} /> : null}
      {spec.secondary?.map((s) => <Secondary key={s.path} {...s} />)}
      <button type="button" className="twin-card__why" disabled={!f?.source}
        onClick={async () => setExplain(explain ? null : await api.explainField(deviceId, spec.path))}>
        {explain ? 'Hide source' : 'Why this value?'}
      </button>
      {explain ? <Explain out={explain} /> : null}
    </div>
  )
})

function Explain({ out }: { out: ExplainOut }) {
  const r = out.source_reading
  const rule = out.severity_rule
  return (
    <div className="twin-explain">
      {r ? (
        <>
          <p><b>Source</b> {r.source}</p>
          <p><b>Metric</b> <code>{r.metric_key}</code> = {String(r.value)} {r.unit}</p>
          <p><b>Collected</b> {new Date(r.collected_at).toLocaleTimeString()} (every {r.interval_s ?? '?'} s)</p>
          <p><b>Received</b> {out.current.source?.received_at ? new Date(out.current.source.received_at).toLocaleTimeString() : '—'} · batch #{out.current.source?.sequence ?? '—'}</p>
        </>
      ) : <p>No source reading in this process (restored state).</p>}
      {rule ? <p><b>Rule</b> elevated {rule.higher_is_worse ? '≥' : '≤'} {rule.elevated} · warning {rule.higher_is_worse ? '≥' : '≤'} {rule.warning} · critical {rule.higher_is_worse ? '≥' : '≤'} {rule.critical} (hysteresis {rule.hysteresis})</p> : null}
      <p><b>Freshness</b> live ≤ {out.freshness_limits_s.live.toFixed(0)} s, recent ≤ {out.freshness_limits_s.recent.toFixed(0)} s · twin v{out.twin_version}</p>
    </div>
  )
}

export function TwinMetricBoard() {
  const deviceId = useTwinDoc((s) => s.deviceId)
  const ready = useTwinDoc((s) => s.status === 'ready')
  const [range, setRange] = useState<Range>('1h')
  const [trends, setTrends] = useState<Record<string, { t: number; v: number }[]>>({})
  const [trendError, setTrendError] = useState<string | null>(null)
  useEffect(() => {
    if (!deviceId || !ready) return
    let cancelled = false
    api.deviceHistory(deviceId, TREND_PATHS, range)
      .then((h) => {
        if (cancelled) return
        const out: Record<string, { t: number; v: number }[]> = {}
        for (const [k, pts] of Object.entries(h.series)) out[k] = pts.map((p) => ({ t: Date.parse(p.t), v: p.avg }))
        setTrends(out)
        setTrendError(null)
      })
      .catch((e) => !cancelled && setTrendError(e instanceof Error ? e.message : String(e)))
    return () => {
      cancelled = true
    }
  }, [deviceId, ready, range])
  if (!deviceId) return null
  return (
    <Panel className="twin-board">
      <PanelHeading eyebrow="CURRENT STATE · FROM TELEMETRY" title="Device state"
        right={(
          <div className="controls-row__group" role="group" aria-label="Trend range">
            {(['15m', '1h', '6h', '24h'] as Range[]).map((r) => <Button key={r} active={range === r} onClick={() => setRange(r)}>{r}</Button>)}
          </div>
        )} />
      {trendError ? <p className="note note--muted">History unavailable ({trendError}); live values continue.</p> : null}
      <div className="twin-grid">
        {CARDS.map((c) => <TwinCard key={c.path} spec={c} trend={trends[c.path] ?? []} deviceId={deviceId} />)}
      </div>
    </Panel>
  )
}

// ------------------------------------------------------------------------------------ health + timeline

export function TwinHealthPanel() {
  const health = useTwinValue<TwinHealthState>('health')
  const alerts = useTwinValue<{ anomaly_id: string; severity: string; title: string; since: string }[]>('alerts.active') ?? []
  return (
    <Panel className="twin-side">
      <PanelHeading eyebrow="DETERMINISTIC RULES" title="Health" right={<Chip tone={HEALTH_TONE[health?.state ?? 'UNKNOWN']}>{health?.state ?? 'UNKNOWN'}</Chip>} />
      {!health ? <p className="note">No health evaluation yet.</p> : null}
      {health?.note ? <p className="note">{health.note}{health.last_known ? ` Last known: ${health.last_known}.` : ''}</p> : null}
      {health && health.state !== 'UNKNOWN' && health.reasons.length === 0 ? <p className="note">All evaluated rules pass.</p> : null}
      <ul className="twin-reasons">
        {health?.reasons.map((r) => (
          <li key={`${r.rule}-${r.field}`} className={`twin-reasons__item is-${r.state.toLowerCase()}`} title={r.rule_description}>
            <span className="twin-reasons__state">{r.state}</span> {r.message}
          </li>
        ))}
      </ul>
      {alerts.length ? (
        <>
          <p className="eyebrow">ACTIVE ALERTS</p>
          {alerts.map((a) => (
            <button key={a.anomaly_id} type="button" className="spec spec--button" onClick={() => navigate('health')}>
              <p className="spec__label">{a.title}</p>
              <p className="spec__value">{a.severity.toUpperCase()}</p>
            </button>
          ))}
        </>
      ) : null}
      <p className="note note--muted">Rules: app/domain/twin/rules.py (thresholds, freshness, connectivity, health). No machine learning.</p>
    </Panel>
  )
}

const KIND_ICON: Record<string, string> = { threshold: '◆', connectivity: '●', health: '♥', agent: '▲' }

export function TwinTimeline() {
  const events = useTwinDoc((s) => s.events)
  const now = useNow(30_000)
  return (
    <Panel className="twin-side">
      <PanelHeading eyebrow="FROM TELEMETRY AND AGENT EVENTS" title="Timeline" right={<p className="panel-meta">{events.length} EVENTS</p>} />
      {events.length === 0 ? <p className="note">No events yet. Threshold changes, connectivity, health and agent events appear here as they happen.</p> : null}
      <ol className="twin-timeline">
        {events.slice(0, 60).map((e) => (
          <li key={e.event_id} className={`twin-timeline__item is-${e.severity}`}>
            <span className="twin-timeline__time" title={new Date(e.time).toLocaleString()}>
              {new Date(e.time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
            <span className="twin-timeline__icon" aria-hidden>{KIND_ICON[e.kind] ?? '●'}</span>
            <span className="twin-timeline__msg">{e.message}<span className="twin-timeline__ago">{ageText(e.time, now)}</span></span>
          </li>
        ))}
      </ol>
    </Panel>
  )
}
