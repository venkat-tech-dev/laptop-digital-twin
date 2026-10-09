import { useEffect, useMemo, useState } from 'react'

import { api } from '../services/api'
import { seriesStore, useSeriesVersion, type Point } from '../stores/seriesStore'

/** Live points from the bounded client buffer for the last `windowMs`. */
export function useLiveSeries(keys: (string | null | undefined)[], windowMs: number, frozenAt?: number | null): { points: Record<string, Point[]>; start: number; end: number } {
  const version = useSeriesVersion((s) => s.version)
  const [tick, setTick] = useState(() => Date.now())
  useEffect(() => {
    if (frozenAt) return
    setTick(Date.now())
  }, [version, frozenAt])
  const end = frozenAt ?? tick
  const start = end - windowMs
  const keyStr = keys.join('|')
  const points = useMemo(() => {
    const out: Record<string, Point[]> = {}
    for (const k of keys) if (k) out[k] = seriesStore.window(k, start)
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyStr, start, version])
  return { points, start, end }
}

/**
 * Persisted history (database, bucketed) merged with the live tail, for windows longer than the
 * client buffer. Refreshes every `refreshMs`.
 */
export function useHistorySeries(keys: (string | null | undefined)[], minutes: number, bucketSeconds?: number, refreshMs = 30_000): { points: Record<string, Point[]>; start: number; end: number; loading: boolean } {
  const valid = keys.filter((k): k is string => Boolean(k))
  const keyStr = valid.join('|')
  const [hist, setHist] = useState<Record<string, Point[]>>({})
  const [loading, setLoading] = useState(true)
  const version = useSeriesVersion((s) => s.version)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!valid.length) {
      setLoading(false)
      return
    }
    let cancelled = false
    const load = async () => {
      try {
        const res = await api.history(valid, minutes, bucketSeconds)
        if (cancelled) return
        const out: Record<string, Point[]> = {}
        for (const [k, pts] of Object.entries(res.series)) out[k] = pts.map((p) => ({ t: Date.parse(p.t), v: p.avg }))
        setHist(out)
      } catch {
        if (!cancelled) setHist({})
      } finally {
        if (!cancelled) {
          setLoading(false)
          setNow(Date.now())
        }
      }
    }
    void load()
    const id = setInterval(load, refreshMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyStr, minutes, bucketSeconds, refreshMs])

  useEffect(() => setNow(Date.now()), [version])

  const end = now
  const start = end - minutes * 60_000
  const points = useMemo(() => {
    const out: Record<string, Point[]> = {}
    for (const k of valid) {
      const live = seriesStore.window(k, start)
      const firstLive = live.length ? live[0].t : Infinity
      const older = (hist[k] ?? []).filter((p) => p.t < firstLive)
      // Thin the 1 Hz live tail to roughly the history bucket so the line weight stays even.
      const step = (bucketSeconds ?? 0) * 1000
      const tail = step > 2000 ? live.filter((p, i) => i === live.length - 1 || Math.floor(p.t / step) !== Math.floor((live[i + 1]?.t ?? 0) / step)) : live
      out[k] = [...older, ...tail]
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hist, keyStr, start, version])

  return { points, start, end, loading }
}

export function stats(points: Point[]): { avg: number | null; max: number | null; min: number | null } {
  if (!points.length) return { avg: null, max: null, min: null }
  let sum = 0
  let max = -Infinity
  let min = Infinity
  for (const p of points) {
    sum += p.v
    max = Math.max(max, p.v)
    min = Math.min(min, p.v)
  }
  return { avg: sum / points.length, max, min }
}

/** Persisted history for an explicit [start, end] window (no live tail). ``null`` window = idle. */
export function useWindowSeries(
  keys: (string | null | undefined)[],
  window: { start: number; end: number } | null,
  bucketSeconds: number,
): { points: Record<string, Point[]>; loading: boolean; error: string | null } {
  const valid = keys.filter((k): k is string => Boolean(k))
  const keyStr = valid.join('|')
  const [points, setPoints] = useState<Record<string, Point[]>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ws = window?.start
  const we = window?.end

  useEffect(() => {
    if (ws === undefined || we === undefined || !valid.length) {
      setPoints({})
      return
    }
    let cancelled = false
    setLoading(true)
    api
      .history(valid, Math.max(1, Math.round((we - ws) / 60_000)), bucketSeconds, {
        start: new Date(ws).toISOString(),
        end: new Date(we).toISOString(),
      })
      .then((res) => {
        if (cancelled) return
        const out: Record<string, Point[]> = {}
        for (const [k, pts] of Object.entries(res.series)) out[k] = pts.map((p) => ({ t: Date.parse(p.t), v: p.avg }))
        setPoints(out)
        setError(null)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyStr, ws, we, bucketSeconds])

  return { points, loading, error }
}
