import { create } from 'zustand'

import type { TwinEventItem, TwinFlat, TwinSnapshotMsg } from '../types/twinDoc'

/**
 * Client copy of one device's digital twin document.
 *
 *   REST snapshot (or WS twin.snapshot) -> twin.state.patch (base_version must equal our version)
 *
 * A patch whose base_version is not our version, or which comes from another epoch (backend
 * restarted), is NOT applied: the store asks for a fresh snapshot instead of guessing what was
 * missed. Patches replace only the changed keys, so components selecting one field re-render only
 * when that field changes.
 */
export type DocStatus = 'idle' | 'loading' | 'ready' | 'missing' | 'error'

export interface TwinPatchMsg {
  device_id: string
  twin_version: number
  base_version: number
  epoch: string
  /** whole values to replace (null = removed) */
  changes: Record<string, unknown>
  /** partial field objects: only the sub-keys that changed (value, timestamp, source.sequence...) */
  merge?: Record<string, Record<string, unknown>>
}

interface TwinDocState {
  deviceId: string | null
  status: DocStatus
  error: string | null
  version: number
  epoch: string | null
  restored: boolean
  fields: TwinFlat
  events: TwinEventItem[]
  patchesApplied: number
  gapsDetected: number
  lastPatchAt: number | null
  reset: (deviceId: string | null) => void
  setStatus: (status: DocStatus, error?: string | null) => void
  applySnapshot: (snap: TwinSnapshotMsg) => void
  applyPatch: (patch: TwinPatchMsg) => 'applied' | 'stale' | 'gap' | 'ignored'
  addEvent: (event: TwinEventItem) => void
  setEvents: (events: TwinEventItem[]) => void
}

const MAX_EVENTS = 200
let resync: (deviceId: string) => void = () => undefined

/** The connection layer registers how to obtain a fresh snapshot (WS twin.sync, REST fallback). */
export function setTwinResync(fn: (deviceId: string) => void): void {
  resync = fn
}

export const useTwinDoc = create<TwinDocState>((set, get) => ({
  deviceId: null,
  status: 'idle',
  error: null,
  version: 0,
  epoch: null,
  restored: false,
  fields: {},
  events: [],
  patchesApplied: 0,
  gapsDetected: 0,
  lastPatchAt: null,

  reset: (deviceId) =>
    set({ deviceId, status: deviceId ? 'loading' : 'idle', error: null, version: 0, epoch: null, fields: {}, events: [], restored: false }),

  setStatus: (status, error = null) => set({ status, error }),

  applySnapshot: (snap) => {
    const s = get()
    if (s.deviceId && snap.device_id !== s.deviceId) return
    // An older snapshot (e.g. a slow REST response after a newer WS snapshot) must not win.
    if (s.epoch === snap.epoch && snap.twin_version < s.version) return
    set({
      deviceId: snap.device_id,
      status: 'ready',
      error: null,
      version: snap.twin_version,
      epoch: snap.epoch,
      restored: snap.restored_from_cache,
      fields: snap.state,
    })
  },

  applyPatch: (patch) => {
    const s = get()
    if (!s.deviceId || patch.device_id !== s.deviceId) return 'ignored'
    if (s.status !== 'ready') return 'ignored' // snapshot pending: it will contain this change
    if (patch.epoch === s.epoch && patch.twin_version <= s.version) return 'stale' // duplicate / old
    if (patch.epoch !== s.epoch || patch.base_version !== s.version) {
      set({ gapsDetected: s.gapsDetected + 1, status: 'loading' })
      resync(s.deviceId)
      return 'gap'
    }
    const merge = patch.merge ?? {}
    if (Object.keys(merge).some((k) => typeof s.fields[k] !== 'object' || s.fields[k] === null)) {
      // a partial update for a field we do not have: our copy is incomplete -> fresh snapshot
      set({ gapsDetected: s.gapsDetected + 1, status: 'loading' })
      resync(s.deviceId)
      return 'gap'
    }
    const fields = { ...s.fields }
    for (const [k, v] of Object.entries(patch.changes)) {
      if (v === null) delete fields[k]
      else fields[k] = v
    }
    for (const [k, part] of Object.entries(merge)) {
      const cur = fields[k] as Record<string, unknown>
      const source = part.source && typeof part.source === 'object' ? { ...(cur.source as object | null ?? {}), ...(part.source as object) } : cur.source
      fields[k] = { ...cur, ...part, source }
    }
    set({ fields, version: patch.twin_version, patchesApplied: s.patchesApplied + 1, lastPatchAt: Date.now() })
    return 'applied'
  },

  addEvent: (event) =>
    set((s) => (s.events.some((e) => e.event_id === event.event_id) ? s : { events: [event, ...s.events].slice(0, MAX_EVENTS) })),

  setEvents: (events) => set({ events: events.slice(0, MAX_EVENTS) }),
}))

/** Subscribe to one twin field: the component re-renders only when this field changes. */
export function useTwinValue<T = unknown>(path: string): T | undefined {
  return useTwinDoc((s) => s.fields[path] as T | undefined)
}
