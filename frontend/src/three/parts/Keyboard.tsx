import { useLayoutEffect, useMemo, useRef } from 'react'
import { InstancedMesh, Object3D } from 'three'

import type { LaptopDims } from '../dims'
import { palette } from '../materials'

/** Row definitions in key units (u). Each row spans 15 u like a 14" ThinkPad layout. */
const ROWS: number[][] = [
  [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
  [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2],
  [1.5, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1.5],
  [1.75, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2.25],
  [2.25, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2.75],
  [1, 1, 1, 1, 5, 1, 1, 1, 1, 1, 1],
]

interface Props {
  dims: LaptopDims
  ghost: boolean
}

export function Keyboard({ dims, ghost }: Props) {
  const ref = useRef<InstancedMesh>(null)
  const pitch = (dims.width * 0.86) / 15
  const keys = useMemo(() => {
    const out: { x: number; z: number; w: number; d: number }[] = []
    const top = -dims.depth / 2 + 0.2
    const fnDepth = pitch * 0.62
    ROWS.forEach((row, r) => {
      const d = r === 0 ? fnDepth : pitch
      // Function row is shorter; the main block starts after a small gap.
      const z = r === 0 ? top + fnDepth / 2 : top + fnDepth + 0.04 + (r - 1) * pitch + pitch / 2
      let x = -7.5 * pitch
      for (const units of row) {
        const w = units * pitch
        out.push({ x: x + w / 2, z, w: w - pitch * 0.14, d: d - pitch * 0.14 })
        x += w
      }
    })
    return out
  }, [dims, pitch])

  useLayoutEffect(() => {
    const mesh = ref.current
    if (!mesh) return
    const o = new Object3D()
    keys.forEach((k, i) => {
      o.position.set(k.x, dims.deckY + 0.008, k.z)
      o.scale.set(k.w, 0.016, k.d)
      o.updateMatrix()
      mesh.setMatrixAt(i, o.matrix)
    })
    mesh.instanceMatrix.needsUpdate = true
  }, [keys, dims])

  const wellZ = (keys[0].z + keys[keys.length - 1].z) / 2
  const wellD = keys[keys.length - 1].z - keys[0].z + pitch * 1.1

  return (
    <group>
      {/* keyboard well */}
      <mesh position={[0, dims.deckY + 0.0005, wellZ]} rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
        <planeGeometry args={[15 * pitch + 0.05, wellD]} />
        <meshStandardMaterial color="#101113" roughness={0.9} transparent={ghost} opacity={ghost ? 0.1 : 1} />
      </mesh>
      <instancedMesh ref={ref} args={[undefined, undefined, keys.length]} castShadow={!ghost} raycast={() => null}>
        <boxGeometry args={[1, 1, 1]} />
        <meshStandardMaterial
          color={palette.keycap}
          roughness={0.62}
          metalness={0.05}
          transparent={ghost}
          opacity={ghost ? 0.08 : 1}
          depthWrite={!ghost}
        />
      </instancedMesh>
      {/* TrackPoint between G/H/B */}
      <mesh position={[pitch * 0.15, dims.deckY + 0.022, -dims.depth / 2 + 0.2 + pitch * 3.62 + 0.04]} raycast={() => null}>
        <cylinderGeometry args={[0.028, 0.03, 0.03, 20]} />
        <meshStandardMaterial color={palette.trackpoint} roughness={0.95} />
      </mesh>
    </group>
  )
}
