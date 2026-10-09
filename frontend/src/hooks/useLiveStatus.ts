import { useTwinStore } from '../stores/twinStore'
import type { DeviceStatus } from '../types/telemetry'
import { computeStatus } from '../utils/freshness'
import { useNow } from './useNow'

export interface LiveStatus {
  status: DeviceStatus
  ageMs: number | null
  link: ReturnType<typeof useTwinStore.getState>['link']
  reconnectInMs: number | null
}

export function useLiveStatus(): LiveStatus {
  const now = useNow(250)
  const link = useTwinStore((s) => s.link)
  const last = useTwinStore((s) => s.lastTelemetryAt)
  const backend = useTwinStore((s) => s.backendStatus)
  const reconnectInMs = useTwinStore((s) => s.reconnectInMs)
  return {
    status: computeStatus(link, last, backend, now),
    ageMs: last === null ? null : Math.max(0, now - last),
    link,
    reconnectInMs,
  }
}
