import type { ComponentType, DeviceStatus, MetricReading, TwinComponent } from '../types/telemetry'

export type Components = Record<string, TwinComponent>

export function firstOfType(components: Components, type: ComponentType): TwinComponent | undefined {
  return Object.values(components).find((c) => c.component_type === type)
}

export function ofType(components: Components, type: ComponentType): TwinComponent[] {
  return Object.values(components).filter((c) => c.component_type === type)
}

/** Unlabelled reading for a metric, or the first labelled one. */
export function readingOf(component: TwinComponent | undefined, metric: string): MetricReading | undefined {
  if (!component) return undefined
  return component.telemetry[metric] ?? Object.values(component.telemetry).find((r) => r.metric === metric)
}

export function readingsOf(component: TwinComponent | undefined, metric: string): MetricReading[] {
  if (!component) return []
  return Object.values(component.telemetry).filter((r) => r.metric === metric)
}

export function num(r: MetricReading | undefined): number | null {
  return r && r.availability === 'available' && typeof r.value === 'number' ? r.value : null
}

export interface CpuTemperature {
  value: number
  key: string
  label: string
  source: string
  isPackageSensor: boolean
}

/** Mirrors backend `cpu_temperature`: package sensor first, else hottest ACPI thermal zone (labelled). */
export function cpuTemperature(components: Components): CpuTemperature | null {
  const pkg = readingOf(components.cpu, 'cpu.temperature_c')
  if (num(pkg) !== null && pkg) {
    return { value: num(pkg) as number, key: pkg.key, label: 'CPU package sensor', source: pkg.source, isPackageSensor: true }
  }
  const zones = readingsOf(components.thermal_sensors, 'thermal.zone_temperature_c').filter((r) => num(r) !== null)
  if (zones.length === 0) return null
  const hottest = zones.reduce((a, b) => ((num(a) ?? 0) >= (num(b) ?? 0) ? a : b))
  return {
    value: num(hottest) as number,
    key: hottest.key,
    label: `ACPI thermal zone ${hottest.labels.zone ?? ''}`.trim(),
    source: hottest.source,
    isPackageSensor: false,
  }
}

export type ThermalBand = 'normal' | 'elevated' | 'hot' | 'critical' | 'unknown'

export function thermalBand(c: number | null): ThermalBand {
  if (c === null) return 'unknown'
  if (c >= 98) return 'critical'
  if (c >= 90) return 'hot'
  if (c >= 80) return 'elevated'
  return 'normal'
}

/** Everything the 3D model needs. Built from LIVE telemetry or from a SIMULATION trajectory, never both. */
export interface VisualState {
  source: 'live' | 'simulation'
  fresh: boolean
  cpuUsage: number | null
  coreUsage: number[]
  cpuTempC: number | null
  cpuTempLabel: string | null
  throttling: boolean
  gpuUsage: number | null
  hasDiscreteGpu: boolean
  memoryPercent: number | null
  diskActivePercent: number | null
  diskReadBps: number | null
  diskWriteBps: number | null
  netRxBps: number | null
  netTxBps: number | null
  netLinkUp: boolean | null
  batteryPercent: number | null
  batteryState: string | null
  batteryPresent: boolean
  fanRpm: number | null
  displayBrightness: number | null
}

export function liveVisualState(components: Components, status: DeviceStatus): VisualState {
  const cpu = components.cpu
  const gpu = firstOfType(components, 'gpu')
  const temp = cpuTemperature(components)
  const cores = readingsOf(cpu, 'cpu.core_usage_percent')
    .sort((a, b) => Number(a.labels.core) - Number(b.labels.core))
    .map((r) => num(r) ?? 0)
  const limits = readingsOf(components.thermal_sensors, 'thermal.passive_limit_percent').map(num)
  const nics = ofType(components, 'network_adapter')
  const linkUp = nics.length ? nics.some((n) => readingOf(n, 'network.link_up')?.value === true) : null
  const battery = components.battery
  const fanReadings = readingsOf(components.fan, 'fan.speed_rpm').map(num).filter((v): v is number => v !== null)
  return {
    source: 'live',
    fresh: status === 'LIVE' || status === 'DEGRADED',
    cpuUsage: num(readingOf(cpu, 'cpu.usage_percent')),
    coreUsage: cores,
    cpuTempC: temp?.value ?? null,
    cpuTempLabel: temp?.label ?? null,
    throttling: limits.some((v) => v !== null && v < 100),
    gpuUsage: num(readingOf(gpu, 'gpu.usage_percent')),
    hasDiscreteGpu: ofType(components, 'gpu').some((g) => g.properties.integrated === false),
    memoryPercent: num(readingOf(components.memory, 'memory.usage_percent')),
    diskActivePercent: num(readingOf(firstOfType(components, 'disk'), 'disk.active_time_percent')),
    diskReadBps: num(components.storage?.telemetry['disk.read_bytes_per_sec']),
    diskWriteBps: num(components.storage?.telemetry['disk.write_bytes_per_sec']),
    netRxBps: num(components.network?.telemetry['network.rx_bytes_per_sec']),
    netTxBps: num(components.network?.telemetry['network.tx_bytes_per_sec']),
    netLinkUp: linkUp,
    batteryPercent: num(readingOf(battery, 'battery.charge_percent')),
    batteryState: battery?.current_state ?? null,
    batteryPresent: battery?.current_state !== 'absent',
    fanRpm: fanReadings.length ? Math.max(...fanReadings) : null,
    displayBrightness: num(readingOf(components.display, 'display.brightness_percent')),
  }
}

/** Map a 3D part id to the twin component id present on this device. */
export function resolvePartComponent(components: Components, part: string): string | null {
  switch (part) {
    case 'gpu':
      return firstOfType(components, 'gpu')?.component_id ?? null
    case 'disk':
      return firstOfType(components, 'disk')?.component_id ?? 'storage'
    case 'wifi': {
      const nics = ofType(components, 'network_adapter')
      return (nics.find((n) => String(n.properties.type) === 'wifi') ?? nics[0])?.component_id ?? 'network'
    }
    default:
      return components[part] ? part : null
  }
}
