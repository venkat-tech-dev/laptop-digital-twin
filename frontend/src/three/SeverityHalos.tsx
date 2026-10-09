import { useFrame } from '@react-three/fiber'
import { useRef } from 'react'
import type { Mesh, MeshBasicMaterial } from 'three'

import type { Visual } from '../types/twinDoc'
import type { LaptopDims } from './dims'
import { partPositions } from './partPositions'

/** Twin section -> physical part it describes, and the halo radius around it (scene units). */
const PARTS: { section: string; part: keyof ReturnType<typeof partPositions>; radius: number }[] = [
  { section: 'cpu', part: 'cpu', radius: 0.17 },
  { section: 'thermal', part: 'cpu', radius: 0.24 },
  { section: 'gpu', part: 'gpu', radius: 0.11 },
  { section: 'memory', part: 'memory', radius: 0.22 },
  { section: 'storage', part: 'disk', radius: 0.2 },
  { section: 'battery', part: 'battery', radius: 0.42 },
  { section: 'network', part: 'wifi', radius: 0.13 },
]

const COLOR: Partial<Record<Visual, string>> = { elevated: '#7c9cd6', warning: '#d9ad6c', critical: '#e5736a' }
const BASE_OPACITY: Partial<Record<Visual, number>> = { elevated: 0.35, warning: 0.6, critical: 0.85 }

function Halo({ position, radius, visual }: { position: [number, number, number]; radius: number; visual: Visual }) {
  const mesh = useRef<Mesh>(null)
  useFrame(({ clock }) => {
    if (visual !== 'critical' || !mesh.current) return
    const m = mesh.current.material as MeshBasicMaterial
    m.opacity = 0.45 + 0.4 * (0.5 + 0.5 * Math.sin(clock.elapsedTime * 4))
  })
  return (
    <mesh ref={mesh} position={[position[0], position[1] + 0.006, position[2]]} rotation-x={-Math.PI / 2} renderOrder={10}>
      <ringGeometry args={[radius * 0.72, radius, 48]} />
      <meshBasicMaterial color={COLOR[visual]} transparent opacity={BASE_OPACITY[visual]} depthTest={false} depthWrite={false} toneMapped={false} />
    </mesh>
  )
}

/**
 * Severity of each twin section drawn on the physical part it describes (normal = no halo). Driven
 * only by the digital twin's section states (thresholds in app/domain/twin/rules.py).
 */
export function SeverityHalos({ dims, severity }: { dims: LaptopDims; severity: Partial<Record<string, Visual>> }) {
  const pos = partPositions(dims)
  return (
    <group>
      {PARTS.map(({ section, part, radius }) => {
        const visual = severity[section]
        if (!visual || !COLOR[visual]) return null
        return <Halo key={section} position={pos[part]} radius={radius} visual={visual} />
      })}
    </group>
  )
}
