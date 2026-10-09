import { useEffect, useRef, useState } from 'react'

import { navigate } from '../app/routes'
import { api } from '../services/api'
import { useNotifications } from '../stores/notificationStore'
import { useTwinStore } from '../stores/twinStore'
import { Icon } from '../ui/primitives'
import { timeAgo } from './alertingFormat'
import { SeverityBadge } from './alertingUi'

/** Top-bar bell: unread in-app notifications (server count), the latest items, mark read / all. */
export function NotificationBell() {
  const unread = useNotifications((s) => s.unread)
  const bySeverity = useNotifications((s) => s.bySeverity)
  const recent = useNotifications((s) => s.recent)
  const synced = useNotifications((s) => s.synced)
  const error = useNotifications((s) => s.error)
  const link = useTwinStore((s) => s.link)
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  const top = bySeverity.CRITICAL ? 'critical' : bySeverity.HIGH ? 'high' : unread ? 'other' : null
  const markAll = async () => {
    await api.readAllNotifications().catch(() => undefined)
    const page = await api.notifications({ limit: 30 }).catch(() => null)
    if (page) useNotifications.getState().setPage(page.items, page.unread, page.unread_by_severity)
  }
  const openOne = async (id: string, alertId: string | null, read: boolean) => {
    if (!read) {
      const n = await api.readNotification(id).catch(() => null)
      if (n) useNotifications.getState().upsert(n)
    }
    setOpen(false)
    navigate('alerts', alertId ? `alert:${alertId}` : undefined)
  }

  return (
    <div className="notify" ref={ref}>
      <button type="button" className="bell" onClick={() => setOpen(!open)} aria-haspopup="dialog" aria-expanded={open}
        aria-label={`${unread} unread notifications`} title={`${unread} unread notifications`}>
        <Icon name="bell" size={18} style={{ color: 'var(--text-2)' }} />
        {unread > 0 ? <span className={`bell__badge bell__badge--${top}`}>{unread > 99 ? '99+' : unread}</span> : null}
      </button>
      {open ? (
        <div className="notify__panel" role="dialog" aria-label="Notifications">
          <div className="notify__head">
            <span className="notify__title">Notifications</span>
            <button type="button" className="link-btn" onClick={() => void markAll()} disabled={!unread}>Mark all as read</button>
          </div>
          {link !== 'open' ? <p className="note notify__state">Live updates paused (connection {link}); the list resynchronises on reconnect.</p> : null}
          {error ? <p className="note notify__state">Notifications unavailable: {error}</p> : null}
          {!synced && !error ? <p className="note notify__state">Loading…</p> : null}
          {synced && recent.length === 0 ? <p className="note notify__state">No notifications yet. Alerts that need your attention appear here.</p> : null}
          <ul className="notify__list">
            {recent.slice(0, 8).map((n) => (
              <li key={n.notification_id}>
                <button type="button" className={`notify__item ${n.read_at ? '' : 'is-unread'}`}
                  onClick={() => void openOne(n.notification_id, n.alert_id, Boolean(n.read_at))}>
                  <SeverityBadge severity={n.severity} />
                  <span className="notify__text">
                    <span className="notify__item-title">{n.title}</span>
                    <span className="notify__item-meta">{n.device_id ?? 'all devices'} · {timeAgo(n.created_at)}</span>
                  </span>
                  {n.read_at ? null : <span className="notify__dot" aria-label="unread" />}
                </button>
              </li>
            ))}
          </ul>
          <button type="button" className="btn notify__all" onClick={() => { setOpen(false); navigate('alerts') }}>View all notifications</button>
        </div>
      ) : null}
    </div>
  )
}
