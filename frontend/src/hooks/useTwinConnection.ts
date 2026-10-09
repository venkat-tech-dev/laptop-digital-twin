import { useEffect, useRef, useState } from 'react'

import { api, ApiError, wsUrl } from '../services/api'
import { auth } from '../services/auth'
import { connection } from '../services/connection'
import { deviceScope } from '../services/deviceScope'
import { TwinSocket } from '../services/twinSocket'
import { useFleet } from '../stores/fleetStore'
import { useAnomalies } from '../stores/anomalyStore'
import { useDiagnosisEvents } from '../stores/diagnosisStore'
import { useRemediationEvents } from '../stores/remediationStore'
import { usePredictionEvents } from '../stores/predictionStore'
import { useNotifications } from '../stores/notificationStore'
import { showBrowserNotification } from '../services/browserNotify'
import { navigate } from '../app/routes'
import type { NotificationRecord } from '../types/alerting'
import { setTwinResync, useTwinDoc, type TwinPatchMsg } from '../stores/twinDocStore'
import type { AnomalyRecord } from '../types/anomaly'
import { usePrefs } from '../stores/prefsStore'
import { useSession } from '../stores/sessionStore'
import { seriesStore } from '../stores/seriesStore'
import { drainLatencyReport, setClockOffsetSource, useTwinStore } from '../stores/twinStore'

/** Phase-3 twin messages (twin.snapshot / twin.state.patch / twin.event.created / twin.summary). */
function handleTwinMessage(ev: Record<string, unknown>): void {
  const kind = ev.event as string
  const doc = useTwinDoc.getState()
  if (kind === 'twin.snapshot') {
    const snap = ev.twin as Parameters<typeof doc.applySnapshot>[0] | null
    if (snap && snap.device_id === doc.deviceId) doc.applySnapshot(snap)
  } else if (kind === 'twin.state.patch') {
    doc.applyPatch(ev as unknown as TwinPatchMsg)
  } else if (kind === 'twin.event.created') {
    const e = ev.timeline_event as { event_id: string; type: string; severity: 'info' | 'warning' | 'error' | 'critical'; timestamp: string; message: string; data: Record<string, unknown> }
    if (ev.device_id === doc.deviceId) {
      doc.addEvent({ event_id: e.event_id, time: e.timestamp, type: e.type, kind: e.type.replace(/^twin\./, ''), severity: e.severity, message: e.message, data: e.data })
    }
  } else if (kind === 'anomaly.detected' || kind === 'anomaly.updated' || kind === 'anomaly.resolved') {
    const a = ev.anomaly as AnomalyRecord | undefined
    if (a && a.device_id === doc.deviceId) {
      useAnomalies.getState().reset(doc.deviceId)
      useAnomalies.getState().upsert(a)
    }
  } else if (kind.startsWith('notification.')) {
    const n = ev.notification as NotificationRecord | undefined
    if (n) {
      if (kind === 'notification.browser') {
        showBrowserNotification(n.notification_id, n.title, n.body, () => navigate('alerts', n.alert_id ?? undefined))
      } else {
        useNotifications.getState().upsert(n)
      }
    }
  } else if (kind.startsWith('alert.')) {
    useNotifications.getState().bump()
  } else if (kind.startsWith('remediation.')) {
    useRemediationEvents.getState().bump()
  } else if (kind.startsWith('diagnosis.')) {
    useDiagnosisEvents.getState().bump()
  } else if (kind.startsWith('prediction.')) {
    if (ev.device_id === doc.deviceId) usePredictionEvents.getState().bump()
  } else if (kind === 'twin.sync.required') {
    if (ev.device_id === doc.deviceId && doc.deviceId) {
      doc.setStatus('loading')
      connection.send({ type: 'twin.sync', device_id: doc.deviceId })
    }
  } else if (kind === 'twin.summary') {
    const row = ev.device as { device_id: string } | undefined
    if (row) useFleet.getState().upsert(row)
  }
}

