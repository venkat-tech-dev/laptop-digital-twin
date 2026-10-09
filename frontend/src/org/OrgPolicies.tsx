import { useState } from 'react'

import { useApi } from '../hooks/useApi'
import { orgApi } from '../services/orgApi'
import { useSession } from '../stores/sessionStore'
import type { Policy, PolicyField, PolicyValidation } from '../types/org'
import { Button, DataTable, Panel, PanelHeading, Spec } from '../ui/primitives'
import { Notice, StatusChip } from './orgUi'
import { useAction, useCan, when } from './orgUtils'

const KIND_LABELS: Record<string, string> = {
  remediation: 'Remediation', diagnosis: 'Diagnosis', notifications: 'Notifications', security: 'Security & sign-in',
  agent: 'Agent versions & collection', enrollment: 'Enrollment', retention: 'Data retention', compliance: 'Compliance',
}
/** Kinds that need an additional permission besides policy.manage (mirrors the server; the server decides). */
const EXTRA: Record<string, string> = { remediation: 'remediation.manage_policy', security: 'security.manage', retention: 'retention.manage' }
const label = (k: string) => k.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
const show = (v: unknown) => (Array.isArray(v) ? v.join(', ') || '—' : v === null || v === undefined ? '—' : String(v))

function FieldInput({ name, field, value, onChange }: { name: string; field: PolicyField; value: unknown; onChange: (v: unknown) => void }) {
  if (field.type === 'bool') {
    return <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} aria-label={name} />
  }
  if (field.type === 'enum') {
    return (
      <select className="form-select" value={String(value)} onChange={(e) => onChange(e.target.value)} aria-label={name}>
        {field.choices.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    )
  }
  if (field.type === 'list_enum') {
    const list = Array.isArray(value) ? (value as string[]) : []
    return (
      <span className="form-row" style={{ flexWrap: 'wrap' }}>
        {field.choices.map((c) => (
          <label key={c} className="note">
            <input type="checkbox" checked={list.includes(c)} onChange={(e) => onChange(e.target.checked ? [...list, c] : list.filter((x) => x !== c))} /> {c}
          </label>
        ))}
      </span>
    )
  }
  if (field.type === 'int' || field.type === 'float') {
    return (
      <input className="form-input" type="number" style={{ maxWidth: 140 }} step={field.type === 'float' ? 0.01 : 1}
        min={field.min ?? undefined} max={field.max ?? undefined} value={String(value ?? '')}
        onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))} aria-label={name} />
    )
  }
  if (field.type === 'list_version') {
    return (
      <input className="form-input" placeholder="e.g. 1.5.0, 1.5.1" value={Array.isArray(value) ? (value as string[]).join(', ') : ''}
        onChange={(e) => onChange(e.target.value.split(',').map((s) => s.trim()).filter(Boolean))} aria-label={name} />
    )
  }
  return <input className="form-input" style={{ maxWidth: 140 }} value={String(value ?? '')} onChange={(e) => onChange(e.target.value)} aria-label={name} />
}

/**
 * Policies: draft -> validate (errors, warnings, preview of affected devices) -> publish; versions are kept
 * and a rollback publishes an earlier body as a new version. Inheritance: organization < business unit <
 * department < team < device group < device; fields locked at organization level cannot be overridden.
 */
