import { useApi } from '../hooks/useApi'
import { useEndpoint } from '../hooks/useEndpoint'
import { useNow } from '../hooks/useNow'
import { api } from '../services/api'
import { canAdmin, useSession } from '../stores/sessionStore'
import { useTwinStore } from '../stores/twinStore'
import type { HealthState, LatencySummary, PresenceState } from '../types/admin'
import { formatBytes } from '../utils/format'
import { readingOf, readingsOf } from '../utils/twin'
import { Button, Chip, DataTable, Panel, PanelHeading, Spec, type ChipTone, type SpecTone } from '../ui/primitives'

const STATE_TONE: Record<HealthState, ChipTone> = { HEALTHY: 'accent', WARNING: 'amber', CRITICAL: 'critical', UNKNOWN: 'muted' }
const CHECK_LABEL: Record<string, string> = {
  antivirus: 'Antivirus protection',
  signatures: 'Antivirus signatures',
  firewall: 'Windows Firewall',
  secure_boot: 'Secure Boot',
  tpm: 'TPM',
  pending_reboot: 'Pending restart',
  disk: 'Drive health',
}

const ago = (iso: string | null | undefined) => {
  if (!iso) return '—'
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`
}
const utc = (iso: string | null | undefined) =>
  iso ? `${new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })} UTC` : '—'


/** AGENT_HEALTH: run mode, queue, sync and collector status as reported by the endpoint agent. */
export function AgentHealthPanel() {
  const { data, error, reload } = useEndpoint()
  const me = useSession((s) => s.me)
  const a = data?.agent_health
  const now = useNow(15_000)
  const failing = (a?.collectors ?? []).filter((c) => c.consecutive_failures > 0)
  const stale = a?.received_at ? now - Date.parse(a.received_at) > 5 * 60_000 : true
  return (
    <Panel>
      <PanelHeading eyebrow="ENDPOINT AGENT" title="Agent health"
        right={<Chip tone={!a ? 'muted' : stale ? 'amber' : failing.length ? 'amber' : 'accent'}>{!a ? 'NO REPORT' : stale ? 'REPORT STALE' : `${a.run_mode.toUpperCase()} · v${a.agent_version}`}</Chip>} />
      {error ? <p className="note">{error}</p> : null}
      <div>
        <Spec label="Run mode" value={a ? (a.run_mode === 'service' ? 'Windows service (independent of browser and logon)' : a.run_mode === 'console' ? 'Console (foreground process)' : a.run_mode) : '—'}
          tone={a?.run_mode === 'service' ? 'accent' : a ? 'amber' : 'na'} />
        <Spec label="Uptime" value={a ? `${(a.uptime_s / 3600).toFixed(1)} h (since ${utc(a.started_at)})` : '—'} />
        <Spec label="Last collection / last sync" value={a ? `${ago(a.last_collection_at)} / ${ago(a.last_sync_at)}` : '—'} />
        <Spec label="Local queue (SQLite)" value={a ? `${a.queue_depth} batches · ${formatBytes(a.queue_bytes)} · ${a.queue_dropped_total} dropped` : '—'}
          tone={a && a.queue_dropped_total > 0 ? 'amber' : 'default'} />
        <Spec label="Sync failures" value={a ? `${a.sync_failures_consecutive} consecutive / ${a.sync_failures_total} total` : '—'} tone={a?.sync_failures_consecutive ? 'amber' : 'default'} />
        <Spec label="Agent footprint" value={a?.cpu_percent != null ? `${a.cpu_percent.toFixed(2)}% CPU · ${formatBytes(a.memory_rss_bytes)}` : '—'} />
        <Spec label="Presence (heartbeat)" value={data?.presence ? `${data.presence.presence} · last contact ${ago(data.presence.last_contact_at)}` : 'No heartbeat yet'}
          tone={data?.presence ? PRESENCE_TONE_SPEC[data.presence.presence] : 'na'} title="ONLINE ≤ 90 s since last contact, STALE ≤ 5 min, then OFFLINE (PRESENCE_* settings)" />
        <Spec label="Sequence (received / missing)" value={data?.sequence ? `#${data.sequence.last_sequence ?? '—'} · ${data.sequence.missing} missing · ${data.sequence.out_of_order} replayed · ${data.sequence.duplicates} duplicate` : '—'}
          tone={data?.sequence?.missing ? 'amber' : 'default'} />
        <Spec label="Clock offset (server − device)" value={data?.sequence?.clock_drift_s != null ? `${data.sequence.clock_drift_s.toFixed(2)} s (includes transit)` : '—'}
          tone={data?.sequence?.clock_drift_s != null && Math.abs(data.sequence.clock_drift_s) > 120 ? 'amber' : 'default'} />
        <Spec label="Device credential" value={data?.credential ? (data.credential.revoked ? 'Revoked' : `Per-device token · used ${ago(data.credential.last_used_at)}`) : 'Shared enrollment key'}
          tone={data?.credential?.revoked ? 'amber' : data?.credential ? 'accent' : 'amber'} />
      </div>
      {a ? (
        <DataTable rowKey={(c) => c.name} rows={[...(a.collectors ?? [])].sort((x, y) => y.consecutive_failures - x.consecutive_failures).slice(0, 8)}
          columns={[
            { key: 'n', header: 'COLLECTOR', width: 130, kind: 'primary', render: (c) => c.name },
            { key: 'l', header: 'LANE', width: 70, render: (c) => c.lane },
            { key: 'i', header: 'EVERY', width: 80, render: (c) => (c.interval_ms >= 60000 ? `${Math.round(c.interval_ms / 60000)} min` : `${c.interval_ms / 1000} s`) },
            { key: 's', header: 'STATE', grow: true, render: (c) => (c.consecutive_failures ? `${c.consecutive_failures} failures: ${c.last_error ?? ''}` : `OK · ${ago(c.last_success_at)}`),
              cellKind: (c) => (c.consecutive_failures ? 'amber' : 'accent') },
          ]} />
      ) : null}
      {canAdmin(me) && data?.credential && !data.credential.revoked ? (
        <Button icon="rotateCcw" onClick={async () => { if (confirm('Revoke this device token? The agent re-enrolls with the enrollment key.')) { await api.revokeDevice(data.device_id); reload() } }}>
          Revoke device token
        </Button>
      ) : null}
    </Panel>
  )
}

