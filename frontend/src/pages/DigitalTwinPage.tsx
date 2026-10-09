import { useMemo, useRef, useState } from 'react'

import leaders from '../assets/figma/overview/component-leaders.svg'
import heroImage from '../assets/figma/overview/laptop-hero.jpg'
import connections from '../assets/figma/twin/telemetry-connections.svg'
import cutaway from '../assets/figma/twin/laptop-cutaway.jpg'
import { captureViewport } from '../app/capture'
import { useCoverage } from '../app/derive'
import { exportJson } from '../app/exportData'
import { batteryStateText, bytesGB, cleanCpuName, cleanGpuName, fmt0, fmt1, gb, ghz, rateMBps, summarize, type LiveSummary } from '../app/liveData'
import { partPoint, useModelTwin, type ModelPart } from '../app/modelTwin'
import { navigate } from '../app/routes'
import { Twin3D } from '../app/Twin3D'
import { useInventory, obj, str } from '../hooks/useInventory'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { useNow } from '../hooks/useNow'
import { useLiveSeries } from '../hooks/useSeries'
import { useTwinStore } from '../stores/twinStore'
import type { MetricReading } from '../types/telemetry'
import { thermalBand, readingOf, type Components } from '../utils/twin'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, Icon, Spec, type SpecTone } from '../ui/primitives'
import { TimeSeries } from '../ui/TimeSeries'
import { ActiveAnomaliesPanel } from '../anomaly/AnomalyViews'
import { DiagnosedPanel } from '../diagnosis/DiagnosisPanel'
import { TwinRemediationPanel } from '../remediation/RemediationViews'
import { PredictiveInsightsPanel } from '../prediction/PredictionViews'
import { TwinHeader, TwinHealthPanel, TwinMetricBoard, TwinTimeline } from '../twin/TwinPanels'

type Part = 'cpu' | 'gpu' | 'ram' | 'ssd' | 'battery' | 'cooling' | 'board'
type View = 'assembled' | 'cutaway' | 'thermal'

/** Positions in the 1312 × 782 design frame (percentages keep them aligned with the render). */
const P = (x: number, y: number) => ({ left: `${(x / 1312) * 100}%`, top: `${(y / 782) * 100}%` })
const ANCHORS: Record<Part, { dot: [number, number]; label: [number, number]; name: string }> = {
  cpu: { dot: [660, 442], label: [464, 289], name: 'CPU' },
  gpu: { dot: [745, 473], label: [847, 390], name: 'GPU' },
  ram: { dot: [585, 415], label: [414, 451], name: 'RAM' },
  ssd: { dot: [815, 500], label: [847, 557], name: 'SSD' },
  battery: { dot: [575, 559], label: [432, 635], name: 'BATTERY' },
  cooling: { dot: [587, 387], label: [353, 357], name: 'FANS' },
  board: { dot: [715, 489], label: [779, 644], name: 'BOARD' },
}
/** Physical part of the 3D twin for each selector entry, and the label offset (px) from its anchor. */
const MODEL_PART: Record<Part, { part: ModelPart; offset: [number, number] }> = {
  cpu: { part: 'cpu', offset: [-150, -120] },
  gpu: { part: 'gpu', offset: [170, -70] },
  ram: { part: 'memory', offset: [-170, 40] },
  ssd: { part: 'disk', offset: [150, 70] },
  battery: { part: 'battery', offset: [-170, 80] },
  cooling: { part: 'fan', offset: [-150, -60] },
  board: { part: 'motherboard', offset: [150, 140] },
}
const PART_COMPONENT: Record<Part, string> = { cpu: 'cpu', gpu: 'gpu', ram: 'memory', ssd: 'disk', battery: 'battery', cooling: 'cooling', board: 'motherboard' }

