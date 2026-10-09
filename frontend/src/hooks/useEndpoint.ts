import { api } from '../services/api'
import type { EndpointState } from '../types/admin'
import { useApi } from './useApi'

/** Endpoint agent state: device posture, agent health and recent device events (refreshed every 15 s). */
export function useEndpoint(): { data: EndpointState | null; error: string | null; reload: () => void } {
  const r = useApi(() => api.endpoint(50), [], 15_000)
  return { data: r.data, error: r.error, reload: r.reload }
}