/** DEVICE_HEALTH: normalised security/compliance posture with the measured facts behind it. */
export function PosturePanel() {
  const { data } = useEndpoint()
  const components = useTwinStore((s) => s.components)
  const h = data?.device_health
  const os = components.os
  const mb = components.motherboard
  const realtime = readingOf(mb, 'security.defender_realtime_enabled')
  const sigAge = readingOf(mb, 'security.defender_signature_age_days')
  const fw = readingsOf(mb, 'security.firewall_enabled')
  const pending = readingOf(os, 'system.updates_pending')
  const lastUpdate = readingOf(os, 'system.last_update_installed_at')
  const reboot = readingOf(os, 'system.update_reboot_required')
  const services = readingsOf(os, 'system.service_status')
  const stopped = services.filter((s) => s.availability === 'available' && s.value !== 'running')
  const v = (r: typeof realtime, f: (x: unknown) => string, tone?: (x: unknown) => SpecTone): { value: string; tone?: SpecTone; title?: string } =>
    r?.availability === 'available' ? { value: f(r.value), tone: tone?.(r.value) } : { value: 'Unavailable', tone: 'na', title: r?.reason ?? 'Not reported yet' }
  return (
    <Panel style={{ flex: '868 0 0' }}>
      <PanelHeading eyebrow="DEVICE POSTURE" title="Security & compliance" right={<Chip tone={h ? STATE_TONE[h.state] : 'muted'}>{h?.state ?? 'NOT EVALUATED'}</Chip>} />
      {h?.reasons.length ? <p className="note" style={{ color: 'var(--amber)' }}>{h.reasons.join(' · ')}</p> : <p className="note">{h ? 'All measured checks pass.' : 'Waiting for the endpoint agent.'}</p>}
      <div className="spec-columns">
        <div>
          {Object.entries(h?.checks ?? {}).map(([k, st]) => (
            <Spec key={k} label={CHECK_LABEL[k] ?? k} value={st === 'UNKNOWN' ? 'Not readable' : st.charAt(0) + st.slice(1).toLowerCase()}
              tone={st === 'HEALTHY' ? 'accent' : st === 'UNKNOWN' ? 'na' : 'amber'} />
          ))}
        </div>
        <div>
          <Spec label="Defender real-time protection" {...v(realtime, (x) => (x ? 'On' : 'Off'), (x) => (x ? 'accent' : 'amber'))} />
          <Spec label="Signature age" {...v(sigAge, (x) => `${x} day${x === 1 ? '' : 's'}`)} />
          <Spec label="Firewall profiles" value={fw.length ? fw.map((r) => `${r.labels.profile} ${r.value ? 'on' : 'OFF'}`).join(' · ') : 'Unavailable'} tone={fw.length ? (fw.every((r) => r.value) ? 'accent' : 'amber') : 'na'} />
          <Spec label="Pending updates" {...v(pending, (x) => `${x}`)} />
          <Spec label="Last update installed" {...v(lastUpdate, (x) => utc(String(x)))} />
          <Spec label="Restart required" {...v(reboot, (x) => (x ? 'Yes' : 'No'), (x) => (x ? 'amber' : 'default'))} />
          <Spec label="Monitored services" value={services.length ? (stopped.length ? `${stopped.map((s) => `${s.labels.service} ${s.value}`).join(', ')}` : `${services.length} running`) : 'Unavailable'}
            tone={services.length ? 'default' : 'na'} title="Allowlist (SERVICE_ALLOWLIST); stopped manual-start services are normal" />
        </div>
      </div>
      <p className="note note--muted">Evaluated on the endpoint from measured facts only {h ? `(${utc(h.evaluated_at)})` : ''}. Read-only: the agent never changes security settings.</p>
    </Panel>
  )
}

