import { useMemo } from 'react'

import { useNow } from '../hooks/useNow'
import { useTwinStore, type Arrival } from '../stores/twinStore'
import type { DeviceInfo, MetricReading, TwinComponent } from '../types/telemetry'

// ------------------------------------------------------------------ stream statistics
export interface StreamStats {
  cadenceMs: number | null
  lastAt: number | null
  received60s: number
  dropped60s: number
  latencyMs: number | null
}

function median(values: number[]): number | null {
  if (!values.length) return null
  const s = [...values].sort((a, b) => a - b)
  return s[Math.floor(s.length / 2)]
}

/** Derived purely from received telemetry_update events (cadence, sequence gaps, sample→browser latency). */
export function streamStats(arrivals: Arrival[], now: number): StreamStats {
  const recent = arrivals.filter((a) => now - a.at <= 60_000)
  const gaps: number[] = []
  for (let i = 1; i < arrivals.length; i += 1) gaps.push(arrivals[i].at - arrivals[i - 1].at)
  let dropped = 0
  for (let i = 1; i < recent.length; i += 1) {
    const d = recent[i].sequence - recent[i - 1].sequence
    if (d > 1) dropped += d - 1
  }
  return {
    cadenceMs: median(gaps.slice(-30)),
    lastAt: arrivals.length ? arrivals[arrivals.length - 1].at : null,
    received60s: recent.length,
    dropped60s: dropped,
    latencyMs: median(recent.map((a) => a.latencyMs).filter((v): v is number => v !== null)),
  }
}

export function useStreamStats(): StreamStats {
  const arrivals = useTwinStore((s) => s.arrivals)
  const now = useNow(1000)
  return useMemo(() => streamStats(arrivals, now), [arrivals, now])
}

// ------------------------------------------------------------------ sensor coverage
export const PROVIDER_GROUPS: { id: string; label: string; match: RegExp; version: string }[] = [
  { id: 'lhm', label: 'LibreHardwareMonitor', match: /LibreHardwareMonitor/i, version: 'external' },
  { id: 'pdh', label: 'Windows performance counters', match: /Performance Counter/i, version: 'OS native' },
  { id: 'wmi', label: 'Windows WMI / CIM', match: /^(?!.*Defender).*(WMI|Storage Management|MaxClockSpeed)/i, version: 'OS native' },
  { id: 'acpi', label: 'Windows ACPI / power', match: /ACPI|power subsystem|battery report|powercfg/i, version: 'OS native' },
  { id: 'nt', label: 'Windows kernel (NT / DXGI)', match: /NtQuerySystemInformation|DXGI/i, version: 'OS native' },
  { id: 'psutil', label: 'psutil (Win32)', match: /psutil/i, version: 'library' },
  { id: 'smart', label: 'NVMe SMART health log', match: /NVMe SMART/i, version: 'OS native' },
  { id: 'security', label: 'Windows security (Defender / Firewall / TPM / Secure Boot)', match: /TPM Base Services|SecureBoot|Security Center|Firewall|Defender/i, version: 'OS native' },
  { id: 'netstack', label: 'Windows networking (NLM / IP Helper / ICMP)', match: /Network List Manager|IP Helper|ICMP|Agent API client/i, version: 'OS native' },
  { id: 'scm', label: 'Windows services & updates', match: /Service Control Manager|Windows Update Agent/i, version: 'OS native' },
  { id: 'evtlog', label: 'Windows event log (WHEA / crashes / boot)', match: /event log|WHEA/i, version: 'OS native' },
]

export interface ProviderCoverage {
  id: string
  label: string
  version: string
  total: number
  available: number
}

export interface Coverage {
  total: number
  available: number
  providers: ProviderCoverage[]
}

export function sensorCoverage(components: Record<string, TwinComponent>): Coverage {
  const providers = new Map<string, ProviderCoverage>()
  let total = 0
  let available = 0
  for (const c of Object.values(components)) {
    if (c.component_type === 'telemetry_agent' || c.component_id === 'agent') continue
    for (const r of Object.values(c.telemetry)) {
      total += 1
      if (r.availability === 'available') available += 1
      // A derived value can cite two sources; attribute it to the first matching group.
      const group = PROVIDER_GROUPS.find((g) => g.match.test(r.source))
      const key = group?.id ?? 'other'
      const entry = providers.get(key) ?? { id: key, label: group?.label ?? 'Other', version: group?.version ?? '—', total: 0, available: 0 }
      entry.total += 1
      if (r.availability === 'available') entry.available += 1
      providers.set(key, entry)
    }
  }
  const order = PROVIDER_GROUPS.map((g) => g.id)
  return {
    total,
    available,
    providers: [...providers.values()].sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id)),
  }
}

export function useCoverage(): Coverage {
  const components = useTwinStore((s) => s.components)
  return useMemo(() => sensorCoverage(components), [components])
}

// ------------------------------------------------------------------ device identity
export function deviceTitle(d: DeviceInfo | null): string {
  if (!d) return 'NO DEVICE'
  return (d.model ?? d.model_number ?? 'Laptop').toUpperCase()
}

export function deviceSubtitle(d: DeviceInfo | null): string {
  if (!d) return 'START THE TELEMETRY AGENT'
  const os = (d.os_name ?? '').replace(/^Microsoft\s+/i, '').toUpperCase()
  return [d.model_number, os].filter(Boolean).join(' / ')
}

export function shortModel(d: DeviceInfo | null): string {
  return d ? [d.manufacturer, d.model].filter(Boolean).join(' ') : 'No device'
}

// ------------------------------------------------------------------ misc
export function sourceName(r: MetricReading | undefined): string {
  if (!r) return '—'
  return r.source.replace(/\s*\(.*\)/, '')
}

export function utcClock(ms: number | null | undefined, withMillis = false): string {
  if (!ms) return '—'
  const iso = new Date(ms).toISOString()
  return `${iso.slice(11, withMillis ? 23 : 19)} UTC`
}

export function utcDate(ms: number): string {
  return new Date(ms).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' }).toUpperCase()
}
