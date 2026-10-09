/**
 * The device this browser tab is looking at. Every device-scoped REST call carries it as
 * ``device_id`` (the server enforces whether the user may see it), and the WebSocket subscribes to
 * ``device:<id>``. ``null`` = the server's default (primary device, or the employee's own device).
 */
type Listener = (deviceId: string | null) => void

const KEY = 'ldt.device'
let current: string | null = (() => {
  try {
    return sessionStorage.getItem(KEY)
  } catch {
    return null
  }
})()
const listeners = new Set<Listener>()

export const deviceScope = {
  get: (): string | null => current,
  set(deviceId: string | null): void {
    if (deviceId === current) return
    current = deviceId
    try {
      if (deviceId) sessionStorage.setItem(KEY, deviceId)
      else sessionStorage.removeItem(KEY)
    } catch {
      /* storage unavailable: in-memory only */
    }
    listeners.forEach((l) => l(deviceId))
  },
  subscribe(l: Listener): () => void {
    listeners.add(l)
    return () => listeners.delete(l)
  },
}

/** REST paths that describe one device and accept ``?device_id=``. */
const SCOPED = [
  '/api/v1/device',
  '/api/v1/hardware',
  '/api/v1/twin',
  '/api/v1/health',
  '/api/v1/telemetry/',
  '/api/v1/system/processes',
  '/api/v1/system/events',
  '/api/v1/anomalies',
  '/api/v1/analytics/',
  '/api/v1/endpoint',
]

export function scopePath(path: string, method = 'GET'): string {
  const id = current
  if (!id || method !== 'GET' || path.includes('device_id=')) return path
  const bare = path.split('?')[0]
  if (bare === '/api/v1/device/list' || !SCOPED.some((p) => bare === p || (p.endsWith('/') ? bare.startsWith(p) : bare.startsWith(`${p}/`) || bare === p))) {
    return path
  }
  if (bare.startsWith('/api/v1/anomalies/')) return path // by anomaly id: authorised server-side
  return `${path}${path.includes('?') ? '&' : '?'}device_id=${encodeURIComponent(id)}`
}
