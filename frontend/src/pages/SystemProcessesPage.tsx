import { useMemo, useState } from 'react'

import { exportJson } from '../app/exportData'
import { gb, fmt0, rateMBps, summarize } from '../app/liveData'
import { useNow } from '../hooks/useNow'
import { processHistory } from '../stores/processHistory'
import { useTwinStore } from '../stores/twinStore'
import type { ProcessInfo, ProcessSnapshot } from '../types/telemetry'
import { formatBytes } from '../utils/format'
import { num, readingOf } from '../utils/twin'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, DataTable, Icon, MetricCard, Panel, PanelHeading, Spec, Tabs } from '../ui/primitives'
import { TimeSeries } from '../ui/TimeSeries'

type Sort = 'cpu' | 'memory' | 'disk' | 'network'
const sockets = (p: ProcessInfo) => (p.tcp_established ?? 0) + (p.tcp_listening ?? 0) + (p.udp_endpoints ?? 0)
const SORTERS: Record<Sort, (p: ProcessInfo) => number> = {
  cpu: (p) => p.cpu_percent ?? -1,
  memory: (p) => p.memory_rss_bytes ?? -1,
  disk: (p) => (p.io_read_bytes_per_sec ?? 0) + (p.io_write_bytes_per_sec ?? 0),
  network: (p) => (p.tcp_established ?? -1) * 1000 + sockets(p),
}

