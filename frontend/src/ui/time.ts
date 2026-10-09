/** Axis/time formatting shared by the SVG charts. */
export function timeAxis(start: number, end: number, count: number, fmt: (t: number) => string): string[] {
  return Array.from({ length: count }, (_, i) => fmt(start + ((end - start) * i) / (count - 1)))
}

export const fmtClock = (t: number) => new Date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC' })
export const fmtHm = (t: number) => new Date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
export const fmtDay = (t: number) => new Date(t).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' }).toUpperCase()
