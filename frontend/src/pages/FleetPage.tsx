import { useEffect, useMemo, useRef, useState } from 'react'

import { navigate } from '../app/routes'
import { useNow } from '../hooks/useNow'
import { ApiError, api } from '../services/api'
import { connection } from '../services/connection'
import { deviceScope } from '../services/deviceScope'
import { useFleet } from '../stores/fleetStore'
import { canAdmin, useSession } from '../stores/sessionStore'
import type { Connectivity, FleetPage as FleetPageData, FleetRow, FleetSummary, TwinHealth, Visual } from '../types/twinDoc'
import { ageText, CONNECTIVITY_LABEL, CONNECTIVITY_TONE, HEALTH_TONE } from '../twin/format'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, DataTable, Panel, PanelHeading, type Column } from '../ui/primitives'

const HEALTHS: TwinHealth[] = ['CRITICAL', 'WARNING', 'HEALTHY', 'UNKNOWN']
const CONNECTIVITIES: Connectivity[] = ['ONLINE', 'DEGRADED', 'STALE', 'OFFLINE', 'UNKNOWN']
const PAGE_SIZE = 50

function pct(v: number | null, status: Visual | null): { text: string; cls: 'accent' | 'amber' | 'mono' } {
  if (v === null || v === undefined) return { text: '—', cls: 'mono' }
  return { text: `${v.toFixed(0)}%`, cls: status === 'warning' || status === 'critical' ? 'amber' : 'mono' }
}

