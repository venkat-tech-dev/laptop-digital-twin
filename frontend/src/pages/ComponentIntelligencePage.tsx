import { useMemo, useState, type ReactNode } from 'react'

import { utcClock } from '../app/derive'
import { exportJson } from '../app/exportData'
import { batteryStateText, bytesGB, cleanCpuName, cleanGpuName, fmt0, fmt1, ghz, healthLabel, healthTone, rateMBps, summarize } from '../app/liveData'
import { useApi } from '../hooks/useApi'
import { useInventory, arr, obj, str } from '../hooks/useInventory'
import { stats, useHistorySeries } from '../hooks/useSeries'
import { api } from '../services/api'
import { useTwinStore } from '../stores/twinStore'
import type { MetricReading, TwinComponent } from '../types/telemetry'
import { num, readingOf, readingsOf } from '../utils/twin'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, DataTable, MetricCard, Panel, PanelHeading, Spec, Tabs, Track, type MetricCardProps, type SpecTone } from '../ui/primitives'
import { fmtHm, timeAxis } from '../ui/time'
import { TimeSeries } from '../ui/TimeSeries'

type TabId = 'cpu' | 'gpu' | 'ram' | 'ssd' | 'battery' | 'cooling'
const TABS: { id: TabId; label: string }[] = [
  { id: 'cpu', label: 'CPU' },
  { id: 'gpu', label: 'GPU' },
  { id: 'ram', label: 'RAM' },
  { id: 'ssd', label: 'SSD' },
  { id: 'battery', label: 'Battery' },
  { id: 'cooling', label: 'Cooling' },
]

interface ChartDef {
  title: string
  keys: (string | null | undefined)[]
  value: string
  min?: number
  max?: number
  caption: (st: { avg: number | null; max: number | null }) => string
}

const val = (r: MetricReading | undefined, f: (v: number) => string): { value: string; tone?: SpecTone } =>
  r && r.availability === 'available' && typeof r.value === 'number' ? { value: f(r.value) } : { value: 'Unavailable', tone: 'na' }

const lhm = (r: MetricReading | undefined, f: (v: number) => string): { value: string; tone?: SpecTone; title?: string } =>
  r && r.availability === 'available' && typeof r.value === 'number'
    ? { value: f(r.value) }
    : { value: 'Unavailable', tone: 'na', title: r?.reason ?? 'Requires LibreHardwareMonitor running as administrator' }

function srcOf(r: MetricReading | undefined): string {
  return r ? r.source.replace(/\s*\((.*)\)/, ' · $1') : '—'
}

function HistoryPanel({ def }: { def: ChartDef }) {
  const { points, start, end } = useHistorySeries(def.keys, 60, 20)
  const primary = def.keys[0] ? points[def.keys[0]] ?? [] : []
  const st = stats(primary)
  return (
    <Panel style={{ flex: '1 0 0' }}>
      <PanelHeading title={def.title} right={<p className="panel-value">{def.value}</p>} />
      <TimeSeries series={def.keys.map((k, i) => ({ points: k ? points[k] ?? [] : [], tone: i === 0 ? 'primary' : 'secondary' }))}
        start={start} end={end} height={110} min={def.min} max={def.max} axis={timeAxis(start, end, 4, fmtHm)} maxGapMs={60_000}
        emptyText={def.keys[0] ? 'NO SAMPLES IN THE LAST HOUR' : 'SENSOR UNAVAILABLE'} />
      <p className="caption-mono">{def.caption(st)}</p>
    </Panel>
  )
}

