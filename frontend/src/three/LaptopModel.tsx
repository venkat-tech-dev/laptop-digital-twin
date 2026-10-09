import type { VisualState } from '../utils/twin'
import type { LaptopDims } from './dims'
import { Base } from './parts/Base'
import { Internals, type PartLabels } from './parts/Internals'
import { Lid } from './parts/Lid'
import { ThermalMap } from './parts/ThermalMap'
import type { ScreenInfo } from './screenCanvas'
import { Selectable } from './Selectable'

export interface LaptopModelProps {
  dims: LaptopDims
  visual: VisualState
  screen: ScreenInfo
  xray: boolean
  /** 0 = assembled, 1 = keyboard deck lifted (cutaway). */
  explode?: number
  thermal?: boolean
  memoryModules?: number | null
  lidOpen: boolean
  ethernetLinkUp: boolean | null
  labels: PartLabels
  names: PartLabels
  selected: string | null
  hovered: string | null
  onSelect: (part: string) => void
  onHover: (part: string | null) => void
}

/**
 * Parametric laptop (model-specific when `dims.profile` is set). Every telemetry-driven visual takes
 * its value from `visual`.
 */
export function LaptopModel(props: LaptopModelProps) {
  const { dims, visual, xray, selected, hovered, onSelect, onHover } = props
  const explode = props.explode ?? 0
  return (
    <group>
      {xray ? (
        <Base dims={dims} xray explode={explode} ethernetLinkUp={props.ethernetLinkUp} />
      ) : (
        <Selectable part="chassis" label={props.names.chassis ?? 'Chassis'} detail="structural — no sensors"
          bounds={[dims.width + 0.02, dims.baseHeight + 0.02, dims.depth + 0.02]}
          boundsOffset={[0, dims.baseHeight / 2, 0]} selected={selected === 'chassis'} hovered={hovered === 'chassis'}
          onSelect={onSelect} onHover={onHover}>
          <Base dims={dims} xray={false} explode={0} ethernetLinkUp={props.ethernetLinkUp} />
          <mesh position={[0, dims.baseHeight / 2, 0]}>
            <boxGeometry args={[dims.width, dims.baseHeight, dims.depth]} />
            <meshBasicMaterial transparent opacity={0} depthWrite={false} />
          </mesh>
        </Selectable>
      )}
      {xray && (
        <Internals dims={dims} memoryModules={props.memoryModules ?? null} visual={visual} visible labels={props.labels} names={props.names} selected={selected}
          hovered={hovered} onSelect={onSelect} onHover={onHover} />
      )}
      {xray && props.thermal ? <ThermalMap dims={dims} visual={visual} /> : null}
      <Lid dims={dims} open={props.lidOpen} visual={visual} screen={props.screen} selected={selected}
        hovered={hovered} onSelect={onSelect} onHover={onHover} />
    </group>
  )
}
