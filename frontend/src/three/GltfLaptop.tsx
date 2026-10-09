import { useGLTF } from '@react-three/drei'
import { useMemo } from 'react'
import { Box3, Vector3 } from 'three'

import type { LaptopDims } from './dims'

/**
 * A manufacturer/model-specific mesh from MODELS_DIR (see models/README.md). It is scaled to the
 * detected width and placed on the floor. Telemetry overlays are still drawn by the parametric parts.
 */
export function GltfLaptop({ url, dims }: { url: string; dims: LaptopDims }) {
  const { scene } = useGLTF(url)
  const object = useMemo(() => {
    const clone = scene.clone(true)
    const box = new Box3().setFromObject(clone)
    const size = box.getSize(new Vector3())
    const scale = size.x > 0 ? dims.width / size.x : 1
    clone.scale.setScalar(scale)
    const scaled = new Box3().setFromObject(clone)
    const center = scaled.getCenter(new Vector3())
    clone.position.set(-center.x, -scaled.min.y, -center.z)
    clone.traverse((o) => {
      o.castShadow = true
      o.receiveShadow = true
    })
    return clone
  }, [scene, dims.width])
  return <primitive object={object} />
}
