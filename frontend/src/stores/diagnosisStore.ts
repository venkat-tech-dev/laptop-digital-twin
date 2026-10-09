import { create } from 'zustand'

/**
 * diagnosis.* WebSocket events (started / updated / available / failed / expired). The server is the
 * source of truth; this revision only tells open diagnosis views to refetch over REST.
 */
export const useDiagnosisEvents = create<{ revision: number; bump: () => void }>((set) => ({
  revision: 0,
  bump: () => set((s) => ({ revision: s.revision + 1 })),
}))
