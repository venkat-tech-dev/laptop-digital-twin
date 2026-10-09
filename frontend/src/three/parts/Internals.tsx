import { useFrame } from '@react-three/fiber'
import { useMemo, useRef } from 'react'
import { CatmullRomCurve3, InstancedMesh, Object3D, Vector3 } from 'three'

import type { VisualState } from '../../utils/twin'
import type { LaptopDims } from '../dims'
import { chargeColor, palette } from '../materials'
import { Selectable } from '../Selectable'
import { partPositions, type Vec3 } from '../partPositions'
import { Fan } from './Fan'
import { Soc } from './Soc'

export interface PartLabels {
  [part: string]: string | null
}

interface Props {
  dims: LaptopDims
  /** Populated memory modules reported by inventory (null = unknown). */
  memoryModules?: number | null
  visual: VisualState
  visible: boolean
  labels: PartLabels
  names: PartLabels
  selected: string | null
  hovered: string | null
  onSelect: (part: string) => void
  onHover: (part: string | null) => void
}

/** Network activity: particles travel along the antenna cable at a speed set by measured throughput. */
function AntennaFlow({ curve, bytesPerSec, fresh }: { curve: CatmullRomCurve3; bytesPerSec: number | null; fresh: boolean }) {
  const ref = useRef<InstancedMesh>(null)
  const phase = useRef(0)
  const dummy = useMemo(() => new Object3D(), [])
  const count = 7
  const active = fresh && bytesPerSec !== null && bytesPerSec > 512
  // log scale: 1 KB/s -> slow, 10 MB/s -> fast
  const speed = active && bytesPerSec ? Math.min(1.2, Math.log10(bytesPerSec / 512) * 0.18) : 0

  useFrame((_, delta) => {
    const mesh = ref.current
    if (!mesh) return
    phase.current = (phase.current + delta * speed) % 1
    for (let i = 0; i < count; i += 1) {
      const t = (phase.current + i / count) % 1
      dummy.position.copy(curve.getPointAt(t))
      dummy.scale.setScalar(active ? 1 : 0)
      dummy.updateMatrix()
      mesh.setMatrixAt(i, dummy.matrix)
    }
    mesh.instanceMatrix.needsUpdate = true
  })

  return (
    <instancedMesh ref={ref} args={[undefined, undefined, count]} raycast={() => null}>
      <sphereGeometry args={[0.012, 10, 10]} />
      <meshBasicMaterial color={palette.cyan} toneMapped={false} />
    </instancedMesh>
  )
}

const cellOffsets = (n: number) => Array.from({ length: n }, (_, i) => i - (n - 1) / 2)

/** Heat-pipe and antenna paths, derived only from chassis dimensions. */
function curvesFor(dims: LaptopDims): { heatpipe: CatmullRomCurve3; antenna: CatmullRomCurve3 } {
  const { width: W, depth: D, boardY: y } = dims
  const soc = [0.3, -D / 2 + 0.55]
  const fan = [-W / 2 + 0.62, -D / 2 + 0.42]
  const wifi = [W / 2 - 0.42, -D / 2 + 0.45]
  return {
    heatpipe: new CatmullRomCurve3([
      new Vector3(soc[0] - 0.05, y + 0.04, soc[1]),
      new Vector3(soc[0] - 0.4, y + 0.045, soc[1] - 0.12),
      new Vector3(fan[0] + 0.45, y + 0.045, fan[1] - 0.2),
      new Vector3(fan[0] + 0.05, y + 0.045, -D / 2 + 0.08),
    ]),
    antenna: new CatmullRomCurve3([
      new Vector3(wifi[0], y + 0.02, wifi[1] - 0.08),
      new Vector3(wifi[0] - 0.1, y + 0.03, -D / 2 + 0.16),
      new Vector3(W / 2 - 0.7, y + 0.06, -D / 2 + 0.05),
      new Vector3(W / 2 - 0.9, dims.baseHeight + 0.02, -D / 2 + 0.03),
    ]),
  }
}

