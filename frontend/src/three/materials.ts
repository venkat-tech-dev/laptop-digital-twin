import { Color } from 'three'

import type { ThermalBand } from '../utils/twin'

/** Palette for physically based materials and telemetry encodings. */
export const palette = {
  chassis: '#1d1f22',
  chassisTop: '#232529',
  keycap: '#141518',
  keyLegend: '#3a3d43',
  bezel: '#0b0c0e',
  pcb: '#0f3a33',
  pcbDark: '#0b2924',
  substrate: '#1f5a3e',
  die: '#9aa3ad',
  copper: '#b8733a',
  aluminium: '#8e959c',
  battery: '#2a2f36',
  trackpoint: '#d4202c',
  cyan: '#5ad1ff',
  violet: '#a78bfa',
  amber: '#f5a524',
  green: '#3ddc97',
  red: '#ff4d5e',
  idle: '#3b4652',
  stale: '#59606a',
}

export const thermalColors: Record<ThermalBand, string> = {
  normal: '#4fb7ff',
  elevated: '#f5a524',
  hot: '#ff7a1a',
  critical: '#ff3046',
  unknown: '#59606a',
}

/** Continuous thermal colour: blue (<=45 °C) -> amber (80) -> orange (90) -> red (>=98). */
export function temperatureColor(celsius: number | null): Color {
  if (celsius === null) return new Color(palette.stale)
  const stops: [number, string][] = [
    [45, '#4fb7ff'],
    [70, '#7fd0ff'],
    [80, '#f5a524'],
    [90, '#ff7a1a'],
    [98, '#ff3046'],
  ]
  if (celsius <= stops[0][0]) return new Color(stops[0][1])
  for (let i = 1; i < stops.length; i += 1) {
    const [t1, c1] = stops[i]
    const [t0, c0] = stops[i - 1]
    if (celsius <= t1) return new Color(c0).lerp(new Color(c1), (celsius - t0) / (t1 - t0))
  }
  return new Color(stops[stops.length - 1][1])
}

/** Load (0-100 %) -> emissive intensity, kept subtle so the scene does not look like a toy. */
export function loadIntensity(percent: number | null, max = 2.2): number {
  if (percent === null) return 0
  return 0.08 + (Math.min(100, Math.max(0, percent)) / 100) * max
}

export function chargeColor(percent: number | null): string {
  if (percent === null) return palette.stale
  if (percent < 20) return palette.red
  if (percent < 50) return palette.amber
  return palette.green
}
