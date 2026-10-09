import { useRoute } from '../app/routes'
import { PredictiveSection } from '../prediction/PredictionViews'
import type { TargetId } from '../types/prediction'
import { useMemo, useState } from 'react'

import { exportJson } from '../app/exportData'
import { fmt0, fmt1, summarize } from '../app/liveData'
import { useApi } from '../hooks/useApi'
import { stats, useHistorySeries, useWindowSeries } from '../hooks/useSeries'
import { api } from '../services/api'
import { useTwinStore } from '../stores/twinStore'
import { firstOfType, num, readingOf } from '../utils/twin'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, MetricCard, Panel, PanelHeading } from '../ui/primitives'
import { fmtDay, fmtHm, timeAxis } from '../ui/time'
import { TimeSeries } from '../ui/TimeSeries'

const RANGES = [
  { label: 'Last 24 hours', minutes: 1440, bucket: 600 },
  { label: 'Last 7 days', minutes: 10080, bucket: 3600 },
  { label: 'Last 30 days', minutes: 43200, bucket: 14400 },
]
const DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI']
const HOURS = Array.from({ length: 16 }, (_, i) => 6 + i)

const DAY_MS = 86_400_000
const toInput = (ms: number) => new Date(ms).toISOString().slice(0, 10)

/** Bucket size giving ~150-400 points for a window. */
function bucketFor(spanMin: number): number {
  if (spanMin <= 1440) return 600
  if (spanMin <= 10080) return 3600
  return 14400
}

