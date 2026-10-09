import { useFrame } from '@react-three/fiber'
import { useMemo, useRef } from 'react'
import { Group, Vector3 } from 'three'

export type Vec3 = [number, number, number]

export interface ScreenPoint {
  x: number
  y: number
  /** True when the point is behind the camera or outside the clip volume. */
  hidden: boolean
}

export interface ProjectorSpec {
  points: Record<string, Vec3>
  /** Called every frame with canvas-pixel positions. Write to the DOM directly - do not set React state. */
  onFrame: (screen: Record<string, ScreenPoint>) => void
}

/** Projects model-space points to screen pixels so HTML labels can follow the 3D model. */
export function Projector({ spec }: { spec: ProjectorSpec }) {
  const ref = useRef<Group>(null)
  const v = useMemo(() => new Vector3(), [])
  const out = useRef<Record<string, ScreenPoint>>({})

  useFrame(({ camera, size }) => {
    const g = ref.current
    if (!g) return
    for (const [id, p] of Object.entries(spec.points)) {
      v.set(p[0], p[1], p[2])
      g.localToWorld(v)
      v.project(camera)
      out.current[id] = {
        x: ((v.x + 1) / 2) * size.width,
        y: ((1 - v.y) / 2) * size.height,
        hidden: v.z > 1 || v.z < -1,
      }
    }
    spec.onFrame(out.current)
  })

  return <group ref={ref} />
}
