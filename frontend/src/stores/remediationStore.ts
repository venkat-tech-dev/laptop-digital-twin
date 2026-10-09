import { create } from 'zustand'

/**
 * remediation.* WebSocket events. The server is the source of truth; this revision tells open views to
 * refetch over REST (also after a reconnect, so nothing is missed while disconnected).
 */
export const useRemediationEvents = create<{ revision: number; bump: () => void }>((set) => ({
  revision: 0,
  bump: () => set((s) => ({ revision: s.revision + 1 })),
}))
