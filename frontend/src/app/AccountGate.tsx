import { useState, type FormEvent } from 'react'

import { ApiError, api } from '../services/api'
import { orgApi } from '../services/orgApi'
import { useSession } from '../stores/sessionStore'

const errText = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e))

/**
 * Sign-in (local account, optional authenticator code) or first-run administrator setup. Organizations with
 * single sign-on: enter the organization id to list its providers (the server never lists organizations).
 */
export function AccountGate({ setup, tokenRequired, onSession }: {
  setup: boolean
  tokenRequired: boolean
  onSession: (token: string, expiresAt: string) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [setupToken, setSetupToken] = useState('')
  const [otp, setOtp] = useState('')
  const [needOtp, setNeedOtp] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [sso, setSso] = useState(false)
  const [orgId, setOrgId] = useState('')
  const [providers, setProviders] = useState<{ provider_id: string; name: string; kind: string }[] | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (setup && password !== confirm) {
      setError('Passwords do not match')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const session = setup
        ? await api.setup(username.trim(), password, setupToken.trim())
        : await api.login(username.trim(), password, needOtp ? otp.trim() : undefined)
      onSession(session.access_token, session.expires_at)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'MFA_CODE_REQUIRED') {
        setNeedOtp(true)
        setError(null)
      } else {
        setError(errText(err))
        if (err instanceof ApiError && err.code === 'MFA_CODE_INVALID') setOtp('')
      }
    } finally {
      setBusy(false)
    }
  }

  const findProviders = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    try {
      setProviders((await api.signInProviders(orgId.trim())).items.filter((p) => p.kind === 'oidc'))
    } catch (err) {
      setError(errText(err))
    }
  }

  if (sso && !setup) {
    return (
      <form className="gate" onSubmit={findProviders}>
        <p className="eyebrow">SINGLE SIGN-ON</p>
        <p className="panel-title">Sign in with your organization</p>
        <p className="note">Enter the organization id your administrator gave you.</p>
        <input autoComplete="organization" placeholder="Organization id" value={orgId} onChange={(e) => setOrgId(e.target.value)} aria-label="Organization id" />
        <button type="submit" className="btn" disabled={orgId.trim().length < 2}>Find sign-in options</button>
        {providers ? (
          providers.length ? (
            providers.map((p) => (
              <a key={p.provider_id} className="btn btn--primary" href={`/api/v1/auth/oidc/${encodeURIComponent(p.provider_id)}/login?return_to=${encodeURIComponent('/')}`}>
                Continue with {p.name}
              </a>
            ))
          ) : (
            <p className="note note--muted">No single sign-on is configured for this organization.</p>
          )
        ) : null}
        {error ? <p className="note" style={{ color: 'var(--critical)' }}>{error}</p> : null}
        <button type="button" className="link" onClick={() => { setSso(false); setError(null) }}>Use a username and password</button>
      </form>
    )
  }

  return (
    <form className="gate" onSubmit={submit}>
      <p className="eyebrow">{setup ? 'FIRST RUN / CREATE ADMINISTRATOR' : 'SIGN IN'}</p>
      <p className="panel-title">{setup ? 'Create the administrator account' : 'Sign in to the digital twin'}</p>
      <p className="note">
        {setup
          ? 'No account exists yet. The first account owns the default organization and administers the platform. Use at least 10 characters with upper- and lower-case letters and a digit.'
          : 'Your session token is kept in this tab only.'}
      </p>
      <input autoComplete="username" placeholder="Username" value={username} disabled={needOtp} onChange={(e) => setUsername(e.target.value)} aria-label="Username" />
      <input type="password" autoComplete={setup ? 'new-password' : 'current-password'} placeholder="Password" value={password} disabled={needOtp}
        onChange={(e) => setPassword(e.target.value)} aria-label="Password" />
      {needOtp ? (
        <input inputMode="numeric" autoComplete="one-time-code" placeholder="6-digit code from your authenticator app" value={otp} maxLength={6} autoFocus
          onChange={(e) => setOtp(e.target.value.replace(/\D/g, ''))} aria-label="Authenticator code" />
      ) : null}
      {setup ? <input type="password" autoComplete="new-password" placeholder="Confirm password" value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-label="Confirm password" /> : null}
      {setup && tokenRequired ? <input type="password" placeholder="Setup token (SETUP_TOKEN)" value={setupToken} onChange={(e) => setSetupToken(e.target.value)} aria-label="Setup token" /> : null}
      {error ? <p className="note" style={{ color: 'var(--critical)' }}>{error}</p> : null}
      <button type="submit" className="btn btn--primary" disabled={busy || !username || !password || (needOtp && otp.length !== 6)}>
        {busy ? 'Please wait…' : setup ? 'Create administrator' : needOtp ? 'Verify code' : 'Sign in'}
      </button>
      {!setup ? (
        needOtp ? (
          <button type="button" className="link" onClick={() => { setNeedOtp(false); setOtp('') }}>Start again</button>
        ) : (
          <button type="button" className="link" onClick={() => { setSso(true); setError(null) }}>Sign in with single sign-on</button>
        )
      ) : null}
    </form>
  )
}

/** The organization requires MFA: enroll an authenticator (TOTP), then sign in again with a code. */
export function MfaSetupGate({ onDone }: { onDone: () => void }) {
  const me = useSession((s) => s.me)
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const begin = async () => {
    setError(null)
    try {
      setSetup(await orgApi.totpSetup())
    } catch (e) {
      setError(errText(e))
    }
  }
  const activate = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await orgApi.totpActivate(code)
      onDone() // all sessions were ended: sign in again with password + code
    } catch (err) {
      setError(errText(err))
    } finally {
      setBusy(false)
    }
  }
  return (
    <form className="gate" onSubmit={activate}>
      <p className="eyebrow">MULTI-FACTOR AUTHENTICATION REQUIRED</p>
      <p className="panel-title">Set up an authenticator app</p>
      <p className="note">
        {me?.organization?.name ?? 'Your organization'} requires a second factor. Until it is set up, this session can do nothing else.
      </p>
      {!setup ? (
        <button type="button" className="btn btn--primary" onClick={begin}>Generate a secret</button>
      ) : (
        <>
          <p className="note">Add this secret to an authenticator app (manual entry, time-based, 6 digits, 30 s):</p>
          <p className="caption-mono" style={{ wordBreak: 'break-all', userSelect: 'all' }}>{setup.secret}</p>
          <p className="note note--muted" style={{ wordBreak: 'break-all' }}>Or use the setup URI: {setup.otpauth_uri}</p>
          <input inputMode="numeric" autoComplete="one-time-code" placeholder="6-digit code" value={code} maxLength={6}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} aria-label="Authenticator code" />
          <button type="submit" className="btn btn--primary" disabled={busy || code.length !== 6}>{busy ? 'Please wait…' : 'Activate and sign in again'}</button>
        </>
      )}
      {error ? <p className="note" style={{ color: 'var(--critical)' }}>{error}</p> : null}
      <button type="button" className="link" onClick={onDone}>Sign out</button>
    </form>
  )
}
