import { create } from 'zustand'

import type { Me, WorkspaceInfo } from '../types/admin'

const WS_KEY = 'ldt.workspace'

function savedWorkspace(): string | null {
  try {
    return localStorage.getItem(WS_KEY)
  } catch {
    return null
  }
}

interface SessionState {
  me: Me | null
  workspaces: WorkspaceInfo[]
  workspaceId: string | null
  setMe: (me: Me | null) => void
  setWorkspaces: (spaces: WorkspaceInfo[]) => void
  selectWorkspace: (id: string) => void
}

/** Who is signed in (role decides which controls are enabled) and which workspace is selected. */
export const useSession = create<SessionState>((set, get) => ({
  me: null,
  workspaces: [],
  workspaceId: savedWorkspace(),
  setMe: (me) => set({ me }),
  setWorkspaces: (spaces) => {
    const current = get().workspaceId
    const valid = spaces.some((w) => w.workspace_id === current)
    set({ workspaces: spaces, workspaceId: valid ? current : spaces[0]?.workspace_id ?? null })
  },
  selectWorkspace: (id) => {
    try {
      localStorage.setItem(WS_KEY, id)
    } catch {
      /* storage unavailable: selection lasts for this page only */
    }
    set({ workspaceId: id })
  },
}))

export const canAdmin = (me: Me | null) => me?.can_admin ?? false
export const canOperate = (me: Me | null) => me?.can_operate ?? false
