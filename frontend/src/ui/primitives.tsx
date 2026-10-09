import type { ButtonHTMLAttributes, CSSProperties, ReactNode } from 'react'

import { icons, type IconName } from './icons'

export function Icon({ name, size = 16, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  const url = `url("${icons[name]}")`
  return (
    <span className="icon" aria-hidden="true"
      style={{ width: size, height: size, WebkitMaskImage: url, maskImage: url, ...style }} />
  )
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon?: IconName
  primary?: boolean
  active?: boolean
}

export function Button({ icon, primary, active, className, children, ...rest }: ButtonProps) {
  return (
    <button type="button" className={`btn ${primary ? 'btn--primary' : ''} ${active ? 'is-active' : ''} ${className ?? ''}`}
      {...rest}>
      {icon ? <Icon name={icon} /> : null}
      {children}
    </button>
  )
}

export type ChipTone = 'accent' | 'amber' | 'critical' | 'muted'

export function Chip({ children, tone = 'accent', title }: { children: ReactNode; tone?: ChipTone; title?: string }) {
  return (
    <span className={`chip ${tone !== 'accent' ? `chip--${tone}` : ''}`} title={title}>
      <span className="chip__dot" />
      {children}
    </span>
  )
}

export function PanelHeading({ eyebrow, title, right }: { eyebrow?: ReactNode; title: ReactNode; right?: ReactNode }) {
  return (
    <div className="panel-heading">
      <div className="panel-heading__identity">
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <p className="panel-title">{title}</p>
      </div>
      {right}
    </div>
  )
}

export function Panel({ children, className, style }: { children: ReactNode; className?: string; style?: CSSProperties }) {
  return (
    <section className={`panel ${className ?? ''}`} style={style}>
      {children}
    </section>
  )
}

export type SpecTone = 'default' | 'accent' | 'amber' | 'na'

export function Spec({ label, value, tone = 'default', title }: { label: ReactNode; value: ReactNode; tone?: SpecTone; title?: string }) {
  return (
    <div className="spec">
      <p className="spec__label">{label}</p>
      <p className={`spec__value ${tone !== 'default' ? `spec__value--${tone}` : ''}`} title={title}>{value}</p>
    </div>
  )
}

export interface MetricCardProps {
  label: string
  value: string | null
  unit?: string
  caption?: ReactNode
  tone?: 'default' | 'amber'
  title?: string
}

/** Metric tile. `value === null` renders "Unavailable" — never a placeholder number. */
export function MetricCard({ label, value, unit, caption, tone = 'default', title }: MetricCardProps) {
  const na = value === null
  return (
    <div className="metric" title={title}>
      <p className="metric__label">{label}</p>
      <div className={`reading ${tone === 'amber' && !na ? 'reading--amber' : ''} ${na ? 'reading--na' : ''}`}>
        <p className="reading__value">{na ? 'UNAVAILABLE' : value}</p>
        {!na && unit ? <p className="reading__unit">{unit}</p> : null}
      </div>
      {caption !== undefined ? (
        <p className={`metric__caption ${tone === 'amber' && !na ? 'metric__caption--amber' : ''}`}>{caption}</p>
      ) : null}
    </div>
  )
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string }[]; value: T; onChange: (id: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" aria-selected={t.id === value} className={`tab ${t.id === value ? 'is-active' : ''}`}
          onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </div>
  )
}

export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return <button type="button" role="switch" aria-checked={checked} aria-label={label} className="switch" disabled={disabled} onClick={() => onChange(!checked)} />
}

export function Preference({ title, description, checked, onChange, disabled }: { title: string; description: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <div className="preference">
      <div className="preference__text">
        <p className="preference__title">{title}</p>
        <p className="preference__desc">{description}</p>
      </div>
      <Switch checked={checked} onChange={onChange} label={title} disabled={disabled} />
    </div>
  )
}

export interface Column<R> {
  key: string
  header: string
  width?: number
  grow?: boolean
  kind?: 'primary' | 'mono' | 'accent' | 'amber'
  render: (row: R) => ReactNode
  cellKind?: (row: R) => 'primary' | 'mono' | 'accent' | 'amber'
  /** Server-side sort key; makes the header a sort button when ``onSort`` is given. */
  sortKey?: string
}

export function DataTable<R>({ columns, rows, rowKey, selected, onSelect, empty, sort, onSort }: {
  columns: Column<R>[]
  rows: R[]
  rowKey: (r: R) => string | number
  selected?: string | number | null
  onSelect?: (r: R) => void
  empty?: ReactNode
  sort?: { key: string; order: 'asc' | 'desc' }
  onSort?: (key: string) => void
}) {
  const style = (c: Column<R>): CSSProperties => (c.grow ? {} : { width: c.width })
  return (
    <div className="table" role="table">
      <div className="table__head" role="row">
        {columns.map((c) =>
          c.sortKey && onSort ? (
            <button key={c.key} type="button" role="columnheader" style={style(c)}
              aria-sort={sort?.key === c.sortKey ? (sort.order === 'asc' ? 'ascending' : 'descending') : 'none'}
              className={`table__cell table__sort ${c.grow ? 'table__cell--grow' : ''} ${sort?.key === c.sortKey ? 'is-sorted' : ''}`}
              onClick={() => onSort(c.sortKey as string)}>
              {c.header}{sort?.key === c.sortKey ? (sort.order === 'asc' ? ' ↑' : ' ↓') : ''}
            </button>
          ) : (
            <p key={c.key} role="columnheader" className={`table__cell ${c.grow ? 'table__cell--grow' : ''}`} style={style(c)}>{c.header}</p>
          ),
        )}
      </div>
      {rows.length === 0 && empty ? <div className="table__row"><p className="na">{empty}</p></div> : null}
      {rows.map((r) => {
        const k = rowKey(r)
        return (
          <div key={k} role="row" className={`table__row ${onSelect ? 'table__row--selectable' : ''} ${selected === k ? 'is-selected' : ''}`}
            onClick={onSelect ? () => onSelect(r) : undefined}>
            {columns.map((c) => (
              <p key={c.key} role="cell" style={style(c)}
                className={`table__cell ${c.grow ? 'table__cell--grow' : ''} table__cell--${c.cellKind ? c.cellKind(r) : c.kind ?? 'mono'}`}>
                {c.render(r)}
              </p>
            ))}
          </div>
        )
      })}
    </div>
  )
}

export function Track({ percent, tone = 'accent' }: { percent: number; tone?: 'accent' | 'amber' }) {
  return (
    <div className="track">
      <div className={`track__fill ${tone === 'amber' ? 'track__fill--amber' : ''}`} style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
    </div>
  )
}
