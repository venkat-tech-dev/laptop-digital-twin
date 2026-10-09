import { useEffect, useMemo } from 'react'
import { AdditiveBlending, CanvasTexture, SRGBColorSpace } from 'three'

import type { VisualState } from '../../utils/twin'
import type { LaptopDims } from '../dims'
import { temperatureColor } from '../materials'
import { partPositions } from '../partPositions'

function markDirty(t: CanvasTexture): void {
  t.needsUpdate = true
}

/**
 * Thermal map of the measured zone only: a glow centred on the SoC whose colour and spread follow the
 * real CPU-area temperature, carried along the heat pipe towards the exhaust. Areas without a sensor
 * get no colour - nothing is interpolated or invented.
 */
export function ThermalMap({ dims, visual }: { dims: LaptopDims; visual: VisualState }) {
  const temp = visual.fresh ? visual.cpuTempC : null
  const pos = partPositions(dims)
  const canvas = useMemo(() => {
    const c = document.createElement('canvas')
    c.width = 256
    c.height = 256
    return c
  }, [])
  const texture = useMemo(() => {
    const t = new CanvasTexture(canvas)
    t.colorSpace = SRGBColorSpace
    return t
  }, [canvas])

  useEffect(() => {
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, 256, 256)
    if (temp !== null) {
      const c = temperatureColor(temp)
      const rgb = `${Math.round(c.r * 255)},${Math.round(c.g * 255)},${Math.round(c.b * 255)}`
      const g = ctx.createRadialGradient(128, 128, 0, 128, 128, 128)
      g.addColorStop(0, `rgba(${rgb},0.95)`)
      g.addColorStop(0.35, `rgba(${rgb},0.45)`)
      g.addColorStop(1, `rgba(${rgb},0)`)
      ctx.fillStyle = g
      ctx.fillRect(0, 0, 256, 256)
    }
    markDirty(texture)
  }, [canvas, texture, temp])

  useEffect(() => () => texture.dispose(), [texture])

  if (temp === null) return null
  // Spread grows with temperature: ~0.7 units at 40 °C, ~1.5 units at 95 °C.
  const spread = 0.7 + Math.min(1, Math.max(0, (temp - 40) / 55)) * 0.8
  const y = dims.boardY + 0.07
  return (
    <group>
      <mesh position={[pos.cpu[0], y, pos.cpu[2]]} rotation={[-Math.PI / 2, 0, 0]} raycast={() => null}>
        <planeGeometry args={[spread * 2, spread * 2]} />
        <meshBasicMaterial map={texture} transparent blending={AdditiveBlending} depthWrite={false} toneMapped={false} />
      </mesh>
      {/* heat carried to the fin stack by the heat pipe */}
      <mesh position={[pos.fan[0], y, pos.fan[2]]} rotation={[-Math.PI / 2, 0, 0]} raycast={() => null}>
        <planeGeometry args={[spread * 1.1, spread * 1.1]} />
        <meshBasicMaterial map={texture} transparent opacity={0.45} blending={AdditiveBlending} depthWrite={false} toneMapped={false} />
      </mesh>
    </group>
  )
}
