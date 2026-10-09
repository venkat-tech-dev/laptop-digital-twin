/** Shared live-value selectors used by several screens (all values come from twin telemetry). */
import type { Anomaly, ComponentHealth, MetricReading, TwinComponent } from '../types/telemetry'
import { formatBytes } from '../utils/format'
import { cpuTemperature, firstOfType, num, readingOf, readingsOf, type Components } from '../utils/twin'

export const GB = 1024 ** 3

export const fmt0 = (v: number | null | undefined) => (v === null || v === undefined ? null : v.toFixed(0))
export const fmt1 = (v: number | null | undefined) => (v === null || v === undefined ? null : v.toFixed(1))
export const ghz = (mhz: number | null) => (mhz === null ? null : (mhz / 1000).toFixed(2))
export const gb = (bytes: number | null) => (bytes === null ? null : (bytes / GB).toFixed(1))

export interface LiveSummary {
  cpu: TwinComponent | undefined
  gpu: TwinComponent | undefined
  disk: TwinComponent | undefined
  cpuUsage: number | null
  cpuFreqMhz: number | null
  cpuNominalMhz: number | null
  temp: ReturnType<typeof cpuTemperature>
  passiveLimit: number | null
  throttling: boolean
  gpuUsage: number | null
  gpuTemp: MetricReading | undefined
  gpuSharedUsed: number | null
  gpuDedicatedTotal: number | null
  memUsed: number | null
  memTotal: number | null
  memAvail: number | null
  memPct: number | null
  battery: TwinComponent | undefined
  batteryPct: number | null
  batteryHealth: number | null
  batteryState: string | null
  onAc: boolean | null
  fanRpm: number | null
  fanReading: MetricReading | undefined
  diskActive: number | null
  diskReadBps: number | null
  diskWriteBps: number | null
  netRxBps: number | null
  netTxBps: number | null
}

export function summarize(c: Components): LiveSummary {
  const cpu = c.cpu
  const gpu = firstOfType(c, 'gpu')
  const disk = firstOfType(c, 'disk')
  const limits = readingsOf(c.thermal_sensors, 'thermal.passive_limit_percent').map(num).filter((v): v is number => v !== null)
  const plugged = readingOf(c.battery, 'battery.power_plugged')
  const fanReading = readingOf(c.fan, 'fan.speed_rpm')
  return {
    cpu,
    gpu,
    disk,
    cpuUsage: num(readingOf(cpu, 'cpu.usage_percent')),
    cpuFreqMhz: num(readingOf(cpu, 'cpu.frequency_mhz')),
    cpuNominalMhz: num(readingOf(cpu, 'cpu.nominal_frequency_mhz')),
    temp: cpuTemperature(c),
    passiveLimit: limits.length ? Math.min(...limits) : null,
    throttling: limits.some((v) => v < 100),
    gpuUsage: num(readingOf(gpu, 'gpu.usage_percent')),
    gpuTemp: readingOf(gpu, 'gpu.temperature_c'),
    gpuSharedUsed: num(readingOf(gpu, 'gpu.shared_memory_used_bytes')),
    gpuDedicatedTotal: num(readingOf(gpu, 'gpu.dedicated_memory_total_bytes')),
    memUsed: num(readingOf(c.memory, 'memory.used_bytes')),
    memTotal: num(readingOf(c.memory, 'memory.total_bytes')),
    memAvail: num(readingOf(c.memory, 'memory.available_bytes')),
    memPct: num(readingOf(c.memory, 'memory.usage_percent')),
    battery: c.battery,
    batteryPct: num(readingOf(c.battery, 'battery.charge_percent')),
    batteryHealth: num(readingOf(c.battery, 'battery.health_percent')),
    batteryState: c.battery?.current_state ?? null,
    onAc: plugged?.availability === 'available' ? plugged.value === true : null,
    fanRpm: num(fanReading),
    fanReading,
    diskActive: num(readingOf(disk, 'disk.active_time_percent')),
    diskReadBps: num(c.storage?.telemetry['disk.read_bytes_per_sec']),
    diskWriteBps: num(c.storage?.telemetry['disk.write_bytes_per_sec']),
    netRxBps: num(c.network?.telemetry['network.rx_bytes_per_sec']),
    netTxBps: num(c.network?.telemetry['network.tx_bytes_per_sec']),
  }
}

