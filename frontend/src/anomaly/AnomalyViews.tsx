import { memo, useEffect, useMemo, useState } from 'react'

import { navigate } from '../app/routes'
import { DiagnosisPanel } from '../diagnosis/DiagnosisPanel'
import { useApi } from '../hooks/useApi'
import { useNow } from '../hooks/useNow'
import { api } from '../services/api'
import { useAnomalies, useAnomalyRecord } from '../stores/anomalyStore'
import { canOperate, useSession } from '../stores/sessionStore'
import { useTwinValue } from '../stores/twinDocStore'
import {
  LEVELS,
  TYPES,
  typeLabel,
  type AnomalyFilters,
  type AnomalyLevel,
  type AnomalyRecord,
  type AnomalyType,
  type TwinAlert,
} from '../types/anomaly'
import { Button, Chip, Panel, PanelHeading, Spec } from '../ui/primitives'
import { levelTone } from './anomalyUtils'

const pct = (c: number | null | undefined): string => (c === null || c === undefined ? '—' : `${Math.round(c * 100)}%`)

function ago(iso: string | null, now: number): string {
  if (!iso) return '—'
  const s = Math.max(0, (now - Date.parse(iso)) / 1000)
  if (s < 90) return `${Math.round(s)} s`
  if (s < 5400) return `${Math.round(s / 60)} min`
  if (s < 172_800) return `${(s / 3600).toFixed(1)} h`
  return `${Math.round(s / 86_400)} d`
}

function fmtValue(v: number | null | undefined, unit?: string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (unit === 'B/s') {
    for (const [u, d] of [['GB/s', 1e9], ['MB/s', 1e6], ['KB/s', 1e3]] as const) if (Math.abs(v) >= d) return `${(v / d).toFixed(1)} ${u}`
    return `${v.toFixed(0)} B/s`
  }
  const digits = Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : 2
  return `${v.toFixed(digits)}${unit === '%' ? '%' : unit ? ` ${unit}` : ''}`
}

// ------------------------------------------------------------------------- twin page panel
const AlertRow = memo(function AlertRow({ a, now }: { a: TwinAlert; now: number }) {
  return (
    <button type="button" className={`anomaly-row anomaly-row--${levelTone(a.level)}`} onClick={() => navigate('health', a.anomaly_id)}
      title={`${typeLabel(a.type)} · confidence ${pct(a.confidence)}`}>
      <span className={`anomaly-level anomaly-level--${a.level.toLowerCase()}`}>{a.level}</span>
      <span className="anomaly-row__title">{a.title}</span>
      <span className="anomaly-row__meta">{typeLabel(a.type)} · {pct(a.confidence)} · {ago(a.since, now)}{a.correlation_key ? ' · correlated' : ''}</span>
    </button>
  )
})

/** Active anomalies of the twin on screen; re-renders only when ``alerts.*`` changes in the twin. */
export function ActiveAnomaliesPanel() {
  const active = useTwinValue<TwinAlert[]>('alerts.active') ?? []
  const highest = useTwinValue<AnomalyLevel | null>('alerts.highest_severity') ?? null
  const recent = useTwinValue<TwinAlert[]>('alerts.recent') ?? []
  const now = useNow(30_000)
  return (
    <Panel className="twin-side">
      <PanelHeading eyebrow="DEVICE-SPECIFIC DETECTION" title="Anomalies"
        right={<Chip tone={highest ? levelTone(highest) : 'accent'}>{active.length ? `${active.length} ACTIVE · ${highest}` : 'NONE ACTIVE'}</Chip>} />
      {active.length === 0 ? <p className="note">Nothing unusual for this device right now. Safety thresholds and learned baselines are evaluated continuously.</p> : null}
      <div className="anomaly-list">{active.map((a) => <AlertRow key={a.anomaly_id} a={a} now={now} />)}</div>
      {recent.length ? (
        <>
          <p className="eyebrow">RECENTLY CLOSED</p>
          <div className="anomaly-list anomaly-list--muted">{recent.map((a) => <AlertRow key={a.anomaly_id} a={a} now={now} />)}</div>
        </>
      ) : null}
      <Button icon="arrowRight" onClick={() => navigate('health')} style={{ alignSelf: 'flex-start' }}>Anomaly history</Button>
    </Panel>
  )
}

