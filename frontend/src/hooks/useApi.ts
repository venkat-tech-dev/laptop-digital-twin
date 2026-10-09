import { useEffect, useRef, useState } from 'react'

export interface ApiState<T> {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => void
}

/** Fetches `fn` on mount (and every `intervalMs` if given). Keeps the last good value on error. */
export function useApi<T>(fn: () => Promise<T>, deps: unknown[], intervalMs?: number): ApiState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        const v = await fnRef.current()
        if (!cancelled) {
          setData(v)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void run()
    // Phase 10: no polling while the tab is hidden (saves backend load); refresh as soon as it is shown
    const tick = () => {
      if (typeof document === 'undefined' || !document.hidden) void run()
    }
    const id = intervalMs ? setInterval(tick, intervalMs) : null
    const onVisible = () => {
      if (intervalMs && !document.hidden) void run()
    }
    if (intervalMs && typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      if (id) clearInterval(id)
      if (intervalMs && typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, intervalMs])

  return { data, error, loading, reload: () => setNonce((n) => n + 1) }
}
