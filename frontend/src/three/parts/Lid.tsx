import { RoundedBox } from '@react-three/drei'
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import { CanvasTexture, Group, SRGBColorSpace } from 'three'

import type { VisualState } from '../../utils/twin'
import type { LaptopDims } from '../dims'
import { palette } from '../materials'
import { drawScreen, type ScreenInfo } from '../screenCanvas'
import { Selectable } from '../Selectable'

interface Props {
  dims: LaptopDims
  open: boolean
  visual: VisualState
  screen: ScreenInfo
  selected: string | null
  hovered: string | null
  onSelect: (part: string) => void
  onHover: (part: string | null) => void
}

function markDirty(t: CanvasTexture): void {
  t.needsUpdate = true
}

const OPEN_ANGLE = (112 * Math.PI) / 180

/** Display lid hinged at the rear of the base. Opening/closing is a view control, not telemetry. */
export function Lid({ dims, open, visual, screen, selected, hovered, onSelect, onHover }: Props) {
  const pivot = useRef<Group>(null)
  const { width: W, depth: D, lidThickness: T, screenW, screenH } = dims
  const hingeOffset = 0.035

  const canvas = useMemo(() => {
    const c = document.createElement('canvas')
    c.width = 1024
    c.height = Math.round((1024 * screenH) / screenW)
    return c
  }, [screenW, screenH])
  const texture = useMemo(() => {
    const t = new CanvasTexture(canvas)
    t.colorSpace = SRGBColorSpace
    t.anisotropy = 4
    return t
  }, [canvas])

  useEffect(() => {
    drawScreen(canvas, visual, screen)
    markDirty(texture)
  }, [canvas, texture, visual, screen])

  useEffect(() => () => texture.dispose(), [texture])

  useFrame((_, delta) => {
    const g = pivot.current
    if (!g) return
    const target = open ? -OPEN_ANGLE : 0
    g.rotation.x += (target - g.rotation.x) * Math.min(1, delta * 5)
  })

  // Real panel brightness (WMI) drives screen luminance; unknown brightness -> neutral level.
  const brightness = visual.displayBrightness
  const emissive = brightness === null ? 0.75 : 0.25 + (brightness / 100) * 0.95
  const along = D - hingeOffset
  const topBezel = dims.profile ? dims.profile.display.bezel_mm.top / 100 : 0.07
  const screenCenterZ = along - topBezel - screenH / 2

  return (
    <group position={[0, dims.baseHeight + 0.004, -D / 2 + hingeOffset]}>
      <group ref={pivot} rotation={[-OPEN_ANGLE, 0, 0]}>
        <RoundedBox args={[W, T, D]} radius={0.024} smoothness={4} position={[0, T / 2, along / 2 + 0.0]}
          castShadow receiveShadow raycast={() => null}>
          <meshStandardMaterial color={dims.profile?.colour.hex ?? palette.chassis} roughness={0.72} metalness={0.16} />
        </RoundedBox>
        <Selectable part="display" label="Display" detail={brightness === null ? 'brightness N/A' : `${brightness}% brightness`}
          bounds={[screenW + 0.04, 0.02, screenH + 0.04]} position={[0, -0.004, screenCenterZ]}
          selected={selected === 'display'} hovered={hovered === 'display'} onSelect={onSelect} onHover={onHover}>
          <mesh rotation={[Math.PI / 2, 0, 0]}>
            <planeGeometry args={[screenW, screenH]} />
            <meshStandardMaterial map={texture} emissiveMap={texture} emissive="#ffffff" emissiveIntensity={emissive}
              roughness={0.25} metalness={0} toneMapped={false} />
          </mesh>
        </Selectable>
        {/* webcam (+ privacy shutter slider on models that have one) */}
        <mesh position={[0, -0.004, along - 0.035]} rotation={[Math.PI / 2, 0, 0]} raycast={() => null}>
          <circleGeometry args={[0.012, 20]} />
          <meshStandardMaterial color="#020203" roughness={0.1} metalness={0.5} />
        </mesh>
        {dims.profile?.webcam.privacy_shutter ? (
          <mesh position={[0.02, -0.0045, along - 0.035]} rotation={[Math.PI / 2, 0, 0]} raycast={() => null}>
            <planeGeometry args={[0.1, 0.03]} />
            <meshStandardMaterial color="#17181a" roughness={0.5} transparent opacity={0.9} />
          </mesh>
        ) : null}
        {/* status LED on the lid: lit only while real telemetry is fresh */}
        <mesh position={[W / 2 - 0.35, T + 0.001, along - 0.3]} rotation={[-Math.PI / 2, 0, 0]} raycast={() => null}>
          <circleGeometry args={[0.014, 16]} />
          <meshStandardMaterial color={visual.fresh ? palette.red : '#3a1a1c'} emissive={palette.red}
            emissiveIntensity={visual.fresh ? 2.5 : 0} />
        </mesh>
      </group>
    </group>
  )
}