/** REST resynchronisation of the inbox (initial load and after every reconnect). */
export async function syncNotifications(): Promise<void> {
  try {
    const page = await api.notifications({ limit: 30 })
    useNotifications.getState().setPage(page.items, page.unread, page.unread_by_severity)
  } catch (e) {
    useNotifications.getState().setError(e instanceof Error ? e.message : String(e))
  }
}

export type BootState = 'loading' | 'needs_key' | 'needs_login' | 'needs_setup' | 'needs_mfa_setup' | 'ready' | 'error'

/**
 * Boots the live twin: resolve auth mode, load device identity, backfill chart buffers,
 * then stream over the WebSocket. Reconnects automatically; device identity refreshes on reconnect.
 */
export function useTwinConnection(): {
  boot: BootState
  error: string | null
  setupTokenRequired: boolean
  submitKey: (key: string) => void
  signIn: (token: string, expiresAt: string) => void
  signOut: () => void
} {
  const [setupTokenRequired, setSetupTokenRequired] = useState(false)
  const [boot, setBoot] = useState<BootState>('loading')
  const [error, setError] = useState<string | null>(null)
  const [keyNonce, setKeyNonce] = useState(0)
  const socketRef = useRef<TwinSocket | null>(null)

  useEffect(() => {
    let cancelled = false
    const store = useTwinStore.getState()

    async function loadDevice(): Promise<void> {
      try {
        store.setDevice(await api.device())
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) {
          if (deviceScope.get()) {
            deviceScope.set(null) // remembered device no longer visible to this account: use the default
            return loadDevice()
          }
          store.setDevice(null)
        } else throw e
      }
    }

    /** REST snapshot first; the WebSocket subscription then delivers twin.snapshot + patches. */
    async function loadTwin(deviceId: string): Promise<void> {
      const doc = useTwinDoc.getState()
      if (doc.deviceId !== deviceId) doc.reset(deviceId)
      try {
        doc.applySnapshot(await api.deviceTwin(deviceId))
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) useTwinDoc.getState().setStatus('missing', e.message)
        else useTwinDoc.getState().setStatus('error', e instanceof Error ? e.message : String(e))
      }
      try {
        const t = await api.deviceTimeline(deviceId, 100)
        if (useTwinDoc.getState().deviceId === deviceId) useTwinDoc.getState().setEvents(t.items)
      } catch {
        /* timeline unavailable (database down): live events still arrive */
      }
    }

    /** Follow ``deviceId``: legacy component store, twin document and WebSocket topic. */
    async function follow(deviceId: string | null): Promise<void> {
      useTwinStore.getState().resetForDevice(deviceId)
      seriesStore.reset()
      await loadDevice()
      const id = useTwinStore.getState().device?.device_id ?? null
      useTwinStore.getState().setSubscribedDevice(id)
      connection.setDevice(id)
      if (id) {
        // same topic may already be subscribed: ask explicitly for fresh snapshots of this device
        connection.send({ type: 'resync', device_id: id })
        await loadTwin(id)
      }
      else useTwinDoc.getState().reset(null)
      try {
        seriesStore.backfill((await api.recent(600)).points)
      } catch {
        /* no device yet or no buffer: charts start empty */
      }
    }

    async function start(): Promise<void> {
      try {
        const cfg = await api.authConfig()
        auth.mode = cfg.mode
        if (cfg.mode === 'accounts') {
          auth.pickUpRedirectToken()
          if (!auth.jwt()) {
            setSetupTokenRequired(Boolean(cfg.setup_token_required))
            setBoot(cfg.setup_required ? 'needs_setup' : 'needs_login')
            return
          }
          // The organization requires MFA and none is enrolled: this session may only enroll an authenticator.
          const me = await api.me()
          useSession.getState().setMe(me)
          if (me.mfa_setup_required) {
            setBoot('needs_mfa_setup')
            return
          }
        } else if (cfg.mode !== 'none') {
          const key = auth.apiKey()
          if (!key) {
            setBoot('needs_key')
            return
          }
          if (cfg.mode === 'jwt' && !auth.jwt()) {
            const t = await api.token(key)
            auth.setJwt(t.access_token, t.expires_at)
          }
        }
        await loadDevice()
        try {
          const recent = await api.recent(600)
          seriesStore.backfill(recent.points)
        } catch {
          /* no device yet or no buffer: charts start empty */
        }
        if (cancelled) return
        try {
          const me = await api.me()
          useSession.getState().setMe(me)
          if (me.role !== 'employee') useSession.getState().setWorkspaces(await api.workspaces())
        } catch {
          /* identity endpoints unavailable: controls fall back to read-only */
        }
        const initialId = useTwinStore.getState().device?.device_id ?? null
        if (initialId) void loadTwin(initialId)
        setBoot('ready')
        const socket = new TwinSocket({
          url: wsUrl,
          onEvent: (ev) => {
            if (ev.event === 'connection_status') void syncNotifications()  // (re)connect: nothing missed
            if (ev.event === 'connection_status') useRemediationEvents.getState().bump()  // refetch remediation views
            if (ev.event === 'connection_status' && !useTwinStore.getState().subscribedDevice) {
              // Initial REST load had no device yet: follow the server's primary device.
              const id = useTwinStore.getState().device?.device_id ?? ev.primary_device_id
              if (id) {
                useTwinStore.getState().setSubscribedDevice(id)
                connection.setDevice(id)
                void loadTwin(id)
              }
            }
            handleTwinMessage(ev as unknown as Record<string, unknown>)
            useTwinStore.getState().applyEvent(ev)
            if (ev.event === 'device_status_changed' || (ev.event === 'twin_snapshot' && ev.twin && !useTwinStore.getState().device)) {
              void loadDevice().catch(() => undefined)
            }
          },
          onState: (state, info) => useTwinStore.getState().setLink(state, info?.attempt ?? 0, info?.retryInMs),
          shouldReconnect: () => usePrefs.getState().saved.reconnect,
          pingPayload: drainLatencyReport,
        })
        setClockOffsetSource(() => socket.clockOffsetMs)
        setTwinResync((deviceId) => {
          // Version gap / new epoch: ask the server for a snapshot; REST if it does not arrive soon.
          socket.send({ type: 'twin.sync', device_id: deviceId })
          setTimeout(() => {
            if (useTwinDoc.getState().deviceId === deviceId && useTwinDoc.getState().status === 'loading') void loadTwin(deviceId)
          }, 3000)
        })
        // REST first (state above), then a scoped subscription: the server answers with snapshots.
        const deviceId = useTwinStore.getState().device?.device_id
        if (deviceId) useTwinStore.getState().setSubscribedDevice(deviceId)
        socketRef.current = socket
        connection.attach(socket)
        connection.setDevice(deviceId ?? null)
        unsubscribeScope = deviceScope.subscribe((id) => void follow(id))
        socket.start()
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          auth.clear()
          setBoot(auth.mode === 'accounts' ? 'needs_login' : 'needs_key')
          return
        }
        setError(e instanceof Error ? e.message : String(e))
        setBoot('error')
        // Backend not reachable yet: retry the bootstrap.
        setTimeout(() => !cancelled && setKeyNonce((n) => n + 1), 3000)
      }
    }

    let unsubscribeScope: (() => void) | null = null
    void start()
    return () => {
      cancelled = true
      unsubscribeScope?.()
      connection.attach(null)
      socketRef.current?.stop()
      socketRef.current = null
    }
  }, [keyNonce])

  return {
    boot,
    error,
    setupTokenRequired,
    submitKey: (key: string) => {
      auth.setApiKey(key)
      setBoot('loading')
      setKeyNonce((n) => n + 1)
    },
    signIn: (token: string, expiresAt: string) => {
      auth.setJwt(token, expiresAt)
      setBoot('loading')
      setKeyNonce((n) => n + 1)
    },
    signOut: () => {
      if (auth.mode === 'accounts' && auth.jwt()) void api.logout().catch(() => undefined) // end the server session too
      auth.clear()
      useSession.getState().setMe(null)
      socketRef.current?.stop()
      setBoot('loading')
      setKeyNonce((n) => n + 1)
    },
  }
}