/** Organization -> department -> employee -> device -> digital twin. */
export function FleetPage() {
  const me = useSession((s) => s.me)
  const live = useFleet((s) => s.live)
  const liveVersion = useFleet((s) => s.version)
  const now = useNow(15_000)
  const [summary, setSummary] = useState<FleetSummary | null>(null)
  const [page, setPage] = useState<FleetPageData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  const [health, setHealth] = useState<TwinHealth[]>([])
  const [conn, setConn] = useState<Connectivity[]>([])
  const [department, setDepartment] = useState<string>('')
  const [sort, setSort] = useState<{ key: string; order: 'asc' | 'desc' }>({ key: 'health', order: 'asc' })
  const [pageNo, setPageNo] = useState(1)
  const [assigning, setAssigning] = useState<FleetRow | null>(null)
  const [reload, setReload] = useState(0)

  // live overview: low-volume twin.summary messages on the fleet topic (no polling)
  useEffect(() => connection.useTopic('fleet'), [])
  useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 300)
    return () => clearTimeout(t)
  }, [q])
  useEffect(() => setPageNo(1), [query, health, conn, department, sort])

  useEffect(() => {
    let cancelled = false
    api.devicesPage({ q: query || undefined, health, connectivity: conn, department: department || undefined, sort: sort.key, order: sort.order, page: pageNo, page_size: PAGE_SIZE })
      .then((p) => !cancelled && (setPage(p), setError(null)))
      .catch((e) => !cancelled && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      cancelled = true
    }
  }, [query, health, conn, department, sort, pageNo, reload])

  // counts: refresh when a device's connectivity/health changes (debounced), never on a timer
  const lastSummary = useRef(0)
  useEffect(() => {
    const wait = Math.max(0, 5000 - (Date.now() - lastSummary.current))
    const t = setTimeout(() => {
      lastSummary.current = Date.now()
      api.fleetSummary().then(setSummary).catch(() => undefined)
    }, liveVersion === 0 ? 0 : wait)
    return () => clearTimeout(t)
  }, [liveVersion, reload])

  const rows = useMemo(() => (page?.items ?? []).map((r) => ({ ...r, ...(live[r.device_id] ?? {}) }) as FleetRow), [page, live])

  const open = (r: FleetRow) => {
    deviceScope.set(r.device_id)
    navigate('twin')
  }
  const toggle = <T,>(list: T[], v: T, set: (l: T[]) => void) => set(list.includes(v) ? list.filter((x) => x !== v) : [...list, v])

  const columns: Column<FleetRow>[] = [
    { key: 'device', header: 'DEVICE', width: 220, kind: 'primary', sortKey: 'device',
      render: (r) => <span title={r.device_id}>{r.hostname ?? r.model ?? r.device_id}<span className="table__sub">{r.model}</span></span> },
    { key: 'owner', header: 'EMPLOYEE', width: 150, sortKey: 'owner', render: (r) => r.owner ?? <span className="na">Unassigned</span> },
    { key: 'status', header: 'STATUS', width: 110, sortKey: 'status',
      render: (r) => <Chip tone={CONNECTIVITY_TONE[r.connectivity]}>{CONNECTIVITY_LABEL[r.connectivity]}</Chip> },
    { key: 'health', header: 'HEALTH', width: 110, sortKey: 'health', render: (r) => <Chip tone={HEALTH_TONE[r.health]}>{r.health}</Chip> },
    { key: 'cpu', header: 'CPU', width: 64, sortKey: 'cpu', render: (r) => pct(r.cpu, r.cpu_status).text, cellKind: (r) => pct(r.cpu, r.cpu_status).cls },
    { key: 'ram', header: 'RAM', width: 64, sortKey: 'memory', render: (r) => pct(r.memory, r.memory_status).text, cellKind: (r) => pct(r.memory, r.memory_status).cls },
    { key: 'disk', header: 'DISK', width: 64, sortKey: 'disk', render: (r) => pct(r.disk, r.disk_status).text, cellKind: (r) => pct(r.disk, r.disk_status).cls },
    { key: 'battery', header: 'BATTERY', width: 76, sortKey: 'battery', render: (r) => pct(r.battery, r.battery_status).text, cellKind: (r) => pct(r.battery, r.battery_status).cls },
    { key: 'temp', header: 'TEMP', width: 64, sortKey: 'temperature', render: (r) => (r.temperature === null || r.temperature === undefined ? '—' : `${r.temperature.toFixed(0)}°C`), cellKind: (r) => pct(r.temperature, r.temperature_status).cls },
    { key: 'net', header: 'NETWORK', width: 92, render: (r) => (r.internet === null || r.internet === undefined ? '—' : r.internet ? 'Online' : 'No internet'), cellKind: (r) => (r.internet === false ? 'amber' : 'mono') },
    { key: 'seen', header: 'LAST SEEN', grow: true, sortKey: 'last_seen', render: (r) => ageText(r.last_telemetry_at, now) },
  ]

  const s = summary
  return (
    <>
      <PageHeading title="Devices & Fleet" subtitle="Organization → department → employee → device → digital twin. Live status from each device's twin." />
      <div className="fleet-overview">
        <Panel className="fleet-totals">
          <PanelHeading eyebrow={(s?.organization ?? 'ORGANIZATION').toUpperCase()} title={s ? `${s.total.toLocaleString()} ${s.total === 1 ? 'device' : 'devices'}` : 'Loading…'} />
          <div className="fleet-counts">
            {HEALTHS.map((h) => (
              <button key={h} type="button" className={`fleet-count fleet-count--${h.toLowerCase()} ${health.includes(h) ? 'is-active' : ''}`}
                onClick={() => toggle(health, h, setHealth)} aria-pressed={health.includes(h)}>
                <span className="fleet-count__n">{(s?.by_health[h] ?? 0).toLocaleString()}</span>
                <span className="fleet-count__label">{h === 'UNKNOWN' ? 'Unknown' : h.charAt(0) + h.slice(1).toLowerCase()}</span>
              </button>
            ))}
          </div>
          <div className="fleet-conn">
            {CONNECTIVITIES.map((c) => (
              <button key={c} type="button" className={`chip ${CONNECTIVITY_TONE[c] === 'accent' ? '' : `chip--${CONNECTIVITY_TONE[c]}`} ${conn.includes(c) ? 'is-active' : ''}`}
                onClick={() => toggle(conn, c, setConn)} aria-pressed={conn.includes(c)}>
                <span className="chip__dot" />{CONNECTIVITY_LABEL[c]} {s?.by_connectivity[c] ?? 0}
              </button>
            ))}
          </div>
        </Panel>
        <Panel className="fleet-departments">
          <PanelHeading eyebrow="DEPARTMENTS" title="By workspace" />
          {s?.departments.length ? s.departments.map((d) => (
            <button key={d.name} type="button" className={`spec spec--button ${department === d.name ? 'is-active' : ''}`}
              onClick={() => setDepartment(department === d.name ? '' : d.name)}>
              <p className="spec__label">{d.name}</p>
              <p className="spec__value">
                {d.total} · {d.by_health.CRITICAL ? `${d.by_health.CRITICAL} critical · ` : ''}{d.by_health.WARNING ? `${d.by_health.WARNING} warning · ` : ''}{d.by_connectivity.OFFLINE ?? 0} offline
              </p>
            </button>
          )) : <p className="note">No departments yet. Administrators group devices into workspaces in Settings.</p>}
        </Panel>
      </div>

      <Panel>
        <PanelHeading eyebrow="DEVICE LIST" title={page ? `${page.total.toLocaleString()} matching` : 'Devices'}
          right={(
            <div className="controls-row__group">
              <input className="input" type="search" placeholder="Search device, employee, model…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search devices" />
              {health.length || conn.length || department || q ? <Button onClick={() => { setHealth([]); setConn([]); setDepartment(''); setQ('') }}>Clear filters</Button> : null}
            </div>
          )} />
        {error ? <p className="note" style={{ color: 'var(--critical)' }}>{error}</p> : null}
        <DataTable rowKey={(r) => r.device_id} rows={rows} columns={canAdmin(me) ? [...columns, {
          key: 'assign', header: '', width: 84, render: (r) => (
            <span role="presentation" onClick={(e) => e.stopPropagation()}>
              <Button onClick={() => setAssigning(r)}>Assign</Button>
            </span>
          ),
        }] : columns}
          onSelect={open} sort={sort}
          onSort={(key) => setSort((cur) => ({ key, order: cur.key === key && cur.order === 'asc' ? 'desc' : 'asc' }))}
          empty={page ? 'No devices match these filters.' : 'Loading devices…'} />
        {page && page.total > PAGE_SIZE ? (
          <div className="pager">
            <Button disabled={pageNo <= 1} onClick={() => setPageNo(pageNo - 1)}>Previous</Button>
            <p className="caption-mono">Page {pageNo} of {Math.ceil(page.total / PAGE_SIZE)}</p>
            <Button disabled={pageNo * PAGE_SIZE >= page.total} onClick={() => setPageNo(pageNo + 1)}>Next</Button>
          </div>
        ) : null}
        <p className="note note--muted">Server-side search, filtering, sorting and pagination ({PAGE_SIZE} per page). Rows update live from the fleet channel; open a device to see its digital twin.</p>
      </Panel>
      {assigning ? <AssignDialog row={assigning} onClose={(changed) => { setAssigning(null); if (changed) setReload((n) => n + 1) }} /> : null}
    </>
  )
}

