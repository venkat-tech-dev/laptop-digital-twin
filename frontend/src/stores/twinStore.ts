import { create } from 'zustand'

import type {
  Anomaly,
  ComponentHealth,
  DeviceInfo,
  DeviceStatus,
  Presence,
  ProcessSnapshot,
  ServerEvent,
  SystemEventMsg,
  TwinComponent,
  TwinSnapshot,
} from '../types/telemetry'
import type { LinkState } from '../utils/freshness'
import { seriesStore } from './seriesStore'

const MAX_EVENTS = 60
const MAX_RESOLVED = 40
const MAX_ARRIVALS = 300

/** One received telemetry batch: used for cadence, dropped-batch and latency statistics. */
export interface Arrival {
  at: number
  sequence: number
  latencyMs: number | null
  /** collected_at -> browser (clock-offset corrected); null for replayed backlog. */
  endToEndMs?: number | null
  /** server publish -> browser (clock-offset corrected). */
  deliveryMs?: number | null
}

const MAX_PENDING_REPORTS = 64
/** Latencies measured since the last ping; sent to the backend for end-to-end observability. */
const pending = { delivery: [] as number[], endToEnd: [] as number[] }
export function drainLatencyReport(): Record<string, unknown> {
  const out = { latency: { websocket_delivery_ms: pending.delivery, end_to_end_latency_ms: pending.endToEnd } }
  pending.delivery = []
  pending.endToEnd = []
  return out
}
let clockOffset = () => 0
/** Server-minus-browser clock offset provider (from the socket's ping/pong). */
export function setClockOffsetSource(f: () => number): void {
  clockOffset = f
}

export interface TwinStoreState {
  link: LinkState
  attempt: number
  reconnectInMs: number | null
  device: DeviceInfo | null
  components: Record<string, TwinComponent>
  overall: ComponentHealth | null
  activeAnomalies: Record<string, Anomaly>
  resolvedAnomalies: Anomaly[]
  processes: ProcessSnapshot | null
  backendStatus: DeviceStatus | null
  lastTelemetryAt: number | null
  lastSequence: number | null
  events: SystemEventMsg[]
  arrivals: Arrival[]
  /** Device this view is subscribed to (events for other devices are ignored). */
  subscribedDevice: string | null
  presence: Presence | null
  setSubscribedDevice: (deviceId: string | null) => void
  /** Switching to another device: drop everything that described the previous one. */
  resetForDevice: (deviceId: string | null) => void
  version: number
  setLink: (link: LinkState, attempt: number, reconnectInMs?: number) => void
  setDevice: (device: DeviceInfo | null) => void
  applySnapshot: (snapshot: TwinSnapshot | null) => void
  applyEvent: (event: ServerEvent, receivedAt?: number) => void
}

