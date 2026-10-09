import { seriesStore } from '../stores/seriesStore'
import { streamStats, utcClock } from '../app/derive'
import { timeAxis } from '../ui/time'
import type { TwinComponent } from '../types/telemetry'
import { formatBytes, formatValue } from './format'
import { computeStatus } from './freshness'
import { RingBuffer } from './ringBuffer'
import { cpuTemperature, liveVisualState, thermalBand } from './twin'

describe('RingBuffer', () => {
  it('is bounded and keeps the newest items in order', () => {
    const rb = new RingBuffer<number>(3)
    ;[1, 2, 3, 4, 5].forEach((n) => rb.push(n))
    expect(rb.length).toBe(3)
    expect(rb.toArray()).toEqual([3, 4, 5])
    expect(rb.last()).toBe(5)
    expect(rb.filter((n) => n > 3)).toEqual([4, 5])
  })
})

describe('computeStatus', () => {
  const now = 100_000
  it('is OFFLINE whenever the socket is not open', () => {
    expect(computeStatus('closed', now, 'LIVE', now)).toBe('OFFLINE')
  })
  it('ages LIVE -> DEGRADED -> STALE', () => {
    expect(computeStatus('open', now - 1_000, 'LIVE', now)).toBe('LIVE')
    expect(computeStatus('open', now - 4_000, 'LIVE', now)).toBe('DEGRADED')
    expect(computeStatus('open', now - 11_000, 'LIVE', now)).toBe('STALE')
  })
  it('takes the worse of local and backend status', () => {
    expect(computeStatus('open', now - 500, 'OFFLINE', now)).toBe('OFFLINE')
  })
})

describe('format', () => {
  it('formats units and never invents values', () => {
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatValue(null, 'celsius')).toBe('Unavailable')
    expect(formatValue(74.04, 'celsius')).toBe('74.0 °C')
    expect(formatValue(2464, 'MHz')).toBe('2.46 GHz')
    expect(formatValue('idle_on_ac', 'state')).toBe('Idle On Ac')
  })
})

function comp(id: string, type: TwinComponent['component_type'], telemetry: TwinComponent['telemetry'] = {}): TwinComponent {
  return {
    component_id: id, component_type: type, name: id, parent_id: null, manufacturer: null, model: null, properties: {},
    current_state: 'unknown', health: { score: null, status: 'unknown', reasons: [] }, availability: 'available',
    last_updated: null, telemetry,
  }
}

function reading(metric: string, value: number | null, labels: Record<string, string> = {}) {
  const key = Object.keys(labels).length ? `${metric}{${Object.entries(labels).map(([k, v]) => `${k}=${v}`).join(',')}}` : metric
  return {
    [key]: {
      key, metric, value, unit: 'celsius', timestamp: new Date().toISOString(), source: 'test', quality: 'GOOD' as const,
      availability: value === null ? ('unavailable' as const) : ('available' as const), kind: 'measured' as const,
      reason: value === null ? 'needs LHM' : null, labels,
    },
  }
}

describe('twin selectors', () => {
  it('prefers the CPU package sensor and labels the ACPI fallback honestly', () => {
    const components = {
      cpu: comp('cpu', 'cpu', reading('cpu.temperature_c', null)),
      thermal_sensors: comp('thermal_sensors', 'thermal_sensors', {
        ...reading('thermal.zone_temperature_c', 61, { zone: '_TZ.THM0' }),
      }),
    }
    const t = cpuTemperature(components)
    expect(t?.value).toBe(61)
    expect(t?.isPackageSensor).toBe(false)
    expect(t?.label).toContain('ACPI thermal zone')
    expect(thermalBand(91)).toBe('hot')
  })

  it('fan RPM stays null (no animation) when the sensor is unavailable', () => {
    const v = liveVisualState({ fan: comp('fan', 'fan', reading('fan.speed_rpm', null)) }, 'LIVE')
    expect(v.fanRpm).toBeNull()
    expect(v.fresh).toBe(true)
    expect(liveVisualState({}, 'STALE').fresh).toBe(false)
  })
})

describe('streamStats', () => {
  it('measures cadence, drops and latency from real arrivals only', () => {
    const now = 100_000
    const arrivals = [
      { at: 90_000, sequence: 1, latencyMs: 10 },
      { at: 91_000, sequence: 2, latencyMs: 20 },
      { at: 92_000, sequence: 5, latencyMs: 30 },
    ]
    const st = streamStats(arrivals, now)
    expect(st.cadenceMs).toBe(1000)
    expect(st.dropped60s).toBe(2)
    expect(st.received60s).toBe(3)
    expect(st.latencyMs).toBe(20)
    expect(st.lastAt).toBe(92_000)
  })

  it('reports nothing when no samples arrived', () => {
    const st = streamStats([], 0)
    expect(st.cadenceMs).toBeNull()
    expect(st.lastAt).toBeNull()
  })
})

describe('chart helpers', () => {
  it('spreads axis ticks evenly across the window', () => {
    expect(timeAxis(0, 100, 3, (t) => String(t))).toEqual(['0', '50', '100'])
  })

  it('formats UTC clock without inventing a time', () => {
    expect(utcClock(Date.UTC(2026, 0, 1, 12, 34, 56))).toContain('12:34:56')
    expect(utcClock(null)).not.toMatch(/\d/)
  })
})

describe('seriesStore.backfill', () => {
  it('keeps every buffered sample, not just the first per key', () => {
    seriesStore.backfill([
      [1000, { 'bf.test': 1 }],
      [2000, { 'bf.test': 2 }],
      [3000, { 'bf.test': 3 }],
    ])
    expect(seriesStore.window('bf.test', 0).map((p) => p.v)).toEqual([1, 2, 3])
  })
})
