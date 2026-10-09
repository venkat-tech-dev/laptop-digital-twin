import { useTwinDoc } from '../stores/twinDocStore'
import { useTwinStore } from '../stores/twinStore'
import type { ChipTone } from '../ui/primitives'

/** Calm by default: colour only where attention is needed. */
export function levelTone(level: string | null | undefined): ChipTone {
  if (level === 'CRITICAL') return 'critical'
  if (level === 'HIGH' || level === 'MEDIUM') return 'amber'
  if (level === 'LOW' || level === 'INFO') return 'muted'
  return 'accent'
}

/** The device on screen (twin document first, legacy store as fallback). */
export function useCurrentDeviceId(): string | null {
  const doc = useTwinDoc((s) => s.deviceId)
  const legacy = useTwinStore((s) => s.device?.device_id ?? null)
  return doc ?? legacy
}
