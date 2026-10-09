import { useMemo, useState } from 'react'

import { useCurrentDeviceId } from '../anomaly/anomalyUtils'
import { AnomalyDetailView, AnomalyHistoryPanel, DetectionStatus } from '../anomaly/AnomalyViews'
import { utcClock } from '../app/derive'
import { exportJson } from '../app/exportData'
import { healthLabel, healthTone, recommendationFor, severityLabel } from '../app/liveData'
import { navigate, useRoute } from '../app/routes'
import { useApi } from '../hooks/useApi'
import { stats, useHistorySeries } from '../hooks/useSeries'
import { api } from '../services/api'
import { obj, useInventory } from '../hooks/useInventory'
import { useNow } from '../hooks/useNow'
import { canOperate, useSession } from '../stores/sessionStore'
import { useTwinStore } from '../stores/twinStore'
import type { Anomaly, ComponentType, TwinComponent } from '../types/telemetry'
import { readingOf, readingsOf } from '../utils/twin'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, Panel, PanelHeading, Spec, Track } from '../ui/primitives'
import { fmtHm, timeAxis } from '../ui/time'
import { TimeSeries } from '../ui/TimeSeries'

const BREAKDOWN: { label: string; type: ComponentType }[] = [
  { label: 'CPU', type: 'cpu' },
  { label: 'GPU', type: 'gpu' },
  { label: 'Memory', type: 'memory' },
  { label: 'Storage', type: 'storage' },
  { label: 'Battery', type: 'battery' },
  { label: 'Cooling', type: 'thermal_sensors' },
]

type AckInfo = NonNullable<Anomaly['acknowledgement']>

