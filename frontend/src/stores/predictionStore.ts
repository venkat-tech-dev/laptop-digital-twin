import { create } from 'zustand'

/**
 * Prediction lifecycle events (prediction.created / updated / invalidated / expired / confirmed) of the
 * device on screen. The live per-target state comes from the twin document (``predictions.<target>``);
 * this revision only tells history views to refetch.
 */
export const usePredictionEvents = create<{ revision: number; bump: () => void }>((set) => ({
  revision: 0,
  bump: () => set((s) => ({ revision: s.revision + 1 })),
}))
