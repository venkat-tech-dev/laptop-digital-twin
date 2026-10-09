import { useMemo } from 'react'

import { useTwinStore } from '../stores/twinStore'
import { dimsFromGeometry, type LaptopDims } from '../three/dims'
import { partPositions } from '../three/partPositions'
import type { Vec3 } from '../three/Projector'
import type { Geometry } from '../types/telemetry'

export type ModelPart = keyof ReturnType<typeof partPositions>

export interface ModelTwin {
  geometry: Geometry | null
  dims: LaptopDims
  /** True when the 3D twin represents this specific model (profile or model file) rather than a generic laptop. */
  modelSpecific: boolean
  /** Short label for chips/captions, e.g. "MODEL PROFILE". */
  label: string
  /** Where the representation comes from, for provenance captions. */
  source: string
}

export function useModelTwin(): ModelTwin {
  const geometry = useTwinStore((s) => s.device?.geometry ?? null)
  return useMemo(() => {
    const dims = dimsFromGeometry(geometry)
    const kind = geometry?.kind ?? 'generic'
    return {
      geometry,
      dims,
      modelSpecific: kind !== 'generic',
      label: geometry?.label ?? 'GENERIC MODEL',
      source: kind === 'profile' ? `Parametric twin from published specifications (${geometry?.profile?.source ?? 'model profile'})`
        : kind === 'exact' || kind === 'matched' ? `3D model file${geometry?.attribution ? ` · ${geometry.attribution}` : ''}`
          : 'Generic illustrative render',
    }
  }, [geometry])
}

/** Model-space point of a component: on the component itself, or on the deck surface above it when assembled. */
export function partPoint(dims: LaptopDims, part: ModelPart, onSurface: boolean): Vec3 {
  const p = partPositions(dims)[part]
  return onSurface ? [p[0], dims.baseHeight + 0.01, p[2]] : [p[0], p[1] + 0.03, p[2]]
}
