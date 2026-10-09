import { create } from 'zustand'

import type { AnomalyRecord } from '../types/anomaly'

/**
 * Live Phase-4 anomaly records (anomaly.detected / anomaly.updated / anomaly.resolved) of the
 * device on screen, keyed by id. Components subscribe to one id (``useAnomalyRecord``) so an
 * update re-renders only the views showing that anomaly.
 */
interface AnomalyState {
  deviceId: string | null
  byId: Record<string, AnomalyRecord>
  revision: number // bumps on every change: lists refetch history lazily
  reset: (deviceId: string | null) => void
  upsert: (a: AnomalyRecord) => void
}

const MAX_RECORDS = 200

export const useAnomalies = create<AnomalyState>((set, get) => ({
  deviceId: null,
  byId: {},
  revision: 0,
  reset: (deviceId) => {
    if (get().deviceId !== deviceId) set({ deviceId, byId: {}, revision: get().revision + 1 })
  },
  upsert: (a) => {
    const s = get()
    if (s.deviceId && a.device_id !== s.deviceId) return
    const current = s.byId[a.anomaly_id]
    if (current && current.updated_at && a.updated_at && a.updated_at < current.updated_at) return // older event
    const byId = { ...s.byId, [a.anomaly_id]: current ? { ...current, ...a } : a }
    const ids = Object.keys(byId)
    if (ids.length > MAX_RECORDS) {
      ids.sort((x, y) => byId[x].started_at.localeCompare(byId[y].started_at))
      for (const id of ids.slice(0, ids.length - MAX_RECORDS)) delete byId[id]
    }
    set({ byId, revision: s.revision + 1 })
  },
}))

export const useAnomalyRecord = (id: string | null): AnomalyRecord | undefined =>
  useAnomalies((s) => (id ? s.byId[id] : undefined))
