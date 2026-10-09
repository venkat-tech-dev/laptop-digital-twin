import { create } from 'zustand'

/** Client-side preferences (persisted in this browser only). */
export interface Prefs {
  reconnect: boolean
  anonymizeExports: boolean
  includeProcessNamesInExports: boolean
  savedAt: string | null
}

const KEY = 'ldt.prefs'
const DEFAULTS: Prefs = { reconnect: true, anonymizeExports: true, includeProcessNamesInExports: false, savedAt: null }

function load(): Prefs {
  try {
    const raw = localStorage.getItem(KEY)
    return raw ? { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Prefs>) } : DEFAULTS
  } catch {
    return DEFAULTS
  }
}

interface PrefsState {
  saved: Prefs
  draft: Prefs
  set: (patch: Partial<Prefs>) => void
  save: () => void
  restoreDefaults: () => void
}

export const usePrefs = create<PrefsState>((set, get) => ({
  saved: load(),
  draft: load(),
  set: (patch) => set((s) => ({ draft: { ...s.draft, ...patch } })),
  save: () => {
    const next = { ...get().draft, savedAt: new Date().toISOString() }
    try {
      localStorage.setItem(KEY, JSON.stringify(next))
    } catch {
      /* storage unavailable: keep in memory */
    }
    set({ saved: next, draft: next })
  },
  restoreDefaults: () => set({ draft: { ...DEFAULTS, savedAt: get().draft.savedAt } }),
}))