const PRESENCE_TONE: Record<PresenceState, ChipTone> = { ONLINE: 'accent', STALE: 'amber', OFFLINE: 'critical', UNKNOWN: 'muted' }
const PRESENCE_TONE_SPEC: Record<PresenceState, SpecTone> = { ONLINE: 'accent', STALE: 'amber', OFFLINE: 'amber', UNKNOWN: 'na' }

const STAGES: [string, string][] = [
  ['collection_to_server_ms', 'Collection → server'],
  ['server_processing_ms', 'Server processing'],
  ['twin_projection_ms', 'Twin projection'],
  ['ws_queue_ms', 'WebSocket queue'],
  ['websocket_delivery_ms', 'WebSocket delivery'],
  ['end_to_end_latency_ms', 'End to end'],
]
const ms = (v: number | null) => (v == null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(v < 10 ? 1 : 0)} ms`)

/** PIPELINE: ingest counters, latency per stage, sequences, persistence and WebSocket fan-out. */
export function PipelinePanel() {
  const stats = useApi(() => api.pipelineStats(), [], 15_000)
  const devices = useApi(() => api.devices(), [], 15_000)
  const p = stats.data
  const c = p?.ingest.counters
  return (
    <Panel>
      <PanelHeading eyebrow="NEAR-REAL-TIME PIPELINE" title="Telemetry pipeline"
        right={<Chip tone={!p ? 'muted' : p.persistence.last_error || p.ingest.receipts.last_error ? 'amber' : 'accent'}>{p ? `${p.persistence.mode.toUpperCase()}${p.persistence.timescaledb ? ' · TIMESCALEDB' : ''}` : 'LOADING'}</Chip>} />
      {stats.error ? <p className="note">{stats.error}</p> : null}
      {p && c ? (
        <>
          <div className="spec-columns">
            <div>
              <Spec label="Batches accepted / duplicate / rejected" value={`${c.accepted} / ${c.duplicates} / ${c.rejected}`} tone={c.rejected ? 'amber' : 'default'}
                title={Object.entries(p.ingest.rejections).map(([k, v]) => `${k}: ${v}`).join(', ') || 'No rejections'} />
              <Spec label="Samples / events ingested" value={`${c.samples.toLocaleString()} / ${c.events.toLocaleString()}`} />
              <Spec label="Rate limited (429)" value={`${c.rate_limited} · limit ${p.ingest.rate_limit.per_device_per_min}/min/device`} tone={c.rate_limited ? 'amber' : 'default'} />
              <Spec label="Persistence queue" value={`${p.persistence.queue_depth} samples · ${p.persistence.written_total.toLocaleString()} written`} tone={p.persistence.last_error ? 'amber' : 'default'} title={p.persistence.last_error ?? undefined} />
            </div>
            <div>
              <Spec label="Presence" value={(Object.entries(p.presence.summary) as [PresenceState, number][]).filter(([, n]) => n).map(([k, n]) => `${n} ${k.toLowerCase()}`).join(' · ') || 'No devices'} />
              <Spec label="WebSocket clients" value={`${p.websocket.clients} · ${p.websocket.sent_total.toLocaleString()} sent · ${p.websocket.slow_consumers_dropped_total} slow dropped`} />
              <Spec label="Retention" value={`raw ${p.retention.raw_days} d · 5-min aggregates ${p.retention.aggregate_days} d · events ${p.retention.event_days} d`}
                title={p.persistence.aggregate_5m ? 'Continuous aggregate telemetry_samples_5m active' : 'Aggregates computed on the fly'} />
              <Spec label="Clock drift warnings" value={p.clock_drift_warnings.length ? p.clock_drift_warnings.join(', ') : 'None'} tone={p.clock_drift_warnings.length ? 'amber' : 'default'} />
            </div>
          </div>
          <DataTable rowKey={(r) => r[0]} rows={STAGES.map(([k, label]) => [k, label, p.ingest.latency[k]] as [string, string, LatencySummary | undefined])}
            columns={[
              { key: 's', header: 'STAGE', width: 170, kind: 'primary', render: (r) => r[1] },
              { key: 'p50', header: 'P50', width: 80, render: (r) => ms(r[2]?.p50 ?? null) },
              { key: 'p95', header: 'P95', width: 80, render: (r) => ms(r[2]?.p95 ?? null) },
              { key: 'max', header: 'MAX', width: 80, render: (r) => ms(r[2]?.max ?? null) },
              { key: 'n', header: 'SAMPLES', grow: true, render: (r) => String(r[2]?.count ?? 0) },
            ]} />
          <p className="note note--muted">Rolling window of the last 4,096 observations per stage. Browser-measured stages are corrected for the clock offset estimated from ping/pong.</p>
        </>
      ) : null}
      {devices.data?.length ? (
        <DataTable rowKey={(d) => d.device_id} rows={devices.data}
          columns={[
            { key: 'id', header: 'DEVICE', width: 190, kind: 'primary', render: (d) => `${d.model ?? d.device_id}${d.primary ? ' (primary)' : ''}` },
            { key: 'p', header: 'PRESENCE', width: 100, render: (d) => <Chip tone={PRESENCE_TONE[d.presence]}>{d.presence}</Chip> },
            { key: 'c', header: 'LAST CONTACT', width: 110, render: (d) => ago(d.last_contact_at) },
            { key: 's', header: 'SEQ / MISSING', width: 120, render: (d) => `#${d.last_sequence ?? '—'} / ${d.missing_batches}`, cellKind: (d) => (d.missing_batches ? 'amber' : 'mono') },
            { key: 'q', header: 'AGENT QUEUE', grow: true, render: (d) => (d.queue_depth == null ? '—' : `${d.queue_depth} batches`) },
          ]} />
      ) : null}
    </Panel>
  )
}

const EVENT_TONE: Record<string, ChipTone> = { info: 'muted', warning: 'amber', error: 'critical', critical: 'critical' }

/** DEVICE_EVENTS: crashes, connectivity, service and posture changes reported by the agent. */
export function DeviceEventsPanel() {
  const { data } = useEndpoint()
  const events = data?.events ?? []
  return (
    <Panel className="side-panel">
      <PanelHeading eyebrow="DEVICE EVENTS" title="Recent events" right={<p className="panel-meta">{events.length} SHOWN</p>} />
      {events.length === 0 ? <p className="note">No device events reported yet (crashes, connectivity changes, service changes, updates).</p> : null}
      <div className="event-list">
        {events.slice(0, 12).map((e) => (
          <div key={e.event_id} className="event-list__row">
            <Chip tone={EVENT_TONE[e.severity] ?? 'muted'}>{e.type.replace(/_/g, ' ').toUpperCase()}</Chip>
            <p className="event-list__msg" title={e.source}>{e.message}</p>
            <p className="caption-mono">{ago(e.timestamp)}</p>
          </div>
        ))}
      </div>
    </Panel>
  )
}