export const useTwinStore = create<TwinStoreState>((set, get) => ({
  link: 'connecting',
  attempt: 0,
  reconnectInMs: null,
  device: null,
  components: {},
  overall: null,
  activeAnomalies: {},
  resolvedAnomalies: [],
  processes: null,
  backendStatus: null,
  lastTelemetryAt: null,
  lastSequence: null,
  events: [],
  arrivals: [],
  subscribedDevice: null,
  presence: null,
  setSubscribedDevice: (deviceId) => set({ subscribedDevice: deviceId }),
  resetForDevice: (deviceId) =>
    set((s) => ({
      subscribedDevice: deviceId,
      device: null,
      components: {},
      overall: null,
      activeAnomalies: {},
      resolvedAnomalies: [],
      processes: null,
      backendStatus: null,
      lastTelemetryAt: null,
      lastSequence: null,
      events: [],
      arrivals: [],
      presence: null,
      version: s.version + 1,
    })),
  version: 0,

  setLink: (link, attempt, reconnectInMs) => set({ link, attempt, reconnectInMs: reconnectInMs ?? null }),
  setDevice: (device) => set({ device }),

  applySnapshot: (snapshot) => {
    if (!snapshot) {
      set({ components: {}, overall: null, backendStatus: 'OFFLINE' })
      return
    }
    const components: Record<string, TwinComponent> = {}
    for (const c of snapshot.components) components[c.component_id] = c
    const active: Record<string, Anomaly> = {}
    for (const a of snapshot.active_anomalies) active[a.anomaly_id] = a
    set((s) => ({
      components,
      overall: snapshot.health.overall,
      activeAnomalies: active,
      processes: snapshot.processes,
      backendStatus: snapshot.device_status,
      // A snapshot is the state "as of" the server's last ingest; it is live only if that was recent.
      lastTelemetryAt: snapshot.last_seen ? Date.parse(snapshot.last_seen) : null,
      version: s.version + 1,
    }))
  },

  applyEvent: (event, receivedAt = Date.now()) => {
    const mine = get().subscribedDevice
    // One socket can carry several devices (fleet topics); this store mirrors exactly one.
    if (mine && event.device_id && event.device_id !== mine && event.event !== 'heartbeat') return
    switch (event.event) {
      case 'twin_snapshot':
        get().applySnapshot(event.twin)
        if (event.twin && 'presence' in event.twin) set({ presence: (event.twin as { presence?: Presence }).presence ?? null })
        return
      case 'device_presence_changed':
        set({ presence: event.presence })
        return
      case 'telemetry_update': {
        const prev = get().components
        const next: Record<string, TwinComponent> = { ...prev }
        for (const [cid, delta] of Object.entries(event.components)) {
          const base = prev[cid]
          if (!base) continue // unknown component: a resync snapshot will add it
          next[cid] = {
            ...base,
            current_state: delta.current_state,
            availability: delta.availability,
            health: delta.health,
            last_updated: delta.last_updated,
            telemetry: { ...base.telemetry, ...delta.telemetry },
          }
        }
        seriesStore.ingest(event.components)
        let newest = 0
        for (const delta of Object.values(event.components)) {
          for (const r of Object.values(delta.telemetry)) newest = Math.max(newest, Date.parse(r.timestamp) || 0)
        }
        const serverNow = receivedAt + clockOffset()
        const t = event.timing
        const deliveryMs = t ? Math.max(0, serverNow - Date.parse(t.published_at)) : null
        const endToEndMs = t && !t.replay ? Math.max(0, serverNow - Date.parse(t.collected_at)) : null
        if (deliveryMs != null && pending.delivery.length < MAX_PENDING_REPORTS) pending.delivery.push(Math.round(deliveryMs * 10) / 10)
        if (endToEndMs != null && pending.endToEnd.length < MAX_PENDING_REPORTS) pending.endToEnd.push(Math.round(endToEndMs))
        const arrival: Arrival = {
          at: receivedAt,
          sequence: event.sequence,
          latencyMs: newest ? Math.max(0, receivedAt - newest) : null,
          endToEndMs,
          deliveryMs,
        }
        set((s) => ({
          arrivals: [...s.arrivals, arrival].slice(-MAX_ARRIVALS),
          components: next,
          backendStatus: event.device_status,
          lastTelemetryAt: receivedAt,
          lastSequence: event.sequence,
          processes: event.processes ?? s.processes,
          overall: next.laptop?.health ?? s.overall,
          version: s.version + 1,
        }))
        return
      }
      case 'heartbeat':
        set({ backendStatus: event.device_status })
        return
      case 'device_status_changed':
        set({ backendStatus: event.status })
        return
      case 'health_changed': {
        const comp = get().components[event.component_id]
        if (comp) {
          set((s) => ({
            components: {
              ...s.components,
              [event.component_id]: {
                ...comp,
                health: { score: event.score, status: event.status, reasons: event.reasons },
              },
            },
            overall: event.component_id === 'laptop' ? { score: event.score, status: event.status, reasons: event.reasons } : s.overall,
          }))
        }
        return
      }
      case 'anomaly_detected':
        set((s) => ({ activeAnomalies: { ...s.activeAnomalies, [event.anomaly.anomaly_id]: event.anomaly } }))
        return
      case 'anomaly_resolved':
        set((s) => {
          const active = { ...s.activeAnomalies }
          delete active[event.anomaly.anomaly_id]
          return { activeAnomalies: active, resolvedAnomalies: [event.anomaly, ...s.resolvedAnomalies].slice(0, MAX_RESOLVED) }
        })
        return
      case 'system_event':
        set((s) => ({ events: [event, ...s.events].slice(0, MAX_EVENTS) }))
        return
      case 'component_state_changed': {
        const msg: SystemEventMsg = {
          event: 'system_event',
          timestamp: event.timestamp,
          event_type: event.domain_event,
          severity: 'info',
          message: `${event.component_id}: ${event.previous_state} → ${event.current_state}`,
          data: {},
        }
        set((s) => ({ events: [msg, ...s.events].slice(0, MAX_EVENTS) }))
        return
      }
      default:
        return
    }
  },
}))