function AssignDialog({ row, onClose }: { row: FleetRow; onClose: (changed: boolean) => void }) {
  const [username, setUsername] = useState(row.owner_username ?? '')
  const [name, setName] = useState(row.owner ?? '')
  const [error, setError] = useState<string | null>(null)
  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true" aria-label="Assign device">
      <form className="gate" onSubmit={async (e) => {
        e.preventDefault()
        try {
          await api.assignDevice(row.device_id, username.trim() || null, name.trim() || null)
          onClose(true)
        } catch (err) {
          setError(err instanceof ApiError ? err.message : String(err))
        }
      }}>
        <p className="eyebrow">ASSIGN DEVICE</p>
        <p className="panel-title">{row.hostname ?? row.model ?? row.device_id}</p>
        <p className="note">Employees (role “employee”) can only see devices assigned to their account. Leave the account empty to unassign.</p>
        <input placeholder="Employee account (username)" value={username} onChange={(e) => setUsername(e.target.value)} aria-label="Employee username" />
        <input placeholder="Display name" value={name} onChange={(e) => setName(e.target.value)} aria-label="Employee name" />
        {error ? <p className="note" style={{ color: 'var(--critical)' }}>{error}</p> : null}
        <div className="controls-row__group">
          <button type="submit" className="btn btn--primary">Save</button>
          <button type="button" className="btn" onClick={() => onClose(false)}>Cancel</button>
        </div>
      </form>
    </div>
  )
}
