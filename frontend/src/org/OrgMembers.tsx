import { useState } from 'react'

import { useApi } from '../hooks/useApi'
import { orgApi } from '../services/orgApi'
import { useSession } from '../stores/sessionStore'
import type { Member } from '../types/org'
import { Button, DataTable, Panel, PanelHeading } from '../ui/primitives'
import { Notice, StatusChip } from './orgUi'
import { useAction, useCan, when } from './orgUtils'

/** Members, roles (least privilege), device-group scope, disable and session revocation. */
export function OrgMembers() {
  const can = useCan()
  const me = useSession((s) => s.me)
  const members = useApi(() => orgApi.members(), [])
  const org = useApi(() => orgApi.current(), [])
  const groups = useApi(() => (can('group.view') ? orgApi.groups() : Promise.resolve({ items: [] })), [can])
  const { notice, run } = useAction()
  const [form, setForm] = useState({ username: '', password: '', role: 'read_only', scope: '' })
  const roles = org.data?.roles ?? []
  const manage = can('user.manage')
  const groupName = (id: string) => groups.data?.items.find((g) => g.group_id === id)?.name ?? id

  const update = (m: Member, changes: Partial<Pick<Member, 'role' | 'status' | 'group_scope'>>, label: string) =>
    run(label, () => orgApi.updateMember(m.username, { role: m.role, group_scope: m.group_scope, status: m.status, ...changes }), members.reload)

  return (
    <Panel>
      <PanelHeading eyebrow="ACCESS" title="Members" right={<p className="panel-meta">{members.data ? `${members.data.items.length} members` : 'Loading…'}</p>} />
      <DataTable rowKey={(m) => m.username} rows={members.data?.items ?? []} empty={members.error ?? 'No members'}
        columns={[
          { key: 'u', header: 'MEMBER', width: 150, kind: 'primary', render: (m) => `${m.username}${m.username === me?.username ? ' (you)' : ''}` },
          { key: 'r', header: 'ROLE', width: 170, render: (m) => manage && m.username !== me?.username ? (
            <select className="form-select" value={m.role} aria-label={`Role of ${m.username}`}
              onChange={(e) => void update(m, { role: e.target.value }, `Role of ${m.username} changed; their sessions were ended.`)}>
              {roles.map((r) => <option key={r.role} value={r.role}>{r.label}</option>)}
            </select>
          ) : (m.role_label ?? m.role) },
          { key: 's', header: 'SCOPE', width: 150, render: (m) => (m.group_scope.length ? m.group_scope.map(groupName).join(', ') : 'Whole organization') },
          { key: 'st', header: 'STATUS', width: 100, render: (m) => <StatusChip value={m.status} /> },
          { key: 'src', header: 'SOURCE', width: 70, render: (m) => m.source },
          { key: 'mfa', header: 'MFA', width: 60, render: (m) => (m.mfa_enrolled ? 'Yes' : m.source === 'oidc' ? 'IdP' : 'No'), cellKind: (m) => (m.mfa_enrolled ? 'mono' : 'amber') },
          { key: 'l', header: 'LAST SIGN-IN', width: 120, render: (m) => when(m.last_login_at) },
          { key: 'a', header: '', grow: true, render: (m) => (m.username === me?.username ? null : (
            <span className="form-row">
              {can('user.disable') ? (
                <button type="button" className="link" onClick={() => void update(m, { status: m.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE' }, m.status === 'ACTIVE' ? `${m.username} disabled; sessions ended.` : `${m.username} enabled.`)}>
                  {m.status === 'ACTIVE' ? 'Disable' : 'Enable'}
                </button>
              ) : null}
              {can('user.disable') ? (
                <button type="button" className="link" onClick={() => void run(`Sessions of ${m.username} ended.`, () => orgApi.revokeMemberSessions(m.username))}>End sessions</button>
              ) : null}
            </span>
          )) },
        ]} />
      {manage ? (
        <form className="form-row" onSubmit={(e) => {
          e.preventDefault()
          void run(`${form.username} added.`, () => orgApi.addMember({ username: form.username.trim(), password: form.password, role: form.role, group_scope: form.scope ? [form.scope] : [] }), members.reload)
            .then((ok) => ok && setForm({ username: '', password: '', role: 'read_only', scope: '' }))
        }}>
          <input className="form-input" placeholder="Username" autoComplete="off" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} aria-label="Username" />
          <input className="form-input" type="password" placeholder="Initial password (new local account)" autoComplete="new-password" value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })} aria-label="Initial password" />
          <select className="form-select" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} aria-label="Role">
            {roles.map((r) => <option key={r.role} value={r.role}>{r.label}</option>)}
          </select>
          <select className="form-select" value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })} aria-label="Device scope">
            <option value="">Whole organization</option>
            {(groups.data?.items ?? []).map((g) => <option key={g.group_id} value={g.group_id}>Only {g.name}</option>)}
          </select>
          <Button type="submit" icon="arrowRight" disabled={!form.username.trim()}>Add member</Button>
        </form>
      ) : null}
      <Notice notice={notice} />
      <details>
        <summary className="note">What each role may do</summary>
        {roles.map((r) => (
          <p key={r.role} className="note note--muted"><strong>{r.label}</strong>: {r.permissions.join(', ')}</p>
        ))}
      </details>
      <p className="note note--muted">
        Nobody can grant a role above their own, change their own role or remove the last owner. Role, scope and status changes end the member's sessions in this organization immediately. Changes need a recent sign-in.
      </p>
    </Panel>
  )
}
