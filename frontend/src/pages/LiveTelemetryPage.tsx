import { useState } from 'react'

import { useStreamStats, utcClock } from '../app/derive'
import { exportJson } from '../app/exportData'
import { batteryStateText, bytesGB, fmt0, gb, rateMBps, rateMbps, summarize } from '../app/liveData'
import { stats, useLiveSeries } from '../hooks/useSeries'
import { useNow } from '../hooks/useNow'
import { useTwinStore } from '../stores/twinStore'
import type { Point } from '../stores/seriesStore'
import { firstOfType, num, readingOf } from '../utils/twin'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, Panel, PanelHeading, Spec } from '../ui/primitives'
import { fmtClock, fmtHm, timeAxis } from '../ui/time'
import { TimeSeries } from '../ui/TimeSeries'

const WINDOWS = [
  { label: 'Last 60 seconds', ms: 60_000 },
  { label: 'Last 5 minutes', ms: 300_000 },
  { label: 'Last 30 minutes', ms: 1_800_000 },
]

const scale = (pts: Point[] | undefined, f: number): Point[] => (pts ?? []).map((p) => ({ t: p.t, v: p.v * f }))

export function LiveTelemetryPage() {
  const components = useTwinStore((s) => s.components)
  const [win, setWin] = useState(0)
  const [frozenAt, setFrozenAt] = useState<number | null>(null)
  const stream = useStreamStats()
  const now = useNow(1000)
  const s = summarize(components)
  const gpu = firstOfType(components, 'gpu')
  const gpuKey = readingOf(gpu, 'gpu.usage_percent')?.key
  const gpuTempKey = s.gpuTemp?.availability === 'available' ? s.gpuTemp.key : null
  const tempKey = s.temp?.key
  const keys = ['cpu.usage_percent', gpuKey, 'memory.used_bytes', tempKey, gpuTempKey, 'disk.read_bytes_per_sec', 'disk.write_bytes_per_sec',
    'network.rx_bytes_per_sec', 'network.tx_bytes_per_sec', 'battery.charge_rate_w', 'battery.discharge_rate_w']
  const { points, start, end } = useLiveSeries(keys, WINDOWS[win].ms, frozenAt)
  const axis = timeAxis(start, end, 4, WINDOWS[win].ms > 300_000 ? fmtHm : fmtClock)
  const gap = Math.max(5000, (stream.cadenceMs ?? 1000) * 4)

  const cpuSt = stats(points['cpu.usage_percent'] ?? [])
  const gpuSt = stats(gpuKey ? points[gpuKey] ?? [] : [])
  const tempSt = stats(tempKey ? points[tempKey] ?? [] : [])
  const charging = s.batteryState === 'charging'
  const batteryKey = charging ? 'battery.charge_rate_w' : 'battery.discharge_rate_w'
  const batteryPower = num(readingOf(components.battery, batteryKey))
  const healthy = stream.dropped60s === 0 && stream.lastAt !== null && now - stream.lastAt < 3000
  const expected = stream.cadenceMs ? Math.round(60_000 / stream.cadenceMs) : null

  const panel = (title: string, value: string, series: { pts: Point[]; tone: 'primary' | 'secondary' }[], caption: string, min?: number, max?: number) => (
    <Panel style={{ flex: '1 0 0' }}>
      <PanelHeading title={title} right={<p className="panel-value">{value}</p>} />
      <TimeSeries series={series.map((x) => ({ points: x.pts, tone: x.tone }))} start={start} end={end} height={106} min={min} max={max} axis={axis} maxGapMs={gap} />
      <p className="caption-mono">{caption}</p>
    </Panel>
  )

  return (
    <>
      <PageHeading title="Live Telemetry"
        subtitle={`Real-time sample stream / ${Object.values(components).reduce((n, c) => n + Object.keys(c.telemetry).length, 0)} series / ${stream.cadenceMs ? (1000 / stream.cadenceMs).toFixed(1) : '—'} batches per second`}
        action={<Button icon="arrowRight" onClick={() => exportJson('telemetry-stream', Object.fromEntries(keys.filter(Boolean).map((k) => [k as string, points[k as string] ?? []])))}>Export stream</Button>} />

      <div className="controls-row">
        <div className="controls-row__group">
          <Button primary icon="chevronDown" onClick={() => setWin((win + 1) % WINDOWS.length)}>{WINDOWS[win].label}</Button>
          <Button icon="timer" disabled title="Sampling cadence is set on the agent (TELEMETRY_INTERVAL_MS); remote configuration is not available yet">
            {stream.cadenceMs ? `${Math.round(stream.cadenceMs / 50) * 50} ms` : '— ms'}
          </Button>
          <Button icon="pause" active={frozenAt !== null} onClick={() => setFrozenAt(frozenAt ? null : Date.now())}>{frozenAt ? 'Resume stream' : 'Pause stream'}</Button>
        </div>
        <div className="controls-row__group">
          <p className="legend legend--primary">— MEASURED SAMPLE</p>
          <p className="legend legend--secondary">— SECONDARY SIGNAL</p>
          <Chip tone={frozenAt ? 'muted' : healthy ? 'accent' : 'amber'}>{frozenAt ? 'STREAM PAUSED (VIEW ONLY)' : healthy ? 'STREAM HEALTHY' : 'STREAM DEGRADED'}</Chip>
        </div>
      </div>

      <div className="grid-3">
        {panel('CPU utilization', `${fmt0(s.cpuUsage) ?? '—'}%`, [{ pts: points['cpu.usage_percent'] ?? [], tone: 'primary' }],
          `0–100% / AVG ${fmt0(cpuSt.avg) ?? '—'}% / PEAK ${fmt0(cpuSt.max) ?? '—'}% · PSUTIL`, 0, 100)}
        {panel('GPU utilization', `${fmt0(s.gpuUsage) ?? '—'}%`, [{ pts: gpuKey ? points[gpuKey] ?? [] : [], tone: 'primary' }],
          `0–100% / AVG ${fmt0(gpuSt.avg) ?? '—'}% / PEAK ${fmt0(gpuSt.max) ?? '—'}% · GPU ENGINE COUNTERS`, 0, 100)}
        {panel('Memory in use', `${gb(s.memUsed) ?? '—'} GB`, [{ pts: scale(points['memory.used_bytes'], 1 / 1024 ** 3), tone: 'primary' }],
          `0–${s.memTotal ? (s.memTotal / 1024 ** 3).toFixed(0) : '—'} GB / ${fmt0(s.memPct) ?? '—'}% USED / ${bytesGB(s.memAvail)} FREE`, 0, s.memTotal ? s.memTotal / 1024 ** 3 : undefined)}
      </div>
      <div className="grid-3">
        {panel('Temperature / CPU + GPU', `${s.temp ? `${s.temp.value.toFixed(0)}°C` : '—'} / ${s.gpuTemp?.availability === 'available' ? `${Number(s.gpuTemp.value).toFixed(0)}°C` : 'N/A'}`,
          [{ pts: tempKey ? points[tempKey] ?? [] : [], tone: 'primary' }, { pts: gpuTempKey ? points[gpuTempKey] ?? [] : [], tone: 'secondary' }],
          `${s.temp?.isPackageSensor ? 'CPU PACKAGE' : 'ACPI ZONE'} PEAK ${fmt0(tempSt.max) ?? '—'}°C · GPU TEMP ${gpuTempKey ? 'LHM' : 'UNAVAILABLE'}`)}
        {panel('Disk throughput / Read + Write', `${rateMBps(s.diskReadBps) ?? '—'} / ${rateMBps(s.diskWriteBps) ?? '—'} MB/s`,
          [{ pts: scale(points['disk.read_bytes_per_sec'], 1e-6), tone: 'primary' }, { pts: scale(points['disk.write_bytes_per_sec'], 1e-6), tone: 'secondary' }],
          `MB/s / ACTIVE TIME ${fmt0(s.diskActive) ?? '—'}% · PHYSICALDISK`, 0)}
        {panel('Network / Down + Up', `${rateMbps(s.netRxBps) ?? '—'} / ${rateMbps(s.netTxBps) ?? '—'} Mb/s`,
          [{ pts: scale(points['network.rx_bytes_per_sec'], 8e-6), tone: 'primary' }, { pts: scale(points['network.tx_bytes_per_sec'], 8e-6), tone: 'secondary' }],
          `Mb/s / ${components.network?.current_state?.toUpperCase() ?? '—'} · LATENCY N/A`, 0)}
      </div>
      <div className="split">
        <Panel style={{ flex: '868 0 0' }}>
          <PanelHeading title={charging ? 'Battery charging power' : 'Battery discharge power'}
            right={<p className="panel-value">{batteryPower !== null ? `${charging ? '+' : '−'}${batteryPower.toFixed(1)} W` : '—'}</p>} />
          <TimeSeries series={[{ points: points[batteryKey] ?? [] }]} start={start} end={end} height={110} axis={axis} maxGapMs={Math.max(gap, 12_000)} min={0} />
          <p className="caption-mono">W / {fmt0(s.batteryPct) ?? '—'}% CHARGE / {batteryStateText(s.batteryState, s.onAc).toUpperCase()} / BATTERY TEMP N/A · WINDOWS ACPI</p>
        </Panel>
        <Panel className="side-panel">
          <PanelHeading title="Stream integrity" right={<Chip tone={healthy ? 'accent' : 'amber'}>{healthy ? 'LIVE' : 'DEGRADED'}</Chip>} />
          <div>
            <Spec label="Sample cadence" value={stream.cadenceMs ? `${stream.cadenceMs.toFixed(0)} ms` : '—'} />
            <Spec label="Last received" value={utcClock(stream.lastAt, true)} />
            <Spec label="Dropped batches / 60s" value={`${stream.dropped60s} / ${expected ?? '—'}`} tone={stream.dropped60s ? 'amber' : 'accent'} />
            <Spec label="End-to-end latency" value={stream.latencyMs !== null ? `${stream.latencyMs.toFixed(0)} ms` : '—'} />
          </div>
          <p className="note--muted note">Measured in this browser from WebSocket batch sequence numbers and sample timestamps.</p>
        </Panel>
      </div>
    </>
  )
}
