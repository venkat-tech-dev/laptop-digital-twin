import type { TwinSocket } from './twinSocket'

/**
 * Topic management for the single WebSocket of this tab: the selected device's topic plus optional
 * extra topics (``fleet`` while the fleet view is open). TwinSocket re-subscribes to the whole set
 * after every reconnect.
 */
let socket: TwinSocket | null = null
let deviceTopic: string | null = null
const extra = new Map<string, number>() // topic -> reference count

function apply(): void {
  socket?.subscribe([...(deviceTopic ? [deviceTopic] : []), ...extra.keys()])
}

export const connection = {
  attach(s: TwinSocket | null): void {
    socket = s
    apply()
  },
  setDevice(deviceId: string | null): void {
    deviceTopic = deviceId ? `device:${deviceId}` : null
    apply()
  },
  /** Reference-counted extra topic; returns the release function. */
  useTopic(topic: string): () => void {
    extra.set(topic, (extra.get(topic) ?? 0) + 1)
    apply()
    return () => {
      const n = (extra.get(topic) ?? 1) - 1
      if (n <= 0) extra.delete(topic)
      else extra.set(topic, n)
      apply()
    }
  },
  send(message: Record<string, unknown>): void {
    socket?.send(message)
  },
}
