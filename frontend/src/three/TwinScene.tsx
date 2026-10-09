import { ContactShadows, Environment, Grid, Lightformer } from '@react-three/drei'
import { Canvas } from '@react-three/fiber'
import { Suspense, useMemo } from 'react'
import { ACESFilmicToneMapping } from 'three'

import { ErrorBoundary } from '../components/ErrorBoundary'
import type { CameraPreset } from './cameraPresets'
import type { Geometry } from '../types/telemetry'
import type { VisualState } from '../utils/twin'
import { CameraRig } from './CameraRig'
import { dimsFromGeometry } from './dims'
import { GltfLaptop } from './GltfLaptop'
import { LaptopModel } from './LaptopModel'
import type { PartLabels } from './parts/Internals'
import { Projector, type ProjectorSpec } from './Projector'
import { SeverityHalos } from './SeverityHalos'
import type { Visual } from '../types/twinDoc'
import type { ScreenInfo } from './screenCanvas'

export interface TwinSceneProps {
  geometry: Geometry | null
  visual: VisualState
  screen: ScreenInfo
  ethernetLinkUp: boolean | null
  labels: PartLabels
  names: PartLabels
  xray: boolean
  explode?: number
  thermal?: boolean
  memoryModules?: number | null
  lidOpen: boolean
  camera: CameraPreset
  cameraNonce: number
  /** Pointer orbit/pan/dolly (false = fixed framing). */
  interactive?: boolean
  zoom?: number
  projector?: ProjectorSpec
  /** Floor grid (off for product-style framings). */
  grid?: boolean
  selected: string | null
  hovered: string | null
  onSelect: (part: string | null) => void
  onHover: (part: string | null) => void
  /** Digital-twin section severity (cpu, memory, storage, battery, network, thermal, gpu). */
  severity?: Partial<Record<string, Visual>>
}

function Studio() {
  // Procedural studio lighting (no HDR download: works fully offline).
  return (
    <Environment resolution={256} frames={1}>
      <Lightformer intensity={2.2} position={[0, 6, 0]} rotation-x={Math.PI / 2} scale={[10, 4, 1]} />
      <Lightformer intensity={1.4} position={[-6, 2, 2]} rotation-y={Math.PI / 2} scale={[6, 2, 1]} color="#b9d8ff" />
      <Lightformer intensity={1.0} position={[6, 2, -2]} rotation-y={-Math.PI / 2} scale={[6, 2, 1]} color="#ffe2c4" />
      <Lightformer intensity={0.6} position={[0, 1, -8]} scale={[12, 3, 1]} color="#5ad1ff" />
    </Environment>
  )
}

export function TwinScene(p: TwinSceneProps) {
  const dims = useMemo(() => dimsFromGeometry(p.geometry), [p.geometry])
  const gltfUrl = p.geometry && (p.geometry.kind === 'matched' || p.geometry.kind === 'exact') ? p.geometry.url : null

  const model = (
    <LaptopModel dims={dims} visual={p.visual} screen={p.screen} xray={p.xray} explode={p.explode} thermal={p.thermal}
      memoryModules={p.memoryModules} lidOpen={p.lidOpen}
      ethernetLinkUp={p.ethernetLinkUp} labels={p.labels} names={p.names} selected={p.selected}
      hovered={p.hovered} onSelect={p.onSelect} onHover={p.onHover} />
  )

  return (
    <Canvas
      shadows
      dpr={[1, 2]}
      camera={{ position: [3.1, 2.6, 3.6], fov: 32, near: 0.05, far: 100 }}
      gl={{ antialias: true, toneMapping: ACESFilmicToneMapping, preserveDrawingBuffer: true }}
      onPointerMissed={() => p.onSelect(null)}
    >
      <color attach="background" args={['#0d1116']} />
      <fog attach="fog" args={['#0d1116', 12, 26]} />
      <ambientLight intensity={0.25} />
      <directionalLight position={[4, 8, 5]} intensity={1.6} castShadow shadow-mapSize={[2048, 2048]}
        shadow-camera-left={-4} shadow-camera-right={4} shadow-camera-top={4} shadow-camera-bottom={-4} />
      <directionalLight position={[-5, 3, -4]} intensity={0.5} color="#9cc8ff" />
      <Studio />
      <group position={[0, 0, 0.25]}>
        {gltfUrl ? (
          <ErrorBoundary fallback={model} label="3D model">
            <Suspense fallback={model}>
              <GltfLaptop url={gltfUrl} dims={dims} />
            </Suspense>
          </ErrorBoundary>
        ) : (
          model
        )}
        {p.projector ? <Projector spec={p.projector} /> : null}
        {p.severity ? <SeverityHalos dims={dims} severity={p.severity} /> : null}
      </group>
      <ContactShadows position={[0, -0.001, 0.25]} opacity={0.55} scale={10} blur={2.4} far={3} />
      {p.grid === false ? null : <Grid position={[0, -0.002, 0]} args={[30, 30]} cellSize={0.25} cellThickness={0.5} cellColor="#16202b"
        sectionSize={1} sectionThickness={0.9} sectionColor="#1d3142" fadeDistance={18} fadeStrength={1.6}
        infiniteGrid />}
      <CameraRig preset={p.camera} nonce={p.cameraNonce} interactive={p.interactive} zoom={p.zoom} />
    </Canvas>
  )
}
