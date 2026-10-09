import { useState } from 'react'

import { useLiveStatus } from '../hooks/useLiveStatus'
import { auth } from '../services/auth'
import { api } from '../services/api'
import { useSession } from '../stores/sessionStore'
import { useTwinStore } from '../stores/twinStore'
import { Icon } from '../ui/primitives'
import { NAV, navigate, type RouteId } from './routes'

const STAFF_ONLY: RouteId[] = ['simulation']

export function Sidebar({ route }: { route: RouteId }) {
  const device = useTwinStore((s) => s.device)
  const { status } = useLiveStatus()
  const live = status === 'LIVE' || status === 'DEGRADED'
  const { me, workspaces, workspaceId, selectWorkspace } = useSession()
  const [wsOpen, setWsOpen] = useState(false)
  const [userOpen, setUserOpen] = useState(false)
  const current = workspaces.find((w) => w.workspace_id === workspaceId) ?? null
  const thisDevice = current?.devices.find((d) => d.device_id === device?.device_id)
  const name = me?.display_name ?? (auth.mode === 'accounts' ? 'Not signed in' : 'Local operator')
  const initials = name.split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((x) => x[0]?.toUpperCase()).join('') || 'LO'

  return (
    <nav className="sidebar" aria-label="Primary">
      <div className="brand">
        <div className="brand__symbol">
          <Icon name="brandBox" size={19} style={{ color: 'var(--accent)' }} />
        </div>
        <div className="brand__identity">
          <p className="brand__name">LAPTOP</p>
          <p className="brand__sub">DIGITAL TWIN</p>
        </div>
      </div>

      {me?.organizations && me.organization ? <OrgSwitcher /> : null}

      {!me?.organization || workspaces.length ? <div className="dropdown">
        <button type="button" className="workspace" aria-haspopup="listbox" aria-expanded={wsOpen} onClick={() => setWsOpen(!wsOpen)}>
          <p className="eyebrow">ENGINEERING WORKSPACE</p>
          <div className="workspace__org">
            <p>{current ? `${current.name} / ${thisDevice ? 'This device' : `${current.devices.length} device${current.devices.length === 1 ? '' : 's'}`}` : 'Local / This device'}</p>
            <Icon name="chevronsUpDown" size={12} />
          </div>
        </button>
        {wsOpen ? (
          <div className="dropdown__menu dropdown__menu--sidebar" role="listbox">
            {workspaces.map((w) => (
              <button key={w.workspace_id} type="button" role="option" aria-selected={w.workspace_id === workspaceId} className="dropdown__item"
                onClick={() => { selectWorkspace(w.workspace_id); setWsOpen(false) }}>
                <span>{w.name}</span>
                <span className="note note--muted">
                  {w.devices.length ? w.devices.map((d) => `${d.name} · ${d.status}`).join(', ') : 'No devices'}
                </span>
              </button>
            ))}
            {me?.can_admin ? (
              <button type="button" className="dropdown__item" onClick={() => { setWsOpen(false); navigate('settings', 'workspaces') }}>
                <span className="link">Manage workspaces</span>
              </button>
            ) : null}
          </div>
        ) : null}
      </div> : null}

      <div className="nav">
        <p className="eyebrow">OBSERVE &amp; UNDERSTAND</p>
        {NAV.filter((n) => !(me?.role === 'employee' && STAFF_ONLY.includes(n.id))).map((n) => (
          <button key={n.id} type="button" className={`nav__item ${route === n.id ? 'is-active' : ''}`}
            aria-current={route === n.id ? 'page' : undefined} onClick={() => navigate(n.id)}>
            <Icon name={n.icon} size={15} />
            <span>{n.label}</span>
          </button>
        ))}
      </div>

      <div className="sidebar__foot">
        <div className="env-card">
          <span className={`chip ${live ? '' : status === 'OFFLINE' ? 'chip--critical' : 'chip--amber'}`}>
            <span className="chip__dot" />
            {live ? 'LIVE DEVICE' : status === 'OFFLINE' ? 'OFFLINE' : status}
          </span>
          <p className="env-card__text">
            {device
              ? live
                ? 'Real hardware telemetry from this laptop. No simulated values in live views.'
                : 'Telemetry is not current. Values shown are the last received readings.'
              : 'No device has connected. Start the telemetry agent on this laptop.'}
          </p>
          <p className="eyebrow">AGENT v{device?.agent_version ?? '—'} · LOCAL FEED</p>
        </div>
        <div className="dropdown dropdown--up">
          <button type="button" className="operator" aria-haspopup="menu" aria-expanded={userOpen} onClick={() => setUserOpen(!userOpen)}>
            <div className="operator__avatar">{initials}</div>
            <div className="operator__details">
              <p className="operator__name">{name}</p>
              <p className="operator__role">
                {me ? `${me.role.charAt(0).toUpperCase()}${me.role.slice(1)} · ` : ''}
                {auth.mode === 'none' ? 'local, no sign-in' : auth.mode === 'accounts' ? (me ? 'signed in' : 'not signed in') : auth.mode.replace('_', ' ')}
              </p>
            </div>
          </button>
          {userOpen ? (
            <div className="dropdown__menu dropdown__menu--sidebar" role="menu">
              {me?.can_admin && auth.mode === 'accounts' ? (
                <button type="button" role="menuitem" className="dropdown__item" onClick={() => { setUserOpen(false); navigate('settings', 'users') }}>
                  <span>Manage users</span>
                </button>
              ) : null}
              {auth.mode === 'accounts' || auth.mode === 'api_key' || auth.mode === 'jwt' ? (
                <button type="button" role="menuitem" className="dropdown__item" onClick={() => { setUserOpen(false); window.dispatchEvent(new Event('ldt:signout')) }}>
                  <span>Sign out</span>
                </button>
              ) : (
                <p className="dropdown__item note note--muted">Local mode: no sign-in. Set AUTH_MODE=accounts to enable user accounts.</p>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </nav>
  )
}

/** Members of several organizations switch here; the server issues a new session for the chosen one. */
function OrgSwitcher() {
  const me = useSession((s) => s.me)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (!me?.organization) return null
  const orgs = me.organizations ?? []
  const switchTo = async (orgId: string) => {
    setError(null)
    try {
      const s = await api.switchOrganization(orgId)
      setOpen(false)
      window.dispatchEvent(new CustomEvent('ldt:session', { detail: { token: s.access_token, expiresAt: s.expires_at } }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <div className="dropdown">
      <button type="button" className="workspace" aria-haspopup="listbox" aria-expanded={open} disabled={orgs.length < 2} onClick={() => setOpen(!open)}>
        <p className="eyebrow">ORGANIZATION</p>
        <div className="workspace__org">
          <p>{me.organization.name} / {me.org_role_label ?? me.org_role ?? '—'}</p>
          {orgs.length > 1 ? <Icon name="chevronsUpDown" size={12} /> : null}
        </div>
      </button>
      {open ? (
        <div className="dropdown__menu dropdown__menu--sidebar" role="listbox">
          {orgs.map((o) => (
            <button key={o.org_id} type="button" role="option" aria-selected={o.org_id === me.organization?.org_id} className="dropdown__item"
              onClick={() => void switchTo(o.org_id)}>
              <span>{o.name}</span>
              <span className="note note--muted">{o.role_label}</span>
            </button>
          ))}
          {error ? <p className="dropdown__item note" style={{ color: 'var(--critical)' }}>{error}</p> : null}
        </div>
      ) : null}
    </div>
  )
}
