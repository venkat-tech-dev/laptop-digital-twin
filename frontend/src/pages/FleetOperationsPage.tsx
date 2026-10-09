import { useState } from 'react'

import { navigate } from '../app/routes'
import { useApi } from '../hooks/useApi'
import { deviceScope } from '../services/deviceScope'
import { fleetApi, type FleetHealth, type Projection } from '../services/fleetApi'
import { PageHeading } from '../ui/PageHeading'
import { DataTable, MetricCard, Panel, PanelHeading, Spec, Track } from '../ui/primitives'
import { Counts, StatusChip } from '../org/orgUi'
import { when } from '../org/orgUtils'

type View = 'executive' | 'it' | 'insights' | 'capacity' | 'models' | 'reliability'
const VIEWS: { id: View; label: string }[] = [
  { id: 'executive', label: 'Executive' },
  { id: 'it', label: 'IT operations' },
  { id: 'insights', label: 'Fleet insights' },
  { id: 'capacity', label: 'Capacity' },
  { id: 'models', label: 'Model quality' },
  { id: 'reliability', label: 'Reliability (SLOs)' },
]

const open = (deviceId: string) => {
  deviceScope.set(deviceId)
  navigate('twin')
}

function DeviceLink({ id }: { id: string }) {
  return <button type="button" className="link caption-mono" onClick={() => open(id)} title="Open this device's twin, timeline and evidence">{id}</button>
}