function partValues(s: LiveSummary): Record<Part, { title: string; short: string; anchor: string }> {
  return {
    cpu: { title: 'CPU', short: `${fmt0(s.cpuUsage) ?? '—'}% / ${s.temp ? `${s.temp.value.toFixed(0)}°C` : 'temp N/A'}`, anchor: s.temp ? `${s.temp.value.toFixed(0)}°C` : `${fmt0(s.cpuUsage) ?? '—'}%` },
    gpu: { title: 'GPU', short: `${fmt0(s.gpuUsage) ?? '—'}% / ${s.gpuTemp?.availability === 'available' ? `${Number(s.gpuTemp.value).toFixed(0)}°C` : 'temp N/A'}`, anchor: `${fmt0(s.gpuUsage) ?? '—'}% load` },
    ram: { title: 'RAM', short: `${gb(s.memUsed) ?? '—'} / ${s.memTotal ? (s.memTotal / 1024 ** 3).toFixed(0) : '—'} GB`, anchor: `${fmt0(s.memPct) ?? '—'}%` },
    ssd: { title: 'SSD', short: `${fmt0(s.diskActive) ?? '—'}% active`, anchor: `${fmt0(s.diskActive) ?? '—'}% active` },
    battery: { title: 'Battery', short: `${fmt0(s.batteryPct) ?? '—'}% / ${s.onAc ? 'AC' : s.onAc === false ? 'battery' : '—'}`, anchor: `${fmt0(s.batteryPct) ?? '—'}%` },
    cooling: { title: 'Cooling', short: s.fanRpm !== null ? `${s.fanRpm.toLocaleString()} RPM` : 'RPM unavailable', anchor: s.fanRpm !== null ? `${s.fanRpm.toLocaleString()} RPM` : 'RPM N/A' },
    board: { title: 'Motherboard', short: 'no sensors / passive', anchor: 'NO SENSOR' },
  }
}

interface InspectorData {
  chip: string
  title: string
  sub: string
  big: string | null
  unit: string
  seriesKey: string | null
  min?: number
  max?: number
  specs: { label: string; value: string; tone?: SpecTone }[]
  provenance: string
  route: string
}

function specOf(r: MetricReading | undefined, fmt: (v: number) => string): { value: string; tone?: SpecTone } {
  if (!r || r.availability !== 'available' || typeof r.value !== 'number') return { value: 'Unavailable', tone: 'na' }
  return { value: fmt(r.value) }
}

