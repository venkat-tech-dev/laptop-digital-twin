import type { VisualState } from '../utils/twin'

export interface ScreenInfo {
  title: string
  mode: 'live' | 'simulation'
  status: string
}

function bar(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, value: number | null, color: string): void {
  ctx.fillStyle = '#18202a'
  ctx.fillRect(x, y, w, 10)
  if (value !== null) {
    ctx.fillStyle = color
    ctx.fillRect(x, y, (w * Math.min(100, Math.max(0, value))) / 100, 10)
  }
}

/**
 * Draws the content shown on the 3D laptop's screen: a readout of the twin itself.
 * Values come from the same VisualState as the rest of the model (live or simulation).
 */
export function drawScreen(canvas: HTMLCanvasElement, v: VisualState, info: ScreenInfo): void {
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const { width: W, height: H } = canvas
  const sim = info.mode === 'simulation'
  const accent = sim ? '#f5a524' : v.fresh ? '#5ad1ff' : '#7a838d'

  const g = ctx.createLinearGradient(0, 0, 0, H)
  g.addColorStop(0, '#0a1018')
  g.addColorStop(1, '#05080c')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, W, H)

  ctx.strokeStyle = 'rgba(90, 209, 255, 0.06)'
  ctx.lineWidth = 1
  for (let x = 0; x < W; x += 32) {
    ctx.beginPath()
    ctx.moveTo(x, 0)
    ctx.lineTo(x, H)
    ctx.stroke()
  }
  for (let y = 0; y < H; y += 32) {
    ctx.beginPath()
    ctx.moveTo(0, y)
    ctx.lineTo(W, y)
    ctx.stroke()
  }

  ctx.fillStyle = accent
  ctx.font = '600 26px "IBM Plex Mono", monospace'
  ctx.fillText(sim ? 'SIMULATION — GENERATED DATA' : `● ${info.status} — REAL HARDWARE`, 40, 58)
  ctx.fillStyle = '#c9d4df'
  ctx.font = '500 34px "IBM Plex Sans", sans-serif'
  ctx.fillText(info.title, 40, 112)

  const rows: [string, number | null, string, string][] = [
    ['CPU', v.cpuUsage, v.cpuUsage === null ? 'N/A' : `${v.cpuUsage.toFixed(0)} %`, '#5ad1ff'],
    ['GPU', v.gpuUsage, v.gpuUsage === null ? 'N/A' : `${v.gpuUsage.toFixed(0)} %`, '#a78bfa'],
    ['RAM', v.memoryPercent, v.memoryPercent === null ? 'N/A' : `${v.memoryPercent.toFixed(0)} %`, '#3ddc97'],
    ['TEMP', v.cpuTempC === null ? null : Math.min(100, v.cpuTempC), v.cpuTempC === null ? 'N/A' : `${v.cpuTempC.toFixed(1)} °C`, '#f5a524'],
    ['BATT', v.batteryPercent, v.batteryPercent === null ? 'N/A' : `${v.batteryPercent.toFixed(0)} %`, '#3ddc97'],
  ]
  rows.forEach(([label, value, text, color], i) => {
    const y = 180 + i * 64
    ctx.fillStyle = '#7d8a97'
    ctx.font = '500 22px "IBM Plex Mono", monospace'
    ctx.fillText(label, 40, y)
    ctx.fillStyle = '#e6edf3'
    ctx.font = '600 30px "IBM Plex Mono", monospace'
    ctx.fillText(text, W - 260, y + 4)
    bar(ctx, 160, y - 14, W - 460, value, v.fresh || sim ? color : '#4a525c')
  })
  if (!v.fresh && !sim) {
    ctx.fillStyle = 'rgba(5, 8, 12, 0.55)'
    ctx.fillRect(0, 0, W, H)
    ctx.fillStyle = '#ffb020'
    ctx.font = '700 44px "IBM Plex Mono", monospace'
    ctx.fillText(info.status, W / 2 - 90, H / 2)
  }
}
