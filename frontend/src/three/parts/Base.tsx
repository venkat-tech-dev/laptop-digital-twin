import { Line, RoundedBox } from '@react-three/drei'

import type { PortKind } from '../../types/telemetry'
import type { LaptopDims } from '../dims'
import { palette } from '../materials'
import { Keyboard } from './Keyboard'

interface Props {
  dims: LaptopDims
  xray: boolean
  /** 0 = assembled, 1 = keyboard deck lifted off the chassis (cutaway). */
  explode: number
  ethernetLinkUp: boolean | null
}

/** Opening size (along the side, height) in scene units for each port kind. */
const PORT_SIZE: Record<PortKind, [number, number]> = {
  usb_c: [0.09, 0.032],
  usb_c_tb: [0.09, 0.032],
  usb_a: [0.13, 0.05],
  hdmi: [0.15, 0.05],
  rj45: [0.15, 0.085],
  audio: [0.036, 0.036],
  lock: [0.06, 0.03],
  smartcard: [0.55, 0.012],
}

const GENERIC_PORTS: { left: PortKind[]; right: PortKind[] } = {
  left: ['usb_c', 'usb_c', 'hdmi', 'usb_a'],
  right: ['rj45', 'usb_a', 'audio'],
}

/** Explode offsets: the (translucent) deck rises off the chassis, as in a service cutaway. */
function deckOffset(dims: LaptopDims, explode: number): [number, number, number] {
  return [0, explode * 0.6, -explode * dims.depth * 0.06]
}

function Port({ kind, side, z, y }: { kind: PortKind; side: 1 | -1; z: number; y: number }) {
  const [len, h] = PORT_SIZE[kind]
  const round = kind === 'audio'
  return (
    <group position={[0, y, z]}>
      {round ? (
        <mesh rotation={[0, 0, Math.PI / 2]} raycast={() => null}>
          <cylinderGeometry args={[h / 2, h / 2, 0.014, 18]} />
          <meshStandardMaterial color="#050506" roughness={0.6} metalness={0.4} />
        </mesh>
      ) : (
        <RoundedBox args={[0.014, h, len]} radius={Math.min(h, len) * 0.3} smoothness={2} raycast={() => null}>
          <meshStandardMaterial color="#050506" roughness={0.6} metalness={0.4} />
        </RoundedBox>
      )}
      {/* tongue / contacts visible inside data ports */}
      {kind === 'usb_a' || kind === 'usb_c' || kind === 'usb_c_tb' ? (
        <mesh position={[side * 0.004, kind === 'usb_a' ? h * 0.12 : 0, 0]} raycast={() => null}>
          <boxGeometry args={[0.008, h * (kind === 'usb_a' ? 0.28 : 0.3), len * 0.68]} />
          <meshStandardMaterial color={kind === 'usb_a' ? '#2c5aa0' : '#1d1f22'} roughness={0.5} />
        </mesh>
      ) : null}
    </group>
  )
}

/**
 * Lower chassis: tray, top deck (C-cover) with keyboard, TrackPoint buttons, trackpad and power
 * button, side ports and hinges. With a model profile, the ports/colour/keyboard follow the
 * published design of that model.
 */
