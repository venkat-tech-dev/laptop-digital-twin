import { useEffect, useRef, useState } from 'react'

import { api } from '../services/api'
import { deviceScope } from '../services/deviceScope'
import type { FleetRow } from '../types/twinDoc'
import { CONNECTIVITY_LABEL, CONNECTIVITY_TONE } from '../twin/format'
import { Icon } from '../ui/primitives'
import { navigate } from './routes'

/** Device picker in the top bar: search the devices this account may see and follow one. */
export function DeviceSwitcher({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [rows, setRows] = useState<FleetRow[] | null>(null)
  const [total, setTotal] = useState(0)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const t = setTimeout(() => {
      api.devicesPage({ q: q.trim() || undefined, sort: 'device', page_size: 20 })
        .then((p) => {
          setRows(p.items)
          setTotal(p.total)
        })
        .catch(() => setRows([]))
    }, 200)
    return () => clearTimeout(t)
  }, [open, q])

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false)
    window.addEventListener('mousedown', close)
    return () => window.removeEventListener('mousedown', close)
  }, [open])

  return (
    <div className="device-switcher" ref={ref}>
      <button type="button" className="device-identity device-identity--button" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>
        {children}
      </button>
      {open ? (
        <div className="device-switcher__menu" role="listbox" aria-label="Devices">
          <input autoFocus className="input" type="search" placeholder="Search devices…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search devices" />
          {rows === null ? <p className="note">Loading…</p> : null}
          {rows?.map((r) => (
            <button key={r.device_id} type="button" role="option" aria-selected={deviceScope.get() === r.device_id} className="device-switcher__item"
              onClick={() => {
                deviceScope.set(r.device_id)
                setOpen(false)
              }}>
              <span className={`status-dot status-dot--${CONNECTIVITY_TONE[r.connectivity]}`} title={CONNECTIVITY_LABEL[r.connectivity]} />
              <span className="device-switcher__name">{r.hostname ?? r.model ?? r.device_id}</span>
              <span className="device-switcher__sub">{r.owner ?? 'Unassigned'} · {r.health}</span>
            </button>
          ))}
          {rows && total > rows.length ? <p className="note note--muted">{total - rows.length} more - refine the search</p> : null}
          <button type="button" className="btn" onClick={() => { setOpen(false); navigate('fleet') }}>
            <Icon name="laptop" /> All devices
          </button>
        </div>
      ) : null}
    </div>
  )
}