// ----------------------------------------------------------------------- expected range chart
/** Observed value against the device's usual range (p05-p95), median and trigger level. */
export function ExpectedBand({ observed, p05, p95, median, trigger, unit }: {
  observed: number; p05: number; p95: number; median: number; trigger?: number | null; unit?: string
}) {
  const lo = Math.min(p05, observed, median)
  const hi = Math.max(p95, observed, trigger ?? p95, median)
  const pad = (hi - lo) * 0.08 || 1
  const min = lo - pad
  const max = hi + pad
  const x = (v: number) => `${((v - min) / (max - min)) * 100}%`
  return (
    <div className="expected-band" role="img"
      aria-label={`Observed ${fmtValue(observed, unit)}; usual range ${fmtValue(p05, unit)} to ${fmtValue(p95, unit)}`}>
      <div className="expected-band__track">
        <div className="expected-band__range" style={{ left: x(p05), width: `calc(${x(p95)} - ${x(p05)})` }} />
        <div className="expected-band__median" style={{ left: x(median) }} />
        {trigger !== null && trigger !== undefined ? <div className="expected-band__trigger" style={{ left: x(trigger) }} /> : null}
        <div className="expected-band__observed" style={{ left: x(observed) }} />
      </div>
      <div className="expected-band__legend">
        <span><i className="swatch swatch--range" /> usual {fmtValue(p05, unit)}–{fmtValue(p95, unit)}</span>
        <span><i className="swatch swatch--median" /> median {fmtValue(median, unit)}</span>
        {trigger !== null && trigger !== undefined ? <span><i className="swatch swatch--trigger" /> trigger {fmtValue(trigger, unit)}</span> : null}
        <span><i className="swatch swatch--observed" /> observed {fmtValue(observed, unit)}</span>
      </div>
    </div>
  )
}

function FactorBar({ label, value }: { label: string; value: number }) {
  return (
    <div className="factor">
      <span className="factor__label">{label.replace('_', ' ')}</span>
      <span className="factor__track"><span className="factor__fill" style={{ width: `${Math.round(value * 100)}%` }} /></span>
      <span className="factor__value">{value.toFixed(2)}</span>
    </div>
  )
}

const LIFECYCLE_TEXT: Record<string, string> = {
  DETECTED: 'Detected', ONGOING: 'Ongoing', ACKNOWLEDGED: 'Acknowledged', RESOLVED: 'Resolved',
  SUPPRESSED: 'Suppressed', EXPIRED: 'Expired (no current data)',
}

