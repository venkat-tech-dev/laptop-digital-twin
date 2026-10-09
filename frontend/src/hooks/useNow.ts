import { useEffect, useState } from 'react'

/** A ticking clock so "last update X s ago" and freshness are re-evaluated even without new data. */
export function useNow(intervalMs = 250): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}
