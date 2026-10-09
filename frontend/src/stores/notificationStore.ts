import { create } from 'zustand'

import type { NotificationRecord } from '../types/alerting'

/**
 * The signed-in user's notification state. The server is the source of truth: on connect / reconnect
 * the client resynchronises over REST (nothing is lost while disconnected); notification.* WebSocket
 * messages only keep it current in between. Ids de-duplicate, ``created_at`` orders.
 */
interface NotificationState {
  unread: number
  bySeverity: Record<string, number>
  recent: NotificationRecord[]
  synced: boolean
  error: string | null
  revision: number // bumps on alert.* / notification.* so open views refetch
  setPage: (items: NotificationRecord[], unread: number, bySeverity: Record<string, number>) => void
  setError: (e: string | null) => void
  upsert: (n: NotificationRecord) => void
  bump: () => void
}

const MAX = 50

export const useNotifications = create<NotificationState>((set, get) => ({
  unread: 0,
  bySeverity: {},
  recent: [],
  synced: false,
  error: null,
  revision: 0,
  setPage: (items, unread, bySeverity) => set({ recent: items.slice(0, MAX), unread, bySeverity, synced: true, error: null }),
  setError: (error) => set({ error }),
  upsert: (n) => {
    if (n.channel !== 'in_app') return
    const s = get()
    const before = s.recent.find((x) => x.notification_id === n.notification_id)
    const recent = [n, ...s.recent.filter((x) => x.notification_id !== n.notification_id)]
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .slice(0, MAX)
    let unread = s.unread
    const bySeverity = { ...s.bySeverity }
    const wasUnread = before ? before.read_at === null && before.status === 'DELIVERED' : false
    const isUnread = n.read_at === null && n.status === 'DELIVERED'
    if (isUnread && !wasUnread) {
      unread += 1
      bySeverity[n.severity] = (bySeverity[n.severity] ?? 0) + 1
    } else if (!isUnread && wasUnread) {
      unread = Math.max(0, unread - 1)
      bySeverity[n.severity] = Math.max(0, (bySeverity[n.severity] ?? 1) - 1)
    }
    set({ recent, unread, bySeverity, revision: s.revision + 1 })
  },
  bump: () => set((s) => ({ revision: s.revision + 1 })),
}))
