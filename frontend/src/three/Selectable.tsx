import { Html } from '@react-three/drei'
import type { ThreeEvent } from '@react-three/fiber'
import { useMemo, type ReactNode } from 'react'
import { BoxGeometry, EdgesGeometry } from 'three'

export interface SelectableProps {
  part: string
  label: string
  detail?: string | null
  /** Size of the highlight box (scene units). */
  bounds: [number, number, number]
  position?: [number, number, number]
  boundsOffset?: [number, number, number]
  selected: boolean
  hovered: boolean
  onSelect: (part: string) => void
  onHover: (part: string | null) => void
  children: ReactNode
}

/** Click/hover target for a physical component with an engineering-style bounding box highlight. */
export function Selectable({
  part,
  label,
  detail,
  bounds,
  position = [0, 0, 0],
  boundsOffset = [0, 0, 0],
  selected,
  hovered,
  onSelect,
  onHover,
  children,
}: SelectableProps) {
  const edges = useMemo(() => new EdgesGeometry(new BoxGeometry(...bounds)), [bounds])
  const active = selected || hovered

  const over = (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation()
    onHover(part)
    document.body.style.cursor = 'pointer'
  }
  const out = () => {
    onHover(null)
    document.body.style.cursor = ''
  }
  const click = (e: ThreeEvent<MouseEvent>) => {
    e.stopPropagation()
    onSelect(part)
  }

  return (
    <group position={position} onPointerOver={over} onPointerOut={out} onClick={click}>
      {children}
      {active && (
        <lineSegments geometry={edges} position={boundsOffset} raycast={() => null}>
          <lineBasicMaterial color={selected ? '#5ad1ff' : '#9fb4c7'} transparent opacity={selected ? 0.95 : 0.6} />
        </lineSegments>
      )}
      {hovered && (
        <Html position={[boundsOffset[0], boundsOffset[1] + bounds[1] / 2 + 0.08, boundsOffset[2]]} center
          style={{ pointerEvents: 'none' }} zIndexRange={[20, 0]}>
          <div className="scene-tag">
            <span className="scene-tag__name">{label}</span>
            {detail ? <span className="scene-tag__value">{detail}</span> : null}
          </div>
        </Html>
      )}
    </group>
  )
}