function inspectorFor(part: Part, c: Components, s: LiveSummary, inv: Record<string, unknown>, now: number): InspectorData {
  const age = (r: MetricReading | undefined) => (r ? `${Math.max(0, (now - Date.parse(r.timestamp)) / 1000).toFixed(1)} s ago` : 'no sample')
  const cpuInv = obj(inv.cpu)
  switch (part) {
    case 'cpu': {
      const usage = readingOf(c.cpu, 'cpu.usage_percent')
      return {
        chip: 'CPU SELECTED', title: cleanCpuName(s.cpu?.model),
        sub: `${str(cpuInv.cores) ?? '—'} cores / ${str(cpuInv.threads) ?? '—'} threads / ${ghz(s.cpuNominalMhz) ?? '—'} GHz base`,
        big: s.temp ? s.temp.value.toFixed(0) : null, unit: '°C', seriesKey: s.temp?.key ?? null,
        specs: [
          { label: 'Utilization', value: `${fmt0(s.cpuUsage) ?? '—'}%`, tone: 'accent' },
          { label: 'Effective clock', value: s.cpuFreqMhz ? `${ghz(s.cpuFreqMhz)} GHz` : 'Unavailable', tone: s.cpuFreqMhz ? 'default' : 'na' },
          { label: 'Package power', ...specOf(readingOf(c.cpu, 'cpu.package_power_w'), (v) => `${v.toFixed(1)} W`) },
          { label: 'Passive cooling limit', value: s.passiveLimit !== null ? `${s.passiveLimit.toFixed(0)}%` : 'Unavailable', tone: s.passiveLimit !== null ? 'default' : 'na' },
          { label: 'Throttle status', value: s.passiveLimit === null ? 'Unavailable' : s.throttling ? 'Throttling' : 'Not throttling', tone: s.passiveLimit === null ? 'na' : s.throttling ? 'amber' : 'accent' },
        ],
        provenance: `Measured · ${s.temp ? `${s.temp.label} (${s.temp.isPackageSensor ? 'package' : 'not the package sensor'})` : 'no temperature sensor'} · ${age(usage)}`,
        route: 'cpu',
      }
    }
    case 'gpu': {
      const usage = readingOf(s.gpu, 'gpu.usage_percent')
      return {
        chip: 'GPU SELECTED', title: cleanGpuName(s.gpu?.name),
        sub: `${str(s.gpu?.properties.vendor) ?? '—'} / ${s.gpu?.properties.integrated ? 'integrated' : 'discrete'} / ${bytesGB(s.gpuDedicatedTotal)} dedicated`,
        big: fmt0(s.gpuUsage), unit: '%', seriesKey: usage?.key ?? null, min: 0, max: 100,
        specs: [
          { label: 'Temperature', ...specOf(s.gpuTemp, (v) => `${v.toFixed(0)}°C`) },
          { label: 'Shared memory used', ...specOf(readingOf(s.gpu, 'gpu.shared_memory_used_bytes'), (v) => bytesGB(v)) },
          { label: 'Dedicated memory used', ...specOf(readingOf(s.gpu, 'gpu.dedicated_memory_used_bytes'), (v) => bytesGB(v)) },
          { label: 'Core clock', ...specOf(readingOf(s.gpu, 'gpu.core_clock_mhz'), (v) => `${v.toFixed(0)} MHz`) },
          { label: 'Power', ...specOf(readingOf(s.gpu, 'gpu.power_w'), (v) => `${v.toFixed(1)} W`) },
        ],
        provenance: `Measured · Windows GPU engine counters · ${age(usage)}`, route: 'gpu',
      }
    }
    case 'ram': {
      const r = readingOf(c.memory, 'memory.usage_percent')
      const mod = obj((obj(inv.memory).modules as unknown[] | undefined)?.[0])
      return {
        chip: 'RAM SELECTED', title: `${bytesGB(s.memTotal)} ${str(mod.type) ?? ''}`.trim(),
        sub: `${str(mod.manufacturer) ?? '—'} / ${str(mod.configured_speed_mts) ?? '—'} MT/s / ${str(mod.form_factor) ?? '—'}`,
        big: fmt0(s.memPct), unit: '%', seriesKey: 'memory.usage_percent', min: 0, max: 100,
        specs: [
          { label: 'In use', value: bytesGB(s.memUsed) },
          { label: 'Available', value: bytesGB(s.memAvail) },
          { label: 'Page file used', ...specOf(readingOf(c.memory, 'memory.swap_used_bytes'), (v) => bytesGB(v)) },
          { label: 'State', value: c.memory?.current_state ?? '—', tone: c.memory?.current_state === 'normal' ? 'accent' : 'amber' },
        ],
        provenance: `Measured · psutil / GlobalMemoryStatusEx · ${age(r)}`, route: 'ram',
      }
    }
    case 'ssd': {
      const disk = s.disk
      const health = readingOf(disk, 'disk.health_status')
      const active = readingOf(disk, 'disk.active_time_percent')
      return {
        chip: 'SSD SELECTED', title: disk?.model ?? 'Storage',
        sub: `${str(disk?.properties.bus_type) ?? '—'} / ${str(disk?.properties.media_type) ?? '—'} / ${bytesGB(Number(disk?.properties.size_bytes ?? NaN) || null)}`,
        big: fmt0(s.diskActive), unit: '% active', seriesKey: active?.key ?? null, min: 0, max: 100,
        specs: [
          { label: 'Read', value: `${rateMBps(s.diskReadBps) ?? '—'} MB/s` },
          { label: 'Write', value: `${rateMBps(s.diskWriteBps) ?? '—'} MB/s` },
          { label: 'Avg read latency', ...specOf(readingOf(disk, 'disk.avg_read_latency_ms'), (v) => `${v.toFixed(2)} ms`) },
          { label: 'Temperature', ...specOf(readingOf(disk, 'disk.temperature_c'), (v) => `${v.toFixed(0)}°C`) },
          { label: 'Windows drive health', value: health?.availability === 'available' ? String(health.value) : 'Unavailable', tone: health?.value === 'Healthy' ? 'accent' : 'amber' },
        ],
        provenance: `Measured · PhysicalDisk counters · ${age(active)}`, route: 'ssd',
      }
    }
    case 'battery': {
      const b = c.battery
      const charge = readingOf(b, 'battery.charge_percent')
      return {
        chip: 'BATTERY SELECTED', title: `${str(b?.manufacturer) ?? ''} ${str(b?.properties.chemistry) ?? 'Battery'}`.trim(),
        sub: `${str(b?.properties.name) ?? '—'} / ${fmt1(readingOf(b, 'battery.design_capacity_wh')?.value as number) ?? '—'} Wh design`,
        big: fmt0(s.batteryPct), unit: '%', seriesKey: 'battery.charge_percent', min: 0, max: 100,
        specs: [
          { label: 'State', value: batteryStateText(s.batteryState, s.onAc), tone: 'accent' },
          { label: 'Capacity health', value: s.batteryHealth !== null ? `${s.batteryHealth.toFixed(1)}%` : 'Unavailable', tone: s.batteryHealth !== null ? 'default' : 'na' },
          { label: 'Cycle count', ...specOf(readingOf(b, 'battery.cycle_count'), (v) => v.toFixed(0)) },
          { label: 'Voltage', ...specOf(readingOf(b, 'battery.voltage_v'), (v) => `${v.toFixed(2)} V`) },
          { label: 'Discharge rate', ...specOf(readingOf(b, 'battery.discharge_rate_w'), (v) => `${v.toFixed(1)} W`) },
        ],
        provenance: `Measured · Windows ACPI battery driver · ${age(charge)}`, route: 'battery',
      }
    }
    case 'cooling': {
      const zone = s.temp
      return {
        chip: 'COOLING SELECTED', title: 'Cooling system', sub: 'Fan + heat pipe / ACPI thermal zones',
        big: s.fanRpm !== null ? s.fanRpm.toFixed(0) : null, unit: 'RPM', seriesKey: s.fanRpm !== null ? (s.fanReading?.key ?? null) : (zone?.key ?? null),
        specs: [
          { label: 'Fan speed', value: s.fanRpm !== null ? `${s.fanRpm.toFixed(0)} RPM` : 'Unavailable', tone: s.fanRpm !== null ? 'default' : 'na' },
          { label: zone?.isPackageSensor ? 'CPU package' : 'ACPI thermal zone', value: zone ? `${zone.value.toFixed(1)}°C` : 'Unavailable', tone: zone ? 'default' : 'na' },
          { label: 'Passive cooling limit', value: s.passiveLimit !== null ? `${s.passiveLimit.toFixed(0)}%` : 'Unavailable', tone: s.passiveLimit !== null ? 'default' : 'na' },
          { label: 'Thermal band', value: thermalBand(zone?.value ?? null), tone: 'accent' },
        ],
        provenance: s.fanRpm !== null ? 'Measured · fan tachometer' : `Fan RPM unavailable: ${s.fanReading?.reason?.split('.')[0] ?? 'not exposed'} · chart shows the thermal zone`,
        route: 'cooling',
      }
    }
    default: {
      const mb = obj(inv.motherboard)
      const bios = obj(inv.bios)
      return {
        chip: 'MOTHERBOARD SELECTED', title: `${str(mb.manufacturer) ?? ''} ${str(mb.product) ?? 'Motherboard'}`.trim(),
        sub: `${str(mb.version) ?? '—'} / BIOS ${str(bios.version) ?? '—'}`, big: null, unit: '', seriesKey: null,
        specs: [
          { label: 'Board temperature', value: 'Unavailable', tone: 'na' },
          { label: 'VRM sensors', value: 'Unavailable', tone: 'na' },
          { label: 'BIOS vendor', value: str(bios.manufacturer) ?? '—' },
        ],
        provenance: 'No board or VRM sensors are exposed to Windows on this platform.', route: 'cpu',
      }
    }
  }
}

