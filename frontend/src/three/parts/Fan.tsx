import { useFrame } from '@react-three/fiber'
import { useRef } from 'react'
import type { Group } from 'three'

import { palette } from '../materials'

interface Props {
  rpm: number | null
  fresh: boolean
}

/**
 * Blower fan. It rotates ONLY when a real tachometer reading exists.
 * Angular speed is rpm scaled down by STROBE_FACTOR so blades remain visible (documented scaling).
 * Without an RPM reading the fan is static and greyed out - never animated for effect.
 */
export const STROBE_FACTOR = 40

export function Fan({ rpm, fresh }: Props) {
  const rotor = useRef<Group>(null)
  const spinning = rpm !== null && rpm > 0 && fresh

  useFrame((_, delta) => {
    if (!spinning || !rotor.current || rpm === null) return
    rotor.current.rotation.y -= ((rpm / 60) * Math.PI * 2 * delta) / STROBE_FACTOR
  })

  const bladeColor = rpm === null ? palette.stale : '#2b3138'
  return (
    <group>
      {/* housing */}
      <mesh castShadow>
        <cylinderGeometry args={[0.27, 0.27, 0.045, 48, 1, true]} />
        <meshStandardMaterial color="#121418" roughness={0.6} metalness={0.3} side={2} />
      </mesh>
      <mesh position={[0, -0.022, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <circleGeometry args={[0.27, 48]} />
        <meshStandardMaterial color="#0d0f12" roughness={0.8} />
      </mesh>
      <group ref={rotor}>
        <mesh>
          <cylinderGeometry args={[0.07, 0.07, 0.04, 32]} />
          <meshStandardMaterial color={spinning ? '#39424c' : '#22272d'} roughness={0.4} metalness={0.5} />
        </mesh>
        {Array.from({ length: 27 }, (_, i) => {
          const a = (i / 27) * Math.PI * 2
          return (
            <mesh key={i} position={[Math.cos(a) * 0.165, 0, Math.sin(a) * 0.165]} rotation={[0, Math.PI / 2 - a + 0.35, 0]}>
              <boxGeometry args={[0.012, 0.036, 0.17]} />
              <meshStandardMaterial color={bladeColor} roughness={0.5} />
            </mesh>
          )
        })}
      </group>
    </group>
  )
}
