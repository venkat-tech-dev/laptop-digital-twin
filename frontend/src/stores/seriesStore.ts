import { create } from 'zustand'

import type { ComponentDelta, MetricReading } from '../types/telemetry'
import { RingBuffer } from '../utils/ringBuffer'

/** 30 minutes at 1 Hz. Older history comes from the database via /telemetry/history. */
export const SERIES_CAPACITY = 1800
const MAX_SERIES = 120

export interface Point {
  t: number
  v: number
}

const LABELLED_CHART_METRICS = new Set([
  'gpu.usage_percent',
  'gpu.temperature_c',
  'thermal.zone_temperature_c',
  'cpu.core_usage_percent',
  'disk.active_time_percent',
  'fan.speed_rpm',
])

export function isChartable(r: Pick<MetricReading, 'metric' | 'labels'>): boolean {
  if (r.metric.startsWith('agent.') || r.metric === 'gpu.engine_usage_percent') return false
  return Object.keys(r.labels).length === 0 || LABELLED_CHART_METRICS.has(r.metric)
}

const buffers = new Map<string, RingBuffer<Point>>()

/** Version counter so React components re-render when series change (data itself lives outside React). */
export const useSeriesVersion = create<{ version: number; bump: () => void }>((set) => ({
  version: 0,
  bump: () => set((s) => ({ version: s.version + 1 })),
}))

function push(key: string, point: Point): void {
  let buf = buffers.get(key)
  if (!buf) {
    if (buffers.size >= MAX_SERIES) return
    buf = new RingBuffer<Point>(SERIES_CAPACITY)
    buffers.set(key, buf)
  }
  const last = buf.last()
  if (last && point.t <= last.t) return // duplicate / out-of-order
  buf.push(point)
}

export const seriesStore = {
  ingest(components: Record<string, ComponentDelta>): void {
    let changed = false
    for (const delta of Object.values(components)) {
      for (const r of Object.values(delta.telemetry)) {
        if (r.availability !== 'available' || typeof r.value !== 'number' || !isChartable(r)) continue
        push(r.key, { t: Date.parse(r.timestamp), v: r.value })
        changed = true
      }
    }
    if (changed) useSeriesVersion.getState().bump()
  },

  /** Backfill from the server's short-term buffer (called once after connecting). */
  backfill(points: [number, Record<string, number>][]): void {
    const sorted = [...points].sort((a, b) => a[0] - b[0])
    // Keys that already have live samples are not backfilled (decided once, before pushing).
    const live = new Set([...buffers].filter(([, b]) => b.length > 0).map(([k]) => k))
    for (const [t, values] of sorted) {
      for (const [key, v] of Object.entries(values)) {
        if (live.has(key)) continue
        push(key, { t, v })
      }
    }
    useSeriesVersion.getState().bump()
  },

  window(key: string, sinceMs: number): Point[] {
    return buffers.get(key)?.filter((p) => p.t >= sinceMs) ?? []
  },

  latest(key: string): Point | undefined {
    return buffers.get(key)?.last()
  },

  keys(): string[] {
    return [...buffers.keys()]
  },

  reset(): void {
    buffers.clear()
    useSeriesVersion.getState().bump()
  },
}
