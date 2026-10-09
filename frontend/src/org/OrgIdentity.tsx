import { useState } from 'react'

import { useApi } from '../hooks/useApi'
import { orgApi } from '../services/orgApi'
import { useSession } from '../stores/sessionStore'
import { Button, DataTable, Panel, PanelHeading, Spec } from '../ui/primitives'
import { Notice, OneTimeSecret, StatusChip } from './orgUi'
import { useAction, useCan, when } from './orgUtils'

export function OrgIdentity() {
  const can = useCan()
  return (
    <>
      <div className="split split--even">
        <MyMfa />
        <MySessions />
      </div>
      {can('identity.manage') ? <Providers /> : null}
      {can('identity.manage') ? <ScimTokens /> : null}
    </>
  )
}

function MyMfa() {
  const me = useSession((s) => s.me)
  const { notice, run } = useAction()
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null)
  const [code, setCode] = useState('')
  return (
    <Panel>
      <PanelHeading eyebrow="MY ACCOUNT" title="Multi-factor authentication" right={<StatusChip value={me?.mfa_enrolled ? 'ACTIVE' : 'DISABLED'} />} />
      {me?.mfa_enrolled ? (
        <p className="note">An authenticator app is enrolled. Sign-in asks for a code.</p>
      ) : setup ? (
        <form onSubmit={(e) => {
          e.preventDefault()
          void run('Authenticator enrolled. Sign in again with a code.', () => orgApi.totpActivate(code), () => window.dispatchEvent(new Event('ldt:signout')))
        }}>
          <p className="note">Add this secret to an authenticator app (time-based, 6 digits, 30 s), then enter a code:</p>
          <p className="caption-mono" style={{ wordBreak: 'break-all', userSelect: 'all' }}>{setup.secret}</p>
          <div className="form-row">
            <input className="form-input" inputMode="numeric" autoComplete="one-time-code" maxLength={6} placeholder="6-digit code" value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} aria-label="Authenticator code" />
            <Button type="submit" disabled={code.length !== 6}>Activate</Button>
          </div>
        </form>
      ) : (
        <>
          <p className="note">Protect your account with an authenticator app. Activating it ends your sessions; you then sign in with password and code.</p>
          <Button onClick={() => void run('Secret generated.', async () => setSetup(await orgApi.totpSetup()))}>Set up authenticator</Button>
        </>
      )}
      <Notice notice={notice} />
    </Panel>
  )
}

function MySessions() {
  const sessions = useApi(() => orgApi.sessions(), [])
  const { notice, run } = useAction()
  return (
    <Panel>
      <PanelHeading eyebrow="MY ACCOUNT" title="Sessions" />
      <DataTable rowKey={(s) => s.session_id} rows={sessions.data?.items ?? []} empty={sessions.error ?? 'No sessions'}
        columns={[
          { key: 'o', header: 'ORGANIZATION', width: 110, kind: 'primary', render: (s) => `${s.org_id}${s.current ? ' (this)' : ''}` },
          { key: 'm', header: 'METHOD', width: 80, render: (s) => `${s.auth_method}${s.mfa ? ' + MFA' : ''}` },
          { key: 'c', header: 'STARTED', width: 120, render: (s) => when(s.created_at) },
          { key: 's', header: 'STATE', width: 120, render: (s) => (s.revoked_at ? <StatusChip value="REVOKED" title={s.revoked_reason ?? ''} /> : <StatusChip value="ACTIVE" />) },
          { key: 'i', header: 'FROM', grow: true, render: (s) => s.ip ?? '—' },
          { key: 'a', header: '', width: 70, render: (s) => (!s.revoked_at && !s.current ? (
            <button type="button" className="link" onClick={() => void run('Session ended.', () => orgApi.revokeSession(s.session_id), sessions.reload)}>End</button>
          ) : null) },
        ]} />
      <Notice notice={notice} />
    </Panel>
  )
}

