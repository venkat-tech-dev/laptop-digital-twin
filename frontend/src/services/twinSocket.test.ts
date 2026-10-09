import { backoffDelay, TwinSocket } from './twinSocket'

class FakeWs {
  static instances: FakeWs[] = []
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onmessage: ((m: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  readonly url: string
  constructor(url: string) {
    this.url = url
    FakeWs.instances.push(this)
  }
  send(d: string) {
    this.sent.push(d)
  }
  close() {
    this.readyState = 3
    this.onclose?.()
  }
  open() {
    this.readyState = 1
    this.onopen?.()
  }
}

describe('backoffDelay', () => {
  it('grows exponentially and is capped', () => {
    expect(backoffDelay(1, 500, 15_000, () => 1)).toBe(500)
    expect(backoffDelay(4, 500, 15_000, () => 1)).toBe(4000)
    expect(backoffDelay(20, 500, 15_000, () => 1)).toBe(15_000)
    expect(backoffDelay(3, 500, 15_000, () => 0)).toBe(250)
  })
})

describe('TwinSocket', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWs.instances = []
    vi.stubGlobal('WebSocket', { OPEN: 1 })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('reconnects after a drop, detects receive timeout, and stops gracefully', () => {
    const states: string[] = []
    const events: unknown[] = []
    const sock = new TwinSocket({
      url: () => 'ws://x/ws/twin',
      onEvent: (e) => events.push(e),
      onState: (s) => states.push(s),
      socketFactory: (u) => new FakeWs(u) as unknown as WebSocket,
      random: () => 1,
      receiveTimeoutMs: 5000,
    })
    sock.start()
    FakeWs.instances[0].open()
    FakeWs.instances[0].onmessage?.({ data: JSON.stringify({ event: 'heartbeat' }) })
    expect(events).toHaveLength(1)

    FakeWs.instances[0].close() // server drop
    expect(states.at(-1)).toBe('closed')
    vi.advanceTimersByTime(1000)
    expect(FakeWs.instances).toHaveLength(2) // reconnected

    FakeWs.instances[1].open()
    vi.advanceTimersByTime(7000) // nothing received -> watchdog closes -> reconnect scheduled
    expect(FakeWs.instances[1].readyState).toBe(3)
    vi.advanceTimersByTime(1000)
    expect(FakeWs.instances.length).toBeGreaterThanOrEqual(3)

    sock.stop()
    const count = FakeWs.instances.length
    vi.advanceTimersByTime(60_000)
    expect(FakeWs.instances).toHaveLength(count) // no reconnect after graceful stop
  })

  it('re-subscribes after every reconnect, reports latency on pings and estimates clock offset', () => {
    vi.setSystemTime(new Date('2026-10-07T10:00:00Z'))
    const sock = new TwinSocket({
      url: () => 'ws://x/ws/twin',
      onEvent: () => undefined,
      onState: () => undefined,
      socketFactory: (u) => new FakeWs(u) as unknown as WebSocket,
      random: () => 1,
      pingIntervalMs: 1000,
      pingPayload: () => ({ latency: { end_to_end_latency_ms: [42] } }),
    })
    sock.start()
    const first = FakeWs.instances[0]
    first.open()
    sock.subscribe(['device:dev-a'])
    expect(JSON.parse(first.sent[0])).toEqual({ type: 'subscribe', topics: ['device:dev-a'] })

    vi.advanceTimersByTime(1000) // ping carries the measured latencies
    const ping = JSON.parse(first.sent.at(-1)!)
    expect(ping).toEqual({ type: 'ping', latency: { end_to_end_latency_ms: [42] } })
    vi.advanceTimersByTime(100) // pong 100 ms later; server clock is 5 s ahead
    first.onmessage?.({ data: JSON.stringify({ event: 'pong', server_time: '2026-10-07T10:00:06.050Z' }) })
    expect(sock.rttMs).toBe(100)
    expect(sock.clockOffsetMs).toBe(5000)

    first.close()
    vi.advanceTimersByTime(1000)
    const second = FakeWs.instances[1]
    second.open()
    second.onmessage?.({ data: JSON.stringify({ event: 'connection_status', status: 'connected' }) })
    expect(JSON.parse(second.sent[0])).toEqual({ type: 'subscribe', topics: ['device:dev-a'] })

    sock.subscribe(['device:dev-b'])
    expect(second.sent.slice(-2).map((m) => JSON.parse(m))).toEqual([
      { type: 'unsubscribe', topics: ['device:dev-a'] },
      { type: 'subscribe', topics: ['device:dev-b'] },
    ])
    sock.stop()
  })
})
