import { memo, useMemo, useState } from 'react'

import { useCurrentDeviceId } from '../anomaly/anomalyUtils'
import { navigate } from '../app/routes'
import { DiagnosisPanel } from '../diagnosis/DiagnosisPanel'
import { useApi } from '../hooks/useApi'
import { useNow } from '../hooks/useNow'
import { api } from '../services/api'
import { usePredictionEvents } from '../stores/predictionStore'
import { useTwinValue } from '../stores/twinDocStore'
import type { ForecastCurve, TargetForecast, TargetId, TwinPrediction } from '../types/prediction'
import { TARGET_ORDER } from '../types/prediction'
import { Chip, Panel, PanelHeading, Spec, type ChipTone } from '../ui/primitives'
import { fmtDuration, healthTone, severityTone } from './predictionUtils'

// ------------------------------------------------------------------------------- formatting
function fmtValue(v: number | null | undefined, unit?: string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${v.toFixed(Math.abs(v) >= 100 ? 0 : 1)}${unit === '%' ? '%' : unit ? ` ${unit}` : ''}`
}

function untilText(iso: string | null | undefined, now: number): number | null {
  return iso ? Math.max(0, (Date.parse(iso) - now) / 1000) : null
}

const STATUS_TEXT: Record<string, string> = {
  AVAILABLE: 'Forecast available',
  INSUFFICIENT_HISTORY: 'Not enough history yet',
  LOW_CONFIDENCE: 'Low confidence',
  UNSTABLE: 'Trend too unstable',
  NOT_APPLICABLE: 'Not applicable',
  STALE_DATA: 'Stale data',
}

// --------------------------------------------------------------------------- twin page panel
const TwinPredictionRow = memo(function TwinPredictionRow({ id }: { id: TargetId }) {
  const p = useTwinValue<TwinPrediction>(`predictions.${id}`)
  const now = useNow(30_000)
  if (!p) return null
  const eta = untilText(p.crossing_at, now)
  const early = untilText(p.crossing_earliest, now)
  const late = untilText(p.crossing_latest, now)
  const hasPrediction = Boolean(p.prediction_id)
  return (
    <button type="button" className={`forecast-row forecast-row--${hasPrediction ? severityTone(p.severity) : 'muted'}`}
      onClick={() => navigate('analytics', `forecast:${id}`)} title={p.statement ?? p.reason}>
      <span className="forecast-row__title">{p.title}</span>
      {hasPrediction && eta !== null ? (
        <span className="forecast-row__eta">
          <span className="forecast-row__label">Estimated</span> {p.threshold}{p.unit === '%' ? '%' : ` ${p.unit}`} in ~{fmtDuration(eta)}
          {early !== null ? <span className="forecast-row__range"> (likely {fmtDuration(early)}–{late !== null ? fmtDuration(late) : 'later'})</span> : null}
        </span>
      ) : (
        <span className="forecast-row__eta forecast-row__eta--muted">{STATUS_TEXT[p.status] ?? p.status}</span>
      )}
      <span className="forecast-row__meta">
        {hasPrediction ? `Confidence ${p.confidence_band?.toLowerCase()} · ${p.severity ?? 'no'} severity` : p.reason}
      </span>
    </button>
  )
})

/** Predictive insights on the twin page; each row re-renders only when its predictions.<target> changes. */
export function PredictiveInsightsPanel() {
  const count = useTwinValue<number>('predictions.active_count') ?? 0
  const highest = useTwinValue<string | null>('predictions.highest_severity') ?? null
  return (
    <Panel className="twin-side">
      <PanelHeading eyebrow="PREDICTED · BASED ON RECENT TRENDS" title="Predictive insights"
        right={<Chip tone={count ? severityTone(highest) : 'accent'}>{count ? `${count} FORECAST${count > 1 ? 'S' : ''}` : 'NO WARNINGS'}</Chip>} />
      <p className="note note--muted">Estimates, not guarantees. Observed values and anomalies are shown separately.</p>
      <div className="forecast-list">{TARGET_ORDER.map((id) => <TwinPredictionRow key={id} id={id} />)}</div>
    </Panel>
  )
}

// ---------------------------------------------------------------------------- forecast chart
interface ChartProps { curve: ForecastCurve; threshold?: number | null; unit?: string; crossingAt?: string | null }

/** Actual (solid) vs forecast (dashed + shaded likely range), threshold, "now" divider; one y axis. */
export function ForecastChart({ curve, threshold, unit, crossingAt }: ChartProps) {
  const [hover, setHover] = useState<{ x: number; text: string } | null>(null)
  const W = 600
  const H = 200
  const PAD = { l: 40, r: 10, t: 10, b: 22 }
  const hist = curve.history.map(([t, v]) => ({ t: t * 1000, v }))
  const fc = curve.forecast.map((p) => ({ t: p.t * 1000, mean: p.mean, lower: p.lower, upper: p.upper }))
  const ts = [...hist.map((p) => p.t), ...fc.map((p) => p.t)]
  const vs = [...hist.map((p) => p.v), ...fc.flatMap((p) => [p.lower, p.upper]), ...(threshold != null ? [threshold] : [])]
  if (!ts.length) return <p className="note">No data to chart yet.</p>
  const t0 = Math.min(...ts)
  const t1 = Math.max(...ts)
  let v0 = Math.min(...vs)
  let v1 = Math.max(...vs)
  const padV = (v1 - v0) * 0.08 || 1
  v0 -= padV
  v1 += padV
  const x = (t: number) => PAD.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - PAD.l - PAD.r)
  const y = (v: number) => PAD.t + (1 - (v - v0) / Math.max(1e-9, v1 - v0)) * (H - PAD.t - PAD.b)
  const line = (pts: { t: number; v: number }[]) => pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ')
  const band = fc.length
    ? `${fc.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.upper).toFixed(1)}`).join(' ')} ${[...fc].reverse().map((p) => `L${x(p.t).toFixed(1)},${y(p.lower).toFixed(1)}`).join(' ')} Z`
    : ''
  const nowT = hist.length ? hist[hist.length - 1].t : t0
  const cross = crossingAt ? Date.parse(crossingAt) : null
  const fmtT = (t: number) => (t1 - t0 > 2 * 86_400_000
    ? new Date(t).toLocaleDateString([], { day: '2-digit', month: 'short' })
    : new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
  const ticksY = [v0 + padV, (v0 + v1) / 2, v1 - padV]

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const px = ((e.clientX - rect.left) / rect.width) * W
    const t = t0 + ((px - PAD.l) / (W - PAD.l - PAD.r)) * (t1 - t0)
    if (t <= nowT && hist.length) {
      const p = hist.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a))
      setHover({ x: x(p.t), text: `Actual ${fmtValue(p.v, unit)} · ${fmtT(p.t)}` })
    } else if (fc.length) {
      const p = fc.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a))
      setHover({ x: x(p.t), text: `Forecast ${fmtValue(p.mean, unit)} (likely ${fmtValue(p.lower, unit)}–${fmtValue(p.upper, unit)}) · ${fmtT(p.t)}` })
    }
  }

  return (
    <div className="forecast-chart">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        aria-label={`Actual values until now and a forecast with its likely range${threshold != null ? `; threshold ${threshold}` : ''}`}>
        {ticksY.map((v) => <line key={v} x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} className="forecast-chart__grid" />)}
        {band ? <path d={band} className="forecast-chart__band" /> : null}
        {threshold != null ? <line x1={PAD.l} x2={W - PAD.r} y1={y(threshold)} y2={y(threshold)} className="forecast-chart__threshold" /> : null}
        <line x1={x(nowT)} x2={x(nowT)} y1={PAD.t} y2={H - PAD.b} className="forecast-chart__now" />
        <path d={line(hist)} className="forecast-chart__actual" />
        {fc.length ? <path d={line(fc.map((p) => ({ t: p.t, v: p.mean })))} className="forecast-chart__forecast" /> : null}
        {cross && threshold != null && cross >= t0 && cross <= t1 ? <circle cx={x(cross)} cy={y(threshold)} r={5} className="forecast-chart__cross" /> : null}
        {hover ? <line x1={hover.x} x2={hover.x} y1={PAD.t} y2={H - PAD.b} className="forecast-chart__hover" /> : null}
      </svg>
      <div className="forecast-chart__axis">
        <span>{fmtT(t0)}</span><span>now</span><span>{fmtT(t1)}</span>
      </div>
      <div className="forecast-chart__yaxis">{ticksY.slice().reverse().map((v) => <span key={v}>{fmtValue(v, unit)}</span>)}</div>
      <div className="forecast-chart__legend">
        <span><i className="sw sw--actual" /> Actual (observed)</span>
        <span><i className="sw sw--forecast" /> Forecast (expected)</span>
        <span><i className="sw sw--band" /> Likely range (80 %)</span>
        {threshold != null ? <span><i className="sw sw--threshold" /> Threshold {fmtValue(threshold, unit)}</span> : null}
        {cross ? <span><i className="sw sw--cross" /> Estimated crossing</span> : null}
      </div>
      <p className="forecast-chart__tooltip" aria-live="polite">{hover?.text ?? ' '}</p>
    </div>
  )
}