function Providers() {
  const providers = useApi(() => orgApi.providers(), [])
  const roles = useApi(() => orgApi.current(), [])
  const { notice, run } = useAction()
  const [f, setF] = useState({ name: '', issuer: '', client_id: '', redirect_uri: '', default_role: 'read_only', jit: true, role_claim: 'groups', mapping: '' })
  const hint = providers.data?.redirect_uri_hint ?? ''
  const add = () => {
    const role_mapping: Record<string, string> = {}
    for (const line of f.mapping.split('\n')) {
      const [group, role] = line.split('=').map((s) => s.trim())
      if (group && role) role_mapping[group] = role
    }
    return run('Identity provider added.', () => orgApi.addProvider({ kind: 'oidc', name: f.name.trim(), config: {
      issuer: f.issuer.trim(), client_id: f.client_id.trim(), redirect_uri: (f.redirect_uri || hint).trim(), default_role: f.default_role,
      jit_provisioning: f.jit, role_claim: f.role_claim.trim() || undefined, role_mapping,
    } }), providers.reload)
  }
  return (
    <Panel>
      <PanelHeading eyebrow="SINGLE SIGN-ON" title="Identity providers" right={<p className="panel-meta">OIDC (authorization code + PKCE). SAML assertions are refused until a validator is installed.</p>} />
      <DataTable rowKey={(p) => p.provider_id} rows={providers.data?.items ?? []} empty={providers.error ?? 'No identity providers'}
        columns={[
          { key: 'n', header: 'NAME', width: 150, kind: 'primary', render: (p) => p.name },
          { key: 'k', header: 'TYPE', width: 60, render: (p) => p.kind.toUpperCase() },
          { key: 'i', header: 'ISSUER', grow: true, kind: 'mono', render: (p) => String(p.config.issuer ?? p.config.entity_id ?? '—') },
          { key: 'r', header: 'DEFAULT ROLE', width: 110, render: (p) => String(p.config.default_role ?? '—') },
          { key: 's', header: 'STATUS', width: 90, render: (p) => <StatusChip value={p.status} /> },
          { key: 'id', header: 'ID', width: 90, kind: 'mono', render: (p) => p.provider_id.slice(0, 8) },
          { key: 'a', header: '', width: 70, render: (p) => (
            <button type="button" className="link" onClick={() => void run('Provider updated.', () => orgApi.providerStatus(p.provider_id, p.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'), providers.reload)}>
              {p.status === 'ACTIVE' ? 'Disable' : 'Enable'}
            </button>
          ) },
        ]} />
      <details>
        <summary className="note">Add an OIDC provider</summary>
        <div className="form-row"><input className="form-input" placeholder="Display name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} aria-label="Provider name" /></div>
        <div className="form-row"><input className="form-input" placeholder="Issuer (https://…)" value={f.issuer} onChange={(e) => setF({ ...f, issuer: e.target.value })} aria-label="Issuer" /></div>
        <div className="form-row"><input className="form-input" placeholder="Client id" value={f.client_id} onChange={(e) => setF({ ...f, client_id: e.target.value })} aria-label="Client id" /></div>
        <div className="form-row"><input className="form-input" placeholder={hint || 'Redirect URI'} value={f.redirect_uri} onChange={(e) => setF({ ...f, redirect_uri: e.target.value })} aria-label="Redirect URI" /></div>
        <div className="form-row">
          <select className="form-select" value={f.default_role} onChange={(e) => setF({ ...f, default_role: e.target.value })} aria-label="Default role">
            {(roles.data?.roles ?? []).map((r) => <option key={r.role} value={r.role}>Default: {r.label}</option>)}
          </select>
          <label className="note"><input type="checkbox" checked={f.jit} onChange={(e) => setF({ ...f, jit: e.target.checked })} /> Create members on first sign-in</label>
          <input className="form-input" placeholder="Role claim (e.g. groups)" value={f.role_claim} onChange={(e) => setF({ ...f, role_claim: e.target.value })} aria-label="Role claim" />
        </div>
        <textarea className="form-input" rows={3} placeholder={'Role mapping, one per line: idp-group = role\nit-admins = it_admin'} value={f.mapping}
          onChange={(e) => setF({ ...f, mapping: e.target.value })} aria-label="Role mapping" />
        <p className="note note--muted">PKCE is always used. A confidential client secret is never entered here: it stays in the server secret store and is referenced by name (client_secret_ref, via the API). A mapping cannot grant a role above your own.</p>
        <Button onClick={() => void add()} disabled={!f.name.trim() || !f.issuer.trim() || !f.client_id.trim()}>Add provider</Button>
      </details>
      <Spec label="Authenticator (TOTP) available" value={providers.data?.mfa_available ? 'Yes' : 'No: DATA_ENCRYPTION_KEY is not set'} tone={providers.data?.mfa_available ? 'default' : 'amber'} />
      <Notice notice={notice} />
    </Panel>
  )
}

function ScimTokens() {
  const tokens = useApi(() => orgApi.scimTokens(), [])
  const { notice, run } = useAction()
  const [label, setLabel] = useState('')
  const [secret, setSecret] = useState<string | null>(null)
  return (
    <Panel>
      <PanelHeading eyebrow="PROVISIONING" title="SCIM tokens" right={<p className="panel-meta">Endpoint: /scim/v2 · Users only · non-administrative roles</p>} />
      {secret ? <OneTimeSecret label="SCIM TOKEN" secret={secret} onDismiss={() => setSecret(null)} /> : null}
      <form className="form-row" onSubmit={(e) => {
        e.preventDefault()
        void run('SCIM token created.', async () => setSecret((await orgApi.createScimToken(label.trim())).token), tokens.reload).then((ok) => ok && setLabel(''))
      }}>
        <input className="form-input" placeholder="Label (e.g. Entra ID)" value={label} onChange={(e) => setLabel(e.target.value)} aria-label="SCIM token label" />
        <Button type="submit">Create token</Button>
      </form>
      <DataTable rowKey={(t) => t.token_id} rows={tokens.data?.items ?? []} empty={tokens.error ?? 'No SCIM tokens'}
        columns={[
          { key: 'l', header: 'LABEL', grow: true, kind: 'primary', render: (t) => t.label || t.token_id.slice(0, 8) },
          { key: 'b', header: 'CREATED BY', width: 110, render: (t) => t.created_by },
          { key: 'c', header: 'CREATED', width: 130, render: (t) => when(t.created_at) },
          { key: 's', header: 'STATUS', width: 90, render: (t) => <StatusChip value={t.revoked_at ? 'REVOKED' : 'ACTIVE'} /> },
          { key: 'a', header: '', width: 70, render: (t) => (t.revoked_at ? null : (
            <button type="button" className="link" onClick={() => void run('SCIM token revoked.', () => orgApi.revokeScimToken(t.token_id), tokens.reload)}>Revoke</button>
          )) },
        ]} />
      <Notice notice={notice} />
    </Panel>
  )
}