/** "13th Gen Intel(R) Core(TM) i5-1335U" -> "Intel Core i5-1335U" */
export function cleanCpuName(name: string | null | undefined): string {
  if (!name) return 'CPU'
  return name
    .replace(/\(R\)|\(TM\)|®|™/g, '')
    .replace(/^\d+(st|nd|rd|th) Gen\s+/i, '')
    .replace(/\s+CPU.*$/i, '')
    .replace(/\s+/g, ' ')
    .trim()
}

export function cleanGpuName(name: string | null | undefined): string {
  return (name ?? 'GPU').replace(/\(R\)|\(TM\)/g, '').replace(/\s+/g, ' ').trim()
}

export function batteryStateText(state: string | null, onAc: boolean | null): string {
  if (state === 'charging') return 'Charging'
  if (state === 'discharging') return 'On battery'
  if (state === 'full') return 'AC power · full'
  if (state === 'idle_on_ac') return 'AC power'
  if (state === 'absent') return 'No battery'
  return onAc === null ? 'Power state unknown' : onAc ? 'AC power' : 'On battery'
}

export type Tone = 'accent' | 'amber' | 'critical'

export function healthTone(h: ComponentHealth | undefined | null): Tone {
  if (!h) return 'accent'
  return h.status === 'critical' ? 'critical' : h.status === 'warning' ? 'amber' : 'accent'
}

export function healthLabel(h: ComponentHealth | undefined | null): string {
  if (!h || h.score === null) return 'NOT OBSERVABLE'
  if (h.status === 'critical') return 'CRITICAL'
  if (h.status === 'warning') return 'ADVISORY'
  return h.reasons.some((r) => r.severity === 'warning') ? 'ADVISORY' : 'NOMINAL'
}

export function severityLabel(a: Anomaly): { text: string; tone: Tone } {
  if (a.severity === 'critical') return { text: 'CRITICAL', tone: 'critical' }
  if (a.severity === 'warning') return { text: 'ADVISORY', tone: 'amber' }
  return { text: 'INFORMATIONAL', tone: 'accent' }
}

export function bytesShort(v: number | null): string {
  return v === null ? '—' : formatBytes(v)
}

export function rateMBps(v: number | null): string | null {
  return v === null ? null : (v / 1e6).toFixed(1)
}

export function rateMbps(v: number | null): string | null {
  return v === null ? null : ((v * 8) / 1e6).toFixed(2)
}

/** Deterministic, rule-specific guidance (no generated text). */
export const RECOMMENDATIONS: Record<string, string> = {
  cpu_temp_high: 'Check that the intake and exhaust vents are unobstructed and reduce sustained load. Re-evaluate after the workload completes.',
  cpu_temp_critical: 'Reduce load immediately and verify cooling. Sustained operation near the junction limit forces throttling.',
  thermal_zone_hot: 'Check that the vents are unobstructed and the laptop is on a hard surface. Consider a balanced power plan during sustained work.',
  thermal_zone_critical: 'Reduce load immediately and verify cooling.',
  thermal_throttling: 'Firmware is limiting performance to stay within thermal limits. Improve airflow or reduce sustained load.',
  memory_pressure: 'Close memory-heavy applications or browser tabs. Sustained pressure causes paging to the SSD.',
  memory_exhaustion: 'Close applications now to avoid paging stalls and out-of-memory errors.',
  disk_space_low: 'Free space on the volume (temporary files, downloads, old installers).',
  disk_space_critical: 'Free space now: Windows needs headroom for updates, paging and hibernation.',
  disk_latency_high: 'Check for heavy background I/O (indexing, antivirus, updates) in System Processes.',
  disk_write_latency_high: 'Check for heavy background I/O (indexing, antivirus, updates) in System Processes.',
  disk_health_warning: 'Back up important data and run the manufacturer diagnostics for this drive.',
  battery_health_degraded: 'Capacity is below 80% of design. Plan a battery replacement if runtime is insufficient.',
  battery_low: 'Connect AC power.',
  battery_critical: 'Connect AC power now.',
  cpu_saturation: 'Identify the top CPU consumer in System Processes.',
  network_errors: 'Check the network adapter driver and link quality.',
  fan_max_prolonged: 'Check the vents and fan for dust; the fan is running at its observed maximum.',
}

export function recommendationFor(a: Anomaly): string {
  if (a.rule_id.startsWith('stat:')) return 'Statistical deviation from this device\'s own baseline. Check System Processes for the workload that coincides with the start time.'
  return RECOMMENDATIONS[a.rule_id] ?? 'Review the metric history around the start time.'
}

export function bytesGB(v: number | null): string {
  return v === null ? '—' : `${(v / GB).toFixed(1)} GB`
}