// ------------------------------------------------------------------------- detail + section
function ForecastDetail({ deviceId, item }: { deviceId: string; item: TargetForecast }) {
  const revision = usePredictionEvents((s) => s.revision)
  const curve = useApi(() => api.forecastCurve(deviceId, item.target_id), [deviceId, item.target_id, revision], 60_000)
  const now = useNow(30_000)
  const p = item.prediction
  const ctx = (curve.data?.context ?? {}) as Record<string, unknown>
  const factors = p?.evidence.confidence?.factors ?? {}
  const sel = ctx.model_selection as { model?: string; mae?: Record<string, number>; reason?: string } | undefined
  const anomalies = (ctx.anomalies as { title: string; level: string }[] | undefined) ?? []
  const eta = untilText(p?.crossing_at, now)
  return (
    <Panel style={{ flex: '868 0 0' }} className="forecast-detail">
      <PanelHeading eyebrow={`PREDICTED / ${item.title.toUpperCase()}`} title={p ? p.statement.split(':')[0] : item.title}
        right={<div className="anomaly-detail__chips">
          <Chip tone={healthTone(item.health)}>{item.health}</Chip>
          {p ? <Chip tone={severityTone(p.severity)}>{p.severity ?? 'NO'} SEVERITY</Chip> : null}
        </div>} />
      <p className="anomaly-detail__summary">{p?.statement ?? item.reason}</p>
      <p className="note note--muted">Estimate based on the recent trend; not a guarantee. {p?.evidence.impact ? `Why it matters: ${p.evidence.impact}.` : ''}</p>
      {curve.data ? <ForecastChart curve={curve.data} threshold={p?.threshold ?? item.threshold ?? null} unit={item.unit} crossingAt={p?.crossing_at ?? item.crossing_at ?? null} />
        : <p className="note">{curve.error ? `Forecast chart unavailable: ${curve.error}` : 'Loading forecast…'}</p>}
      <div className="anomaly-detail__grid">
        <Spec label="Observed now" value={fmtValue(item.current_value, item.unit)} />
        <Spec label="Threshold" value={fmtValue(p?.threshold ?? item.threshold, item.unit)} />
        <Spec label="Estimated time" value={eta !== null ? `~${fmtDuration(eta)}` : '—'} />
        <Spec label="Likely window" value={p?.crossing_earliest ? `${fmtDuration(untilText(p.crossing_earliest, now) ?? 0)}–${p.crossing_latest ? fmtDuration(untilText(p.crossing_latest, now) ?? 0) : 'later'}` : '—'} />
        <Spec label="Confidence" value={p ? `${p.confidence_band} (${Math.round(p.confidence * 100)}%)` : item.confidence_band ?? '—'} />
        <Spec label="Expected in horizon" value={item.forecast?.expected != null ? `${fmtValue(item.forecast.lower, item.unit)}–${fmtValue(item.forecast.upper, item.unit)} in ${fmtDuration(item.forecast.horizon_s)}` : '—'} />
      </div>
      {p?.prediction_id ? <DiagnosisPanel kind="prediction" id={p.prediction_id} compact /> : null}
      <div className="anomaly-detail__columns">
        <div>
          <p className="eyebrow">MODEL</p>
          <ul className="anomaly-methods">
            <li>{p?.model_type ?? item.model ?? '—'} ({p?.model_version ?? item.model_version ?? '—'}), features {p?.feature_version ?? 'features-v1'}</li>
            {sel?.reason ? <li>{sel.reason}</li> : null}
            {sel?.mae && Object.keys(sel.mae).length ? <li>recent error at the horizon: {Object.entries(sel.mae).map(([m, v]) => `${m} ${v}`).join(' · ')}</li> : null}
          </ul>
          <p className="eyebrow">DATA QUALITY</p>
          <ul className="anomaly-methods">
            <li>{fmtDuration(Number(ctx.history_span_s ?? 0))} of usable history ({String(ctx.buckets ?? '—')} points of {String(ctx.bucket_s ?? '—')} s)</li>
            <li>coverage {Math.round(Number(ctx.coverage ?? 0) * 100)}% · largest gap {fmtDuration(Number(ctx.largest_gap_s ?? 0))} · newest sample {fmtDuration(Number(ctx.newest_age_s ?? 0))} old</li>
            {ctx.regime_change ? <li>regime change detected (history before it is not used)</li> : null}
          </ul>
          {anomalies.length ? (
            <>
              <p className="eyebrow">RELATED ANOMALIES (CONTEXT, NOT A FORECAST INPUT)</p>
              <ul className="anomaly-methods">{anomalies.map((a) => <li key={a.title}>{a.level}: {a.title}</li>)}</ul>
            </>
          ) : null}
        </div>
        <div>
          <p className="eyebrow">CONFIDENCE FACTORS</p>
          {Object.keys(factors).length ? Object.entries(factors).map(([k, v]) => (
            <div key={k} className="factor"><span className="factor__label">{k.replace('_', ' ')}</span>
              <span className="factor__track"><span className="factor__fill" style={{ width: `${Math.round(v * 100)}%` }} /></span>
              <span className="factor__value">{v.toFixed(2)}</span></div>
          )) : <p className="note">No active prediction for this metric.</p>}
          <p className="eyebrow">LIFECYCLE</p>
          <ul className="anomaly-methods">
            {p ? <li>{p.status} · created {new Date(p.created_at).toLocaleString()} · {p.revisions} revision(s)</li> : <li>{STATUS_TEXT[item.status] ?? item.status}: {item.reason}</li>}
            {p ? <li>last updated {fmtDuration((now - Date.parse(p.updated_at)) / 1000)} ago</li> : null}
          </ul>
        </div>
      </div>
    </Panel>
  )
}

