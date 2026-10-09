import { useState } from 'react'

import { useApi } from '../hooks/useApi'
import { orgApi } from '../services/orgApi'
import type { DeviceGroup, OrgUnit } from '../types/org'
import { Button, DataTable, Panel, PanelHeading } from '../ui/primitives'
import { Notice, StatusChip } from './orgUi'
import { useAction, useCan } from './orgUtils'

const UNIT_KINDS: OrgUnit['kind'][] = ['business_unit', 'department', 'team']
const GROUP_KINDS = ['custom', 'department', 'team', 'location', 'business_unit', 'os', 'environment', 'role']
const label = (k: string) => k.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())

/** Business units > departments > teams, and device groups (with policy priority and membership). */
export function OrgStructure() {
  const can = useCan()
  const manage = can('group.manage')
  const units = useApi(() => orgApi.units(), [])
  const groups = useApi(() => orgApi.groups(), [])
  const devices = useApi(() => (can('device.view') ? orgApi.devices() : Promise.resolve({ items: [], count: 0 })), [can])
  const { notice, run } = useAction()
  const [unit, setUnit] = useState({ kind: 'business_unit' as OrgUnit['kind'], name: '', parent: '' })
  const [group, setGroup] = useState({ name: '', kind: 'custom', unit: '', priority: 100 })
  const [selected, setSelected] = useState<string | null>(null)
  const [addDevice, setAddDevice] = useState('')
  const live = (units.data?.items ?? []).filter((u) => u.status === 'ACTIVE')
  const unitName = (id: string | null) => (id ? units.data?.items.find((u) => u.unit_id === id)?.name ?? id : '—')
  const sel: DeviceGroup | undefined = groups.data?.items.find((g) => g.group_id === selected)

  const depth = (u: OrgUnit): number => (u.parent_id ? 1 + depth(units.data?.items.find((x) => x.unit_id === u.parent_id) ?? { ...u, parent_id: null }) : 0)
  const ordered = [...live].sort((a, b) => depth(a) - depth(b) || a.name.localeCompare(b.name))

  return (
    <>
      <div className="split split--even">
        <Panel>
          <PanelHeading eyebrow="HIERARCHY" title="Business units, departments and teams" />
          <DataTable rowKey={(u) => u.unit_id} rows={ordered} empty={units.error ?? 'No units yet'}
            columns={[
              { key: 'n', header: 'NAME', grow: true, kind: 'primary', render: (u) => `${'— '.repeat(depth(u))}${u.name}` },
              { key: 'k', header: 'KIND', width: 120, render: (u) => label(u.kind) },
              { key: 'a', header: '', width: 70, render: (u) => (manage ? (
                <button type="button" className="link" onClick={() => { if (confirm(`Archive ${u.name}?`)) void run(`${u.name} archived.`, () => orgApi.archiveUnit(u.unit_id), units.reload) }}>Archive</button>
              ) : null) },
            ]} />
          {manage ? (
            <form className="form-row" onSubmit={(e) => {
              e.preventDefault()
              void run(`${unit.name} created.`, () => orgApi.createUnit(unit.kind, unit.name.trim(), unit.parent || null), units.reload).then((ok) => ok && setUnit({ ...unit, name: '' }))
            }}>
              <select className="form-select" value={unit.kind} onChange={(e) => setUnit({ ...unit, kind: e.target.value as OrgUnit['kind'] })} aria-label="Unit kind">
                {UNIT_KINDS.map((k) => <option key={k} value={k}>{label(k)}</option>)}
              </select>
              <input className="form-input" placeholder="Name" value={unit.name} onChange={(e) => setUnit({ ...unit, name: e.target.value })} aria-label="Unit name" />
              <select className="form-select" value={unit.parent} onChange={(e) => setUnit({ ...unit, parent: e.target.value })} aria-label="Parent unit">
                <option value="">No parent</option>
                {live.map((u) => <option key={u.unit_id} value={u.unit_id}>{u.name}</option>)}
              </select>
              <Button type="submit" disabled={!unit.name.trim()}>Add</Button>
            </form>
          ) : null}
        </Panel>
        <Panel>
          <PanelHeading eyebrow="DEVICE GROUPS" title="Groups" right={<p className="panel-meta">Lower priority number wins when a device's groups disagree</p>} />
          <DataTable rowKey={(g) => g.group_id} rows={groups.data?.items ?? []} empty={groups.error ?? 'No groups yet'} selected={selected} onSelect={(g) => setSelected(g.group_id)}
            columns={[
              { key: 'n', header: 'GROUP', grow: true, kind: 'primary', render: (g) => g.name },
              { key: 'k', header: 'KIND', width: 100, render: (g) => label(g.kind) },
              { key: 'u', header: 'UNIT', width: 110, render: (g) => unitName(g.unit_id) },
              { key: 'p', header: 'PRIORITY', width: 70, kind: 'mono', render: (g) => g.priority },
              { key: 'c', header: 'DEVICES', width: 70, kind: 'mono', render: (g) => g.device_count ?? 0 },
              { key: 's', header: 'STATUS', width: 90, render: (g) => <StatusChip value={g.status} /> },
            ]} />
          {manage ? (
            <form className="form-row" onSubmit={(e) => {
              e.preventDefault()
              void run(`${group.name} created.`, () => orgApi.saveGroup({ name: group.name.trim(), kind: group.kind, unit_id: group.unit || null, priority: group.priority, tags: [], status: 'ACTIVE' }), groups.reload)
                .then((ok) => ok && setGroup({ ...group, name: '' }))
            }}>
              <input className="form-input" placeholder="Group name" value={group.name} onChange={(e) => setGroup({ ...group, name: e.target.value })} aria-label="Group name" />
              <select className="form-select" value={group.kind} onChange={(e) => setGroup({ ...group, kind: e.target.value })} aria-label="Group kind">
                {GROUP_KINDS.map((k) => <option key={k} value={k}>{label(k)}</option>)}
              </select>
              <select className="form-select" value={group.unit} onChange={(e) => setGroup({ ...group, unit: e.target.value })} aria-label="Unit">
                <option value="">No unit</option>
                {live.map((u) => <option key={u.unit_id} value={u.unit_id}>{u.name}</option>)}
              </select>
              <input className="form-input" type="number" min={0} max={10000} style={{ maxWidth: 90 }} value={group.priority}
                onChange={(e) => setGroup({ ...group, priority: Number(e.target.value) })} aria-label="Priority" />
              <Button type="submit" disabled={!group.name.trim()}>Add group</Button>
            </form>
          ) : null}
        </Panel>
      </div>
      {sel ? (
        <Panel>
          <PanelHeading eyebrow="GROUP" title={sel.name} right={manage ? (
            <button type="button" className="link" onClick={() => void run(sel.status === 'ACTIVE' ? 'Group archived.' : 'Group restored.', () => orgApi.saveGroup({ name: sel.name, kind: sel.kind, unit_id: sel.unit_id, priority: sel.priority, tags: sel.tags, status: sel.status === 'ACTIVE' ? 'ARCHIVED' : 'ACTIVE' }, sel.group_id), groups.reload)}>
              {sel.status === 'ACTIVE' ? 'Archive group' : 'Restore group'}
            </button>
          ) : null} />
          <DataTable rowKey={(d) => d} rows={sel.devices ?? []} empty="No devices in this group"
            columns={[
              { key: 'd', header: 'DEVICE', grow: true, kind: 'mono', render: (d) => d },
              { key: 'a', header: '', width: 80, render: (d) => (manage ? <button type="button" className="link" onClick={() => void run(`${d} removed.`, () => orgApi.groupMembers(sel.group_id, [], [d]), groups.reload)}>Remove</button> : null) },
            ]} />
          {manage ? (
            <form className="form-row" onSubmit={(e) => {
              e.preventDefault()
              void run(`${addDevice} added.`, () => orgApi.groupMembers(sel.group_id, [addDevice], []), groups.reload).then((ok) => ok && setAddDevice(''))
            }}>
              <select className="form-select" value={addDevice} onChange={(e) => setAddDevice(e.target.value)} aria-label="Device to add">
                <option value="">Choose a device…</option>
                {(devices.data?.items ?? []).filter((d) => !(sel.devices ?? []).includes(d.device_id)).map((d) => (
                  <option key={d.device_id} value={d.device_id}>{d.device_id}{d.model ? ` · ${d.model}` : ''}</option>
                ))}
              </select>
              <Button type="submit" disabled={!addDevice}>Add to group</Button>
            </form>
          ) : null}
        </Panel>
      ) : null}
      <Notice notice={notice} />
    </>
  )
}