export function AnalyticsPage() {
  const routeParam = useRoute().param
  const components = useTwinStore((s) => s.components)
  const [ri, setRi] = useState(1)
  const [custom, setCustom] = useState<{ start: number; end: number } | null>(null)
  const [picker, setPicker] = useState<{ from: string; to: string } | null>(null)
  const [compare, setCompare] = useState(false)
  const preset = RANGES[ri]
  const spanMin = custom ? Math.max(60, Math.round((custom.end - custom.start) / 60_000)) : preset.minutes
  const range = custom
    ? { label: 'Custom range', minutes: spanMin, bucket: bucketFor(spanMin) }
    : preset
  const endIso = custom ? new Date(custom.end).toISOString() : undefined
  const s = summarize(components)
  const gpuKey = readingOf(firstOfType(components, 'gpu'), 'gpu.usage_percent')?.key
  const gpuTempKey = s.gpuTemp?.availability === 'available' ? s.gpuTemp.key : null
  const tempKey = s.temp?.key

  const perf = useApi(() => api.performance(range.minutes, endIso), [range.minutes, endIso], 120_000)
  const thermal = useApi(() => api.thermal(range.minutes, endIso), [range.minutes, endIso], 120_000)
  const predictions = useApi(() => api.predictions(), [], 120_000)
  const seriesKeys = ['cpu.usage_percent', gpuKey, tempKey, gpuTempKey, 'battery.health_percent']
  const rolling = useHistorySeries(seriesKeys, range.minutes, range.bucket, 120_000)
  const fixed = useWindowSeries(seriesKeys, custom, range.bucket)
  const points = custom ? fixed.points : rolling.points
  const start = custom ? custom.start : rolling.start
  const end = custom ? custom.end : rolling.end
  const span = end - start
  const prevWindow = compare ? { start: start - span, end: start } : null
  const prev = useWindowSeries(['cpu.usage_percent', tempKey], prevWindow ? { start: Math.round(prevWindow.start / 60_000) * 60_000, end: Math.round(prevWindow.end / 60_000) * 60_000 } : null, range.bucket)
  const prevPerf = useApi(
    () => (prevWindow ? api.performance(range.minutes, new Date(prevWindow.end).toISOString()) : Promise.resolve(null)),
    [compare, range.minutes, Math.round(start / 60_000)],
  )
  const shift = (pts: { t: number; v: number }[] | undefined) => (pts ?? []).map((p) => ({ t: p.t + span, v: p.v }))
  const prevCpu = shift(prev.points['cpu.usage_percent'])
  const prevTemp = shift(tempKey ? prev.points[tempKey] : [])
  const prevAvgCpu = prevPerf.data?.metrics['cpu.usage_percent']?.mean ?? null
  const week = useHistorySeries(['cpu.usage_percent'], 10080, 3600, 300_000)

  const cpuPts = points['cpu.usage_percent'] ?? []
  const avgCpu = perf.data?.metrics['cpu.usage_percent']?.mean ?? stats(cpuPts).avg
  const gpuAvg = stats(gpuKey ? points[gpuKey] ?? [] : []).avg
  const sensors = (thermal.data?.sensors as { metric_key: string; max_c: number | null; mean_c: number | null; time_above_90c_s: number | null }[] | undefined) ?? []
  const tempSensor = sensors.find((x) => x.metric_key === tempKey)
  const gpuTempSensor = sensors.find((x) => x.metric_key === gpuTempKey)
  const health = points['battery.health_percent'] ?? []
  const hLo = Math.min(75, ...health.map((p) => p.v))
  const healthDelta = health.length > 1 ? health[health.length - 1].v - health[0].v : null
  const hoursActive = cpuPts.length * (range.bucket / 3600)
  const days = range.minutes / 1440

  const matrix = useMemo(() => {
    const cells = new Map<string, { sum: number; n: number }>()
    for (const p of week.points['cpu.usage_percent'] ?? []) {
      const d = new Date(p.t)
      const day = (d.getDay() + 6) % 7
      if (day > 4) continue
      const key = `${day}-${d.getHours()}`
      const c = cells.get(key) ?? { sum: 0, n: 0 }
      c.sum += p.v
      c.n += 1
      cells.set(key, c)
    }
    let peakHour: number | null = null
    let peakVal = -1
    for (const h of HOURS) {
      let sum = 0
      let n = 0
      for (let d = 0; d < 5; d += 1) {
        const c = cells.get(`${d}-${h}`)
        if (c) {
          sum += c.sum / c.n
          n += 1
        }
      }
      if (n && sum / n > peakVal) {
        peakVal = sum / n
        peakHour = h
      }
    }
    return { cells, peakHour }
  }, [week.points])

  const axis = timeAxis(start, end, 4, span <= DAY_MS ? fmtHm : fmtDay)
  const dateLabel = `${new Date(start).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })} – ${new Date(end).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })}`
  const observation = (predictions.data?.predictions ?? []).filter((p) => p.status !== 'unavailable').map((p) => p.statement).join(' ')

  return (
    <>
      <PageHeading title="Analytics" subtitle={`Performance over time / Persisted telemetry history / ${components.laptop?.model ?? 'Device'}`}
        action={<Button icon="arrowRight" onClick={() => exportJson('analysis', { range: range.label, performance: perf.data, thermal: thermal.data, predictions: predictions.data })}>Export analysis</Button>} />

      <PredictiveSection key={routeParam ?? ''} initial={routeParam?.startsWith('forecast:') ? (routeParam.slice(9) as TargetId) : null} />

      <div className="controls-row">
        <div className="controls-row__group">
          <Button primary={!custom} icon="calendarDays" onClick={() => { setCustom(null); setRi(custom ? ri : (ri + 1) % RANGES.length) }}>{custom ? RANGES[ri].label : range.label}</Button>
          <div className="dropdown">
            <Button icon="chevronDown" active={Boolean(custom)} onClick={() => setPicker(picker ? null : { from: toInput(start), to: toInput(end) })}>{dateLabel}</Button>
            {picker ? (
              <form className="dropdown__menu date-range" onSubmit={(e) => {
                e.preventDefault()
                const from = Date.parse(`${picker.from}T00:00:00Z`)
                const to = Math.min(Date.now(), Date.parse(`${picker.to}T23:59:59Z`))
                if (Number.isFinite(from) && Number.isFinite(to) && to > from) {
                  setCustom({ start: from, end: to })
                  setPicker(null)
                }
              }}>
                <p className="eyebrow">CUSTOM RANGE (UTC)</p>
                <label className="date-range__field">From<input className="form-input" type="date" value={picker.from} max={picker.to} onChange={(e) => setPicker({ ...picker, from: e.target.value })} /></label>
                <label className="date-range__field">To<input className="form-input" type="date" value={picker.to} min={picker.from} max={toInput(end)} onChange={(e) => setPicker({ ...picker, to: e.target.value })} /></label>
                <div className="form-row">
                  <Button type="submit" primary icon="check">Apply</Button>
                  {custom ? <Button onClick={() => { setCustom(null); setPicker(null) }}>Clear</Button> : null}
                </div>
              </form>
            ) : null}
          </div>
          <Button icon="columns2" active={compare} onClick={() => setCompare(!compare)}>Compare previous period</Button>
        </div>
        <p className="panel-meta">{range.bucket / 60} MIN AGGREGATION / UTC{compare ? ' / PREVIOUS PERIOD DASHED' : ''}</p>
      </div>

      <div className="metric-row">
        <MetricCard label="AVG CPU UTILIZATION" value={fmt0(avgCpu)} unit="%"
          caption={compare ? (prevAvgCpu !== null && avgCpu !== null ? `${avgCpu - prevAvgCpu >= 0 ? '+' : '−'}${Math.abs(avgCpu - prevAvgCpu).toFixed(1)} pt vs previous period` : 'No data in the previous period') : `${perf.data?.metrics['cpu.usage_percent']?.samples ?? 0} persisted samples`} />
        <MetricCard label={s.temp?.isPackageSensor ? 'PEAK CPU TEMPERATURE' : 'PEAK CPU-AREA TEMPERATURE'} value={tempSensor?.max_c != null ? tempSensor.max_c.toFixed(0) : null} unit="°C"
          tone={(tempSensor?.max_c ?? 0) >= 80 ? 'amber' : 'default'} caption={tempSensor ? `mean ${fmt0(tempSensor.mean_c)}°C · ${s.temp?.isPackageSensor ? 'package' : 'ACPI zone'}` : 'No samples in range'} />
        <MetricCard label="BATTERY CAPACITY HEALTH" value={fmt0(s.batteryHealth)} unit="%"
          caption={healthDelta === null ? 'No change recorded in range' : `${healthDelta >= 0 ? '+' : '−'}${Math.abs(healthDelta).toFixed(1)} pt over ${range.label.toLowerCase().replace('last ', '')}`} />
        <MetricCard label="ACTIVE DEVICE TIME" value={fmt1(hoursActive)} unit="h" caption={`${fmt1(hoursActive / days)} h daily average · from telemetry coverage`} />
      </div>

      <div className="split split--even">
        <Panel style={{ flex: '1 0 0' }}>
          <PanelHeading title="Historical performance / CPU + GPU" right={<p className="panel-value">{fmt0(avgCpu) ?? '—'}% / {fmt0(gpuAvg) ?? '—'}% avg</p>} />
          <TimeSeries series={[{ points: cpuPts }, { points: gpuKey ? points[gpuKey] ?? [] : [], tone: 'secondary' }, ...(compare ? [{ points: prevCpu, tone: 'secondary' as const, dashed: true }] : [])]}
            start={start} end={end} height={123} min={0} max={100} axis={axis} maxGapMs={range.bucket * 3000} showLatest />
          <p className="caption-mono">0–100% / CYAN CPU / BLUE GPU{compare ? ' / DASHED PREVIOUS-PERIOD CPU' : ''} / {range.bucket / 60} MIN MEAN</p>
        </Panel>
        <Panel style={{ flex: '1 0 0' }}>
          <PanelHeading title="Thermal trends / CPU + GPU" right={<p className="panel-value">{fmt0(tempSensor?.mean_c ?? null) ?? '—'}°C / {gpuTempSensor ? `${fmt0(gpuTempSensor.mean_c)}°C` : 'N/A'} avg</p>} />
          <TimeSeries series={[{ points: tempKey ? points[tempKey] ?? [] : [] }, { points: gpuTempKey ? points[gpuTempKey] ?? [] : [], tone: 'secondary' }, ...(compare ? [{ points: prevTemp, tone: 'secondary' as const, dashed: true }] : [])]}
            start={start} end={end} height={123} axis={axis} maxGapMs={range.bucket * 3000} />
          <p className="caption-mono">{s.temp?.isPackageSensor ? 'CPU PACKAGE' : 'ACPI ZONE'} / {tempSensor?.time_above_90c_s ? `${Math.round(tempSensor.time_above_90c_s / 60)} MIN ABOVE 90°C` : 'NO TIME ABOVE 90°C'} / {(thermal.data?.throttled_seconds as number | undefined) ? `${thermal.data?.throttled_seconds} S THROTTLED` : 'NO THROTTLING'}</p>
        </Panel>
      </div>

      <div className="split split--even">
        <Panel style={{ flex: '1 0 0' }}>
          <PanelHeading title="Battery degradation" right={<Chip tone={(s.batteryHealth ?? 100) < 80 ? 'amber' : 'accent'}>{(s.batteryHealth ?? 100) < 80 ? 'BELOW 80% OF DESIGN' : 'WITHIN EXPECTED RANGE'}</Chip>} />
          <div style={{ position: 'relative' }}>
            <TimeSeries series={[{ points: health }]} start={start} end={end} height={115} axis={axis} maxGapMs={range.minutes * 60_000} min={hLo} max={100}
              emptyText="CAPACITY HISTORY BUILDS AS THE AGENT REPORTS" />
            {s.batteryHealth !== null ? <p className="mono chart-tag" style={{ top: 115 * (1 - (s.batteryHealth - hLo) / (100 - hLo)) - 18 }}>{s.batteryHealth.toFixed(0)}%</p> : null}
          </div>
          <p className="note">{fmt1(num(readingOf(components.battery, 'battery.design_capacity_wh')))} Wh design → {fmt1(num(readingOf(components.battery, 'battery.full_charge_capacity_wh')))} Wh full charge / {fmt0(num(readingOf(components.battery, 'battery.cycle_count')))} cycles / {health.length ? `${((end - health[0].t) / 86_400_000).toFixed(1)}-day history` : 'no history yet'}</p>
        </Panel>
        <Panel style={{ flex: '1 0 0' }}>
          <PanelHeading title="Resource utilization by hour" right={<p className="panel-meta">CPU / LOCAL WORK HOURS / LAST 7 DAYS</p>} />
          <div className="heatmap">
            {DAYS.map((d, di) => (
              <div key={d} className="heatmap__row">
                <p className="heatmap__day">{d}</p>
                {HOURS.map((h) => {
                  const c = matrix.cells.get(`${di}-${h}`)
                  const v = c ? c.sum / c.n : null
                  return <div key={h} className={`heatmap__cell ${v === null ? 'is-empty' : ''}`} style={v === null ? undefined : { opacity: Math.max(0.08, Math.min(0.75, v / 100)) }}
                    title={`${d} ${String(h).padStart(2, '0')}:00 · ${v === null ? 'no data' : `${v.toFixed(0)}% avg CPU`}`} />
                })}
              </div>
            ))}
          </div>
          <div className="heatmap__axis"><p>06:00</p><p>10:00</p><p>14:00</p><p>18:00</p><p>22:00</p></div>
          <p className="eyebrow">LOW ░░▒▓ HIGH / {matrix.peakHour !== null ? `PEAK ACTIVITY ${String(matrix.peakHour).padStart(2, '0')}:00–${String(matrix.peakHour + 1).padStart(2, '0')}:00` : 'NOT ENOUGH HISTORY YET'} · EMPTY = NO DATA</p>
        </Panel>
      </div>

      <div className="observation">
        <Chip>OBSERVATION</Chip>
        <p>{observation || 'Observations appear once enough history has been recorded for trend analysis.'}</p>
      </div>
    </>
  )
}