export function DigitalTwinPage({ initial }: { initial: string | null }) {
  const components = useTwinStore((s) => s.components)
  const device = useTwinStore((s) => s.device)
  const { status } = useLiveStatus()
  const coverage = useCoverage()
  const { inventory } = useInventory()
  const now = useNow(1000)
  const [part, setPart] = useState<Part>((initial as Part) in ANCHORS ? (initial as Part) : 'cpu')
  const [view, setView] = useState<View>('cutaway')
  const [mode, setMode] = useState<'inspect' | 'rotate'>('inspect')
  const [zoom, setZoom] = useState(1.25)
  const [annotate, setAnnotate] = useState(true)
  const [nonce, setNonce] = useState(0)
  const twin = useModelTwin()
  const viewportRef = useRef<HTMLDivElement>(null)
  const [capturing, setCapturing] = useState(false)
  const capture = async () => {
    if (!viewportRef.current) return
    setCapturing(true)
    try {
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      await captureViewport(
        viewportRef.current,
        `${device?.manufacturer ?? ''} ${device?.model ?? ''} · ${view.toUpperCase()} · ${twin.label} · ${new Date().toISOString().slice(0, 19).replace('T', ' ')} UTC · LIVE TELEMETRY`,
        `twin-${view}-${stamp}.png`,
      )
    } finally {
      setCapturing(false)
    }
  }

  const s = summarize(components)
  const values = partValues(s)
  const ins = useMemo(() => inspectorFor(part, components, s, obj(inventory), now), [part, components, s, inventory, now])
  const { points, start, end } = useLiveSeries([ins.seriesKey], 60_000)
  const live = status === 'LIVE' || status === 'DEGRADED'
  const band = thermalBand(s.temp?.value ?? null)
  const heat = { normal: 'rgba(105,207,229,0.45)', elevated: 'rgba(217,173,108,0.55)', hot: 'rgba(229,140,90,0.6)', critical: 'rgba(229,115,106,0.65)', unknown: 'rgba(110,124,141,0.3)' }[band]

  return (
    <>
      <PageHeading title="Digital Twin" subtitle="Inspect the physical system through its live digital representation."
        action={(
          <div className="controls-row__group">
            <Button icon="download" onClick={() => exportJson('twin-view', { view, selected: part, component: components[PART_COMPONENT[part]] ?? null })}>Export data</Button>
            <Button icon="arrowRight" disabled={capturing} onClick={capture}>{capturing ? 'Capturing…' : 'Capture view'}</Button>
          </div>
        )} />

      <TwinHeader />

      <div className="viewport viewport--twin" ref={viewportRef}>
        {twin.modelSpecific ? (
          <Twin3D
            view={{ camera: view === 'assembled' ? 'hero' : 'cutaway', xray: view !== 'assembled', explode: view === 'assembled' ? 0 : 1, thermal: view === 'thermal' }}
            interactive={mode === 'rotate'} zoom={zoom / 1.25} nonce={nonce} annotate={annotate}
            fallback={<img className="viewport__fallback" src={view === 'assembled' ? heroImage : cutaway} alt="" />}
            onSelect={(picked) => {
              const hit = (Object.keys(MODEL_PART) as Part[]).find((k) => MODEL_PART[k].part === picked || (picked === 'thermal_sensors' && k === 'cpu'))
              if (hit) setPart(hit)
            }}
            anchors={(Object.keys(ANCHORS) as Part[]).map((p) => ({
              id: p,
              point: partPoint(twin.dims, MODEL_PART[p].part, view === 'assembled'),
              offset: MODEL_PART[p].offset,
              selected: part === p,
              onClick: () => setPart(p),
              label: (
                <span className="telemetry-anchor telemetry-anchor--model">
                  <span className={part === p ? 'telemetry-anchor__name is-selected' : 'telemetry-anchor__name'}>{ANCHORS[p].name}</span>
                  <span className="telemetry-anchor__value">{view === 'thermal' && p !== 'cpu' && p !== 'cooling' ? 'NO SENSOR' : values[p].anchor}</span>
                </span>
              ),
            }))} />
        ) : mode === 'rotate' ? (
          <Twin3D view={{ camera: 'iso', xray: true, explode: 0, thermal: false }} interactive grid />
        ) : (
          <div className="viewport__image" style={{ transform: `scale(${view === 'assembled' ? 1 : zoom / 1.25})` }}>
            <img src={view === 'assembled' ? heroImage : cutaway} alt="Illustrative internal layout of a laptop" />
            {view === 'assembled' ? (annotate ? <img src={leaders} alt="" className="viewport__leaders" /> : null) : (
              <>
                {view === 'thermal' ? <span className="thermal-glow" style={{ ...P(664, 446), background: `radial-gradient(circle, ${heat} 0%, transparent 70%)` }} /> : null}
                {annotate ? <img src={connections} alt="" className="viewport__leaders" /> : null}
                {(Object.keys(ANCHORS) as Part[]).map((p) => (
                  <button key={p} type="button" className={`sensor-anchor ${part === p ? 'is-selected' : ''}`} style={P(...ANCHORS[p].dot)}
                    aria-label={`Select ${ANCHORS[p].name}`} onClick={() => setPart(p)} />
                ))}
                {annotate ? (Object.keys(ANCHORS) as Part[]).map((p) => (
                  <button key={p} type="button" className="telemetry-anchor" style={P(...ANCHORS[p].label)} onClick={() => setPart(p)}>
                    <span className={part === p ? 'telemetry-anchor__name is-selected' : 'telemetry-anchor__name'}>{ANCHORS[p].name}</span>
                    <span className="telemetry-anchor__value">{view === 'thermal' && p !== 'cpu' && p !== 'cooling' ? 'NO SENSOR' : values[p].anchor}</span>
                  </button>
                )) : null}
              </>
            )}
          </div>
        )}

        <div className="viewport__toolbar">
          <div className="viewport__modes">
            <Button active={view === 'assembled'} onClick={() => { setView('assembled'); setMode('inspect') }}>Assembled</Button>
            <Button active={view === 'cutaway'} onClick={() => { setView('cutaway'); setMode('inspect') }}>Component cutaway</Button>
            <Button active={view === 'thermal'} onClick={() => { setView('thermal'); setMode('inspect') }}>Thermal map</Button>
          </div>
          <Chip tone={live ? 'accent' : 'critical'}>{coverage.available} SENSORS · {status}</Chip>
        </div>
        <p className="viewport__model-note" title={twin.source}>{(device?.model ?? 'DEVICE').toUpperCase()} / {twin.modelSpecific ? `${twin.label === 'MODEL PROFILE' ? 'Model-profile twin' : '3D model'}` : 'Illustrative internal layout'}</p>

        <div className="component-selector">
          <p className="eyebrow">PHYSICAL COMPONENTS</p>
          {(Object.keys(ANCHORS) as Part[]).map((p) => (
            <button key={p} type="button" className={`selectable ${part === p ? 'is-selected' : ''}`} onClick={() => setPart(p)}>
              <span className="selectable__title">
                <span>{values[p].title}</span>
                {part === p ? <Icon name="crosshair" size={12} style={{ color: 'var(--accent)' }} /> : null}
              </span>
              <span className="selectable__value">{values[p].short}</span>
            </button>
          ))}
          <p className="note--muted note">Select a component to isolate its sensors and operating state.</p>
        </div>

        <div className="inspector">
          <Chip>{ins.chip}</Chip>
          <p className="inspector__title">{ins.title}</p>
          <p className="inspector__sub">{ins.sub}</p>
          <div className="reading">
            {ins.big !== null ? (
              <>
                <p className="inspector__big">{ins.big}</p>
                <p className="inspector__unit">{ins.unit}</p>
              </>
            ) : <p className="inspector__big inspector__big--na">UNAVAILABLE</p>}
          </div>
          <TimeSeries series={[{ points: ins.seriesKey ? points[ins.seriesKey] ?? [] : [], label: ins.title }]} start={start} end={end}
            height={70} min={ins.min} max={ins.max} axis={['−60s', '−30s', 'now']} emptyText={ins.seriesKey ? 'WAITING FOR SAMPLES' : 'NO SENSOR'} />
          <div>
            {ins.specs.map((sp) => <Spec key={sp.label} label={sp.label} value={sp.value} tone={sp.tone} />)}
          </div>
          <p className="note--muted note">{ins.provenance}</p>
          <Button primary icon="arrowUpRight" onClick={() => navigate('components', ins.route)} style={{ alignSelf: 'flex-start' }}>Component intelligence</Button>
        </div>

        <div className="viewport__controls viewport__controls--twin">
          <Button icon="rotate3d" active={mode === 'rotate'} onClick={() => setMode(mode === 'rotate' ? 'inspect' : 'rotate')}>Rotate</Button>
          <Button icon="zoomIn" active={zoom > 1.25} disabled={!twin.modelSpecific && (mode === 'rotate' || view === 'assembled')} onClick={() => setZoom(zoom >= 1.75 ? 1.25 : zoom + 0.25)}>Zoom</Button>
          <Button icon="scan" active={annotate && mode === 'inspect'} onClick={() => { setMode('inspect'); setAnnotate(!annotate || mode !== 'inspect') }}>Inspect</Button>
          <Button icon="rotateCcw" onClick={() => { setMode('inspect'); setView('cutaway'); setZoom(1.25); setAnnotate(true); setPart('cpu'); setNonce((n) => n + 1) }}>Reset</Button>
        </div>
        <div className="viewport__legend">
          <p className="viewport__legend-key">○ SENSOR LOCATION / ● SELECTED</p>
          <p>{mode === 'rotate' ? `${twin.modelSpecific ? twin.label : 'GENERIC 3D MODEL'} · DRAG TO ORBIT` : `${view === 'thermal' ? 'THERMAL MAP · MEASURED ZONE ONLY' : view === 'assembled' ? 'ASSEMBLED' : 'CUTAWAY'} · PERSPECTIVE / ${Math.round(zoom * 100)}% ZOOM`}</p>
          <p className="viewport__legend-key">HALOS = TWIN SEVERITY · <span className="sev sev--elevated">ELEVATED</span> <span className="sev sev--warning">WARNING</span> <span className="sev sev--critical">CRITICAL</span></p>
        </div>
      </div>

      <TwinMetricBoard />
      <div className="twin-columns">
        <TwinHealthPanel />
        <ActiveAnomaliesPanel />
        <PredictiveInsightsPanel />
        <DiagnosedPanel />
        <TwinRemediationPanel />
        <TwinTimeline />
      </div>
    </>
  )
}
