import type { MetricReading, MetricValue } from '../types/telemetry'

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB']

export function formatBytes(bytes: number | null | undefined, digits = 1): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—'
  let value = Math.abs(bytes)
  let unit = 0
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${(Math.sign(bytes) * value).toFixed(unit === 0 ? 0 : digits)} ${BYTE_UNITS[unit]}`
}

export function formatRate(bytesPerSec: number | null | undefined): string {
  if (bytesPerSec === null || bytesPerSec === undefined) return '—'
  return `${formatBytes(bytesPerSec)}/s`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h ${m}m`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s % 60}s`
  return `${s}s`
}

export function formatAge(ms: number | null): string {
  if (ms === null) return '—'
  if (ms < 1000) return `${(ms / 1000).toFixed(1)} s`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  return formatDuration(ms / 1000)
}

/** Human formatting of a reading's value in its canonical unit. */
export function formatValue(value: MetricValue, unit: string, digits = 1): string {
  if (value === null || value === undefined) return 'Unavailable'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'string') return humanizeState(value)
  switch (unit) {
    case 'percent':
      return `${value.toFixed(digits)} %`
    case 'percent_of_nominal':
      return `${value.toFixed(0)} %`
    case 'celsius':
      return `${value.toFixed(digits)} °C`
    case 'bytes':
      return formatBytes(value)
    case 'B/s':
      return formatRate(value)
    case 'MHz':
      return value >= 1000 ? `${(value / 1000).toFixed(2)} GHz` : `${value.toFixed(0)} MHz`
    case 'W':
      return `${value.toFixed(digits)} W`
    case 'Wh':
      return `${value.toFixed(2)} Wh`
    case 'V':
      return `${value.toFixed(2)} V`
    case 's':
      return formatDuration(value)
    case 'ms':
      return `${value.toFixed(2)} ms`
    case 'rpm':
      return `${value.toFixed(0)} rpm`
    case 'Mbps':
      return value >= 1000 ? `${(value / 1000).toFixed(1)} Gbps` : `${value.toFixed(0)} Mbps`
    case 'count':
      return value.toLocaleString()
    default:
      return `${Number.isInteger(value) ? value : value.toFixed(digits)} ${unit}`.trim()
  }
}

export function formatReading(r: MetricReading | undefined | null, digits = 1): string {
  if (!r || r.availability !== 'available') return 'Unavailable'
  return formatValue(r.value, r.unit, digits)
}

export function humanizeState(state: string): string {
  return state.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export function metricLabel(metric: string): string {
  const name = metric.split('.').slice(1).join('.') || metric
  return humanizeState(name.replace(/_(percent|bytes|mhz|c|w|wh|v|s|ms|rpm|mbps|per_sec)$/i, ''))
}