function since(iso: string | null | undefined, now: number): string {
  if (!iso) return 'Unavailable'
  const s = Math.max(0, (now - Date.parse(iso)) / 1000)
  const ago = s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} d`
  return `${new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })} UTC · ${ago} ago`
}

export function SystemProcessesPage() {
  const liveSnap = useTwinStore((s) => s.processes)
  const components = useTwinStore((s) => s.components)
  const [paused, setPaused] = useState<ProcessSnapshot | null>(null)
  const [sort, setSort] = useState<Sort>('cpu')
  const [query, setQuery] = useState('')
  const [selectedPid, setSelectedPid] = useState<number | null>(null)
  const now = useNow(3000)
  const snap = paused ?? liveSnap
  const s = summarize(components)

  const all = useMemo(() => snap?.processes ?? [], [snap])
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = all.filter((p) => !q || p.name.toLowerCase().includes(q) || String(p.pid).includes(q))
    const key = SORTERS[sort]
    return [...filtered].sort((a, b) => key(b) - key(a)).slice(0, 11)
  }, [all, query, sort])
  const selected = all.find((p) => p.pid === selectedPid) ?? rows[0] ?? null

  const listedCpu = rows.reduce((a, p) => a + (p.cpu_percent ?? 0), 0)
  const listedMem = rows.reduce((a, p) => a + (p.memory_rss_bytes ?? 0), 0)
  const otherCpu = s.cpuUsage !== null ? Math.max(0, s.cpuUsage - listedCpu) : null
  const otherMem = s.memUsed !== null ? Math.max(0, s.memUsed - listedMem) : null
  const threads = num(readingOf(components.os, 'system.thread_count'))
  const handles = all.some((p) => p.handle_count != null) ? all.reduce((a, p) => a + (p.handle_count ?? 0), 0) : null
  const detailsOn = snap?.details_collected ?? false
  const cpuHist = selected ? processHistory.cpu(selected.pid) : []
  const memHist = selected ? processHistory.memory(selected.pid).map((p) => ({ t: p.t, v: p.v / 1024 ** 3 })) : []
  const largestMem = [...all].sort((a, b) => (b.memory_rss_bytes ?? 0) - (a.memory_rss_bytes ?? 0))[0]
  const ioRate = (p: ProcessInfo) => (p.io_read_bytes_per_sec === null ? '—' : `${(((p.io_read_bytes_per_sec ?? 0) + (p.io_write_bytes_per_sec ?? 0)) / 1e6).toFixed(1)} MB/s`)
  const windowStart = now - 120_000

  return (
    <>
      <PageHeading title="System Processes" subtitle={`Real-time process attribution / ${snap?.source.replace(/\s*\(.*\)/, '') ?? 'Windows'} / Updated every ~3 seconds`}
        action={<Button icon="arrowRight" onClick={() => exportJson('process-list', snap)}>Export process list</Button>} />

      <div className="metric-row">
        <MetricCard label="TOTAL CPU" value={fmt0(s.cpuUsage)} unit="%" caption={`${Object.keys(components.cpu?.telemetry ?? {}).filter((k) => k.startsWith('cpu.core_usage_percent')).length} logical processors`} />
        <MetricCard label="PHYSICAL MEMORY" value={gb(s.memUsed)} unit="GB" caption={`${fmt0(s.memPct) ?? '—'}% of ${s.memTotal ? (s.memTotal / 1024 ** 3).toFixed(0) : '—'} GB installed`} />
        <MetricCard label="DISK I/O" value={s.diskReadBps !== null && s.diskWriteBps !== null ? ((s.diskReadBps + s.diskWriteBps) / 1e6).toFixed(1) : null} unit="MB/s"
          caption={`${rateMBps(s.diskReadBps) ?? '—'} read / ${rateMBps(s.diskWriteBps) ?? '—'} write`} />
        <MetricCard label="RUNNING PROCESSES" value={snap ? String(snap.total_processes) : null}
          caption={`${threads !== null ? threads.toLocaleString() : '—'} threads / ${handles !== null ? `${handles.toLocaleString()} handles (listed)` : 'handles —'}`} />
      </div>

      <div className="split">
        <Panel style={{ flex: '868 0 0' }}>
          <PanelHeading title="Top resource consumers" right={<Chip tone={paused ? 'muted' : 'accent'}>{paused ? 'PAUSED' : 'LIVE · REAL'}</Chip>} />
          <div className="process-filters">
            <label className="search">
              <Icon name="search" size={14} style={{ color: 'var(--muted)' }} />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search name or PID" aria-label="Search processes" />
            </label>
            <Button icon="pause" active={paused !== null} onClick={() => setPaused(paused ? null : liveSnap)}>{paused ? 'Resume' : 'Pause'}</Button>
          </div>
          <Tabs<Sort> tabs={[{ id: 'cpu', label: sort === 'cpu' ? 'CPU ↓' : 'CPU' }, { id: 'memory', label: sort === 'memory' ? 'Memory ↓' : 'Memory' }, { id: 'disk', label: sort === 'disk' ? 'Disk ↓' : 'Disk' }, { id: 'network', label: sort === 'network' ? 'Network ↓' : 'Network' }]}
            value={sort} onChange={setSort} />
          {sort === 'network' ? (
            <p className="note" style={{ color: 'var(--text-2)' }}>Ranked by open connections (TCP established / listening, UDP endpoints) from the Windows IP Helper tables. Bytes per process are not available: {snap?.unavailable_fields.network ?? 'requires ETW kernel tracing (administrator)'}.</p>
          ) : null}
          <DataTable rowKey={(p) => p.pid} rows={rows} selected={selected?.pid ?? null} onSelect={(p) => setSelectedPid(p.pid)} empty={snap ? 'No matching process' : 'Waiting for the agent\'s process snapshot'}
            columns={[
              { key: 'n', header: 'PROCESS', width: 240, kind: 'primary', render: (p) => p.name },
              { key: 'p', header: 'PID', width: 74, render: (p) => p.pid },
              { key: 'c', header: sort === 'cpu' || sort === 'network' ? 'CPU ↓' : 'CPU', width: 82, render: (p) => (p.cpu_percent === null ? '—' : `${p.cpu_percent.toFixed(1)}%`) },
              { key: 'm', header: sort === 'memory' ? 'MEMORY ↓' : 'MEMORY', width: 118, render: (p) => formatBytes(p.memory_rss_bytes) },
              sort === 'network'
                ? { key: 'd', header: 'TCP / UDP ↓', width: 122, render: (p: ProcessInfo) => (p.tcp_established == null ? '—' : `${p.tcp_established}+${p.tcp_listening ?? 0}L / ${p.udp_endpoints ?? 0}`) }
                : { key: 'd', header: sort === 'disk' ? 'DISK I/O ↓' : 'DISK I/O', width: 122, render: ioRate },
              { key: 's', header: 'STATE', grow: true, render: (p) => p.status.charAt(0).toUpperCase() + p.status.slice(1) },
            ]} />
          <div className="table-foot">
            <p className="eyebrow">{rows.length} OF {snap?.total_processes ?? 0} PROCESSES / TOP {sort === 'network' ? 'CONNECTIONS' : sort.toUpperCase()}</p>
            <p className="note" style={{ fontSize: 10 }}>Other processes: {otherCpu !== null ? `${otherCpu.toFixed(1)}%` : '—'} CPU / {otherMem !== null ? formatBytes(otherMem) : '—'} memory</p>
          </div>
        </Panel>

        <div className="side-stack">
          <Panel>
            <PanelHeading eyebrow={`SELECTED PROCESS / PID ${selected?.pid ?? '—'}`} title={selected?.name ?? 'No process selected'}
              right={selected ? <Chip tone={selected.status === 'running' ? 'accent' : 'muted'}>{selected.status.toUpperCase()}</Chip> : null} />
            <div>
              <Spec label="Publisher" value={selected?.publisher ?? (detailsOn ? 'Not available' : 'Not collected')} tone={selected?.publisher ? 'default' : 'na'}
                title={detailsOn ? 'No version resource or access denied (protected process)' : 'Opt-in: Settings → Privacy → Collect process details'} />
              <Spec label="User" value={selected?.user ?? (detailsOn ? 'Not available' : 'Not collected')} tone={selected?.user ? 'default' : 'na'}
                title={detailsOn ? 'Access denied (other session or protected process)' : 'Opt-in: Settings → Privacy → Collect process details'} />
              <Spec label="Started" value={since(selected?.started_at, now)} tone={selected?.started_at ? 'default' : 'na'} />
              <Spec label="Threads / handles" value={`${selected?.num_threads ?? '—'} / ${selected?.handle_count ?? '—'}`} />
              <Spec label="Connections" value={selected?.tcp_established != null ? `${selected.tcp_established} TCP · ${selected.tcp_listening ?? 0} listening · ${selected.udp_endpoints ?? 0} UDP` : '—'} />
              <Spec label="GPU" value={selected?.gpu_percent != null ? `${selected.gpu_percent.toFixed(1)}%` : '—'} />
            </div>
            {selected?.path ? <p className="caption-mono" style={{ wordBreak: 'break-all' }} title="Account folder name redacted">{selected.path}</p> : null}
            <p className="note--muted note">{detailsOn ? 'Path (account name redacted), user and publisher collected by opt-in.' : 'Path, user and publisher are opt-in (Settings → Privacy).'} Command lines are never collected. Read-only: processes are never terminated.</p>
          </Panel>
          <Panel>
            <PanelHeading title="CPU contribution" right={<p className="panel-value" style={{ fontSize: 17 }}>{selected?.cpu_percent != null ? `${selected.cpu_percent.toFixed(1)}%` : '—'}</p>} />
            <TimeSeries series={[{ points: cpuHist }]} start={windowStart} end={now} height={68} min={0} axis={['−120s', '−60s', 'now']} maxGapMs={10_000} emptyText="HISTORY BUILDS WHILE THIS PAGE IS OPEN" />
            <p className="note" style={{ fontSize: 10 }}>{selected?.cpu_percent != null && s.cpuUsage ? `${((selected.cpu_percent / s.cpuUsage) * 100).toFixed(1)}% of current total CPU activity` : '—'}</p>
          </Panel>
          <Panel>
            <PanelHeading title="Working set" right={<p className="panel-value" style={{ fontSize: 17 }}>{selected ? formatBytes(selected.memory_rss_bytes) : '—'}</p>} />
            <TimeSeries series={[{ points: memHist }]} start={windowStart} end={now} height={55} axis={['−120s', '−60s', 'now']} maxGapMs={10_000} emptyText="HISTORY BUILDS WHILE THIS PAGE IS OPEN" />
            <p className="note" style={{ fontSize: 10 }}>
              {selected && largestMem?.pid === selected.pid ? 'Largest memory consumer / ' : ''}
              {selected?.memory_percent != null ? `${selected.memory_percent.toFixed(1)}% of installed RAM` : '—'}
            </p>
          </Panel>
        </div>
      </div>
    </>
  )
}
