import type { DeviceProfile, Geometry } from '../types/telemetry'

/** Scene units are decimetres (1 unit = 10 cm). Base sits on y = 0, centred on the origin. */
export interface LaptopDims {
  width: number
  depth: number
  baseHeight: number
  trayHeight: number
  lidThickness: number
  screenW: number
  screenH: number
  deckY: number
  boardY: number
  /** Published model design, when the detected laptop has a built-in profile. */
  profile: DeviceProfile | null
}

function panelSize(geometry: Geometry | null | undefined): { screenW: number; screenH: number } | null {
  const panel = geometry?.panel
  if (!panel || panel.width_cm <= 15 || panel.height_cm <= 8) return null
  const [rw, rh] = (geometry?.resolution ?? '1920x1080').split('x').map(Number)
  const aspect = rw && rh ? rw / rh : panel.width_cm / panel.height_cm
  const screenW = panel.width_cm / 10
  return { screenW, screenH: screenW / aspect }
}

/**
 * Chassis dimensions.
 * - With a model profile: the published chassis size (mm) of that exact model; the panel comes from
 *   EDID when available, otherwise from the profile's diagonal and aspect ratio.
 * - Otherwise: a generic laptop scaled to the detected panel size (EDID).
 */
export function dimsFromGeometry(geometry: Geometry | null | undefined): LaptopDims {
  const profile = geometry?.profile ?? null
  if (profile) {
    const { width: wmm, depth: dmm, height: hmm } = profile.chassis_mm
    const width = wmm / 100
    const depth = dmm / 100
    const lidThickness = profile.lid_mm / 100
    const baseHeight = (hmm - profile.lid_mm) / 100
    const [aw, ah] = profile.display.aspect.split(':').map(Number)
    const diag = (profile.display.diagonal_in * 2.54) / 10
    const fromProfile = { screenW: (diag * aw) / Math.hypot(aw, ah), screenH: (diag * ah) / Math.hypot(aw, ah) }
    const panel = panelSize(geometry) ?? fromProfile
    const trayHeight = baseHeight * 0.55
    return {
      width,
      depth,
      baseHeight,
      trayHeight,
      lidThickness,
      screenW: Math.min(panel.screenW, width - 0.1),
      screenH: Math.min(panel.screenH, depth - 0.2),
      deckY: baseHeight,
      boardY: trayHeight + 0.004,
      profile,
    }
  }

  const panel = panelSize(geometry) ?? { screenW: 3.1, screenH: 1.74 }
  const width = panel.screenW + 0.16
  const depth = Math.max(panel.screenH + 0.45, width * 0.66)
  const baseHeight = 0.19
  const trayHeight = baseHeight * 0.5
  return {
    width,
    depth,
    baseHeight,
    trayHeight,
    lidThickness: 0.055,
    screenW: panel.screenW,
    screenH: panel.screenH,
    deckY: baseHeight,
    boardY: trayHeight + 0.004,
    profile: null,
  }
}
