import { useMemo } from 'react'

export interface TsPoint {
  t: number
  v: number
}

export interface TsSeries {
  points: TsPoint[]
  tone?: 'primary' | 'secondary'
  dashed?: boolean
  label?: string
  /** Uncertainty band drawn behind the line (same timestamps as ``points``). */
  band?: { low: TsPoint[]; high: TsPoint[] }
}

export interface TsReference {
  value: number
  label: string
}

interface Props {
  series: TsSeries[]
  start: number
  end: number
  height: number
  min?: number
  max?: number
  axis: string[]
  /** Breaks the line instead of interpolating across gaps longer than this. */
  maxGapMs?: number
  reference?: TsReference
  showLatest?: boolean
  emptyText?: string
}

const STROKE = { primary: { color: '#69cfe5', width: 1.7 }, secondary: { color: '#7c9cd6', width: 1.5 } }

/**
 * Plot matching the Figma chart anatomy: four grid rules, a cyan measured signal, a blue comparison
 * signal, a latest-sample dot and a mono time axis. Lines come from real samples only; gaps are gaps.
 */
export function TimeSeries({ series, start, end, height, min, max, axis, maxGapMs = 5000, reference, showLatest = true, emptyText = 'NO SAMPLES IN THIS WINDOW' }: Props) {
  const { paths, bands, lo, hi, latest } = useMemo(() => {
    const all = series.flatMap((s) => [...s.points, ...(s.band?.low ?? []), ...(s.band?.high ?? [])].filter((p) => p.t >= start && p.t <= end))
    const values = all.map((p) => p.v)
    if (reference) values.push(reference.value)
    let lo = min ?? (values.length ? Math.min(...values) : 0)
    let hi = max ?? (values.length ? Math.max(...values) : 1)
    if (hi - lo < 1e-9) {
      hi += 1
      lo -= 1
    }
    const pad = min === undefined || max === undefined ? (hi - lo) * 0.12 : 0
    if (min === undefined) lo -= pad
    if (max === undefined) hi += pad
    const W = 1000
    const x = (t: number) => ((t - start) / Math.max(1, end - start)) * W
    const y = (v: number) => height - ((v - lo) / (hi - lo)) * height
    const paths = series.map((s) => {
      let d = ''
      let prev: TsPoint | null = null
      for (const p of s.points) {
        if (p.t < start || p.t > end) continue
        const brk = prev === null || p.t - prev.t > maxGapMs
        d += `${brk ? 'M' : 'L'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`
        prev = p
      }
      return d
    })
    const bands = series.map((s) => {
      if (!s.band) return ''
      const lowPts = s.band.low.filter((p) => p.t >= start && p.t <= end)
      const highPts = s.band.high.filter((p) => p.t >= start && p.t <= end).reverse()
      if (lowPts.length < 2) return ''
      return [...lowPts, ...highPts].map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join('') + 'Z'
    })
    const first = series[0]?.points.filter((p) => p.t >= start && p.t <= end)
    const last = first && first.length ? first[first.length - 1] : null
    const latest = last ? { left: (x(last.t) / W) * 100, top: y(last.v) } : null
    return { paths, bands, lo, hi, latest }
  }, [series, start, end, height, min, max, maxGapMs, reference])

  const empty = paths.every((p) => p === '')
  const refTop = reference ? height - ((reference.value - lo) / (hi - lo)) * height : null

  return (
    <div className="timeseries">
      <div className="timeseries__plot" style={{ height }}>
        <svg width="100%" height={height} viewBox={`0 0 1000 ${height}`} preserveAspectRatio="none" role="img"
          aria-label={series.map((s) => s.label).filter(Boolean).join(', ') || 'time series'}>
          {[0, 1 / 3, 2 / 3, 1].map((f) => (
            <line key={f} x1={0} x2={1000} y1={Math.min(height - 0.5, f * height + 0.5)} y2={Math.min(height - 0.5, f * height + 0.5)}
              stroke="#2b333e" strokeOpacity={0.6} strokeWidth={1} vectorEffect="non-scaling-stroke" />
          ))}
          {refTop !== null && !empty ? (
            <line x1={0} x2={1000} y1={refTop} y2={refTop} stroke="#d9ad6c" strokeOpacity={0.5} strokeWidth={1} vectorEffect="non-scaling-stroke" />
          ) : null}
          {bands.map((d, i) => (d ? (
            <path key={`band${i}`} d={d} fill={STROKE[series[i].tone ?? (i === 0 ? 'primary' : 'secondary')].color} fillOpacity={0.14} stroke="none" />
          ) : null))}
          {paths.map((d, i) => {
            const s = series[i]
            const st = STROKE[s.tone ?? (i === 0 ? 'primary' : 'secondary')]
            return d ? (
              <path key={i} d={d} fill="none" stroke={st.color} strokeWidth={st.width} strokeLinejoin="round"
                strokeDasharray={s.dashed ? '6 5' : undefined} vectorEffect="non-scaling-stroke" />
            ) : null
          })}
        </svg>
        {reference && refTop !== null && !empty ? (
          <p className="mono" style={{ position: 'absolute', right: 0, top: refTop + 3, fontSize: 9, color: '#d9ad6c' }}>{reference.label}</p>
        ) : null}
        {showLatest && latest ? (
          <span style={{ position: 'absolute', left: `calc(${latest.left}% - 3px)`, top: latest.top - 3, width: 6, height: 6, borderRadius: '50%', background: '#69cfe5' }} />
        ) : null}
        {empty ? <div className="timeseries__empty">{emptyText}</div> : null}
      </div>
      <div className="timeseries__axis">
        {axis.map((a, i) => <p key={`${a}${i}`}>{a}</p>)}
      </div>
    </div>
  )
}

/** Evenly spaced axis labels between start and end. */