// ---------------------------------------------------------------------------- detail view
export function AnomalyDetailView({ id }: { id: string }) {
  const fetched = useApi(() => api.anomaly(id), [id])
  const live = useAnomalyRecord(id)
  const me = useSession((s) => s.me)
  const now = useNow(15_000)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const a: AnomalyRecord | null = useMemo(() => {
    if (!fetched.data) return live ?? null
    return live && (live.updated_at ?? '') > (fetched.data.updated_at ?? '') ? { ...fetched.data, ...live } : fetched.data
  }, [fetched.data, live])

  if (!a) {
    return (
      <Panel style={{ flex: '868 0 0' }}>
        <PanelHeading eyebrow="ANOMALY" title={fetched.error ? 'Anomaly unavailable' : 'Loading…'} />
        {fetched.error ? <p className="note">{fetched.error}</p> : null}
      </Panel>
    )
  }
  const ev = a.evidence ?? {}
  const unit = ev.observed?.unit
  const exp = ev.expected ?? {}
  const factors = ev.confidence_factors ?? {}
  const sev = ev.severity
  const ack = a.acknowledgement
  const feedback = async (verdict: 'true_positive' | 'false_positive' | 'unsure') => {
    setBusy(true)
    try {
      await api.anomalyFeedback(a.anomaly_id, verdict)
      setMsg('Feedback saved. Thank you — it is used to evaluate detection quality.')
      fetched.reload()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  const ackToggle = async () => {
    setBusy(true)
    try {
      if (ack) await api.unacknowledge(a.anomaly_id)
      else await api.acknowledge(a.anomaly_id)
      fetched.reload()
    } finally {
      setBusy(false)
    }
  }
  const showBand = typeof ev.observed?.value === 'number' && typeof exp.p05 === 'number' && typeof exp.p95 === 'number' && typeof exp.median === 'number'

  return (
    <Panel style={{ flex: '868 0 0' }} className="anomaly-detail">
      <PanelHeading eyebrow={`${typeLabel(a.anomaly_type).toUpperCase()} / ${(a.category ?? a.component_id).toUpperCase()}`} title={a.title}
        right={<div className="anomaly-detail__chips">
          <Chip tone={levelTone(a.level)} title={sev ? `${sev.points} severity points` : undefined}>{a.level}</Chip>
          <Chip tone="muted" title={ev.confidence_factors ? Object.entries(factors).map(([k, v]) => `${k} ${v}`).join(' · ') : undefined}>
            CONFIDENCE {pct(a.confidence)}{a.confidence_band ?? ev.confidence_band ? ` · ${a.confidence_band ?? ev.confidence_band}` : ''}
          </Chip>
        </div>} />
      <p className="anomaly-detail__summary">{a.message}</p>
      {ev.summary && ev.summary !== a.message ? <p className="note">{ev.summary}</p> : null}
      {ev.note ? <p className="note note--muted">{ev.note}</p> : null}

      {showBand ? (
        <ExpectedBand observed={ev.observed!.value as number} p05={exp.p05 as number} p95={exp.p95 as number} median={exp.median as number}
          trigger={typeof exp.upper_trigger === 'number' ? exp.upper_trigger : null} unit={unit} />
      ) : null}

      <div className="anomaly-detail__grid">
        <Spec label="Observed (2-min average)" value={typeof ev.observed?.value === 'number' ? fmtValue(ev.observed.value, unit) : String(a.value ?? '—')} />
        <Spec label="Usually" value={typeof exp.p05 === 'number' && typeof exp.p95 === 'number' ? `${fmtValue(exp.p05, unit)}–${fmtValue(exp.p95, unit)}` : exp.threshold !== undefined ? `threshold ${String(exp.threshold)}` : '—'} />
        <Spec label="Deviation" value={a.deviation_score !== null ? (a.anomaly_type === 'multivariate_anomaly' ? `+${a.deviation_score.toFixed(3)} score` : a.anomaly_type === 'volatility_anomaly' ? `${a.deviation_score.toFixed(1)}× usual` : `${a.deviation_score.toFixed(1)} σ (robust)`) : '—'} />
        <Spec label="Duration" value={ago(a.started_at, a.resolved_at ? Date.parse(a.resolved_at) : now)} />
        <Spec label="Status" value={LIFECYCLE_TEXT[ack && a.status === 'active' ? 'ACKNOWLEDGED' : a.lifecycle] ?? a.lifecycle} />
        <Spec label="Occurrences" value={a.occurrences} title="A recurrence within the cooldown re-opens the same anomaly" />
      </div>
      <DiagnosisPanel kind="anomaly" id={a.anomaly_id} />

      <div className="anomaly-detail__columns">
        <div>
          <p className="eyebrow">HOW IT WAS DETECTED</p>
          <ul className="anomaly-methods">
            {(ev.methods ?? []).map((m) => <li key={m.id}>{m.text}</li>)}
          </ul>
          {ev.baseline ? (
            <p className="note">
              Baseline: {ev.baseline.status ?? (ev.baseline.model_id ? 'model' : '—')}
              {ev.baseline.context ? ` · context ${contextText(ev.baseline.context)}` : ''}
              {ev.baseline.sample_count ? ` · ${ev.baseline.sample_count.toLocaleString()} learned minutes` : ''}
              {ev.baseline.model_id ? ` · ${ev.baseline.model_id} (${ev.baseline.trained_samples?.toLocaleString()} minutes)` : ''}
            </p>
          ) : null}
          {ev.contributions?.length ? (
            <>
              <p className="eyebrow">WHAT MAKES IT UNUSUAL</p>
              {ev.contributions.slice(0, 4).map((c) => <FactorBar key={c.signal_id} label={c.title} value={Math.min(1, c.robust_deviation / 10)} />)}
            </>
          ) : null}
          {a.related?.length ? (
            <>
              <p className="eyebrow">RELATED SIGNALS (SAME TIME WINDOW)</p>
              <table className="anomaly-related">
                <tbody>
                  {a.related.map((r) => (
                    <tr key={r.signal_id} className={r.abnormal ? 'is-abnormal' : ''}>
                      <td>{r.title}</td><td>{fmtValue(r.value, r.unit)}</td><td>usual {fmtValue(r.expected, r.unit)}</td><td>{r.abnormal ? 'abnormal' : 'normal'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : null}
          {ev.incident ? <p className="note">{ev.incident.title}: {ev.incident.signals.join(', ')} — {ev.incident.wording ?? 'abnormal at the same time'}.</p> : null}
          {ev.process_context?.processes?.length ? (
            <>
              <p className="eyebrow">ASSOCIATED PROCESSES</p>
              <p className="note note--muted">Observed at the same time; not established as the cause.</p>
              {ev.process_context.processes.map((p, i) => (
                <p key={`${p.name}-${i}`} className="cause-list__row"><span>{p.name ?? 'process'}</span><span>{p.cpu_percent !== null ? `${p.cpu_percent.toFixed(1)}% CPU` : ''}</span></p>
              ))}
            </>
          ) : null}
        </div>
        <div>
          <p className="eyebrow">CONFIDENCE (EVIDENCE-BASED)</p>
          {Object.entries(factors).map(([k, v]) => <FactorBar key={k} label={k} value={v} />)}
          {Object.keys(factors).length === 0 ? <p className="note">Safety thresholds are deterministic limits (fixed high confidence).</p> : null}
          {sev ? (
            <>
              <p className="eyebrow">SEVERITY ({sev.points} POINTS)</p>
              <ul className="anomaly-methods">
                {sev.breakdown.map((b, i) => <li key={`${b.factor}-${i}`}><b>{b.points > 0 ? `+${b.points}` : b.points}</b> {b.factor}: {b.reason}</li>)}
              </ul>
            </>
          ) : null}
          <p className="eyebrow">TIMELINE</p>
          <ul className="anomaly-methods">
            <li>Started {new Date(a.started_at).toLocaleString()}</li>
            <li>Last seen {new Date(a.last_seen_at).toLocaleString()}</li>
            {a.resolved_at ? <li>{a.lifecycle === 'EXPIRED' ? 'Expired' : 'Resolved'} {new Date(a.resolved_at).toLocaleString()}{ev.closed_because ? ` — ${ev.closed_because}` : ''}</li> : null}
          </ul>
        </div>
      </div>

      <div className="recommendation">
        <p className="recommendation__label">OPERATOR FEEDBACK</p>
        <p className="note">Was this a real problem? Feedback measures detection quality; a false positive mutes this detector on this device for an hour and keeps the period in the learned baseline.</p>
        <div className="recommendation__actions">
          <Button icon="check" disabled={busy || !canOperate(me)} active={a.feedback?.verdict === 'true_positive'} onClick={() => void feedback('true_positive')}>Real issue</Button>
          <Button disabled={busy || !canOperate(me)} active={a.feedback?.verdict === 'false_positive'} onClick={() => void feedback('false_positive')}>False positive</Button>
          <Button disabled={busy || !canOperate(me)} active={a.feedback?.verdict === 'unsure'} onClick={() => void feedback('unsure')}>Unsure</Button>
          <Button disabled={busy || !canOperate(me)} active={Boolean(ack)} onClick={() => void ackToggle()}
            title={canOperate(me) ? undefined : 'Operator role required'}>{ack ? 'Acknowledged' : 'Acknowledge'}</Button>
        </div>
        {a.feedback ? <p className="note note--muted">Feedback: {a.feedback.verdict.replace('_', ' ')} by {a.feedback.by}.</p> : null}
        {msg ? <p className="note">{msg}</p> : null}
      </div>
    </Panel>
  )
}

function contextText(ctx: string): string {
  const m = /^how:(wd|we):(\d\d)$/.exec(ctx)
  if (m) return `${m[1] === 'wd' ? 'weekdays' : 'weekends'} ${m[2]}:00–${m[2]}:59`
  if (ctx === 'dt:wd') return 'weekdays'
  if (ctx === 'dt:we') return 'weekends'
  return 'all times'
}

// ------------------------------------------------------------------------------- history
const RANGES: { id: string; label: string; hours: number | null }[] = [
  { id: '24h', label: '24 h', hours: 24 },
  { id: '7d', label: '7 days', hours: 168 },
  { id: '30d', label: '30 days', hours: 720 },
  { id: 'all', label: 'All', hours: null },
]

export function AnomalyHistoryPanel({ deviceId, selectedId, onSelect }: { deviceId: string; selectedId: string | null; onSelect: (id: string) => void }) {
  const [levels, setLevels] = useState<AnomalyLevel[]>([])
  const [type, setType] = useState<AnomalyType | ''>('')
  const [status, setStatus] = useState<'' | 'active' | 'resolved'>('')
  const [range, setRange] = useState('7d')
  const [minConf, setMinConf] = useState<number | ''>('')
  const revision = useAnomalies((s) => s.revision)
  const now = useNow(30_000)
  const filters: AnomalyFilters = useMemo(() => ({
    level: levels.length ? levels : undefined,
    type: type ? [type] : undefined,
    status: status || undefined,
    min_confidence: minConf === '' ? undefined : minConf,
    limit: 100,
  }), [levels, type, status, minConf])
  const hours = RANGES.find((r) => r.id === range)?.hours ?? null
  const page = useApi(
    () => api.deviceAnomalies(deviceId, { ...filters, since: hours ? new Date(Date.now() - hours * 3_600_000).toISOString() : undefined }),
    [deviceId, filters, hours, revision],
    60_000,
  )
  const items = useMemo(() => page.data?.items ?? [], [page.data])
  const toggle = (lv: AnomalyLevel) => setLevels((cur) => (cur.includes(lv) ? cur.filter((x) => x !== lv) : [...cur, lv]))

  useEffect(() => {
    if (!selectedId && items[0]) onSelect(items[0].anomaly_id)
  }, [items, selectedId, onSelect])

  return (
    <Panel className="side-panel" style={{ flex: '424 0 0' }}>
      <PanelHeading title="Anomaly history" right={<p className="panel-meta">{items.length}{items.length === 100 ? '+' : ''} SHOWN</p>} />
      <div className="anomaly-filters" role="group" aria-label="Filters">
        <div className="anomaly-filters__levels">
          {LEVELS.map((lv) => (
            <button key={lv} type="button" aria-pressed={levels.includes(lv)} className={`filter-chip ${levels.includes(lv) ? 'is-on' : ''}`} onClick={() => toggle(lv)}>{lv}</button>
          ))}
        </div>
        <select aria-label="Type" value={type} onChange={(e) => setType(e.target.value as AnomalyType | '')}>
          <option value="">All types</option>
          {TYPES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
        </select>
        <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value as '' | 'active' | 'resolved')}>
          <option value="">Active and closed</option>
          <option value="active">Active</option>
          <option value="resolved">Closed</option>
        </select>
        <select aria-label="Date range" value={range} onChange={(e) => setRange(e.target.value)}>
          {RANGES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
        </select>
        <select aria-label="Minimum confidence" value={minConf} onChange={(e) => setMinConf(e.target.value === '' ? '' : Number(e.target.value))}>
          <option value="">Any confidence</option>
          <option value={0.4}>≥ 40% (moderate)</option>
          <option value={0.7}>≥ 70% (high)</option>
          <option value={0.9}>≥ 90% (very high)</option>
        </select>
      </div>
      {page.error ? <p className="note">History unavailable: {page.error}</p> : null}
      {!page.error && items.length === 0 && !page.loading ? <p className="note">No anomalies match these filters.</p> : null}
      <div className="anomaly-list">
        {items.map((a) => (
          <button key={a.anomaly_id} type="button" className={`anomaly-row anomaly-row--${levelTone(a.level)} ${selectedId === a.anomaly_id ? 'is-selected' : ''}`} onClick={() => onSelect(a.anomaly_id)}>
            <span className={`anomaly-level anomaly-level--${a.level.toLowerCase()}`}>{a.level}</span>
            <span className="anomaly-row__title">{a.title}</span>
            <span className="anomaly-row__meta">
              {typeLabel(a.anomaly_type)} · {pct(a.confidence)} · {a.status === 'active' ? `active ${ago(a.started_at, now)}` : `${(a.lifecycle || 'resolved').toLowerCase()} · ${new Date(a.started_at).toLocaleDateString()} ${new Date(a.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`}
            </span>
          </button>
        ))}
      </div>
    </Panel>
  )
}

/** Detection status of the device: graceful degradation is visible, not silent. */
export function DetectionStatus({ deviceId }: { deviceId: string }) {
  const revision = useAnomalies((s) => s.revision)
  const summary = useApi(() => api.anomalySummary(deviceId), [deviceId, revision], 60_000)
  const s = summary.data
  if (!s) return null
  const statuses = Object.values(s.baseline_status)
  const count = (st: string) => statuses.filter((x) => x === st).length
  return (
    <p className="eyebrow anomaly-status" title={s.detection.reason}>
      DETECTION: {s.detection.mode.replace('_', ' ').toUpperCase()} · BASELINES {count('STABLE')} STABLE / {count('DEVELOPING')} DEVELOPING / {count('COLD')} COLD
      {count('DEGRADED') ? ` / ${count('DEGRADED')} DEGRADED` : ''} · LAST EVALUATED {s.last_evaluated ? new Date(s.last_evaluated).toLocaleTimeString() : '—'}
    </p>
  )
}
