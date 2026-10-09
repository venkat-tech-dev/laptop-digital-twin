import { useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { ErrorBoundary } from '../components/ErrorBoundary'
import { arr, obj, useInventory } from '../hooks/useInventory'
import { useLiveStatus } from '../hooks/useLiveStatus'
import { useTwinStore } from '../stores/twinStore'
import type { CameraPreset } from '../three/cameraPresets'
import type { ScreenPoint, Vec3 } from '../three/Projector'
import { TwinScene } from '../three/TwinScene'
import { useNow } from '../hooks/useNow'
import { useTwinValue } from '../stores/twinDocStore'
import { ageText } from '../twin/format'
import type { Connectivity, TwinSection } from '../types/twinDoc'
import { liveVisualState, ofType, readingOf } from '../utils/twin'

export interface ModelView {
  camera: CameraPreset
  xray: boolean
  /** 0 = assembled, 1 = keyboard deck lifted. */
  explode: number
  thermal: boolean
}

export interface ModelAnchor {
  id: string
  /** Model-space point the leader line points at. */
  point: Vec3
  /** Label offset from the projected point, in px. Negative x = label to the left. */
  offset: [number, number]
  label?: ReactNode
  selected?: boolean
  onClick?: () => void
}

interface Props {
  view: ModelView
  anchors?: ModelAnchor[]
  annotate?: boolean
  interactive?: boolean
  zoom?: number
  nonce?: number
  grid?: boolean
  fallback?: ReactNode
  onSelect?: (part: string | null) => void
}

/**
 * Live WebGL twin of the detected laptop (model-specific when the backend reports a profile) with
 * HTML annotations that follow the projected component positions every frame.
 */
export function Twin3D({ view, anchors = [], annotate = true, interactive = false, zoom = 1, nonce = 0, grid = false, fallback, onSelect }: Props) {
  const components = useTwinStore((s) => s.components)
  const device = useTwinStore((s) => s.device)
  const { inventory } = useInventory()
  const { status } = useLiveStatus()
  const [hovered, setHovered] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const visual = useMemo(() => liveVisualState(components, status), [components, status])
  const connectivity = useTwinValue<Connectivity>('connectivity.status')
  const lastTelemetry = useTwinValue<string>('connectivity.last_telemetry_at')
  const now = useNow(5000)
  const s = {
    cpu: useTwinValue<TwinSection>('sections.cpu')?.visual,
    thermal: useTwinValue<TwinSection>('sections.thermal')?.visual,
    gpu: useTwinValue<TwinSection>('sections.gpu')?.visual,
    memory: useTwinValue<TwinSection>('sections.memory')?.visual,
    storage: useTwinValue<TwinSection>('sections.storage')?.visual,
    battery: useTwinValue<TwinSection>('sections.battery')?.visual,
    network: useTwinValue<TwinSection>('sections.network')?.visual,
  }
  const severity = useMemo(() => s, [s.cpu, s.thermal, s.gpu, s.memory, s.storage, s.battery, s.network]) // eslint-disable-line react-hooks/exhaustive-deps
  const disconnected = connectivity === 'OFFLINE' || connectivity === 'UNKNOWN' || connectivity === 'STALE'
  const ethernetUp = useMemo(() => {
    const eth = ofType(components, 'network_adapter').find((n) => n.properties.type === 'ethernet')
    const r = readingOf(eth, 'network.link_up')
    return r?.availability === 'available' ? r.value === true : null
  }, [components])
  const screen = useMemo(() => ({ title: device ? `${device.manufacturer ?? ''} ${device.model ?? ''}`.trim() : 'No device', mode: 'live' as const, status }), [device, status])
  const modules = inventory ? arr(obj(obj(inventory).memory).modules).length || null : null

  // DOM nodes positioned from the projector (no React state per frame).
  const dots = useRef<Record<string, HTMLElement | null>>({})
  const labels = useRef<Record<string, HTMLElement | null>>({})
  const lines = useRef<Record<string, SVGLineElement | null>>({})
  const anchorsRef = useRef(anchors)
  useLayoutEffect(() => {
    anchorsRef.current = anchors
  }, [anchors])
  const points = useMemo(() => Object.fromEntries(anchors.map((a) => [a.id, a.point])), [anchors])
  const onFrame = useCallback((screenPts: Record<string, ScreenPoint>) => {
    for (const a of anchorsRef.current) {
      const p = screenPts[a.id]
      if (!p) continue
      const lx = p.x + a.offset[0]
      const ly = p.y + a.offset[1]
      const dot = dots.current[a.id]
      const label = labels.current[a.id]
      const line = lines.current[a.id]
      const vis = p.hidden ? 'hidden' : 'visible'
      if (dot) {
        dot.style.transform = `translate(${p.x - 4}px, ${p.y - 4}px)`
        dot.style.visibility = vis
      }
      if (label) {
        label.style.transform = `translate(${lx}px, ${ly}px) translate(${a.offset[0] < 0 ? '-100%' : '0'}, -50%)`
        label.style.visibility = vis
      }
      if (line) {
        line.setAttribute('x1', String(p.x))
        line.setAttribute('y1', String(p.y))
        line.setAttribute('x2', String(lx))
        line.setAttribute('y2', String(ly))
        line.style.visibility = vis
      }
    }
  }, [])
  const projector = useMemo(() => (anchors.length ? { points, onFrame } : undefined), [anchors.length, points, onFrame])

  return (
    <ErrorBoundary label="3D model" fallback={fallback}>
      <div className={`viewport__canvas ${disconnected ? 'viewport__canvas--disconnected' : ''}`} data-connectivity={connectivity ?? 'UNKNOWN'}>
        <TwinScene geometry={device?.geometry ?? null} visual={visual} screen={screen} ethernetLinkUp={ethernetUp}
          labels={{}} names={{}} xray={view.xray} explode={view.explode} thermal={view.thermal} memoryModules={modules}
          lidOpen camera={view.camera} cameraNonce={nonce} interactive={interactive} zoom={zoom} grid={grid}
          projector={projector} selected={selected} hovered={hovered} severity={disconnected ? undefined : severity}
          onSelect={(p) => {
            setSelected(p)
            onSelect?.(p)
          }}
          onHover={setHovered} />
      </div>
      {disconnected ? (
        <div className={`twin-offline twin-offline--${(connectivity ?? 'UNKNOWN').toLowerCase()}`} role="status">
          <p className="twin-offline__title">
            {connectivity === 'OFFLINE' ? 'DEVICE OFFLINE' : connectivity === 'STALE' ? 'TELEMETRY DELAYED' : 'STATE UNKNOWN'}
          </p>
          <p className="twin-offline__sub">
            {connectivity === 'UNKNOWN' ? 'No contact from the agent since the backend started' : `Last update ${ageText(lastTelemetry, now)} - showing the last known state`}
          </p>
        </div>
      ) : null}
      {anchors.length ? (
        <div className="model-overlay">
          <svg className="model-overlay__lines" aria-hidden="true">
            {annotate ? anchors.filter((a) => a.label).map((a) => (
              <line key={a.id} ref={(el) => { lines.current[a.id] = el }} className={a.selected ? 'is-selected' : undefined} />
            )) : null}
          </svg>
          {anchors.map((a) => (
            <button key={a.id} type="button" ref={(el) => { dots.current[a.id] = el }} aria-label={`Select ${a.id}`}
              className={`model-overlay__dot ${a.selected ? 'is-selected' : ''}`} onClick={a.onClick} tabIndex={a.onClick ? 0 : -1} />
          ))}
          {annotate ? anchors.filter((a) => a.label).map((a) => (
            <div key={a.id} ref={(el) => { labels.current[a.id] = el }} className="model-overlay__label" onClick={a.onClick}
              role={a.onClick ? 'button' : undefined}>
              {a.label}
            </div>
          )) : null}
        </div>
      ) : null}
    </ErrorBoundary>
  )
}