export function ComponentIntelligencePage({ initial }: { initial: string | null }) {
  const [tab, setTab] = useState<TabId>(TABS.some((t) => t.id === initial) ? (initial as TabId) : 'cpu')
  const components = useTwinStore((st) => st.components)
  const { inventory } = useInventory()
  const thermal = useApi(() => api.thermal(60), [], 60_000)
  const s = summarize(components)
  const inv = obj(inventory)
  const throttledS = (thermal.data?.throttled_seconds as number | undefined) ?? null
  const peakOf = (key: string | undefined) =>
    ((thermal.data?.sensors as { metric_key: string; max_c: number | null }[] | undefined) ?? []).find((x) => x.metric_key === key)?.max_c ?? null

  const content = useMemo((): {
    eyebrow: string; title: string; chip: string; line: string; metrics: MetricCardProps[]; side: ReactNode; charts: [ChartDef, ChartDef];
    detail: ReactNode; provenance: { label: string; value: string; tone?: SpecTone; title?: string }[]; note: string; component?: TwinComponent
  } => {
    const cpuInv = obj(inv.cpu)
    const healthSide = (comp: TwinComponent | undefined, title: string) => (
      <Panel className="side-panel">
        <PanelHeading title={title} right={<Chip tone={healthTone(comp?.health)}>{healthLabel(comp?.health)}</Chip>} />
        <div className="health-score"><p className="health-score__value health-score__value--md">{comp?.health.score ?? '—'}</p><p className="health-score__max">/ 100</p></div>
        <Track percent={comp?.health.score ?? 0} tone={healthTone(comp?.health) === 'accent' ? 'accent' : 'amber'} />
        {(comp?.health.reasons ?? []).slice(0, 3).map((r) => (
          <Spec key={r.message} label={r.message.replace(/\s*\(.*\)$/, '')} value={r.impact ? `${r.impact}` : 'OK'} tone={r.impact ? 'amber' : 'accent'} />
        ))}
        <p className="note">State: {comp?.current_state?.replace(/_/g, ' ') ?? 'unknown'}. Health is rule-based and explainable.</p>
      </Panel>
    )

    switch (tab) {
      case 'cpu': {
        const cores = readingsOf(s.cpu, 'cpu.core_usage_percent').sort((a, b) => Number(a.labels.core) - Number(b.labels.core))
        const avgCore = cores.length ? cores.reduce((a, r) => a + (num(r) ?? 0), 0) / cores.length : null
        const temp = s.temp
        const peak = peakOf(temp?.key)
        const lo = 40
        const hi = 100
        const cur = temp?.value ?? null
        const pct = cur === null ? 0 : ((cur - lo) / (hi - lo)) * 100
        const usage = readingOf(s.cpu, 'cpu.usage_percent')
        const topo = obj(obj(inventory).cpu_topology) as { hybrid?: boolean; source?: string; performance_logical?: number[]; efficient_logical?: number[]; performance_cores?: number; efficient_cores?: number }
        const pSet = new Set(topo.hybrid ? topo.performance_logical ?? [] : [])
        const eSet = new Set(topo.hybrid ? topo.efficient_logical ?? [] : [])
        const avgOf = (set: Set<number>) => {
          const vals = cores.filter((r) => set.has(Number(r.labels.core))).map((r) => num(r) ?? 0)
          return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null
        }
        const busy = (load: number | null) => (load === null ? '—' : load >= 70 ? 'Busy' : load >= 25 ? 'Active' : 'Light load')
        const clusters = topo.hybrid
          ? [
              { cluster: `Performance cores · ${topo.performance_cores} (${pSet.size} threads)`, clock: ghz(s.cpuFreqMhz), load: avgOf(pSet), state: busy(avgOf(pSet)) },
              { cluster: `Efficient cores · ${topo.efficient_cores} (${eSet.size} threads)`, clock: ghz(s.cpuFreqMhz), load: avgOf(eSet), state: busy(avgOf(eSet)) },
            ]
          : [{ cluster: `All logical processors · ${cores.length}`, clock: ghz(s.cpuFreqMhz), load: avgCore, state: s.cpu?.current_state ?? '—' }]
        return {
          eyebrow: `CPU / ${str(cpuInv.manufacturer) ?? '—'} / SOCKET ${str(cpuInv.socket) ?? '—'}${topo.hybrid ? ` / ${topo.performance_cores}P + ${topo.efficient_cores}E` : ''}`,
          title: cleanCpuName(s.cpu?.model), chip: 'MEASURED',
          line: `${str(cpuInv.cores) ?? '—'} cores · ${str(cpuInv.threads) ?? '—'} threads · ${cpuInv.l3_cache_kb ? `${(Number(cpuInv.l3_cache_kb) / 1024).toFixed(0)} MB L3 cache` : 'L3 —'} · ${cpuInv.l2_cache_kb ? `${(Number(cpuInv.l2_cache_kb) / 1024).toFixed(1)} MB L2` : ''}`,
          metrics: [
            { label: 'UTILIZATION', value: fmt0(s.cpuUsage), unit: '%', caption: `${cores.length} logical processors` },
            { label: 'EFFECTIVE CLOCK', value: ghz(s.cpuFreqMhz), unit: 'GHz', caption: `${ghz(s.cpuNominalMhz) ?? '—'} GHz nominal` },
            { label: 'PACKAGE POWER', value: fmt1(num(readingOf(s.cpu, 'cpu.package_power_w'))), unit: 'W',
              caption: readingOf(s.cpu, 'cpu.package_power_w')?.availability === 'available' ? 'LibreHardwareMonitor' : 'Requires LibreHardwareMonitor (admin)',
              title: readingOf(s.cpu, 'cpu.package_power_w')?.reason ?? undefined },
            { label: temp?.isPackageSensor ? 'PACKAGE TEMP' : 'CPU-AREA TEMP', value: cur !== null ? cur.toFixed(0) : null, unit: '°C', tone: cur !== null && cur >= 80 ? 'amber' : 'default',
              caption: `${peak !== null ? `${peak.toFixed(0)}°C peak / last hour` : 'peak —'}${temp && !temp.isPackageSensor ? ' · ACPI zone' : ''}` },
          ],
          side: (
            <Panel className="side-panel">
              <PanelHeading title="Thermal envelope" right={<Chip tone={cur !== null && cur >= 80 ? 'amber' : 'accent'}>{cur === null ? 'NO SENSOR' : cur >= 90 ? 'HOT' : cur >= 80 ? 'ADVISORY' : 'NOMINAL'}</Chip>} />
              <div className="thermal-range">
                <div className="thermal-range__scale"><p>{lo}°C</p><p className="thermal-range__current">{cur !== null ? `${cur.toFixed(0)}°C CURRENT` : 'NO READING'}</p><p>{hi}°C REF</p></div>
                <div className="thermal-range__track">
                  <div style={{ width: `${Math.max(0, Math.min(100, pct))}%`, background: 'var(--accent)' }} />
                  <div style={{ position: 'absolute', left: `${((80 - lo) / (hi - lo)) * 100}%`, width: `${((90 - 80) / (hi - lo)) * 100}%`, top: 0, bottom: 0, background: 'var(--amber-bg)' }} />
                </div>
              </div>
              <Spec label={`Headroom to ${hi}°C reference`} value={cur !== null ? `${(hi - cur).toFixed(0)}°C` : 'Unavailable'} tone={cur !== null ? 'default' : 'na'} />
              <Spec label="Throttled time / 1h" value={throttledS === null ? '—' : `${throttledS} s`} tone="accent" />
              <p className="note">{temp ? `${temp.label}. ${temp.isPackageSensor ? '' : 'This firmware sensor is near the SoC, not the CPU die. '}Firmware passive limit ${s.passiveLimit !== null ? `${s.passiveLimit.toFixed(0)}%` : 'unknown'}.` : 'No temperature sensor is exposed.'}</p>
            </Panel>
          ),
          charts: [
            { title: 'Utilization history', keys: ['cpu.usage_percent'], value: `${fmt0(s.cpuUsage) ?? '—'}%`, min: 0, max: 100,
              caption: (st) => `AVG ${fmt0(st.avg) ?? '—'}% / MAX ${fmt0(st.max) ?? '—'}% · ${srcOf(usage).toUpperCase()} · 1 HOUR` },
            { title: 'Package temperature & power', keys: [temp?.key, readingOf(s.cpu, 'cpu.package_power_w')?.availability === 'available' ? 'cpu.package_power_w' : null],
              value: `${cur !== null ? `${cur.toFixed(0)}°C` : '—'} / ${fmt1(num(readingOf(s.cpu, 'cpu.package_power_w'))) ?? '—'} W`,
              caption: () => `CYAN TEMPERATURE${temp && !temp.isPackageSensor ? ' (ACPI ZONE)' : ''} / BLUE POWER ${readingOf(s.cpu, 'cpu.package_power_w')?.availability === 'available' ? '' : '(UNAVAILABLE)'} · 1 HOUR` },
          ],
          detail: (
            <Panel style={{ flex: '868 0 0' }}>
              <PanelHeading title="Core activity" right={<p className="panel-meta">{cores.length} LOGICAL PROCESSORS</p>} />
              <div className="core-bars">
                {cores.map((r) => {
                  const v = num(r) ?? 0
                  return (
                    <div key={r.key} className="core-bar">
                      <p className="core-bar__pct">{v.toFixed(0)}%</p>
                      <div className="core-bar__scale"><div className="core-bar__fill" style={{ height: `${v}%` }} /></div>
                      <p className="core-bar__label">{pSet.has(Number(r.labels.core)) ? 'P' : eSet.has(Number(r.labels.core)) ? 'E' : 'CPU'}{r.labels.core}</p>
                    </div>
                  )
                })}
              </div>
              <DataTable rowKey={(r) => r.cluster} rows={clusters}
                columns={[
                  { key: 'c', header: 'CLUSTER', width: 210, kind: 'primary', render: (r) => r.cluster },
                  { key: 'k', header: 'ACTIVE CLOCK', width: 230, render: (r) => (r.clock ? `${r.clock} GHz` : '—') },
                  { key: 'l', header: 'AVG LOAD', width: 200, render: (r) => (r.load !== null ? `${r.load.toFixed(0)}%` : '—') },
                  { key: 's', header: 'STATE', grow: true, kind: 'accent', render: (r) => r.state.replace(/_/g, ' ') },
                ]} />
              <p className="note--muted note">{topo?.hybrid
                ? `Core types from ${String(topo.source ?? 'Windows')}. Logical processors ${(topo.performance_logical as number[]).join(', ')} are performance cores; the rest are efficient cores. Clock is the package-wide effective clock (per-core clocks need LibreHardwareMonitor).`
                : topo ? 'All cores report the same efficiency class (not a hybrid CPU).' : 'Core topology not reported by the agent yet.'}</p>
            </Panel>
          ),
          provenance: [
            { label: 'PL1 / sustained limit', ...lhm(readingOf(s.cpu, 'cpu.power_limit_pl1_w'), (v) => `${v.toFixed(0)} W`) },
            { label: 'PL2 / burst limit', ...lhm(readingOf(s.cpu, 'cpu.power_limit_pl2_w'), (v) => `${v.toFixed(0)} W`) },
            { label: 'Core voltage', ...lhm(readingOf(s.cpu, 'cpu.core_voltage_v'), (v) => `${v.toFixed(3)} V`) },
            { label: 'Utilization source', value: srcOf(usage) },
            { label: 'Temperature source', value: temp ? temp.label : 'Unavailable', tone: temp ? 'default' : 'na' },
            { label: 'Last sample', value: usage ? utcClock(Date.parse(usage.timestamp)) : '—' },
          ],
          note: readingOf(s.cpu, 'cpu.package_power_w')?.availability === 'available'
            ? 'Package power, limits and voltage from LibreHardwareMonitor; utilization measured by Windows.'
            : 'Package temperature, power, limits and voltage need LibreHardwareMonitor running as administrator (scripts/install-lhm.ps1). Core utilization and core types are measured by Windows.',
          component: s.cpu,
        }
      }
      case 'gpu': {
        const g = s.gpu
        const engines = readingsOf(g, 'gpu.engine_usage_percent')
        const usage = readingOf(g, 'gpu.usage_percent')
        return {
          eyebrow: `GPU / ${str(g?.properties.vendor) ?? '—'} / ${g?.properties.integrated ? 'INTEGRATED' : 'DISCRETE'}`, title: cleanGpuName(g?.name), chip: 'MEASURED',
          line: `Driver ${str(g?.properties.driver_version) ?? '—'} · ${str(g?.properties.resolution) ?? '—'} @ ${str(g?.properties.refresh_rate_hz) ?? '—'} Hz · ${bytesGB(s.gpuDedicatedTotal)} dedicated`,
          metrics: [
            { label: 'UTILIZATION', value: fmt0(s.gpuUsage), unit: '%', caption: `${engines.length} engine types` },
            { label: 'SHARED MEMORY', value: s.gpuSharedUsed !== null ? (s.gpuSharedUsed / 1024 ** 3).toFixed(1) : null, unit: 'GB', caption: `of ${bytesGB(num(readingOf(g, 'gpu.shared_memory_total_bytes')))} shared limit` },
            { label: 'CORE CLOCK', value: fmt0(num(readingOf(g, 'gpu.core_clock_mhz'))), unit: 'MHz', caption: 'Requires LibreHardwareMonitor' },
            { label: 'GPU TEMP', value: s.gpuTemp?.availability === 'available' ? Number(s.gpuTemp.value).toFixed(0) : null, unit: '°C', caption: s.gpuTemp?.reason?.split(':')[0] ?? 'Measured' },
          ],
          side: healthSide(g, 'GPU health'),
          charts: [
            { title: 'Utilization history', keys: [usage?.key], value: `${fmt0(s.gpuUsage) ?? '—'}%`, min: 0, max: 100, caption: (st) => `AVG ${fmt0(st.avg) ?? '—'}% / MAX ${fmt0(st.max) ?? '—'}% · GPU ENGINE COUNTERS · 1 HOUR` },
            { title: 'Shared memory in use', keys: [readingOf(g, 'gpu.shared_memory_used_bytes')?.key], value: bytesGB(s.gpuSharedUsed), caption: () => 'GPU ADAPTER MEMORY COUNTERS · 1 HOUR' },
          ],
          detail: (
            <Panel style={{ flex: '868 0 0' }}>
              <PanelHeading title="Engine activity" right={<p className="panel-meta">{engines.length} ENGINE TYPES</p>} />
              <DataTable rowKey={(r) => r.key} rows={engines.sort((a, b) => (num(b) ?? 0) - (num(a) ?? 0))}
                columns={[
                  { key: 'e', header: 'ENGINE', width: 260, kind: 'primary', render: (r) => r.labels.engine || 'Other' },
                  { key: 'u', header: 'UTILIZATION', width: 200, render: (r) => `${(num(r) ?? 0).toFixed(1)}%` },
                  { key: 'b', header: 'LOAD', grow: true, render: (r) => <Track percent={num(r) ?? 0} /> },
                ]} />
            </Panel>
          ),
          provenance: [
            { label: 'Adapter LUID', value: str(g?.properties.luid) ?? '—' },
            { label: 'Utilization source', value: srcOf(usage) },
            { label: 'Memory source', value: srcOf(readingOf(g, 'gpu.shared_memory_used_bytes')) },
            { label: 'Power', ...val(readingOf(g, 'gpu.power_w'), (v) => `${v.toFixed(1)} W`) },
            { label: 'Last sample', value: usage ? utcClock(Date.parse(usage.timestamp)) : '—' },
          ],
          note: 'GPU temperature, clock and power need LibreHardwareMonitor (or a vendor API) and administrator rights.',
          component: g,
        }
      }
      case 'ram': {
        const mods = arr(obj(inv.memory).modules)
        const usage = readingOf(components.memory, 'memory.usage_percent')
        return {
          eyebrow: `MEMORY / ${str(mods[0]?.type) ?? '—'} / ${mods.length} MODULE${mods.length === 1 ? '' : 'S'}`, title: `${bytesGB(s.memTotal)} system memory`, chip: 'MEASURED',
          line: mods.map((m) => `${bytesGB(Number(m.capacity_bytes) || null)} ${str(m.manufacturer) ?? ''} ${str(m.part_number) ?? ''} @ ${str(m.configured_speed_mts) ?? '—'} MT/s`).join(' · ') || 'Module details unavailable',
          metrics: [
            { label: 'IN USE', value: s.memUsed !== null ? (s.memUsed / 1024 ** 3).toFixed(1) : null, unit: 'GB', caption: `${fmt0(s.memPct) ?? '—'}% of ${bytesGB(s.memTotal)}` },
            { label: 'AVAILABLE', value: s.memAvail !== null ? (s.memAvail / 1024 ** 3).toFixed(1) : null, unit: 'GB', caption: 'Free + standby' },
            { label: 'PAGE FILE', value: fmt0(num(readingOf(components.memory, 'memory.swap_percent'))), unit: '%', caption: bytesGB(num(readingOf(components.memory, 'memory.swap_used_bytes'))) + ' used' },
            { label: 'SPEED', value: str(mods[0]?.configured_speed_mts), unit: 'MT/s', caption: `${str(mods[0]?.form_factor) ?? '—'} · SMBIOS` },
          ],
          side: healthSide(components.memory, 'Memory health'),
          charts: [
            { title: 'Utilization history', keys: ['memory.usage_percent'], value: `${fmt0(s.memPct) ?? '—'}%`, min: 0, max: 100, caption: (st) => `AVG ${fmt0(st.avg) ?? '—'}% / MAX ${fmt0(st.max) ?? '—'}% · 1 HOUR` },
            { title: 'Page file usage', keys: ['memory.swap_percent'], value: `${fmt0(num(readingOf(components.memory, 'memory.swap_percent'))) ?? '—'}%`, min: 0, max: 100, caption: () => 'PSUTIL PAGE FILE · 1 HOUR' },
          ],
          detail: (
            <Panel style={{ flex: '868 0 0' }}>
              <PanelHeading title="Installed modules" right={<p className="panel-meta">{mods.length} DETECTED</p>} />
              <DataTable rowKey={(m) => String(m.slot)} rows={mods} empty="No module information"
                columns={[
                  { key: 's', header: 'SLOT', width: 230, kind: 'primary', render: (m) => str(m.slot) ?? '—' },
                  { key: 'c', header: 'CAPACITY', width: 130, render: (m) => bytesGB(Number(m.capacity_bytes) || null) },
                  { key: 't', header: 'TYPE / SPEED', width: 200, render: (m) => `${str(m.type) ?? '—'} / ${str(m.configured_speed_mts) ?? '—'} MT/s` },
                  { key: 'p', header: 'PART', grow: true, render: (m) => `${str(m.manufacturer) ?? ''} ${str(m.part_number) ?? ''}` },
                ]} />
            </Panel>
          ),
          provenance: [
            { label: 'Source', value: srcOf(usage) },
            { label: 'Module inventory', value: 'WMI Win32_PhysicalMemory' },
            { label: 'Last sample', value: usage ? utcClock(Date.parse(usage.timestamp)) : '—' },
          ],
          note: 'Memory figures are measured by Windows. Module details come from SMBIOS via WMI.',
          component: components.memory,
        }
      }
      case 'ssd': {
        const d = s.disk
        const vols = readingsOf(components.storage, 'disk.usage_percent')
        const active = readingOf(d, 'disk.active_time_percent')
        return {
          eyebrow: `STORAGE / ${str(d?.properties.bus_type) ?? '—'} / ${str(d?.properties.media_type) ?? '—'}`, title: d?.model ?? 'Storage', chip: 'MEASURED',
          line: `${bytesGB(Number(d?.properties.size_bytes) || null)} · firmware ${str(d?.properties.firmware) ?? '—'} · ${str(d?.properties.partitions) ?? '—'} partitions`,
          metrics: [
            { label: 'ACTIVE TIME', value: fmt0(s.diskActive), unit: '%', caption: `queue ${fmt1(num(readingOf(d, 'disk.queue_length'))) ?? '—'}` },
            { label: 'READ', value: rateMBps(s.diskReadBps), unit: 'MB/s', caption: `${fmt0(num(components.storage?.telemetry['disk.read_ops_per_sec'])) ?? '—'} IOPS` },
            { label: 'WRITE', value: rateMBps(s.diskWriteBps), unit: 'MB/s', caption: `${fmt0(num(components.storage?.telemetry['disk.write_ops_per_sec'])) ?? '—'} IOPS` },
            { label: 'DRIVE TEMP', value: fmt0(num(readingOf(d, 'disk.smart_temperature_c')) ?? num(readingOf(d, 'disk.temperature_c'))), unit: '°C',
              tone: (num(readingOf(d, 'disk.smart_temperature_c')) ?? 0) >= 70 ? 'amber' : 'default',
              caption: readingOf(d, 'disk.smart_temperature_c')?.availability === 'available' ? 'NVMe SMART composite' : readingOf(d, 'disk.smart_temperature_c')?.reason ?? 'Waiting for SMART read' },
          ],
          side: healthSide(d, 'Drive health'),
          charts: [
            { title: 'Active time', keys: [active?.key], value: `${fmt0(s.diskActive) ?? '—'}%`, min: 0, max: 100, caption: (st) => `AVG ${fmt0(st.avg) ?? '—'}% / MAX ${fmt0(st.max) ?? '—'}% · 1 HOUR` },
            { title: 'Throughput / Read + Write', keys: ['disk.read_bytes_per_sec', 'disk.write_bytes_per_sec'], value: `${rateMBps(s.diskReadBps) ?? '—'} / ${rateMBps(s.diskWriteBps) ?? '—'} MB/s`, caption: () => 'CYAN READ / BLUE WRITE · 1 HOUR' },
          ],
          detail: (
            <Panel style={{ flex: '868 0 0' }}>
              <PanelHeading title="Volumes" right={<p className="panel-meta">{vols.length} MOUNTED</p>} />
              <DataTable rowKey={(r) => r.key} rows={vols}
                columns={[
                  { key: 'v', header: 'VOLUME', width: 140, kind: 'primary', render: (r) => r.labels.volume },
                  { key: 'f', header: 'FILE SYSTEM', width: 140, render: (r) => r.labels.fstype ?? '—' },
                  { key: 'u', header: 'USED', width: 140, render: (r) => `${(num(r) ?? 0).toFixed(1)}%` },
                  { key: 'b', header: 'CAPACITY', grow: true, render: (r) => <Track percent={num(r) ?? 0} tone={(num(r) ?? 0) >= 90 ? 'amber' : 'accent'} /> },
                ]} />
            </Panel>
          ),
          provenance: [
            { label: 'Windows drive health', value: String(readingOf(d, 'disk.health_status')?.value ?? 'Unavailable'), tone: readingOf(d, 'disk.health_status')?.value === 'Healthy' ? 'accent' : 'amber' },
            { label: 'Avg read latency', ...val(readingOf(d, 'disk.avg_read_latency_ms'), (v) => `${v.toFixed(2)} ms`) },
            { label: 'Avg write latency', ...val(readingOf(d, 'disk.avg_write_latency_ms'), (v) => `${v.toFixed(2)} ms`) },
            { label: 'SMART wear (percentage used)', ...val(readingOf(d, 'disk.wear_percent'), (v) => `${v.toFixed(0)}%`) },
            { label: 'Available spare', ...val(readingOf(d, 'disk.available_spare_percent'), (v) => `${v.toFixed(0)}%`) },
            { label: 'Power-on hours', ...val(readingOf(d, 'disk.power_on_hours'), (v) => `${v.toLocaleString()} h`) },
            { label: 'Media errors / unsafe shutdowns', value: readingOf(d, 'disk.media_errors')?.availability === 'available' ? `${readingOf(d, 'disk.media_errors')?.value} / ${readingOf(d, 'disk.unsafe_shutdowns')?.value ?? '—'}` : 'Unavailable',
              tone: Number(readingOf(d, 'disk.media_errors')?.value ?? 0) > 0 ? 'amber' : readingOf(d, 'disk.media_errors')?.availability === 'available' ? 'default' : 'na' },
            { label: 'Data written', ...val(readingOf(d, 'disk.data_written_bytes'), (v) => `${(v / 1e12).toFixed(2)} TB`) },
            { label: 'Last sample', value: active ? utcClock(Date.parse(active.timestamp)) : '—' },
          ],
          note: 'SMART values come from the NVMe health log read through the Windows storage stack (no administrator rights needed).',
          component: d,
        }
      }
      case 'battery': {
        const b = components.battery
        const r = (m: string) => readingOf(b, m)
        return {
          eyebrow: `BATTERY / ${str(b?.properties.chemistry) ?? '—'} / ${str(b?.properties.name) ?? '—'}`, title: `${str(b?.manufacturer) ?? ''} battery`.trim(), chip: 'MEASURED',
          line: `${fmt1(num(r('battery.design_capacity_wh'))) ?? '—'} Wh design · ${fmt1(num(r('battery.full_charge_capacity_wh'))) ?? '—'} Wh full charge · ${fmt0(num(r('battery.cycle_count'))) ?? '—'} cycles`,
          metrics: [
            { label: 'CHARGE', value: fmt0(s.batteryPct), unit: '%', caption: batteryStateText(s.batteryState, s.onAc) },
            { label: 'CAPACITY HEALTH', value: fmt1(s.batteryHealth), unit: '%', caption: 'Full charge ÷ design (derived)' },
            { label: 'VOLTAGE', value: num(r('battery.voltage_v'))?.toFixed(2) ?? null, unit: 'V', caption: 'ACPI battery driver' },
            { label: 'TIME REMAINING', value: num(r('battery.time_remaining_s')) !== null ? ((num(r('battery.time_remaining_s')) as number) / 3600).toFixed(1) : null, unit: 'h', caption: r('battery.time_remaining_s')?.reason ?? 'Windows estimate' },
          ],
          side: healthSide(b, 'Battery health'),
          charts: [
            { title: 'Charge level', keys: ['battery.charge_percent'], value: `${fmt0(s.batteryPct) ?? '—'}%`, min: 0, max: 100, caption: () => 'WINDOWS POWER SUBSYSTEM · 1 HOUR' },
            { title: 'Charge / discharge power', keys: ['battery.charge_rate_w', 'battery.discharge_rate_w'], value: `+${fmt1(num(r('battery.charge_rate_w'))) ?? '—'} / −${fmt1(num(r('battery.discharge_rate_w'))) ?? '—'} W`, caption: () => 'CYAN CHARGE / BLUE DISCHARGE · 1 HOUR' },
          ],
          detail: (
            <Panel style={{ flex: '868 0 0' }}>
              <PanelHeading title="Capacity" right={<p className="panel-meta">FUEL GAUGE</p>} />
              <DataTable rowKey={(x) => x.k} rows={[
                { k: 'Design capacity', v: r('battery.design_capacity_wh'), u: 'Wh' },
                { k: 'Full charge capacity', v: r('battery.full_charge_capacity_wh'), u: 'Wh' },
                { k: 'Remaining capacity', v: r('battery.remaining_capacity_wh'), u: 'Wh' },
                { k: 'Cycle count', v: r('battery.cycle_count'), u: '' },
              ]}
                columns={[
                  { key: 'k', header: 'MEASURE', width: 260, kind: 'primary', render: (x) => x.k },
                  { key: 'v', header: 'VALUE', width: 200, render: (x) => (x.v?.availability === 'available' ? `${Number(x.v.value).toFixed(x.u ? 2 : 0)} ${x.u}` : 'Unavailable') },
                  { key: 's', header: 'SOURCE', grow: true, render: (x) => (x.v ? x.v.source : '—') },
                ]} />
            </Panel>
          ),
          provenance: [
            { label: 'Charge rate', ...val(r('battery.charge_rate_w'), (v) => `${v.toFixed(1)} W`) },
            { label: 'Discharge rate', ...val(r('battery.discharge_rate_w'), (v) => `${v.toFixed(1)} W`) },
            { label: 'Battery temperature', value: 'Unavailable', tone: 'na' },
            { label: 'Last sample', value: r('battery.charge_percent') ? utcClock(Date.parse(r('battery.charge_percent')!.timestamp)) : '—' },
          ],
          note: 'Capacity and cycle data are reported by the battery fuel gauge through the Windows ACPI driver and powercfg.',
          component: b,
        }
      }
      default: {
        const zones = readingsOf(components.thermal_sensors, 'thermal.zone_temperature_c')
        const temp = s.temp
        return {
          eyebrow: 'COOLING / FAN + HEAT PIPE / ACPI THERMAL ZONES', title: 'Cooling system', chip: s.fanRpm !== null ? 'MEASURED' : 'PARTIAL',
          line: `${zones.length} thermal zone${zones.length === 1 ? '' : 's'} · fan tachometer ${s.fanRpm !== null ? 'available' : 'not exposed'}`,
          metrics: [
            { label: 'FAN SPEED', value: s.fanRpm !== null ? s.fanRpm.toFixed(0) : null, unit: 'RPM', caption: s.fanRpm !== null ? 'Tachometer' : 'Not exposed to Windows' },
            { label: 'ZONE TEMP', value: temp ? temp.value.toFixed(1) : null, unit: '°C', tone: temp && temp.value >= 80 ? 'amber' : 'default', caption: temp?.label ?? '—' },
            { label: 'PASSIVE LIMIT', value: s.passiveLimit !== null ? s.passiveLimit.toFixed(0) : null, unit: '%', caption: s.throttling ? 'Firmware is throttling' : '100% = no passive cooling' },
            { label: 'THROTTLED / 1H', value: throttledS !== null ? String(throttledS) : null, unit: 's', caption: 'From ACPI passive limit history' },
          ],
          side: healthSide(components.thermal_sensors, 'Thermal health'),
          charts: [
            { title: 'Thermal zone temperature', keys: [temp?.key], value: temp ? `${temp.value.toFixed(0)}°C` : '—', caption: (st) => `AVG ${fmt0(st.avg) ?? '—'}°C / MAX ${fmt0(st.max) ?? '—'}°C · ACPI · 1 HOUR` },
            { title: 'Fan speed', keys: [s.fanRpm !== null ? s.fanReading?.key : null], value: s.fanRpm !== null ? `${s.fanRpm.toFixed(0)} RPM` : 'Unavailable', caption: () => (s.fanRpm !== null ? 'TACHOMETER · 1 HOUR' : 'FAN RPM NOT EXPOSED ON THIS MACHINE') },
          ],
          detail: (
            <Panel style={{ flex: '868 0 0' }}>
              <PanelHeading title="Thermal zones" right={<p className="panel-meta">{zones.length} ZONES</p>} />
              <DataTable rowKey={(r) => r.key} rows={zones}
                columns={[
                  { key: 'z', header: 'ZONE', width: 200, kind: 'primary', render: (r) => r.labels.zone },
                  { key: 't', header: 'TEMPERATURE', width: 200, render: (r) => `${(num(r) ?? 0).toFixed(1)}°C` },
                  { key: 'l', header: 'PASSIVE LIMIT', width: 200, render: (r) => { const l = readingsOf(components.thermal_sensors, 'thermal.passive_limit_percent').find((x) => x.labels.zone === r.labels.zone); return l ? `${num(l)?.toFixed(0)}%` : '—' } },
                  { key: 's', header: 'STATE', grow: true, kind: 'accent', render: () => components.thermal_sensors?.current_state ?? '—' },
                ]} />
            </Panel>
          ),
          provenance: [
            { label: 'Fan source', value: s.fanReading?.availability === 'available' ? s.fanReading.source : 'Unavailable', tone: s.fanReading?.availability === 'available' ? 'default' : 'na' },
            { label: 'Zone source', value: srcOf(zones[0]) },
            { label: 'Last sample', value: zones[0] ? utcClock(Date.parse(zones[0].timestamp)) : '—' },
          ],
          note: s.fanReading?.reason ?? 'Fan speed is measured by the embedded-controller tachometer.',
          component: components.cooling,
        }
      }
    }
  }, [tab, components, s, inv, throttledS, thermal.data]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <PageHeading title="Component Intelligence" subtitle="Sensor-level understanding. Physical context. Historical perspective."
        action={<Button icon="download" onClick={() => exportJson(`component-${tab}`, content.component ?? null)}>Export snapshot</Button>} />
      <Tabs tabs={TABS} value={tab} onChange={setTab} />

      <div className="split">
        <Panel style={{ flex: '868 0 0' }}>
          <PanelHeading eyebrow={content.eyebrow} title={content.title} right={<Chip tone={content.chip === 'MEASURED' ? 'accent' : 'amber'}>{content.chip}</Chip>} />
          <p className="note">{content.line}</p>
          <div className="metric-row metric-row--tight">
            {content.metrics.map((m) => <MetricCard key={m.label} {...m} />)}
          </div>
        </Panel>
        {content.side}
      </div>

      <div className="split split--even">
        <HistoryPanel key={`${tab}-a`} def={content.charts[0]} />
        <HistoryPanel key={`${tab}-b`} def={content.charts[1]} />
      </div>

      <div className="split">
        {content.detail}
        <Panel className="side-panel">
          <PanelHeading title="Power & sensor provenance" />
          <div>{content.provenance.map((p) => <Spec key={p.label} label={p.label} value={p.value} tone={p.tone} title={p.title} />)}</div>
          <p className="note--muted note">{content.note}</p>
        </Panel>
      </div>
    </>
  )
}
