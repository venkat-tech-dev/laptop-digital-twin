import { create } from 'zustand'

import type { FleetRow } from '../types/twinDoc'

/**
 * Live fleet rows from ``twin.summary`` messages on the ``fleet`` topic (one compact message per
 * device at most every few seconds, immediately on connectivity/health changes). The device list
 * loads a page over REST and overlays these updates - no per-row connections, no polling.
 */
interface FleetState {
  live: Record<string, Partial<FleetRow>>
  version: number
  upsert: (row: Partial<FleetRow> & { device_id: string }) => void
  clear: () => void
}

export const useFleet = create<FleetState>((set) => ({
  live: {},
  version: 0,
  upsert: (row) => set((s) => ({ live: { ...s.live, [row.device_id]: { ...s.live[row.device_id], ...row } }, version: s.version + 1 })),
  clear: () => set({ live: {}, version: 0 }),
}))
