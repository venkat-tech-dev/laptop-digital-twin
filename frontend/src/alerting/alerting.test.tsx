import { act, fireEvent, render, screen } from '@testing-library/react'

import { showBrowserNotification } from '../services/browserNotify'
import { useNotifications } from '../stores/notificationStore'
import type { NotificationRecord } from '../types/alerting'
import { fmtDuration, timeAgo } from './alertingFormat'
import { SeverityBadge } from './alertingUi'
import { NotificationBell } from './NotificationBell'

vi.mock('../services/api', () => ({
  api: { readAllNotifications: vi.fn(async () => ({ marked: 0 })), notifications: vi.fn(), readNotification: vi.fn() },
  ApiError: class extends Error {},
}))

const note = (id: string, over: Partial<NotificationRecord> = {}): NotificationRecord => ({
  notification_id: id, alert_id: 'a1', user_id: 'local', device_id: 'dev', channel: 'in_app', status: 'DELIVERED',
  priority: 2, severity: 'HIGH', category: 'anomaly', title: `Alert ${id}`, body: 'b', payload: {},
  delivered_at: new Date().toISOString(), read_at: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  ...over,
})

beforeEach(() => useNotifications.setState({ unread: 0, bySeverity: {}, recent: [], synced: true, error: null, revision: 0 }))

describe('notification store', () => {
  it('de-duplicates by id and keeps the unread count consistent', () => {
    const s = useNotifications.getState()
    s.upsert(note('n1'))
    s.upsert(note('n1')) // the same message again (reconnect / duplicate delivery)
    s.upsert(note('n2', { severity: 'CRITICAL' }))
    expect(useNotifications.getState().unread).toBe(2)
    expect(useNotifications.getState().bySeverity).toEqual({ HIGH: 1, CRITICAL: 1 })
    s.upsert(note('n1', { status: 'READ', read_at: new Date().toISOString() }))
    expect(useNotifications.getState().unread).toBe(1)
    s.upsert(note('x', { channel: 'browser' })) // only the in-app inbox is counted
    expect(useNotifications.getState().recent.map((n) => n.notification_id)).toEqual(expect.arrayContaining(['n1', 'n2']))
    expect(useNotifications.getState().recent).toHaveLength(2)
  })
})

describe('notification ui', () => {
  it('bell shows the server count and an honest empty state', () => {
    render(<NotificationBell />)
    fireEvent.click(screen.getByRole('button', { name: '0 unread notifications' }))
    expect(screen.getByText(/No notifications yet/)).toBeTruthy()
    act(() => useNotifications.getState().upsert(note('n1', { severity: 'CRITICAL', title: 'Storage capacity risk' })))
    expect(screen.getByRole('button', { name: '1 unread notifications' })).toBeTruthy()
    expect(screen.getByText('Storage capacity risk')).toBeTruthy()
  })

  it('severity is shown with a glyph and a word, not colour only', () => {
    render(<SeverityBadge severity="CRITICAL" />)
    expect(screen.getByText(/CRITICAL/).textContent).toContain('■')
  })

  it('formats times and durations', () => {
    expect(timeAgo(new Date(Date.now() - 5 * 60_000).toISOString())).toBe('5 min ago')
    expect(fmtDuration(11 * 86400)).toBe('11 days')
  })

  it('browser notifications respect the permission', () => {
    expect(showBrowserNotification('n1', 't', 'b', () => undefined)).toBe(false) // jsdom: no permission
  })
})