const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)} %`)

/**
 * Fleet operations (Phase 10). Everything is computed server-side for the signed-in member's visible
 * devices only; platform figures (pipeline, storage, SLOs) appear for platform administrators.
 */
export function FleetOperationsPage({ param }: { param: string | null }) {
  const view: View = VIEWS.some((v) => v.id === param) ? (param as View) : 'executive'
  return (
    <>
      <PageHeading title="Fleet operations" subtitle="Fleet health, recurring issues and capacity from real telemetry. Associations are not causes; projections state their assumptions." />
      <div className="seg seg--tabs" role="tablist" aria-label="Fleet views">
        {VIEWS.map((v) => (
          <button key={v.id} type="button" role="tab" aria-selected={view === v.id} className={`seg__btn ${view === v.id ? 'is-on' : ''}`}
            onClick={() => navigate('operations', v.id)}>{v.label}</button>
        ))}
      </div>
      {view === 'executive' ? <Executive /> : null}
      {view === 'it' ? <ItOperations /> : null}
      {view === 'insights' ? <Insights /> : null}
      {view === 'capacity' ? <Capacity /> : null}
      {view === 'models' ? <Models /> : null}
      {view === 'reliability' ? <Reliability /> : null}
    </>
  )
}

// ------------------------------------------------------------------------------ executive
function HealthTrend({ h }: { h: FleetHealth }) {
  const pts = h.history.slice(-48)
  if (pts.length < 2) return <p className="note note--muted">Trend: not enough snapshots yet (one per hour is recorded).</p>
  const w = 320
  const ht = 60
  const x = (i: number) => (i / (pts.length - 1)) * w
  const y = (s: number) => ht - (s / 100) * ht
  return (
    <figure style={{ margin: 0 }}>
      <svg width="100%" viewBox={`0 0 ${w} ${ht}`} role="img" aria-label={`Fleet health trend, ${pts.length} hourly snapshots, latest ${pts[pts.length - 1].score}`}>
        <polyline fill="none" stroke="var(--accent)" strokeWidth="2" points={pts.map((p, i) => `${x(i)},${y(p.score)}`).join(' ')} />
      </svg>
      <figcaption className="note note--muted">Last {pts.length} hourly snapshots · {when(pts[0].at)} → {when(pts[pts.length - 1].at)}</figcaption>
    </figure>
  )
}

function Executive() {
  const health = useApi(() => fleetApi.health(), [], 60_000)
  const ins = useApi(() => fleetApi.insights(30, 168), [], 120_000)
  const h = health.data
  return (
    <>
      <div className="metric-row">
        <MetricCard label="FLEET HEALTH" value={h?.score !== null && h?.score !== undefined ? String(h.score) : null} unit="/ 100"
          caption={h ? (h.status === 'OK' ? `${h.band} · confidence ${h.confidence} · ${h.scored}/${h.devices} devices scored` : 'Insufficient data') : undefined}
          tone={h?.band && h.band !== 'HEALTHY' ? 'amber' : 'default'} title={h?.formula} />
        <MetricCard label="CRITICAL CONDITIONS" value={h ? String(h.critical_conditions.length) : null} tone={h?.critical_conditions.length ? 'amber' : 'default'} caption="shown even when the score is high" />
        <MetricCard label="RECURRING ISSUES" value={ins.data ? String(ins.data.recurring_issues.length) : null} caption="alert types seen ≥ 3 times in 30 days" />
        <MetricCard label="FLEET INSIGHTS" value={ins.data ? String(ins.data.correlation.insights.length) : null} caption="multi-device bursts, last 7 days" />
      </div>
      {health.error ? <p className="note" style={{ color: 'var(--critical)' }}>{health.error}</p> : null}
      <div className="split split--even">
        <Panel>
          <PanelHeading eyebrow={h?.version ?? 'FLEET HEALTH'} title="What lowers the score" />
          {h && h.contributors.length ? h.contributors.map((c) => (
            <Spec key={c.factor} label={c.factor.replace(/_/g, ' ')} value={`−${c.avg_points_deducted} avg · ${c.devices} device(s)`} />
          )) : <p className="note note--muted">{h?.status === 'INSUFFICIENT_DATA' ? 'No device has current telemetry.' : 'Nothing is lowering the score.'}</p>}
          {h ? <HealthTrend h={h} /> : null}
          {h?.unknown_devices.length ? <p className="note note--muted">{h.unknown_devices.length} device(s) without telemetry in 15 min are not scored (coverage {pct(h.coverage)}).</p> : null}
        </Panel>
        <Panel>
          <PanelHeading eyebrow="NEEDS ATTENTION" title="Critical conditions" />
          {h && h.critical_conditions.length ? (
            <DataTable rowKey={(r) => r.device_id} rows={h.critical_conditions} columns={[
              { key: 'd', header: 'DEVICE', width: 170, render: (r) => <DeviceLink id={r.device_id} /> },
              { key: 'r', header: 'WHY', grow: true, render: (r) => r.reasons.join('; ') },
            ]} />
          ) : <p className="note">{h ? 'No device is in a critical condition.' : 'Loading…'}</p>}
        </Panel>
      </div>
      <Panel>
        <PanelHeading eyebrow="TRENDS" title="Recurring operational risks" right={<p className="panel-meta">observed facts, last 30 days</p>} />
        <Recurring rows={ins.data?.recurring_issues ?? []} />
      </Panel>
      <p className="note note--muted">No financial savings, productivity or risk-reduction figures are shown: the platform has no data to support them.</p>
    </>
  )
}

function Recurring({ rows }: { rows: { alert_type: string; occurrences: number; devices_affected: number; devices_with_repeats: number; worst_severity: string; last_7_days: number; previous_7_days: number; trend: string; last_seen: string }[] }) {
  return (
    <DataTable rowKey={(r) => r.alert_type} rows={rows} empty="No alert type occurred 3 or more times in 30 days."
      columns={[
        { key: 't', header: 'ISSUE', grow: true, kind: 'primary', render: (r) => r.alert_type.replace(/_/g, ' ') },
        { key: 'o', header: 'TIMES', width: 70, kind: 'mono', render: (r) => r.occurrences },
        { key: 'd', header: 'DEVICES', width: 80, kind: 'mono', render: (r) => `${r.devices_affected} (${r.devices_with_repeats} repeat)` },
        { key: 's', header: 'WORST', width: 100, render: (r) => <StatusChip value={r.worst_severity === 'CRITICAL' || r.worst_severity === 'HIGH' ? 'HIGH' : r.worst_severity} /> },
        { key: 'w', header: '7 D vs PRIOR', width: 110, kind: 'mono', render: (r) => `${r.last_7_days} vs ${r.previous_7_days}` },
        { key: 'tr', header: 'TREND', width: 90, render: (r) => r.trend.toLowerCase() },
        { key: 'l', header: 'LAST', width: 130, render: (r) => when(r.last_seen) },
      ]} />
  )
}

// ------------------------------------------------------------------------------ IT operations
function ItOperations() {
  const ops = useApi(() => fleetApi.operations(), [], 15_000)
  const o = ops.data
  if (!o) return <p className="note">{ops.error ?? 'Loading…'}</p>
  return (
    <>
      <div className="metric-row">
        <MetricCard label="DEVICES" value={String(o.devices)} caption={Object.entries(o.presence).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(' · ')} />
        <MetricCard label="CRITICAL / WARNING" value={`${o.critical_devices.length} / ${o.warning_devices.length}`} tone={o.critical_devices.length ? 'amber' : 'default'} caption="twin health" />
        <MetricCard label="ACTIVE ANOMALIES" value={String(o.active_anomalies)} />
        <MetricCard label="CROSSINGS ≤ 24 H" value={String(o.upcoming_crossings_24h.length)} caption="predicted threshold crossings" />
      </div>
      <div className="split split--even">
        <Panel>
          <PanelHeading eyebrow="DEVICES" title="Health and connectivity" />
          <Spec label="Connectivity" value={<Counts data={o.presence} />} />
          <Spec label="Health" value={<Counts data={o.health} />} />
          <Spec label="Compliance" value={<Counts data={o.compliance} />} />
          <Spec label="Critical" value={o.critical_devices.length ? <span className="form-row" style={{ flexWrap: 'wrap' }}>{o.critical_devices.map((d) => <DeviceLink key={d} id={d} />)}</span> : 'none'} />
          <Spec label="Warning" value={o.warning_devices.length ? <span className="form-row" style={{ flexWrap: 'wrap' }}>{o.warning_devices.map((d) => <DeviceLink key={d} id={d} />)}</span> : 'none'} />
        </Panel>
        <Panel>
          <PanelHeading eyebrow="AGENTS" title="Agent versions" right={<p className="panel-meta">recommended {o.agents.recommended_version ?? '—'}</p>} />
          <Spec label="By version" value={Object.entries(o.agents.by_version).map(([v, n]) => `${v} × ${n}`).join(' · ') || '—'} />
          <Spec label="Below recommended" value={o.agents.outdated.length ? o.agents.outdated.join(', ') : 'none'} tone={o.agents.outdated.length ? 'amber' : 'default'} />
          <Spec label="Shared-key enrollment" value={o.agents.legacy_enrolled.length ? `${o.agents.legacy_enrolled.length} device(s)` : 'none'} tone={o.agents.legacy_enrolled.length ? 'amber' : 'default'} />
          {o.pipeline ? (
            <>
              <PanelHeading eyebrow="PLATFORM" title="Pipeline" />
              <Spec label="Background loops" value={<StatusChip value={o.pipeline.background.status === 'ok' ? 'ACTIVE' : o.pipeline.background.status === 'failing' ? 'FAILURE' : 'WARNING'} />} />
              <Spec label="Write queue" value={`${o.pipeline.persist_queue_depth} samples · oldest ${o.pipeline.persist_oldest_age_s ?? 0} s`} />
              {o.notification_backlog ? <Spec label="Notifications due" value={`${o.notification_backlog.due} · oldest ${o.notification_backlog.oldest_due_s ? Math.round(o.notification_backlog.oldest_due_s) + ' s' : '—'}`} /> : null}
            </>
          ) : null}
        </Panel>
      </div>
      <Panel>
        <PanelHeading eyebrow="NEXT 24 HOURS" title="Predicted threshold crossings" />
        <DataTable rowKey={(u) => `${u.device_id}:${u.target}`} rows={o.upcoming_crossings_24h} empty="No crossing predicted within 24 hours."
          columns={[
            { key: 'd', header: 'DEVICE', width: 170, render: (u) => <DeviceLink id={u.device_id} /> },
            { key: 't', header: 'TARGET', width: 120, render: (u) => u.target },
            { key: 'w', header: 'EXPECTED', width: 140, render: (u) => when(u.crossing_at) },
            { key: 'c', header: 'CONFIDENCE', width: 100, render: (u) => u.confidence },
            { key: 's', header: 'STATEMENT', grow: true, render: (u) => u.statement },
          ]} />
      </Panel>
      <Panel>
        <PanelHeading eyebrow="REMEDIATION" title="Outcomes by action" right={<p className="panel-meta">recent records in memory</p>} />
        <DataTable rowKey={(r) => r.action_type} rows={o.remediation_outcomes} empty="No completed remediation."
          columns={[
            { key: 'a', header: 'ACTION', grow: true, kind: 'primary', render: (r) => r.action_type.replace(/_/g, ' ') },
            { key: 'n', header: 'COMPLETED', width: 100, kind: 'mono', render: (r) => r.completed },
            { key: 's', header: 'SUCCESS', width: 100, render: (r) => (r.success_rate === null ? (r.note ?? '—') : pct(r.success_rate)) },
            { key: 'b', header: 'BY OUTCOME', width: 260, render: (r) => Object.entries(r.by_status).map(([k, v]) => `${k.toLowerCase()} ${v}`).join(' · ') },
          ]} />
      </Panel>
    </>
  )
}

// ------------------------------------------------------------------------------ insights
function Insights() {
  const [hours, setHours] = useState(24)
  const ins = useApi(() => fleetApi.insights(30, hours), [hours], 60_000)
  const d = ins.data
  return (
    <>
      <div className="form-row">
        <label className="note" htmlFor="ins-hours">Look back</label>
        <select id="ins-hours" className="form-select" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
          {[6, 24, 72, 168].map((h) => <option key={h} value={h}>{h < 24 ? `${h} hours` : `${h / 24} day(s)`}</option>)}
        </select>
        {d ? <span className="note note--muted">{d.scope.devices} devices · {d.scope.anomalies_considered} anomalies considered</span> : null}
      </div>
      {d?.correlation.status === 'INSUFFICIENT_DATA' ? (
        <Panel><PanelHeading eyebrow="CROSS-DEVICE CORRELATION" title="Insufficient data" /><p className="note">{d.correlation.reason}</p></Panel>
      ) : null}
      {(d?.correlation.insights ?? []).map((i) => (
        <Panel key={i.insight_id}>
          <PanelHeading eyebrow="FLEET INSIGHT" title={i.observed_fact} right={<StatusChip value="UNKNOWN" title="Causation has not been established" />} />
          <Spec label="Observed fact" value={`${i.devices.length} devices, ${when(i.window.start)} → ${when(i.window.end)}`} />
          {i.statistical_associations.length ? i.statistical_associations.map((a) => (
            <Spec key={`${a.dimension}:${a.value}`} label={`Association (${a.strength.toLowerCase()})`}
              value={`${a.devices_in_burst} of ${a.of_burst} share ${a.dimension} = ${a.value} (fleet: ${a.devices_in_fleet} of ${a.of_fleet}; ×${a.lift}; adjusted p ${a.p_adjusted})`} />
          )) : <Spec label="Association" value="none: no device attribute is over-represented" />}
          <Spec label="Possible explanation" value={i.possible_explanation} />
          <Spec label="Conclusion" value="Possible common issue. Causation has not been established." />
          <details><summary className="note">Devices and method</summary>
            <p className="note"><span className="form-row" style={{ flexWrap: 'wrap' }}>{i.devices.map((x) => <DeviceLink key={x} id={x} />)}</span></p>
            <p className="note note--muted">{i.method}</p>
          </details>
        </Panel>
      ))}
      {d && d.correlation.status === 'OK' && !d.correlation.insights.length ? <p className="note">No anomaly signal appeared on {d.correlation.min_devices}+ devices within {d.correlation.window_minutes} minutes.</p> : null}
      <Panel>
        <PanelHeading eyebrow="RECURRING" title="Recurring issues" />
        <Recurring rows={d?.recurring_issues ?? []} />
      </Panel>
    </>
  )
}

// ------------------------------------------------------------------------------ capacity
function ProjectionView({ title, p, unit }: { title: string; p: Projection | undefined; unit?: string }) {
  if (!p) return null
  return (
    <Panel>
      <PanelHeading eyebrow="CAPACITY" title={title} right={<StatusChip value={p.status === 'OK' ? 'ACTIVE' : 'UNKNOWN'} title={p.status} />} />
      <Spec label="Current" value={p.current === null ? '—' : `${p.current}${unit ?? ''}`} />
      {p.status === 'INSUFFICIENT_DATA' ? (
        <p className="note">Insufficient data: {p.points ?? 0} day(s) observed, {p.needed} needed for a projection.</p>
      ) : (
        <>
          <Spec label="Observed growth" value={`${p.growth_per_day} per day (range ${p.growth_per_day_range?.join(' … ')})`} />
          {p.limit !== undefined ? <><Spec label="Limit" value={String(p.limit)} />{p.utilization !== null && p.utilization !== undefined ? <Track percent={Math.min(100, p.utilization * 100)} tone={p.utilization > 0.8 ? 'amber' : 'accent'} /> : null}</> : null}
          {p.limit !== undefined ? <Spec label="Days to limit" value={p.days_to_limit === null || p.days_to_limit === undefined ? 'not growing' : `${p.days_to_limit}${p.days_to_limit_earliest ? ` (earliest ${p.days_to_limit_earliest})` : ''}`} /> : null}
          <Spec label="Review by" value={p.review_by ?? '—'} />
          <p className="note note--muted">{p.assumptions}</p>
        </>
      )}
    </Panel>
  )
}

function Capacity() {
  const cap = useApi(() => fleetApi.capacity(), [], 300_000)
  const c = cap.data
  if (!c) return <p className="note">{cap.error ?? 'Loading…'}</p>
  return (
    <>
      <div className="split split--even">
        <ProjectionView title="Enrolled devices" p={c.devices} />
        <ProjectionView title="Alerts per day" p={c.alert_volume} />
      </div>
      {c.storage ? (
        <Panel>
          <PanelHeading eyebrow="PLATFORM" title="Storage" />
          {c.storage.status === 'NOT_MEASURED' ? <p className="note">Not measured: {c.storage.reason}</p> : (
            <>
              <Spec label="Database size" value={c.storage.database_bytes ? `${(c.storage.database_bytes / 2 ** 20).toFixed(0)} MiB` : '—'} />
              <Spec label="Uncompressed telemetry per day" value={(c.storage.uncompressed_chunk_bytes_per_day ?? []).map((d) => `${d.day}: ${(d.bytes / 2 ** 20).toFixed(0)} MiB`).join(' · ') || '—'} />
              {c.storage.daily_volume_trend?.status === 'INSUFFICIENT_DATA' ? <p className="note">Trend: insufficient data ({c.storage.daily_volume_trend.points} of {c.storage.daily_volume_trend.needed} days).</p> : null}
              <p className="note note--muted">{c.storage.note}</p>
            </>
          )}
        </Panel>
      ) : null}
    </>
  )
}

// ------------------------------------------------------------------------------ models
function Models() {
  const m = useApi(() => fleetApi.models(30), [], 300_000)
  const d = m.data
  if (!d) return <p className="note">{m.error ?? 'Loading…'}</p>
  const overall = d.prediction?.overall
  return (
    <>
      <Panel>
        <PanelHeading eyebrow="ANOMALY DETECTION" title="Detector quality (operator feedback)" right={<p className="panel-meta">config v{d.anomaly_detection.config_version ?? '—'} · last {d.window_days} days</p>} />
        <DataTable rowKey={(r) => r.detector} rows={d.anomaly_detection.detectors} empty="No anomalies in the window."
          columns={[
            { key: 'd', header: 'DETECTOR', grow: true, kind: 'primary', render: (r) => r.detector },
            { key: 'n', header: 'DETECTED', width: 90, kind: 'mono', render: (r) => r.detected },
            { key: 'l', header: 'LABELLED', width: 90, kind: 'mono', render: (r) => `${r.labelled} (${pct(r.label_coverage)})` },
            { key: 'f', header: 'FALSE POSITIVES', width: 130, render: (r) => (r.false_positive_rate === null ? r.status.replace(/_/g, ' ').toLowerCase() : pct(r.false_positive_rate)) },
          ]} />
        <p className="note note--muted">{d.anomaly_detection.note}. Detection delay: {d.anomaly_detection.detection_delay}.</p>
      </Panel>
      {overall ? (
        <Panel>
          <PanelHeading eyebrow="FORECASTING" title="Prediction outcomes" right={<p className="panel-meta">{Object.entries(d.prediction?.models_used ?? {}).map(([k, v]) => `${k} × ${v}`).join(' · ') || 'no closed predictions'}</p>} />
          <Spec label="Closed predictions" value={String(overall.closed ?? 0)} />
          <Spec label="Confirmed (crossing happened)" value={`${overall.confirmed ?? 0} · hit rate ${pct(overall.hit_rate as number | null)}`} />
          <Spec label="Expired (did not happen)" value={`${overall.expired ?? 0} · false prediction rate ${pct(overall.false_prediction_rate as number | null)}`} />
          <Spec label="Median timing error" value={overall.median_abs_timing_error_s ? `${Math.round((overall.median_abs_timing_error_s as number) / 60)} min` : '—'} />
          <Spec label="Mean lead time" value={overall.mean_lead_time_s ? `${Math.round((overall.mean_lead_time_s as number) / 60)} min` : '—'} />
        </Panel>
      ) : null}
      {d.diagnosis ? (
        <Panel>
          <PanelHeading eyebrow="PLATFORM" title="Diagnosis operations" />
          <Spec label="Prompt version" value={d.diagnosis.prompt_version} />
          <Spec label="Model" value={String(d.diagnosis.provider.selected_model ?? `none (${String(d.diagnosis.provider.selection ?? 'rules only')})`)} />
          <Spec label="Completed / failed" value={`${d.diagnosis.stats.completed ?? 0} / ${d.diagnosis.stats.failed ?? 0} · mean ${d.diagnosis.stats.mean_ms ?? 0} ms`} />
          <Spec label="Queue" value={String(d.diagnosis.queue_depth)} />
        </Panel>
      ) : null}
    </>
  )
}

// ------------------------------------------------------------------------------ reliability
function Reliability() {
  const s = useApi(() => fleetApi.slo(), [], 30_000)
  if (s.error) return <p className="note">Service-level objectives are visible to platform administrators. ({s.error})</p>
  const d = s.data
  if (!d) return <p className="note">Loading…</p>
  return (
    <Panel>
      <PanelHeading eyebrow="SLOs · TARGETS PROPOSED" title="Service-level objectives" right={<p className="panel-meta">since process start ({Math.round(d.process_uptime_s / 60)} min)</p>} />
      <DataTable rowKey={(r) => r.slo_id} rows={d.slos}
        columns={[
          { key: 'j', header: 'JOURNEY', grow: true, kind: 'primary', render: (r) => r.journey },
          { key: 't', header: 'TARGET', width: 90, kind: 'mono', render: (r) => (r.kind === 'ratio_good' ? pct(r.target) : `≤ ${r.target}`) },
          { key: 'c', header: 'CURRENT', width: 90, kind: 'mono', render: (r) => (r.current === null ? '—' : r.kind === 'ratio_good' ? pct(r.current) : String(Math.round(r.current * 10) / 10)) },
          { key: 's', header: 'STATE', width: 130, render: (r) => <StatusChip value={r.state === 'MEETING' ? 'ACTIVE' : r.state === 'NOT_MEETING' ? 'FAILURE' : 'UNKNOWN'} title={r.state} /> },
          { key: 'w', header: 'WINDOW', width: 100, render: (r) => r.evaluation_window },
        ]} />
      <p className="note note--muted">{d.note}</p>
    </Panel>
  )
}