function AnomalyDetail({ a, ack, canAck, onAck }: { a: Anomaly; ack: AckInfo | null; canAck: boolean; onAck: () => void }) {
  const analysis = useApi(() => api.anomalyAnalysis(a.anomaly_id), [a.anomaly_id], 30_000)
  const components = useTwinStore((s) => s.components)
  const now = useNow(15_000)
  const startedAt = Date.parse(a.started_at)
  const minutes = Math.max(10, Math.ceil((now - startedAt) / 60_000) + 5)
  const { points, start, end } = useHistorySeries([a.metric_key, 'cpu.usage_percent'], Math.min(minutes, 240), minutes > 60 ? 60 : 10)
  const series = points[a.metric_key] ?? []
  const st = stats(series.filter((p) => p.t >= startedAt))
  const current = components[a.component_id]?.telemetry[a.metric_key]
  const an = analysis.data
  const conf = an?.confidence ?? a.confidence ?? null
  const unit = current?.unit === 'celsius' ? '°C' : current?.unit === 'percent' ? '%' : current?.unit ?? ''
  const fmtV = (v: number | null) => (v === null ? '—' : `${v.toFixed(unit === '%' || unit === '°C' ? 0 : 2)}${unit}`)
  const confidence = `${a.detector === 'statistical'
    ? `STATISTICAL · z = ${Number(a.context.zscore ?? 0).toFixed(1)}`
    : `RULE · ${String(a.context.rule ?? '')}`}${conf ? ` · ${Math.round(conf.value * 100)}% CONFIDENCE` : ''}`
  const sev = severityLabel(a)
  const sim = a.metric_key.startsWith('memory') ? 'ram_intensive' : 'cpu_intensive'

  return (
    <Panel style={{ flex: '868 0 0' }}>
      <PanelHeading eyebrow={`ANOMALY / ${a.rule_id.toUpperCase()}`} title={a.title} right={<Chip tone={sev.tone} title={conf?.method}>{confidence}</Chip>} />
      <div className="root-cause">
        <div className="root-cause__text">
          <p className="root-cause__title">{a.status === 'resolved' ? 'Resolved' : 'Active'} since {new Date(a.started_at).toLocaleTimeString('en-GB')}</p>
          <p className="note" style={{ fontSize: 12 }}>{a.message}.</p>
          <p className="note" style={{ fontSize: 12, color: 'var(--text)' }}>
            {an ? an.summary : analysis.error ? `Root-cause analysis unavailable: ${analysis.error}` : 'Correlating recorded signals and process snapshots…'}
          </p>
          {an ? (
            <div className="cause-list">
              {an.correlated_signals.slice(0, 3).map((c) => (
                <p key={c.metric_key} className="cause-list__row">
                  <span>{c.label}</span>
                  <span className={Math.abs(c.r) >= 0.6 ? 'is-strong' : ''}>r = {c.r >= 0 ? '+' : ''}{c.r.toFixed(2)} · {c.samples} pts</span>
                </p>
              ))}
              {an.process_attribution.processes.slice(0, 3).map((pr) => (
                <p key={pr.process} className="cause-list__row">
                  <span>{pr.process}</span>
                  <span>{an.process_attribution.ranked_by === 'memory'
                    ? `${(pr.memory_bytes_during / 1024 ** 3).toFixed(2)} GB avg${pr.memory_bytes_before !== null ? ` (before ${(pr.memory_bytes_before / 1024 ** 3).toFixed(2)})` : ''}`
                    : `${pr.cpu_percent_during.toFixed(1)}% CPU avg${pr.cpu_percent_before !== null ? ` (before ${pr.cpu_percent_before.toFixed(1)}%)` : ''}`}</span>
                </p>
              ))}
              {an.process_attribution.note ? <p className="note note--muted">{an.process_attribution.note}</p> : null}
            </div>
          ) : null}
        </div>
        <div className="root-cause__evidence">
          <Spec label="Event peak" value={fmtV(st.max ?? (typeof a.value === 'number' ? a.value : null))} />
          <Spec label="Current" value={current?.availability === 'available' && typeof current.value === 'number' ? fmtV(current.value) : String(current?.value ?? '—')} />
          <Spec label="Threshold" value={a.threshold === null ? '—' : typeof a.threshold === 'number' ? fmtV(a.threshold) : String(a.threshold)} />
          <Spec label="Confidence" value={conf ? `${Math.round(conf.value * 100)}%` : '—'} title={conf?.method} tone="accent" />
        </div>
      </div>
      <TimeSeries series={[{ points: series, label: a.metric_key }, { points: a.metric_key === 'cpu.usage_percent' ? [] : points['cpu.usage_percent'] ?? [], tone: 'secondary' }]}
        start={start} end={end} height={89} axis={timeAxis(start, end, 4, fmtHm)} maxGapMs={120_000} />
      <p className="eyebrow" style={{ color: 'var(--text-2)' }}>CYAN {a.metric_key.toUpperCase()} / BLUE CPU UTILIZATION / {a.context.source ? String(a.context.source).toUpperCase() : 'TWIN'}</p>
      <div className="recommendation">
        <p className="recommendation__label">RECOMMENDED ACTION</p>
        <p className="recommendation__text">{recommendationFor(a)}</p>
        <div className="recommendation__actions">
          <Button primary icon="flaskConical" onClick={() => navigate('simulation', sim)}>Preview in simulation</Button>
          <Button icon="check" active={Boolean(ack)} disabled={!canAck} onClick={onAck}
            title={canAck ? undefined : 'Operator role required'}>{ack ? 'Acknowledged' : 'Acknowledge'}</Button>
        </div>
        {ack ? <p className="note note--muted">Acknowledged by {ack.acknowledged_by} at {new Date(ack.acknowledged_at).toLocaleString('en-GB')}{ack.note ? ` — “${ack.note}”` : ''}. Saved on the backend.</p> : null}
      </div>
    </Panel>
  )
}

/** Phase 4: learned, device-specific anomalies of every type with evidence and history. */
function AnomalyIntelligenceSection({ param }: { param: string | null }) {
  const deviceId = useCurrentDeviceId()
  const [selected, setSelected] = useState<string | null>(param) // remounted (key) when the link changes
  if (!deviceId) return null
  return (
    <>
      <DetectionStatus deviceId={deviceId} />
      <div className="split split--rev">
        <AnomalyHistoryPanel deviceId={deviceId} selectedId={selected} onSelect={setSelected} />
        {selected ? <AnomalyDetailView key={selected} id={selected} /> : (
          <Panel style={{ flex: '868 0 0' }}>
            <PanelHeading eyebrow="ANOMALY" title="No anomaly selected" />
            <p className="note">Select an anomaly to see what was observed, what is usual for this device, how it was detected and how confident the detection is.</p>
          </Panel>
        )}
      </div>
    </>
  )
}

