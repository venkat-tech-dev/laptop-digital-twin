/**
 * Credential handling for AUTH_MODE=api_key|jwt|accounts. Keys and session tokens live in
 * sessionStorage only (cleared with the tab).
 */

const KEY = 'ldt.apiKey'
const TOKEN = 'ldt.jwt'

export type AuthMode = 'none' | 'api_key' | 'jwt' | 'accounts'

export const auth = {
  mode: 'none' as AuthMode,

  apiKey(): string | null {
    return sessionStorage.getItem(KEY)
  },
  setApiKey(key: string): void {
    sessionStorage.setItem(KEY, key)
    sessionStorage.removeItem(TOKEN)
  },
  jwt(): string | null {
    const raw = sessionStorage.getItem(TOKEN)
    if (!raw) return null
    try {
      const { token, expiresAt } = JSON.parse(raw) as { token: string; expiresAt: string }
      return new Date(expiresAt).getTime() - Date.now() > 30_000 ? token : null
    } catch {
      return null
    }
  },
  setJwt(token: string, expiresAt: string): void {
    sessionStorage.setItem(TOKEN, JSON.stringify({ token, expiresAt }))
  },
  /**
   * OIDC sign-in returns to the SPA with ``#access_token=...`` (a fragment is never sent to a server or
   * logged by proxies). Store it, then remove it from the address bar and history.
   */
  pickUpRedirectToken(): boolean {
    const m = /^#access_token=([A-Za-z0-9._-]+)$/.exec(location.hash)
    if (!m) return false
    const token = m[1]
    let expiresAt = new Date(Date.now() + 15 * 60_000).toISOString()
    try {
      const claims = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))) as { exp?: number }
      if (typeof claims.exp === 'number') expiresAt = new Date(claims.exp * 1000).toISOString()
    } catch {
      /* not a readable JWT: the server rejects it on first use */
    }
    this.setJwt(token, expiresAt)
    history.replaceState(null, '', `${location.pathname}${location.search}#/overview`)
    return true
  },
  clear(): void {
    sessionStorage.removeItem(KEY)
    sessionStorage.removeItem(TOKEN)
  },
}
