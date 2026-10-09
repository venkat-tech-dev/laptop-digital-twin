import type { DeviceStatus } from '../types/telemetry'

/** Thresholds shared with the backend (TWIN_DEGRADED_AFTER_S / TWIN_STALE_AFTER_S). */
export const DEGRADED_AFTER_MS = 3_000
export const STALE_AFTER_MS = 10_000

export type LinkState = 'connecting' | 'open' | 'closed'

/**
 * The UI status shown in the header. Old data is never presented as live:
 * - socket closed -> OFFLINE
 * - backend says the agent is OFFLINE/STALE -> that status
 * - otherwise based on the age of the last telemetry update received
 */
export function computeStatus(
  link: LinkState,
  lastTelemetryAt: number | null,
  backendStatus: DeviceStatus | null,
  now: number,
): DeviceStatus {
  if (link !== 'open') return 'OFFLINE'
  if (lastTelemetryAt === null) return backendStatus === 'LIVE' ? 'DEGRADED' : 'OFFLINE'
  const age = now - lastTelemetryAt
  let local: DeviceStatus = 'LIVE'
  if (age > STALE_AFTER_MS) local = 'STALE'
  else if (age > DEGRADED_AFTER_MS) local = 'DEGRADED'
  const rank: Record<DeviceStatus, number> = { LIVE: 0, DEGRADED: 1, STALE: 2, OFFLINE: 3 }
  if (backendStatus && rank[backendStatus] > rank[local]) return backendStatus
  return local
}

export function isFresh(status: DeviceStatus): boolean {
  return status === 'LIVE' || status === 'DEGRADED'
}