export function HealthAnomaliesPage() {
  const routeParam = useRoute().param
  const overall = useTwinStore((s) => s.overall)
  const components = useTwinStore((s) => s.components)
  const active = useTwinStore((s) => s.activeAnomalies)
  const [showResolved, setShowResolved] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const now = useNow(15_000)
  const me = useSession((st) => st.me)
  const inventory = useInventory().inventory
  const decorated = useApi(() => api.anomalies(undefined, 200), [Object.keys(active).join(',')], 10_000)
  const meta = useMemo(() => new Map((decorated.data ?? []).map((a) => [a.anomaly_id, a])), [decorated.data])
  const ackOf = (id: string) => meta.get(id)?.acknowledgement ?? null
  const events = useApi(() => api.healthEvents(500), [], 60_000)
  const resolved = useApi(() => api.anomalies('resolved', 30), [showResolved], showResolved ? 30_000 : undefined)

  const list: Anomaly[] = useMemo(() => {
    if (showResolved) return resolved.data ?? []
    return Object.values(active).sort((a, b) => (a.severity === b.severity ? b.started_at.localeCompare(a.started_at) : a.severity === 'critical' ? -1 : 1))
  }, [active, resolved.data, showResolved])
  const selected = list.find((a) => a.anomaly_id === selectedId) ?? list[0] ?? null

  // 24 h score trend from recorded health transitions of the device (component "laptop").
  const trend = useMemo(() => {
    const pts = (events.data ?? [])
      .filter((e) => e.component_id === 'laptop' && e.score !== null && now - Date.parse(e.time) < 86_400_000)
      .map((e) => ({ t: Date.parse(e.time), v: e.score as number }))
      .sort((a, b) => a.t - b.t)
    if (overall?.score !== null && overall?.score !== undefined) pts.push({ t: now, v: overall.score })
    return pts
  }, [events.data, overall, now])
  const delta = trend.length > 1 ? trend[trend.length - 1].v - trend[0].v : null

  const comp = (type: ComponentType): TwinComponent | undefined => Object.values(components).find((c) => c.component_type === type)
  const counts = { critical: 0, advisory: 0, info: 0 }
  Object.values(active).forEach((a) => {
    if (a.severity === 'critical') counts.critical += 1
    else if (a.severity === 'warning') counts.advisory += 1
    else counts.info += 1
  })
  const disks = Object.values(components).filter((c) => c.component_type === 'disk')
  const smart = disks.reduce((n, d) => n
    + readingsOf(d, 'disk.health_status').filter((r) => r.availability === 'available' && r.value !== 'Healthy').length
    + readingsOf(d, 'disk.critical_warning').filter((r) => r.availability === 'available' && Number(r.value) !== 0).length
    + readingsOf(d, 'disk.media_errors').filter((r) => r.availability === 'available' && Number(r.value) > 0).length, 0)
  const whea = readingOf(components.os, 'system.whea_errors_30d')
  const wheaFatal = readingOf(components.os, 'system.whea_fatal_30d')
  const wheaEvents = (obj(obj(inventory).whea).events as { time: string; description: string }[] | undefined) ?? []
  const statusChip = overall?.status === 'critical' ? 'CRITICAL' : overall?.status === 'warning' ? 'ADVISORY' : overall?.score === null || !overall ? 'UNKNOWN' : 'STABLE'

  const ack = async (id: string) => {
    try {
      if (ackOf(id)) await api.unacknowledge(id)
      else await api.acknowledge(id)
    } finally {
      decorated.reload()
    }
  }

  return (
    <>
      <PageHeading title="Health & Anomalies" subtitle="Explainable detection. Prioritized attention. Evidence before action."
        action={<Button icon="arrowRight" onClick={async () => exportJson('health-report', { health: await api.twin().then((t) => t.health), anomalies: await api.anomalies(undefined, 200), events: events.data })}>Generate health report</Button>} />

      <div className="split split--rev">
        <Panel className="score-card">
          <PanelHeading eyebrow="WEIGHTED COMPONENT SCORE" title="System health" right={<Chip tone={healthTone(overall)}>{statusChip}</Chip>} />
          <div className="health-score"><p className="health-score__value health-score__value--lg">{overall?.score ?? '—'}</p><p className="health-score__max" style={{ color: 'var(--text-2)' }}>/ 100</p></div>
          <TimeSeries series={[{ points: trend }]} start={now - 86_400_000} end={now} height={58} min={0} max={100} axis={['−24h', '−12h', 'now']} maxGapMs={86_400_000} emptyText="NO TRANSITIONS RECORDED" />
          <p className="note">{delta === null ? 'Score history builds as health transitions are recorded.' : `${delta >= 0 ? '+' : '−'}${Math.abs(delta)} points / 24h${overall?.reasons.find((r) => r.severity === 'warning') ? ` · ${overall.reasons.find((r) => r.severity === 'warning')!.message.replace(/\s*\(.*\)$/, '').toLowerCase()}` : ''}`}</p>
        </Panel>
        <Panel style={{ flex: '970 0 0' }}>
          <PanelHeading title="Component health breakdown" right={<p className="panel-meta">{counts.critical} CRITICAL / {counts.advisory} ADVISORY / {counts.info} INFO</p>} />
          <div className="score-grid">
            {BREAKDOWN.map((b) => {
              const c = comp(b.type)
              const h = c?.health
              const tone = healthTone(h)
              return (
                <button key={b.label} type="button" className="score-item" onClick={() => navigate('components', b.type === 'thermal_sensors' ? 'cooling' : b.type === 'memory' ? 'ram' : b.type === 'storage' ? 'ssd' : b.type)}>
                  <p className="score-item__label">{b.label}</p>
                  <p className={`score-item__value ${tone !== 'accent' ? 'is-amber' : ''}`}>{h?.score ?? '—'}</p>
                  <Track percent={h?.score ?? 0} tone={tone === 'accent' ? 'accent' : 'amber'} />
                  <p className="eyebrow">{healthLabel(h)}</p>
                </button>
              )
            })}
          </div>
          <p className="note">Health combines thermal margin, utilization, storage integrity and battery capacity. Advisory detection does not imply hardware failure.</p>
          <p className="eyebrow">RULE-BASED ENGINE / EVALUATED {utcClock(now)} / LIVE TELEMETRY</p>
        </Panel>
      </div>

      <AnomalyIntelligenceSection key={routeParam ?? ''} param={routeParam} />

      <div className="split split--rev">
        <Panel className="side-panel" style={{ flex: '424 0 0' }}>
          <PanelHeading title={showResolved ? 'Resolved threshold alerts' : 'Safety threshold alerts'} right={<p className="panel-meta">{String(list.length).padStart(2, '0')} {showResolved ? 'RESOLVED' : 'OPEN'}</p>} />
          {list.length === 0 ? <p className="note">{showResolved ? 'No resolved anomalies recorded yet.' : 'No active anomalies. Rules and statistical baselines evaluate every sample.'}</p> : null}
          {list.slice(0, 6).map((a) => {
            const sev = severityLabel(a)
            const isSel = selected?.anomaly_id === a.anomaly_id
            return (
              <button key={a.anomaly_id} type="button" className={`anomaly-card anomaly-card--${sev.tone} ${isSel ? 'is-selected' : ''}`} onClick={() => setSelectedId(a.anomaly_id)}>
                <Chip tone={sev.tone}>{sev.text} · {a.status === 'resolved' ? 'RESOLVED' : ackOf(a.anomaly_id) ? 'ACKNOWLEDGED' : 'ACTIVE'}</Chip>
                <p className={`anomaly-card__title ${isSel ? 'is-lg' : ''}`}>{a.title}</p>
                <p className="note">{a.message}</p>
                <p className="eyebrow" style={{ color: 'var(--text-2)' }}>FIRST SEEN {new Date(a.started_at).toLocaleTimeString('en-GB')} / {a.component_id.toUpperCase()}</p>
              </button>
            )
          })}
          <div>
            <Spec label="Hardware error events (30 d)"
              value={whea?.availability === 'available' ? `${whea.value}${wheaFatal?.availability === 'available' && Number(wheaFatal.value) > 0 ? ` · ${wheaFatal.value} fatal` : ''}` : 'Unavailable'}
              tone={whea?.availability !== 'available' ? 'na' : Number(whea.value) > 0 ? 'amber' : 'accent'}
              title={whea?.availability === 'available' ? `${whea.source}${wheaEvents[0] ? ` · latest: ${wheaEvents[0].description} at ${wheaEvents[0].time}` : ''}` : whea?.reason ?? 'Waiting for the agent (WHEA scan every 5 minutes)'} />
            <Spec label="Storage SMART warnings" value={smart} tone={smart ? 'amber' : 'accent'}
              title="Drive health status, NVMe critical-warning flags and media errors" />
          </div>
          <Button icon="history" onClick={() => { setShowResolved(!showResolved); setSelectedId(null) }} style={{ alignSelf: 'flex-start' }}>
            {showResolved ? 'View open anomalies' : 'View resolved events'}
          </Button>
        </Panel>
        {selected ? (
          <AnomalyDetail key={selected.anomaly_id} a={meta.get(selected.anomaly_id) ?? selected} ack={ackOf(selected.anomaly_id)}
            canAck={canOperate(me)} onAck={() => void ack(selected.anomaly_id)} />
        ) : (
          <Panel style={{ flex: '868 0 0' }}>
            <PanelHeading eyebrow="ANOMALY" title="No anomaly selected" />
            <p className="note">When the rule or statistical engines detect an anomaly, its evidence, history and recommended action appear here.</p>
          </Panel>
        )}
      </div>
    </>
  )
}