export function Internals({ dims, memoryModules = null, visual, visible, labels, names, selected, hovered, onSelect, onHover }: Props) {
  const { width: W, depth: D, boardY: y } = dims
  const live = visual.fresh
  const sel = (part: string) => ({
    selected: selected === part,
    hovered: hovered === part,
    onSelect,
    onHover,
    label: names[part] ?? part,
    detail: labels[part] ?? null,
  })

  const pos = partPositions(dims)
  const socPos = pos.cpu
  const fanPos = pos.fan
  const wifiPos = pos.wifi
  const slots = dims.profile?.internals.sodimm_slots ?? 1
  const populated = memoryModules === null ? slots : Math.min(slots, memoryModules)
  const { heatpipe, antenna } = useMemo(() => curvesFor(dims), [dims])
  const memLit = visual.memoryPercent === null ? 0 : Math.round((visual.memoryPercent / 100) * 8)
  const diskGlow = live && visual.diskActivePercent !== null ? 0.1 + Math.min(100, visual.diskActivePercent) / 25 : 0.04
  const charge = visual.batteryPercent
  const batColor = chargeColor(live ? charge : null)

  return (
    <group visible={visible}>
      {/* main board */}
      <Selectable part="motherboard" bounds={[W - 0.5, 0.02, 1.0]} position={pos.motherboard}
        {...sel('motherboard')}>
        <mesh receiveShadow>
          <boxGeometry args={[W - 0.5, 0.008, 1.0]} />
          <meshStandardMaterial color={palette.pcb} roughness={0.7} metalness={0.1} />
        </mesh>
      </Selectable>

      <Selectable part="cpu" bounds={[0.5, 0.06, 0.34]} position={socPos} boundsOffset={[0, 0.02, 0]} {...sel('cpu')}>
        <Soc visual={visual} highlightGpu={selected === 'gpu' || hovered === 'gpu'} />
      </Selectable>
      {/* invisible pick target for the integrated GPU tile (it lives on the CPU package) */}
      <Selectable part="gpu" bounds={[0.1, 0.03, 0.18]} position={pos.gpu}
        {...sel('gpu')}>
        <mesh>
          <boxGeometry args={[0.09, 0.01, 0.16]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
      </Selectable>

      {/* thermal sensor marker (ACPI zone / package sensor sits on the SoC) */}
      <Selectable part="thermal_sensors" bounds={[0.06, 0.04, 0.06]} position={[socPos[0] - 0.3, socPos[1] + 0.01, socPos[2] + 0.1]}
        {...sel('thermal_sensors')}>
        <mesh>
          <cylinderGeometry args={[0.018, 0.018, 0.012, 16]} />
          <meshStandardMaterial color="#20262c" emissive={palette.amber} emissiveIntensity={live && visual.cpuTempC !== null ? 0.8 : 0} />
        </mesh>
      </Selectable>

      {/* VRM inductors (no sensors exposed) */}
      <Selectable part="vrm" bounds={[0.34, 0.05, 0.08]} position={[socPos[0], y + 0.015, socPos[2] + 0.26]} {...sel('vrm')}>
        {[-0.12, -0.04, 0.04, 0.12].map((x) => (
          <mesh key={x} position={[x, 0, 0]} castShadow>
            <boxGeometry args={[0.06, 0.03, 0.06]} />
            <meshStandardMaterial color="#2a2d31" roughness={0.5} metalness={0.4} />
          </mesh>
        ))}
      </Selectable>

      {/* copper heat pipe from SoC to fan/fin stack */}
      <mesh raycast={() => null} castShadow>
        <tubeGeometry args={[heatpipe, 48, 0.022, 10, false]} />
        <meshStandardMaterial color={palette.copper} roughness={0.32} metalness={0.95} />
      </mesh>
      {/* fin stack at exhaust */}
      <group position={[fanPos[0], y + 0.03, -D / 2 + 0.07]}>
        {Array.from({ length: 18 }, (_, i) => (
          <mesh key={i} position={[-0.26 + i * 0.03, 0, 0]} raycast={() => null}>
            <boxGeometry args={[0.006, 0.05, 0.08]} />
            <meshStandardMaterial color={palette.aluminium} roughness={0.35} metalness={0.9} />
          </mesh>
        ))}
      </group>

      <Selectable part="fan" bounds={[0.58, 0.07, 0.58]} position={fanPos} {...sel('fan')}>
        <Fan rpm={visual.fanRpm} fresh={live} />
      </Selectable>

      {/* SO-DIMM slots: populated modules light chips proportional to measured RAM utilisation */}
      {Array.from({ length: slots }, (_, slot) => {
        const filled = slot < populated
        const at: Vec3 = [pos.memory[0] - slot * 0.76, pos.memory[1], pos.memory[2]]
        const body = (
          <>
            {/* slot connector */}
            <mesh position={[0, -0.004, 0.17]} raycast={() => null}>
              <boxGeometry args={[0.72, 0.02, 0.04]} />
              <meshStandardMaterial color="#1b1d20" roughness={0.7} />
            </mesh>
            {filled ? (
              <>
                <mesh castShadow>
                  <boxGeometry args={[0.68, 0.01, 0.3]} />
                  <meshStandardMaterial color={palette.pcbDark} roughness={0.6} />
                </mesh>
                {Array.from({ length: 8 }, (_, i) => (
                  <mesh key={i} position={[-0.27 + (i % 4) * 0.18, 0.012, i < 4 ? -0.06 : 0.07]} castShadow>
                    <boxGeometry args={[0.13, 0.012, 0.09]} />
                    <meshStandardMaterial color="#15181c" emissive={live ? palette.green : palette.stale}
                      emissiveIntensity={i < memLit && live ? 1.4 : 0.03} toneMapped={false} />
                  </mesh>
                ))}
                {Array.from({ length: 24 }, (_, i) => (
                  <mesh key={`p${i}`} position={[-0.32 + i * 0.028, 0.0, 0.155]} raycast={() => null}>
                    <boxGeometry args={[0.018, 0.011, 0.012]} />
                    <meshStandardMaterial color="#c9a227" metalness={1} roughness={0.3} />
                  </mesh>
                ))}
              </>
            ) : (
              /* empty slot: latch frame only */
              <mesh position={[0, -0.002, 0]} raycast={() => null}>
                <boxGeometry args={[0.7, 0.006, 0.32]} />
                <meshStandardMaterial color="#2a3139" roughness={0.8} transparent opacity={0.85} />
              </mesh>
            )}
          </>
        )
        return slot === 0 ? (
          <Selectable key={slot} part="memory" bounds={[0.72, 0.04, 0.34]} position={at} {...sel('memory')}>{body}</Selectable>
        ) : (
          <group key={slot} position={at}>{body}</group>
        )
      })}

      {/* M.2 SSD with activity indicator = measured disk active time */}
      <Selectable part="disk" bounds={[0.84, 0.035, 0.26]} position={pos.disk} {...sel('disk')}>
        <mesh castShadow>
          <boxGeometry args={[0.8, 0.008, 0.22]} />
          <meshStandardMaterial color="#14171b" roughness={0.6} />
        </mesh>
        <mesh position={[-0.22, 0.01, 0]}>
          <boxGeometry args={[0.16, 0.01, 0.16]} />
          <meshStandardMaterial color="#1c1f24" roughness={0.4} metalness={0.3} />
        </mesh>
        {[0.02, 0.24].map((x) => (
          <mesh key={x} position={[x, 0.01, 0]}>
            <boxGeometry args={[0.18, 0.01, 0.17]} />
            <meshStandardMaterial color="#202328" roughness={0.45} />
          </mesh>
        ))}
        <mesh position={[0.36, 0.012, 0.06]}>
          <boxGeometry args={[0.03, 0.006, 0.03]} />
          <meshStandardMaterial color="#0a0f0a" emissive={palette.cyan} emissiveIntensity={diskGlow} toneMapped={false} />
        </mesh>
      </Selectable>

      {/* Wi-Fi M.2 2230 card + antenna cable with throughput flow */}
      <Selectable part="wifi" bounds={[0.26, 0.03, 0.34]} position={wifiPos} {...sel('wifi')}>
        <mesh castShadow>
          <boxGeometry args={[0.22, 0.008, 0.3]} />
          <meshStandardMaterial color="#173326" roughness={0.6} />
        </mesh>
        <mesh position={[0, 0.008, 0.02]}>
          <boxGeometry args={[0.16, 0.008, 0.18]} />
          <meshStandardMaterial color={palette.aluminium} roughness={0.3} metalness={0.9} />
        </mesh>
      </Selectable>
      <mesh raycast={() => null}>
        <tubeGeometry args={[antenna, 40, 0.006, 6, false]} />
        <meshStandardMaterial color="#1b1b1d" roughness={0.6} />
      </mesh>
      <AntennaFlow curve={antenna} bytesPerSec={(visual.netRxBps ?? 0) + (visual.netTxBps ?? 0)} fresh={live} />

      {/* battery pack under the palm rest: fill = measured state of charge */}
      <Selectable part="battery" bounds={[W - 0.7, 0.07, 0.72]} position={pos.battery} {...sel('battery')}>
        <mesh castShadow receiveShadow>
          <boxGeometry args={[W - 0.75, 0.05, 0.66]} />
          <meshStandardMaterial color={palette.battery} roughness={0.55} metalness={0.2} />
        </mesh>
        {visual.batteryPresent &&
          cellOffsets(dims.profile?.internals.battery_cells ?? 4).map((k, _i, all) => {
            const cellW = (W - 0.95) / all.length
            const fill = charge === null ? 0 : Math.max(0.02, charge / 100)
            return (
              <group key={k} position={[k * (cellW + 0.03), 0.027, 0]}>
                <mesh>
                  <boxGeometry args={[cellW, 0.004, 0.56]} />
                  <meshStandardMaterial color="#1a1e23" roughness={0.6} />
                </mesh>
                <mesh position={[0, 0.003, 0.28 - (0.56 * fill) / 2]}>
                  <boxGeometry args={[cellW * 0.86, 0.003, 0.56 * fill]} />
                  <meshStandardMaterial color="#0d1410" emissive={batColor} emissiveIntensity={live ? 0.28 : 0.05}
                    toneMapped={false} />
                </mesh>
              </group>
            )
          })}
      </Selectable>

      {/* speakers */}
      {[-1, 1].map((s) => (
        <mesh key={s} position={[s * (W / 2 - 0.22), y + 0.02, D / 2 - 0.35]} raycast={() => null}>
          <boxGeometry args={[0.2, 0.04, 0.5]} />
          <meshStandardMaterial color="#17191c" roughness={0.8} />
        </mesh>
      ))}
    </group>
  )
}