/** Analytics page section: every forecast target with its status, and the detail of one. */
export function PredictiveSection({ initial }: { initial: TargetId | null }) {
  const deviceId = useCurrentDeviceId()
  const revision = usePredictionEvents((s) => s.revision)
  const data = useApi(() => (deviceId ? api.devicePredictions(deviceId) : Promise.resolve(null)), [deviceId, revision], 30_000)
  const [selected, setSelected] = useState<TargetId | null>(initial)
  const items = useMemo(() => data.data?.targets ?? [], [data.data])
  const current = items.find((t) => t.target_id === selected) ?? items.find((t) => t.prediction) ?? items[0]
  const now = useNow(30_000)
  if (!deviceId) return null
  return (
    <div className="split split--rev">
      <Panel className="side-panel" style={{ flex: '424 0 0' }}>
        <PanelHeading eyebrow="PREDICTED · ESTIMATES, NOT GUARANTEES" title="Predictive insights" />
        {data.error ? <p className="note">Forecasts unavailable: {data.error}</p> : null}
        <div className="forecast-list">
          {items.map((t) => {
            const eta = untilText(t.prediction?.crossing_at, now)
            const tone: ChipTone = t.prediction ? severityTone(t.prediction.severity) : 'muted'
            return (
              <button key={t.target_id} type="button" className={`forecast-row forecast-row--${tone} ${current?.target_id === t.target_id ? 'is-selected' : ''}`}
                onClick={() => setSelected(t.target_id)}>
                <span className="forecast-row__title">{t.title}</span>
                <span className={`forecast-row__eta ${t.prediction ? '' : 'forecast-row__eta--muted'}`}>
                  {t.prediction && eta !== null
                    ? <>May reach {fmtValue(t.prediction.threshold, t.unit)} in ~{fmtDuration(eta)} · confidence {t.prediction.confidence_band.toLowerCase()}</>
                    : STATUS_TEXT[t.status] ?? t.status}
                </span>
                <span className="forecast-row__meta">{t.prediction ? `Based on recent trend · ${t.prediction.model_type}` : t.reason}</span>
              </button>
            )
          })}
        </div>
      </Panel>
      {current ? <ForecastDetail key={current.target_id} deviceId={deviceId} item={current} /> : (
        <Panel style={{ flex: '868 0 0' }}><PanelHeading eyebrow="PREDICTED" title="No forecast yet" /></Panel>
      )}
    </div>
  )
}
