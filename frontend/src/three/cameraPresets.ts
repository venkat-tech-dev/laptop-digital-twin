export type CameraPreset = 'iso' | 'front' | 'rear' | 'top' | 'left' | 'right' | 'internals' | 'hero' | 'cutaway' | 'photo'

type Vec = [number, number, number]

export const CAMERA_PRESETS: Record<CameraPreset, { position: Vec; target: Vec; label: string }> = {
  iso: { position: [3.1, 2.6, 3.6], target: [0, 0.65, 0.1], label: 'Iso' },
  front: { position: [0, 1.6, 4.6], target: [0, 0.85, 0], label: 'Front' },
  rear: { position: [0, 1.9, -6.0], target: [0, 0.6, 0], label: 'Rear' },
  top: { position: [0, 6.8, 0.05], target: [0, 0, 0], label: 'Top' },
  left: { position: [-6.2, 1.4, 0.4], target: [0, 0.4, 0], label: 'Left' },
  right: { position: [6.2, 1.4, 0.4], target: [0, 0.4, 0], label: 'Right' },
  internals: { position: [0.6, 3.3, 1.9], target: [0.1, 0.1, -0.15], label: 'Internals' },
  /** Product three-quarter view used by the overview hero. */
  hero: { position: [-3.7, 2.7, 5.9], target: [0.15, 0.7, -0.1], label: 'Hero' },
  /** High front view looking into the opened chassis (deck lifted). */
  cutaway: { position: [0.3, 7.6, 3.9], target: [0.05, 0.2, 0.05], label: 'Cutaway' },
  /** Compact product shot for the inventory card. */
  photo: { position: [-3.3, 2.4, 5.1], target: [0.1, 0.62, -0.05], label: 'Photo' },
}