export function OrgPolicies() {
  const can = useCan()
  const orgId = useSession((s) => s.me?.organization?.org_id ?? '')
  const schema = useApi(() => orgApi.policySchema(), [])
  const [kind, setKind] = useState('remediation')
  const policies = useApi(() => orgApi.policies(kind), [kind])
  const effective = useApi(() => orgApi.effective(kind), [kind, policies.data])
  const units = useApi(() => (can('group.view') ? orgApi.units() : Promise.resolve({ items: [] })), [can])
  const groups = useApi(() => (can('group.view') ? orgApi.groups() : Promise.resolve({ items: [] })), [can])
  const devices = useApi(() => (can('device.view') ? orgApi.devices() : Promise.resolve({ items: [], count: 0 })), [can])
  const { notice, run } = useAction()
  const [scope, setScope] = useState({ type: 'organization', id: '' })
  const [body, setBody] = useState<Record<string, unknown>>({})
  const [locked, setLocked] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [draft, setDraft] = useState<Policy | null>(null)
  const [validation, setValidation] = useState<PolicyValidation | null>(null)
  const fields = schema.data?.kinds[kind] ?? {}
  const editable = can('policy.manage') && (!EXTRA[kind] || can(EXTRA[kind]))

  const scopeOptions = (): { id: string; name: string }[] => {
    if (scope.type === 'device_group') return (groups.data?.items ?? []).map((g) => ({ id: g.group_id, name: g.name }))
    if (scope.type === 'device') return (devices.data?.items ?? []).map((d) => ({ id: d.device_id, name: d.device_id }))
    return (units.data?.items ?? []).filter((u) => u.kind === scope.type && u.status === 'ACTIVE').map((u) => ({ id: u.unit_id, name: u.name }))
  }
  const scopeName = (p: Policy) => {
    if (p.scope_type === 'organization') return 'Organization'
    const all = [...(units.data?.items ?? []).map((u) => [u.unit_id, u.name]), ...(groups.data?.items ?? []).map((g) => [g.group_id, g.name])]
    return `${label(p.scope_type)}: ${all.find(([id]) => id === p.scope_id)?.[1] ?? p.scope_id}`
  }
  const reset = (k = kind) => {
    setBody({})
    setLocked([])
    setNote('')
    setDraft(null)
    setValidation(null)
    setKind(k)
  }
  const edit = (p: Policy) => {
    setScope({ type: p.scope_type, id: p.scope_type === 'organization' ? '' : p.scope_id })
    setBody({ ...p.body })
    setLocked([...p.locked])
    setNote(p.note)
    setDraft(p.status === 'DRAFT' ? p : null)
    setValidation(null)
  }
  const saveDraft = () =>
    run('Draft saved. Validate it to see what would change.', async () => {
      const p = await orgApi.saveDraft({ kind, scope_type: scope.type, scope_id: scope.type === 'organization' ? orgId : scope.id, body, locked, note })
      setDraft(p)
      setValidation(null)
    }, policies.reload)
  const validate = () => draft && run('Validated.', async () => setValidation(await orgApi.validatePolicy(draft.policy_id, draft.version)))
  const publish = () =>
    draft && run(`Version ${draft.version} published.`, async () => {
      await orgApi.publishPolicy(draft.policy_id, draft.version)
      setDraft(null)
      setValidation(null)
    }, policies.reload)

  const versions = [...(policies.data?.items ?? [])].sort((a, b) => a.policy_id.localeCompare(b.policy_id) || b.version - a.version)

  return (
    <>
      <div className="seg" role="tablist" aria-label="Policy kind">
        {Object.keys(schema.data?.kinds ?? KIND_LABELS).map((k) => (
          <button key={k} type="button" role="tab" aria-selected={kind === k} className={`seg__btn ${kind === k ? 'is-on' : ''}`} onClick={() => reset(k)}>{KIND_LABELS[k] ?? label(k)}</button>
        ))}
      </div>
      <div className="split split--even">
        <Panel>
          <PanelHeading eyebrow="IN EFFECT" title={`${KIND_LABELS[kind] ?? kind} for the organization`} />
          {Object.entries(effective.data?.values ?? {}).map(([k, v]) => (
            <Spec key={k} label={label(k)} value={show(v)} title={`from ${effective.data?.source[k] ?? 'platform default'}`}
              tone={(effective.data?.source[k] ?? 'platform').startsWith('platform') ? 'default' : 'accent'} />
          ))}
          <p className="note note--muted">Highlighted values are set by this organization; the rest are platform defaults. Device groups and devices can narrow them further.</p>
        </Panel>
        <Panel>
          <PanelHeading eyebrow="VERSIONS" title="Policies and history" />
          <DataTable rowKey={(p) => `${p.policy_id}:${p.version}`} rows={versions} empty={policies.error ?? 'No policies of this kind: platform defaults apply'}
            columns={[
              { key: 's', header: 'SCOPE', grow: true, kind: 'primary', render: scopeName },
              { key: 'v', header: 'VER', width: 50, kind: 'mono', render: (p) => p.version },
              { key: 'st', header: 'STATUS', width: 100, render: (p) => <StatusChip value={p.status} /> },
              { key: 'b', header: 'BY', width: 90, render: (p) => p.updated_by ?? p.created_by ?? '—' },
              { key: 'w', header: 'WHEN', width: 120, render: (p) => when(p.updated_at ?? p.created_at) },
              { key: 'a', header: '', width: 150, render: (p) => (editable ? (
                <span className="form-row">
                  <button type="button" className="link" onClick={() => edit(p)}>{p.status === 'DRAFT' ? 'Continue' : 'Edit as new'}</button>
                  {p.status === 'ARCHIVED' ? (
                    <button type="button" className="link" onClick={() => void run(`Version ${p.version} restored as a new version.`, () => orgApi.rollbackPolicy(p.policy_id, p.version), policies.reload)}>Roll back</button>
                  ) : null}
                  {p.status === 'PUBLISHED' ? (
                    <button type="button" className="link" onClick={() => { if (confirm('Archive this policy? Its scope falls back to inherited values.')) void run('Policy archived.', () => orgApi.archivePolicy(p.policy_id), policies.reload) }}>Archive</button>
                  ) : null}
                </span>
              ) : null) },
            ]} />
        </Panel>
      </div>
      {editable ? (
        <Panel>
          <PanelHeading eyebrow={draft ? `DRAFT · VERSION ${draft.version}` : 'NEW DRAFT'} title="Edit policy" />
          <div className="form-row">
            <select className="form-select" value={scope.type} onChange={(e) => setScope({ type: e.target.value, id: '' })} aria-label="Scope">
              {(schema.data?.scopes ?? ['organization']).map((s) => <option key={s} value={s}>{label(s)}</option>)}
            </select>
            {scope.type !== 'organization' ? (
              <select className="form-select" value={scope.id} onChange={(e) => setScope({ ...scope, id: e.target.value })} aria-label="Scope target">
                <option value="">Choose…</option>
                {scopeOptions().map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
              </select>
            ) : null}
          </div>
          <p className="note note--muted">Tick a field to set it at this scope; unticked fields are inherited.</p>
          {Object.entries(fields).map(([name, f]) => {
            const on = name in body
            return (
              <div key={name} className="form-row" style={{ alignItems: 'center' }}>
                <label className="note" style={{ minWidth: 240 }}>
                  <input type="checkbox" checked={on} onChange={(e) => {
                    const next = { ...body }
                    if (e.target.checked) next[name] = effective.data?.values[name] ?? f.default
                    else delete next[name]
                    setBody(next)
                  }} /> {label(name)}
                </label>
                {on ? <FieldInput name={name} field={f} value={body[name]} onChange={(v) => setBody({ ...body, [name]: v })} /> : <span className="note note--muted">inherited: {show(effective.data?.values[name] ?? f.default)}</span>}
                {on && scope.type === 'organization' ? (
                  <label className="note" title="Lower scopes cannot override a locked field">
                    <input type="checkbox" checked={locked.includes(name)} onChange={(e) => setLocked(e.target.checked ? [...locked, name] : locked.filter((x) => x !== name))} /> lock
                  </label>
                ) : null}
                {f.doc ? <span className="note note--muted">{f.doc}</span> : null}
              </div>
            )
          })}
          <input className="form-input" placeholder="Change note (why)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Change note" />
          <div className="form-row">
            <Button onClick={() => void saveDraft()} disabled={scope.type !== 'organization' && !scope.id}>Save draft</Button>
            <Button onClick={() => void validate()} disabled={!draft}>Validate & preview</Button>
            <Button primary onClick={() => void publish()} disabled={!draft || !validation || validation.errors.length > 0}>Publish</Button>
            <Button onClick={() => reset()}>Discard</Button>
          </div>
          {validation ? (
            <div className="panel">
              {validation.errors.map((e) => <p key={e} className="note" style={{ color: 'var(--critical)' }}>Error: {e}</p>)}
              {validation.warnings.map((w) => <p key={w} className="note" style={{ color: 'var(--amber)' }}>Warning: {w}</p>)}
              <Spec label="Devices affected" value={String(validation.preview.affected_devices)} />
              {validation.preview.devices_on_blocked_versions !== undefined ? (
                <Spec label="Devices on blocked versions" value={String(validation.preview.devices_on_blocked_versions)} tone={validation.preview.devices_on_blocked_versions ? 'amber' : 'default'} />
              ) : null}
              {Object.entries(validation.preview.changes).map(([k, c]) => <Spec key={k} label={label(k)} value={`${show(c.from)} → ${show(c.to)}`} tone="accent" />)}
              {!Object.keys(validation.preview.changes).length ? <p className="note note--muted">No effective value changes.</p> : null}
            </div>
          ) : null}
        </Panel>
      ) : <p className="note note--muted">You can view policies. Changing {KIND_LABELS[kind] ?? kind} policy needs policy.manage{EXTRA[kind] ? ` and ${EXTRA[kind]}` : ''}.</p>}
      <Notice notice={notice} />
    </>
  )
}
