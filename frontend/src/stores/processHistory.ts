import type { Point } from './seriesStore'
import { useTwinStore } from './twinStore'
import { RingBuffer } from '../utils/ringBuffer'

/**
 * Per-process history built from the agent's process snapshots (every ~3 s) while the app is open.
 * Bounded: 60 points per process, at most 200 processes tracked.
 */
const CAPACITY = 60
const MAX_PIDS = 200
const cpu = new Map<number, RingBuffer<Point>>()
const mem = new Map<number, RingBuffer<Point>>()
let lastTs = ''

function push(map: Map<number, RingBuffer<Point>>, pid: number, p: Point): void {
  let buf = map.get(pid)
  if (!buf) {
    if (map.size >= MAX_PIDS) return
    buf = new RingBuffer<Point>(CAPACITY)
    map.set(pid, buf)
  }
  buf.push(p)
}

useTwinStore.subscribe((s) => {
  const snap = s.processes
  if (!snap || snap.timestamp === lastTs) return
  lastTs = snap.timestamp
  const t = Date.parse(snap.timestamp)
  const seen = new Set<number>()
  for (const p of snap.processes) {
    seen.add(p.pid)
    if (p.cpu_percent !== null) push(cpu, p.pid, { t, v: p.cpu_percent })
    if (p.memory_rss_bytes !== null) push(mem, p.pid, { t, v: p.memory_rss_bytes })
  }
  // Forget processes that are no longer reported.
  for (const pid of [...cpu.keys()]) if (!seen.has(pid) && cpu.get(pid)!.last()!.t < t - 120_000) cpu.delete(pid)
  for (const pid of [...mem.keys()]) if (!seen.has(pid) && mem.get(pid)!.last()!.t < t - 120_000) mem.delete(pid)
})

export const processHistory = {
  cpu: (pid: number): Point[] => cpu.get(pid)?.toArray() ?? [],
  memory: (pid: number): Point[] => mem.get(pid)?.toArray() ?? [],
}
