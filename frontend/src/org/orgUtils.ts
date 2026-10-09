import { useCallback, useState } from 'react'

import { ApiError } from '../services/api'
import { useSession } from '../stores/sessionStore'

export const errText = (e: unknown) => {
  if (e instanceof ApiError) {
    if (e.code === 'REAUTHENTICATION_REQUIRED') return 'This change needs a recent sign-in. Sign out and sign in again, then retry.'
    return e.code ? `${e.message} (${e.code})` : e.message
  }
  return e instanceof Error ? e.message : String(e)
}

export const when = (iso: string | null | undefined) =>
  iso ? `${new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })} UTC` : '—'

/** UI hint only: every action is authorized again by the server. */
export function useCan(): (permission: string) => boolean {
  const perms = useSession((s) => s.me?.permissions)
  return useCallback((p: string) => Boolean(perms?.includes(p)), [perms])
}

/** Run an action, show its outcome, reload on success. */
export function useAction(): { notice: { ok: boolean; text: string } | null; run: (label: string, fn: () => Promise<unknown>, after?: () => void) => Promise<boolean>; clear: () => void } {
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)
  const run = async (label: string, fn: () => Promise<unknown>, after?: () => void) => {
    setNotice(null)
    try {
      await fn()
      after?.()
      setNotice({ ok: true, text: label })
      return true
    } catch (e) {
      setNotice({ ok: false, text: errText(e) })
      return false
    }
  }
  return { notice, run, clear: () => setNotice(null) }
}
