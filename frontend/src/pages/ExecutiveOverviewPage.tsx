import { useMemo, useState } from 'react'

import leaders from '../assets/figma/overview/component-leaders.svg'
import heroImage from '../assets/figma/overview/laptop-hero.jpg'
import { useCoverage, utcClock } from '../app/derive'
import { exportJson } from '../app/exportData'
import { batteryStateText, cleanCpuName, cleanGpuName, fmt0, gb, ghz, severityLabel, summarize } from '../app/liveData'
import { partPoint, useModelTwin } from '../app/modelTwin'
import { navigate } from '../app/routes'
import { Twin3D } from '../app/Twin3D'
import { useApi } from '../hooks/useApi'
import { useInventory, obj, str } from '../hooks/useInventory'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { api } from '../services/api'
import { useTwinStore } from '../stores/twinStore'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, MetricCard, Panel, PanelHeading, Spec, type ChipTone } from '../ui/primitives'

interface Attention {
  id: string
  label: string
  tone: ChipTone
  title: string
  text: string
  link: string
  go: () => void
}

/** Viewport annotations positioned in the 970 × 450 design frame (as percentages). */
const P = (x: number, y: number) => ({ left: `${(x / 970) * 100}%`, top: `${(y / 450) * 100}%` })

export function ExecutiveOverviewPage() {
  const components = useTwinStore((s) => s.components)
  const overall = useTwinStore((s) => s.overall)
  const active = useTwinStore((s) => s.activeAnomalies)
  const device = useTwinStore((s) => s.device)
  const last = useTwinStore((s) => s.lastTelemetryAt)
  const { status } = useLiveStatus()
  const coverage = useCoverage()
  const { inventory } = useInventory()
  const [mode, setMode] = useState<'inspect' | 'rotate'>('inspect')
  const [zoom, setZoom] = useState(1)
  const [annotate, setAnnotate] = useState(true)
  const [nonce, setNonce] = useState(0)
  const twin = useModelTwin()
  const thermal = useApi(() => api.thermal(60), [], 60_000)

  const s = summarize(components)
  const anomalies = Object.values(active)
  const critical = anomalies.filter((a) => a.severity === 'critical').length
  const advisories = anomalies.length - critical
  const peak = useMemo(() => {
    const sensors = (thermal.data?.sensors as { metric_key: string; max_c: number | null }[] | undefined) ?? []
    const key = s.temp?.key
    return sensors.find((x) => x.metric_key === key)?.max_c ?? null
  }, [thermal.data, s.temp?.key])

  const attention: Attention[] = useMemo(() => {
    const items: Attention[] = anomalies
      .sort((a, b) => (a.severity === b.severity ? 0 : a.severity === 'critical' ? -1 : a.severity === 'warning' && b.severity === 'info' ? -1 : 1))
      .map((a) => {
        const sev = severityLabel(a)
        return {
          id: a.anomaly_id, label: sev.text === 'INFORMATIONAL' ? 'INFORMATION' : sev.text, tone: sev.tone, title: a.title,
          text: `${a.message}. Since ${new Date(a.started_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}.`,
          link: 'Review in Health & Anomalies ↗', go: () => navigate('health'),
        }
      })
    for (const r of overall?.reasons ?? []) {
      if (r.severity === 'ok' || items.length >= 4) continue
      const isBattery = r.metric?.startsWith('battery') ?? false
      items.push({
        id: `reason-${r.message}`, label: r.severity === 'warning' ? 'ADVISORY' : r.severity === 'critical' ? 'CRITICAL' : 'INFORMATION',
        tone: r.severity === 'warning' ? 'amber' : r.severity === 'critical' ? 'critical' : 'accent',
        title: isBattery && s.batteryHealth !== null ? `Battery capacity at ${s.batteryHealth.toFixed(0)}% of design` : r.message.replace(/\s*\(.*\)$/, ''),
        text: r.message + (r.impact ? ` Health impact ${r.impact} points.` : ''),
        link: isBattery ? 'Explore battery history ↗' : 'Review system health ↗',
        go: () => (isBattery ? navigate('components', 'battery') : navigate('health')),
      })
    }
    return items.slice(0, 2)
  }, [anomalies, overall, s.batteryHealth])

  const advisoryCount = attention.filter((a) => a.tone !== 'accent').length
  const infoCount = attention.length - advisoryCount
  const kind = str(obj(inventory).pc_system_type) === 'Mobile' ? 'Mobile computer' : str(obj(inventory).pc_system_type) ?? 'Laptop'
  const healthStatus = overall?.status === 'critical' ? 'CRITICAL' : overall?.status === 'warning' ? 'ADVISORY' : overall?.score === null || !overall ? 'UNKNOWN' : 'STABLE'
  const healthChipTone: ChipTone = overall?.status === 'critical' ? 'critical' : overall?.status === 'warning' ? 'amber' : 'accent'
  const warningReasons = (overall?.reasons ?? []).filter((r) => r.severity === 'warning' || r.severity === 'critical').length
  const healthText = overall?.score === null || !overall
    ? 'Health is not observable yet: waiting for telemetry.'
    : `${overall.status === 'healthy' ? 'Operating within expected limits.' : 'Attention required.'} ${warningReasons ? `${warningReasons} advisor${warningReasons === 1 ? 'y needs' : 'ies need'} attention.` : 'No advisories.'}`

  const live = status === 'LIVE' || status === 'DEGRADED'

  return (
    <>
      <PageHeading title="One device. Complete visibility." subtitle={`Executive overview / ${device?.model ?? 'No device'} / ${kind}`}
        action={<Button icon="download" onClick={async () => exportJson('twin-snapshot', await api.twin())}>Export snapshot</Button>} />

      <div className="overview-row">
        <div className="viewport viewport--hero">
          {twin.modelSpecific ? (
            <Twin3D view={{ camera: 'hero', xray: false, explode: 0, thermal: false }} interactive={mode === 'rotate'} zoom={zoom}
              nonce={nonce} annotate={annotate} fallback={<img className="viewport__fallback" src={heroImage} alt="" />}
              anchors={[
                { id: 'cpu', point: partPoint(twin.dims, 'cpu', true), offset: [-250, -120], label: (
                  <div className="annotation annotation--model">
                    <p className="annotation__title">CPU / {fmt0(s.cpuUsage) ?? '—'}%</p>
                    <p>{s.temp ? `${s.temp.value.toFixed(0)}°C` : 'temp N/A'} · {ghz(s.cpuFreqMhz) ?? '—'} GHz</p>
                  </div>
                ) },
                { id: 'gpu', point: partPoint(twin.dims, 'gpu', true), offset: [240, -130], label: (
                  <div className="annotation annotation--model">
                    <p className="annotation__title">GPU / {fmt0(s.gpuUsage) ?? '—'}%</p>
                    <p>{s.gpuTemp?.availability === 'available' ? `${Number(s.gpuTemp.value).toFixed(0)}°C` : 'temp N/A'} · {gb(s.gpuSharedUsed) ?? '—'} GB shared</p>
                  </div>
                ) },
                { id: 'battery', point: partPoint(twin.dims, 'battery', true), offset: [-300, 40], label: (
                  <div className="annotation annotation--model">
                    <p className="annotation__title">BATTERY / {fmt0(s.batteryPct) ?? '—'}%</p>
                    <p>{batteryStateText(s.batteryState, s.onAc)}</p>
                  </div>
                ) },
              ]} />
          ) : mode === 'rotate' ? (
            <Twin3D view={{ camera: 'iso', xray: true, explode: 0, thermal: false }} interactive grid />
          ) : (
            <div className="viewport__image" style={{ transform: `scale(${zoom})` }}>
              <img src={heroImage} alt={`Illustrative render of a laptop representing ${device?.model ?? 'the device'}`} />
              {annotate ? <img src={leaders} alt="" className="viewport__leaders" /> : null}
              {annotate ? (
                <>
                  {[[441, 311], [541, 326], [546, 366]].map(([x, y]) => <span key={x} className="anchor-dot" style={P(x, y)} />)}
                  <div className="annotation" style={P(71, 140)}>
                    <p className="annotation__title">CPU / {fmt0(s.cpuUsage) ?? '—'}%</p>
                    <p>{s.temp ? `${s.temp.value.toFixed(0)}°C` : 'temp N/A'} · {ghz(s.cpuFreqMhz) ?? '—'} GHz</p>
                  </div>
                  <div className="annotation" style={P(779, 145)}>
                    <p className="annotation__title">GPU / {fmt0(s.gpuUsage) ?? '—'}%</p>
                    <p>{s.gpuTemp?.availability === 'available' ? `${Number(s.gpuTemp.value).toFixed(0)}°C` : 'temp N/A'} · {gb(s.gpuSharedUsed) ?? '—'} GB shared</p>
                  </div>
                  <div className="annotation" style={P(71, 343)}>
                    <p className="annotation__title">BATTERY / {fmt0(s.batteryPct) ?? '—'}%</p>
                    <p>{batteryStateText(s.batteryState, s.onAc)}</p>
                  </div>
                </>
              ) : null}
            </div>
          )}
          <div className="viewport__identity">
            <p className="viewport__eyebrow">PHYSICAL SYSTEM / DIGITAL REPRESENTATION</p>
            <p className="viewport__sub">{device?.model ?? 'No device'} · {twin.modelSpecific ? `${twin.label === 'MODEL PROFILE' ? 'Model-specific 3D twin' : '3D model'} / ${mode === 'rotate' ? 'Drag to orbit' : 'Perspective'}` : `${mode === 'rotate' ? 'Generic 3D model' : 'Illustrative render'} / Perspective`}</p>
          </div>
          <div className="viewport__status">
            <Chip tone={live ? 'accent' : 'critical'} title={twin.source}>{status} · {twin.modelSpecific ? twin.label : mode === 'rotate' ? 'GENERIC 3D' : 'ILLUSTRATIVE'}</Chip>
          </div>
          <div className="viewport__controls">
            <Button icon="rotate3d" active={mode === 'rotate'} onClick={() => setMode(mode === 'rotate' ? 'inspect' : 'rotate')}>Rotate</Button>
            <Button icon="zoomIn" active={zoom > 1} disabled={mode === 'rotate' && !twin.modelSpecific} onClick={() => setZoom(zoom >= 1.5 ? 1 : zoom + 0.25)}>Zoom</Button>
            <Button icon="scan" active={annotate && mode === 'inspect'} onClick={() => {
              setMode('inspect')
              setAnnotate(!annotate || mode !== 'inspect')
            }}>Inspect</Button>
            <Button icon="rotateCcw" onClick={() => {
              setMode('inspect')
              setZoom(1)
              setAnnotate(true)
              setNonce((n) => n + 1)
            }}>Reset</Button>
          </div>
          <p className="viewport__caption">TWIN SYNC / {utcClock(last)}</p>
        </div>

        <Panel className="health-card">
          <PanelHeading eyebrow="DEVICE READINESS" title="System health" right={<Chip tone={healthChipTone}>{healthStatus}</Chip>} />
          <div className="health-score">
            <p className="health-score__value">{overall?.score ?? '—'}</p>
            <p className="health-score__max">/ 100</p>
          </div>
          <p className="note" style={{ fontSize: 12 }}>{healthText}</p>
          <Spec label="Critical anomalies" value={critical} tone={critical ? 'amber' : 'accent'} />
          <Spec label="Active advisories" value={advisories} />
          <Spec label="Sensor coverage" value={`${coverage.available} / ${coverage.total}`} tone="accent" />
          <Button icon="arrowUpRight" onClick={() => navigate('health')} style={{ alignSelf: 'flex-start' }}>Review system health</Button>
        </Panel>
      </div>

      <div className="metric-row">
        <MetricCard label="CPU UTILIZATION" value={fmt0(s.cpuUsage)} unit="%"
          caption={`${cleanCpuName(s.cpu?.model).replace(/^Intel Core /, '')} · ${ghz(s.cpuFreqMhz) ?? '—'} GHz`} />
        <MetricCard label="GPU UTILIZATION" value={fmt0(s.gpuUsage)} unit="%"
          caption={`${cleanGpuName(s.gpu?.name).replace(/^Intel /, '')} · ${s.gpuTemp?.availability === 'available' ? `${Number(s.gpuTemp.value).toFixed(0)}°C` : 'temp N/A'}`} />
        <MetricCard label="MEMORY" value={gb(s.memUsed)} unit={s.memTotal ? `/ ${(s.memTotal / 1024 ** 3).toFixed(0)} GB` : 'GB'}
          caption={s.memPct !== null ? `${s.memPct.toFixed(0)}% in use · ${gb(s.memAvail) ?? '—'} GB free` : 'Unavailable'} />
        <MetricCard label="BATTERY" value={fmt0(s.batteryPct)} unit="%"
          caption={`${batteryStateText(s.batteryState, s.onAc)}${s.batteryHealth !== null ? ` · ${s.batteryHealth.toFixed(0)}% capacity health` : ''}`} />
        <MetricCard label={s.temp?.isPackageSensor ? 'CPU TEMPERATURE' : 'CPU-AREA TEMPERATURE'} value={s.temp ? s.temp.value.toFixed(0) : null} unit="°C"
          tone={s.temp && s.temp.value >= 80 ? 'amber' : 'default'} title={s.temp ? `${s.temp.label} · ${s.temp.source}` : undefined}
          caption={s.temp ? `${peak !== null ? `${peak.toFixed(0)}°C peak · ` : ''}${s.temp.isPackageSensor ? 'package sensor' : 'ACPI thermal zone'}` : 'No temperature sensor exposed'} />
      </div>

      <Panel>
        <PanelHeading title="Attention queue"
          right={<p className="panel-meta">{String(advisoryCount).padStart(2, '0')} ADVISORY / {String(infoCount).padStart(2, '0')} INFORMATIONAL</p>} />
        <div className="attention">
          {attention.length === 0 ? <p className="note">Nothing needs attention. Rules and statistical baselines are evaluating every sample.</p> : null}
          {attention.map((a) => (
            <div key={a.id} className="attention__item">
              <div className="attention__identity">
                <Chip tone={a.tone}>{a.label}</Chip>
                <p className="attention__title">{a.title}</p>
              </div>
              <p className="note">{a.text}</p>
              <button type="button" className="link" onClick={a.go}>{a.link}</button>
            </div>
          ))}
        </div>
      </Panel>
    </>
  )
}
