import type { ChipTone } from '../ui/primitives'
import type { Connectivity, Freshness, TwinField, TwinHealth, Visual } from '../types/twinDoc'

/** Severity -> design-system tone. Normal is calm (no colour), never "green = good" noise. */
export const VISUAL_TONE: Record<Visual, ChipTone> = {
  normal: 'accent',
  elevated: 'accent',
  warning: 'amber',
  critical: 'critical',
  offline: 'muted',
  unknown: 'muted',
}

export const HEALTH_TONE: Record<TwinHealth, ChipTone> = { HEALTHY: 'accent', WARNING: 'amber', CRITICAL: 'critical', UNKNOWN: 'muted' }
export const CONNECTIVITY_TONE: Record<Connectivity, ChipTone> = {
  ONLINE: 'accent',
  DEGRADED: 'amber',
  STALE: 'amber',
  OFFLINE: 'critical',
  UNKNOWN: 'muted',
}
export const CONNECTIVITY_LABEL: Record<Connectivity, string> = {
  ONLINE: 'LIVE',
  DEGRADED: 'DEGRADED',
  STALE: 'STALE',
  OFFLINE: 'OFFLINE',
  UNKNOWN: 'UNKNOWN',
}

export const FRESHNESS_TEXT: Record<Freshness, string> = {
  LIVE: 'Live',
  RECENT: 'Recent',
  STALE: 'Stale',
  OFFLINE: 'Device offline',
  UNKNOWN: 'No data yet',
  UNSUPPORTED: 'Unsupported',
}

export function isField(v: unknown): v is TwinField {
  return typeof v === 'object' && v !== null && 'freshness' in v && 'interval_s' in v
}

export function ageText(iso: string | null | undefined, now: number): string {
  if (!iso) return '—'
  const s = Math.max(0, (now - Date.parse(iso)) / 1000)
  if (s < 2) return 'just now'
  if (s < 90) return `${Math.round(s)} s ago`
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 172800) return `${(s / 3600).toFixed(1)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

const BYTES = ['B', 'KB', 'MB', 'GB', 'TB']

/** Wording for boolean fields (true, false) - never a bare On/Off where it would be ambiguous. */
const BOOL_TEXT: Record<string, [string, string]> = {
  'network.internet_connected': ['Online', 'No internet'],
  'network.device_connected': ['Connected', 'Disconnected'],
  'security.realtime_protection': ['Protected', 'Protection off'],
  'security.antivirus_enabled': ['On', 'Off'],
  'security.firewall_enabled': ['On', 'Off'],
  'security.secure_boot': ['On', 'Off'],
  'storage.health_ok': ['Healthy', 'Not healthy'],
  'storage.smart_critical_warning': ['Warning', 'None'],
  'thermal.throttling': ['Throttling', 'No'],
  'operating_system.reboot_required': ['Yes', 'No'],
}

/** Human value of a field. ``null`` when unknown - callers render an explicit state, never 0. */
export function formatField(f: TwinField | undefined, path?: string): { value: string; unit: string } | null {
  if (!f || f.value === null || f.value === undefined) return null
  const v = f.value
  if (typeof v === 'boolean') {
    const words = (path && BOOL_TEXT[path]) || ['On', 'Off']
    return { value: v ? words[0] : words[1], unit: '' }
  }
  if (typeof v !== 'number') return { value: String(v), unit: '' }
  switch (f.unit) {
    case '%':
      return { value: v.toFixed(v < 10 ? 1 : 0), unit: '%' }
    case '°C':
      return { value: v.toFixed(0), unit: '°C' }
    case 'bytes': {
      let n = v
      let i = 0
      while (n >= 1024 && i < BYTES.length - 1) {
        n /= 1024
        i++
      }
      return { value: n.toFixed(n < 10 ? 1 : 0), unit: BYTES[i] }
    }
    case 'B/s': {
      const mbit = (v * 8) / 1e6
      return mbit >= 1 ? { value: mbit.toFixed(mbit < 10 ? 1 : 0), unit: 'Mbit/s' } : { value: ((v * 8) / 1e3).toFixed(0), unit: 'kbit/s' }
    }
    case 'ms':
      return { value: v.toFixed(v < 10 ? 1 : 0), unit: 'ms' }
    case 's': {
      const h = v / 3600
      return h >= 24 ? { value: (h / 24).toFixed(1), unit: 'days' } : { value: h.toFixed(1), unit: 'h' }
    }
    default:
      return { value: Number.isInteger(v) ? String(v) : v.toFixed(1), unit: f.unit === 'count' ? '' : f.unit }
  }
}

export function freshnessTone(f: Freshness): ChipTone {
  return f === 'LIVE' || f === 'RECENT' ? 'accent' : f === 'STALE' ? 'amber' : 'muted'
}
