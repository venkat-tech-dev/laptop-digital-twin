import { CameraControls } from '@react-three/drei'
import { useEffect, useRef } from 'react'

import { CAMERA_PRESETS, type CameraPreset } from './cameraPresets'

interface Props {
  preset: CameraPreset
  nonce: number
  /** Orbit / pan / dolly by pointer. Off = fixed framing (image-like viewport). */
  interactive?: boolean
  zoom?: number
}

/** Orbit / zoom / pan with animated transitions to engineering camera presets. */
export function CameraRig({ preset, nonce, interactive = true, zoom = 1 }: Props) {
  const controls = useRef<CameraControls>(null)

  useEffect(() => {
    const c = controls.current
    if (!c) return
    const { position, target } = CAMERA_PRESETS[preset]
    void c.setLookAt(...position, ...target, nonce > 0)
  }, [preset, nonce])

  useEffect(() => {
    void controls.current?.zoomTo(zoom, true)
  }, [zoom, nonce])

  return (
    <CameraControls
      ref={controls}
      makeDefault
      enabled={interactive}
      minDistance={1.2}
      maxDistance={14}
      maxPolarAngle={Math.PI * 0.95}
      smoothTime={0.35}
      draggingSmoothTime={0.08}
    />
  )
}
