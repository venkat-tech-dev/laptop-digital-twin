import type { LaptopDims } from './dims'

export type Vec3 = [number, number, number]

/** Where each physical component sits (scene units, model space). Shared with the 2D overlays. */
export function partPositions(dims: LaptopDims): Record<'cpu' | 'gpu' | 'fan' | 'wifi' | 'memory' | 'disk' | 'battery' | 'motherboard', Vec3> {
  const { width: W, depth: D, boardY: y } = dims
  const soc: Vec3 = [0.3, y + 0.012, -D / 2 + 0.55]
  return {
    cpu: soc,
    gpu: [soc[0] + 0.11, soc[1] + 0.025, soc[2]],
    fan: [-W / 2 + 0.62, y + 0.03, -D / 2 + 0.42],
    wifi: [W / 2 - 0.42, y + 0.006, -D / 2 + 0.45],
    memory: [-0.38, y + 0.01, -D / 2 + 0.95],
    disk: [W / 2 - 0.75, y + 0.008, -D / 2 + 1.0],
    battery: [0, y + 0.025, D / 2 - 0.5],
    motherboard: [0, y - 0.004, -D / 2 + 0.6],
  }
}
