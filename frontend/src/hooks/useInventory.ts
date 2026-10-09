import { api } from '../services/api'
import { useTwinStore } from '../stores/twinStore'
import { useApi } from './useApi'

export type Inventory = Record<string, unknown>

export const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {})
export const arr = (v: unknown): Record<string, unknown>[] => (Array.isArray(v) ? (v as Record<string, unknown>[]) : [])
export const str = (v: unknown): string | null => (v === null || v === undefined || v === '' ? null : String(v))

/** Hardware inventory discovered by the agent (refetched when the device changes). */
export function useInventory(): { inventory: Inventory | null; discoveredAt: string | null } {
  const deviceId = useTwinStore((s) => s.device?.device_id ?? null)
  const { data } = useApi(() => (deviceId ? api.hardware() : Promise.resolve(null)), [deviceId], 300_000)
  return { inventory: data?.inventory ?? null, discoveredAt: data?.discovered_at ?? null }
}
