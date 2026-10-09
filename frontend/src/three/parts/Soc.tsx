import { useMemo } from 'react'
import { AdditiveBlending, Color } from 'three'

import type { VisualState } from '../../utils/twin'
import { palette, temperatureColor } from '../materials'

interface Props {
  visual: VisualState
  highlightGpu: boolean
}

/**
 * CPU package with integrated GPU tile.
 * - Each tile on the die is one logical processor; its glow is that processor's real utilisation.
 * - The iGPU tile glows with real GPU engine utilisation.
 * - The halo colour is the real CPU-area temperature (package sensor or labelled ACPI zone).
 */
export function Soc({ visual, highlightGpu }: Props) {
  const cores = visual.coreUsage
  const cols = Math.max(1, Math.ceil(cores.length / 2))
  const live = visual.fresh
  const heat = temperatureColor(live ? visual.cpuTempC : null)
  const haloOpacity = live && visual.cpuTempC !== null ? Math.min(0.55, Math.max(0.06, (visual.cpuTempC - 35) / 90)) : 0.03
  const cpuGlow = useMemo(() => new Color(palette.cyan), [])
  const gpuGlow = useMemo(() => new Color(palette.violet), [])

  return (
    <group>
      {/* substrate */}
      <mesh castShadow receiveShadow>
        <boxGeometry args={[0.46, 0.018, 0.3]} />
        <meshStandardMaterial color={palette.substrate} roughness={0.55} metalness={0.2} />
      </mesh>
      {/* die */}
      <mesh position={[0, 0.014, 0]} castShadow>
        <boxGeometry args={[0.32, 0.01, 0.19]} />
        <meshPhysicalMaterial color={palette.die} roughness={0.22} metalness={0.85} clearcoat={0.6}
          emissive={heat} emissiveIntensity={live && visual.cpuTempC !== null ? 0.18 : 0} />
      </mesh>
      {/* per-logical-processor tiles */}
      {cores.map((usage, i) => {
        const col = i % cols
        const row = Math.floor(i / cols)
        const x = -0.14 + ((col + 0.5) * 0.2) / cols
        const z = -0.06 + row * 0.065
        return (
          <mesh key={i} position={[x, 0.0205, z]}>
            <boxGeometry args={[(0.2 / cols) * 0.8, 0.003, 0.05]} />
            <meshStandardMaterial color="#0d1620" emissive={live ? cpuGlow : new Color(palette.stale)}
              emissiveIntensity={live ? 0.1 + (usage / 100) * 3 : 0.05} toneMapped={false} />
          </mesh>
        )
      })}
      {/* integrated GPU tile */}
      <mesh position={[0.11, 0.0205, 0]}>
        <boxGeometry args={[0.075, 0.003, 0.15]} />
        <meshStandardMaterial color="#120d20" emissive={live ? gpuGlow : new Color(palette.stale)}
          emissiveIntensity={live && visual.gpuUsage !== null ? 0.1 + (visual.gpuUsage / 100) * 3 : 0.05}
          toneMapped={false} />
      </mesh>
      {highlightGpu && (
        <mesh position={[0.11, 0.024, 0]}>
          <boxGeometry args={[0.085, 0.002, 0.16]} />
          <meshBasicMaterial color={palette.violet} wireframe />
        </mesh>
      )}
      {/* thermal halo: colour = measured temperature band */}
      <mesh position={[0, 0.03, 0]} rotation={[-Math.PI / 2, 0, 0]} raycast={() => null}>
        <circleGeometry args={[0.42, 48]} />
        <meshBasicMaterial color={heat} transparent opacity={haloOpacity} blending={AdditiveBlending} depthWrite={false}
          toneMapped={false} />
      </mesh>
      {visual.throttling && live && (
        <mesh position={[0, 0.032, 0]} rotation={[-Math.PI / 2, 0, 0]} raycast={() => null}>
          <ringGeometry args={[0.3, 0.33, 48]} />
          <meshBasicMaterial color={palette.red} transparent opacity={0.8} toneMapped={false} />
        </mesh>
      )}
    </group>
  )
}