export function Base({ dims, xray, explode, ethernetLinkUp }: Props) {
  const { width: W, depth: D, baseHeight: H, trayHeight: TH, profile } = dims
  const deckH = H - TH
  const chassis = profile?.colour.hex ?? palette.chassis
  const deckColor = profile ? '#202124' : palette.chassisTop
  const ports = profile
    ? { left: profile.ports.left.map((p) => p.kind), right: profile.ports.right.map((p) => p.kind) }
    : GENERIC_PORTS
  const buttons = profile?.keyboard.trackpad_buttons ?? 3
  const hasFingerprint = profile?.keyboard.fingerprint === 'power_button'
  const deck = deckOffset(dims, explode)
  const ghost = xray
  const portY = H * 0.46

  /** Ports are laid out rear -> front with a fixed gap, starting behind the hinge line. */
  const layout = (kinds: PortKind[]) => {
    let z = -D / 2 + 0.3
    return kinds.map((k) => {
      const len = PORT_SIZE[k][0]
      const at = z + len / 2
      z += len + 0.075
      return { kind: k, z: at }
    })
  }
  const left = layout(ports.left)
  const right = layout(ports.right)
  const rj45 = right.find((p) => p.kind === 'rj45') ?? left.find((p) => p.kind === 'rj45')

  return (
    <group>
      {/* bottom tray (D-cover) */}
      <RoundedBox args={[W, TH, D]} radius={Math.min(0.04, TH / 2.2)} smoothness={4} position={[0, TH / 2, 0]} castShadow receiveShadow
        raycast={() => null}>
        <meshStandardMaterial color={chassis} roughness={0.8} metalness={0.12} />
      </RoundedBox>

      {/* rubber feet */}
      {[[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([sx, sz]) => (
        <mesh key={`${sx}${sz}`} position={[sx * (W / 2 - 0.25), 0.004, sz * (D / 2 - 0.2)]} raycast={() => null}>
          <boxGeometry args={[0.35, 0.012, 0.05]} />
          <meshStandardMaterial color="#0a0a0b" roughness={1} />
        </mesh>
      ))}

      {/* side ports (in the tray / deck seam) */}
      {left.map((p, i) => <group key={`l${i}`} position={[-W / 2 + 0.006, 0, 0]}><Port kind={p.kind} side={-1} z={p.z} y={portY} /></group>)}
      {right.map((p, i) => <group key={`r${i}`} position={[W / 2 - 0.006, 0, 0]}><Port kind={p.kind} side={1} z={p.z} y={portY} /></group>)}
      {rj45 ? (
        <mesh position={[(right.includes(rj45) ? 1 : -1) * (W / 2 + 0.002), portY + 0.05, rj45.z + 0.05]} raycast={() => null}>
          <boxGeometry args={[0.004, 0.012, 0.018]} />
          <meshStandardMaterial
            color={ethernetLinkUp ? palette.green : '#1a1d20'}
            emissive={ethernetLinkUp ? palette.green : '#000000'}
            emissiveIntensity={ethernetLinkUp ? 2 : 0}
          />
        </mesh>
      ) : null}

      {/* rear exhaust vent slats */}
      {Array.from({ length: 22 }, (_, i) => (
        <mesh key={i} position={[-W / 2 + 0.35 + i * 0.035, TH * 0.6, -D / 2 - 0.001]} raycast={() => null}>
          <boxGeometry args={[0.018, TH * 0.5, 0.01]} />
          <meshStandardMaterial color="#060607" roughness={0.9} />
        </mesh>
      ))}

      {/* hinge barrels */}
      {[-1, 1].map((s) => (
        <mesh key={s} position={[s * (W / 2 - 0.42), H + 0.01, -D / 2 + 0.035]} rotation={[0, 0, Math.PI / 2]}
          castShadow raycast={() => null}>
          <cylinderGeometry args={[0.04, 0.04, 0.5, 24]} />
          <meshStandardMaterial color="#141517" roughness={0.45} metalness={0.6} />
        </mesh>
      ))}

      {/* service guide lines from the lifted deck down to the chassis */}
      {explode > 0.01
        ? [[-1, 1], [1, 1], [-1, -1], [1, -1]].map(([sx, sz]) => {
            const x = sx * (W / 2 - 0.06)
            const z = sz * (D / 2 - 0.06)
            return (
              <Line key={`${sx}${sz}`} points={[[x, TH, z], [x + deck[0], TH + deck[1], z + deck[2]]]} color="#9aa7b4"
                lineWidth={1} dashed dashSize={0.04} gapSize={0.03} transparent opacity={0.55 * explode} />
            )
          })
        : null}

      {/* top deck (C-cover) + everything mounted on it */}
      <group position={deck}>
        <RoundedBox args={[W, deckH, D]} radius={Math.min(0.04, deckH / 2.2)} smoothness={4} position={[0, TH + deckH / 2, 0]}
          castShadow={!ghost} receiveShadow raycast={() => null}>
          <meshPhysicalMaterial
            color={deckColor}
            roughness={ghost ? 0.2 : 0.74}
            metalness={0.1}
            transparent={ghost}
            opacity={ghost ? 0.1 : 1}
            depthWrite={!ghost}
            clearcoat={ghost ? 0.6 : 0.05}
          />
        </RoundedBox>

        <Keyboard dims={dims} ghost={ghost} />

        {/* TrackPoint buttons (red-striped middle button on ThinkPads) */}
        <group position={[0, H + 0.002, 0.33]}>
          {Array.from({ length: buttons }, (_, i) => (i - (buttons - 1) / 2) * 0.3).map((x) => (
            <mesh key={x} position={[x, 0, 0]} raycast={() => null}>
              <boxGeometry args={[0.28, 0.008, 0.1]} />
              <meshStandardMaterial color="#121315" roughness={0.6} transparent={ghost} opacity={ghost ? 0.12 : 1} />
            </mesh>
          ))}
          {buttons === 3
            ? [-0.145, 0.145].map((x) => (
                <mesh key={x} position={[x * 1.03, 0.0045, 0]} raycast={() => null}>
                  <boxGeometry args={[0.008, 0.002, 0.1]} />
                  <meshStandardMaterial color={palette.trackpoint} roughness={0.8} transparent={ghost} opacity={ghost ? 0.2 : 1} />
                </mesh>
              ))
            : null}
        </group>

        {/* trackpad */}
        <mesh position={[0, H + 0.001, 0.69]} rotation={[-Math.PI / 2, 0, 0]} receiveShadow raycast={() => null}>
          <planeGeometry args={[1.1, 0.56]} />
          <meshPhysicalMaterial color="#17181b" roughness={0.35} clearcoat={0.4} transparent={ghost} opacity={ghost ? 0.1 : 1} />
        </mesh>

        {/* power button (with fingerprint reader on the model profile) */}
        <group position={[W / 2 - 0.26, H + 0.003, -D / 2 + 0.16]}>
          <mesh rotation={[-Math.PI / 2, 0, 0]} raycast={() => null}>
            <circleGeometry args={[0.045, 28]} />
            <meshStandardMaterial color={hasFingerprint ? '#0c0d0f' : '#151618'} roughness={hasFingerprint ? 0.25 : 0.6}
              metalness={hasFingerprint ? 0.4 : 0.1} transparent={ghost} opacity={ghost ? 0.15 : 1} />
          </mesh>
          {hasFingerprint ? (
            <mesh position={[0, 0.0008, 0]} rotation={[-Math.PI / 2, 0, 0]} raycast={() => null}>
              <ringGeometry args={[0.042, 0.046, 28]} />
              <meshStandardMaterial color="#6e7378" metalness={0.9} roughness={0.3} />
            </mesh>
          ) : null}
        </group>
      </group>
    </group>
  )
}
