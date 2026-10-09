import type { ServerEvent } from '../types/telemetry'

export interface SocketOptions {
  url: () => string
  onEvent: (event: ServerEvent) => void
  onState: (state: 'connecting' | 'open' | 'closed', info?: { attempt: number; retryInMs?: number }) => void
  /** Close and reconnect if nothing is received for this long (server heartbeats every 5 s). */
  receiveTimeoutMs?: number
  pingIntervalMs?: number
  minBackoffMs?: number
  maxBackoffMs?: number
  socketFactory?: (url: string) => WebSocket
  random?: () => number
  /** Return false to stay disconnected after a drop (user preference). */
  shouldReconnect?: () => boolean
  /** Extra fields for each ping (the browser reports its measured latencies to the backend). */
  pingPayload?: () => Record<string, unknown>
}

/** Exponential backoff with full jitter, capped. */
export function backoffDelay(attempt: number, minMs: number, maxMs: number, random: () => number = Math.random): number {
  const ceiling = Math.min(maxMs, minMs * 2 ** Math.max(0, attempt - 1))
  return Math.round(minMs / 2 + random() * (ceiling - minMs / 2))
}

/**
 * Resilient WebSocket client: reconnect with backoff, client pings, receive timeout
 * (detects half-open connections), and graceful disconnect.
 */
export class TwinSocket {
  private ws: WebSocket | null = null
  private attempt = 0
  private stopped = true
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private watchdog: ReturnType<typeof setInterval> | null = null
  private lastReceived = 0
  private pingSentAt = 0
  private topics: string[] = []
  /** Server clock minus browser clock (ms), estimated from ping/pong round trips. */
  clockOffsetMs = 0
  /** Last measured WebSocket round-trip time (ms). */
  rttMs: number | null = null
  private readonly opts: Required<Omit<SocketOptions, 'socketFactory' | 'shouldReconnect' | 'pingPayload'>> &
    Pick<SocketOptions, 'socketFactory' | 'shouldReconnect' | 'pingPayload'>

  constructor(options: SocketOptions) {
    this.opts = {
      receiveTimeoutMs: 15_000,
      pingIntervalMs: 10_000,
      minBackoffMs: 500,
      maxBackoffMs: 15_000,
      random: Math.random,
      ...options,
    }
  }

  start(): void {
    if (!this.stopped) return
    this.stopped = false
    this.connect()
  }

  /** Graceful disconnect: no reconnect afterwards. */
  stop(): void {
    this.stopped = true
    this.clearTimers()
    if (this.ws) {
      this.ws.onclose = null
      this.ws.close(1000, 'client closing')
      this.ws = null
    }
    this.opts.onState('closed', { attempt: this.attempt })
  }

  send(message: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message))
  }

  resync(): void {
    this.send({ type: 'resync' })
  }

  /**
   * Subscribe to topics (e.g. ``device:<id>``). Remembered: after every reconnect the socket
   * re-subscribes as soon as the server confirms the connection, and the server answers with fresh
   * snapshots - so a reconnect never leaves the view on stale state.
   */
  subscribe(topics: string[]): void {
    const fresh = topics.filter((t) => !this.topics.includes(t))
    const gone = this.topics.filter((t) => !topics.includes(t))
    this.topics = [...topics]
    if (gone.length) this.send({ type: 'unsubscribe', topics: gone })
    if (fresh.length) this.send({ type: 'subscribe', topics: fresh })
  }

  private ping(): void {
    this.pingSentAt = Date.now()
    this.send({ type: 'ping', ...(this.opts.pingPayload?.() ?? {}) })
  }

  private connect(): void {
    this.clearTimers()
    this.attempt += 1
    this.opts.onState('connecting', { attempt: this.attempt })
    const url = this.opts.url()
    const ws = this.opts.socketFactory ? this.opts.socketFactory(url) : new WebSocket(url)
    this.ws = ws
    ws.onopen = () => {
      this.attempt = 0
      this.lastReceived = Date.now()
      this.opts.onState('open', { attempt: 0 })
      this.pingTimer = setInterval(() => this.ping(), this.opts.pingIntervalMs)
      this.watchdog = setInterval(() => {
        if (Date.now() - this.lastReceived > this.opts.receiveTimeoutMs) {
          // Half-open connection (e.g. server frozen): the close handshake may never complete,
          // so treat the timeout itself as the disconnect and reconnect immediately.
          const handler = ws.onclose
          ws.onclose = null
          ws.close(4000, 'receive timeout')
          handler?.call(ws, new CloseEvent('close', { code: 4000 }))
        }
      }, 1000)
    }
    ws.onmessage = (msg: MessageEvent<string>) => {
      this.lastReceived = Date.now()
      try {
        const event = JSON.parse(msg.data) as ServerEvent
        if (event.event === 'connection_status' && this.topics.length) {
          this.send({ type: 'subscribe', topics: this.topics }) // resubscribe after (re)connect
        } else if (event.event === 'pong' && this.pingSentAt && 'server_time' in event && event.server_time) {
          const now = Date.now()
          this.rttMs = now - this.pingSentAt
          this.clockOffsetMs = Date.parse(event.server_time) - (this.pingSentAt + now) / 2
        }
        this.opts.onEvent(event)
      } catch {
        /* ignore malformed frame */
      }
    }
    ws.onerror = () => {
      /* onclose follows and handles reconnect */
    }
    ws.onclose = () => {
      this.clearTimers()
      this.ws = null
      if (this.stopped) return
      if (this.opts.shouldReconnect && !this.opts.shouldReconnect()) {
        this.opts.onState('closed', { attempt: this.attempt })
        return
      }
      const delay = backoffDelay(this.attempt + 1, this.opts.minBackoffMs, this.opts.maxBackoffMs, this.opts.random)
      this.opts.onState('closed', { attempt: this.attempt, retryInMs: delay })
      this.reconnectTimer = setTimeout(() => this.connect(), delay)
    }
  }

  private clearTimers(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    if (this.pingTimer) clearInterval(this.pingTimer)
    if (this.watchdog) clearInterval(this.watchdog)
    this.reconnectTimer = this.pingTimer = this.watchdog = null
  }
}
