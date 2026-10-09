import { useApi } from '../hooks/useApi'
import { orgApi } from '../services/orgApi'
import { MetricCard, Panel, PanelHeading, Spec, Track } from '../ui/primitives'
import { Counts } from './orgUi'
import { useCan, when } from './orgUtils'

const QUOTA_LABELS: Record<string, string> = {
  max_devices: 'Devices',
  max_users: 'Active members',
  telemetry_batches_per_min: 'Telemetry batches / min',
  api_requests_per_min: 'API requests / min',
  diagnosis_jobs_per_hour: 'Diagnosis jobs / hour',
  remediation_requests_per_day: 'Remediation requests / day',
  exports_per_hour: 'Exports / hour',
}

/** Organization dashboard, security posture and usage: all from live state and the audit trail. */
export function OrgOverview() {
  const can = useCan()
  const dash = useApi(() => orgApi.dashboard(), [], 15_000)
  const sec = useApi(() => (can('audit.view') ? orgApi.security() : Promise.resolve(null)), [can], 30_000)
  const usage = useApi(() => (can('usage.view') ? orgApi.usage() : Promise.resolve(null)), [can], 15_000)
  const d = dash.data
  const s = sec.data
  const u = usage.data
  return (
    <>
      <div className="metric-row">
        <MetricCard label="DEVICES" value={d ? String(d.devices_total) : null} caption="visible to you" />
        <MetricCard label="ACTIVE ALERTS" value={d ? String(d.active_alerts) : null} tone={d && d.active_alerts ? 'amber' : 'default'}
          caption={d ? `${d.alerts_by_severity.CRITICAL ?? 0} critical · ${d.alerts_by_severity.HIGH ?? 0} high` : undefined} />
        <MetricCard label="NON-COMPLIANT" value={d ? String(d.by_compliance.NON_COMPLIANT ?? 0) : null}
          tone={d && d.by_compliance.NON_COMPLIANT ? 'amber' : 'default'} caption="by organization policy" />
        <MetricCard label="ACTIVE PREDICTIONS" value={d ? String(d.active_predictions) : null} caption="forecast risks" />
      </div>
      {dash.error ? <p className="note" style={{ color: 'var(--critical)' }}>{dash.error}</p> : null}
      <div className="split split--even">
        <Panel>
          <PanelHeading eyebrow="FLEET" title="Devices" right={<p className="panel-meta">{d ? `updated ${when(d.generated_at)}` : 'Loading…'}</p>} />
          <Spec label="Connectivity" value={<Counts data={d?.by_presence} />} />
          <Spec label="Health" value={<Counts data={d?.by_health} empty="No health state yet" />} />
          <Spec label="Lifecycle" value={<Counts data={d?.by_lifecycle} />} />
          <Spec label="Compliance" value={<Counts data={d?.by_compliance} />} />
          <Spec label="Security posture" value={<Counts data={d?.by_security_posture} empty="Not reported" />} />
          <Spec label="Agent versions" value={d ? Object.entries(d.by_agent_version).map(([v, n]) => `${v === 'None' ? 'unknown' : v} × ${n}`).join(' · ') || '—' : '—'} />
          <Spec label="Remediation" value={<Counts data={d?.remediation_by_status} empty="No remediation" />} />
        </Panel>
        {s ? (
          <Panel>
            <PanelHeading eyebrow={`SECURITY · LAST ${s.window_hours} H`} title="Security posture" />
            <Spec label="Failed sign-ins" value={String(s.authentication_failures)} tone={s.authentication_failures ? 'amber' : 'default'} />
            <Spec label="Authorization denials" value={String(s.authorization_denials)} />
            <Spec label="Cross-organization attempts" value={String(s.cross_tenant_attempts)} tone={s.cross_tenant_attempts ? 'amber' : 'default'} />
            <Spec label="Failed enrollments" value={String(s.failed_enrollments)} />
            <Spec label="MFA adoption" value={`${s.mfa_adoption.with_mfa} of ${s.mfa_adoption.members} members`} tone={s.mfa_adoption.with_mfa < s.mfa_adoption.members ? 'amber' : 'default'} />
            <Spec label="Inactive members (30 d)" value={String(s.inactive_users_30d)} />
            <Spec label="Devices on shared enrollment key" value={String(s.devices_legacy_enrolled)} tone={s.devices_legacy_enrolled ? 'amber' : 'default'}
              title="Re-enroll these devices with an enrollment token to get per-organization credentials" />
            <Spec label="Devices below recommended agent" value={String(s.devices_outdated_agent)} />
            <Spec label="Revoked devices" value={String(s.devices_revoked)} />
            <Spec label="Open security alerts" value={String(s.security_alerts_open)} />
            <p className="note note--muted">{s.measured}.</p>
          </Panel>
        ) : null}
      </div>
      {u ? (
        <Panel>
          <PanelHeading eyebrow="USAGE" title="Usage and quotas" right={<p className="panel-meta">{u.since}</p>} />
          <div className="split split--even">
            <div>
              <Spec label="Active members" value={String(u.users)} />
              <Spec label="Devices" value={String(u.devices)} />
              <Spec label="Live connections" value={String(u.websocket_connections)} />
              <Spec label="Telemetry batches" value={String(u.telemetry_batches_since_start)} />
              <Spec label="API requests" value={String(u.api_requests_since_start)} />
              <Spec label="Recent remediations" value={String(u.remediations_recent)} />
              <Spec label="Storage" value={u.storage === 'NOT_MEASURED' ? 'Not measured per organization' : u.storage} />
            </div>
            <div>
              {Object.entries(u.quotas).map(([name, q]) => {
                const pct = q.limit ? Math.min(100, (q.used / q.limit) * 100) : 0
                return (
                  <div key={name} style={{ marginBottom: 10 }}>
                    <Spec label={QUOTA_LABELS[name] ?? name} value={`${q.used} / ${q.limit} · ${q.mode.toLowerCase()}${q.rejected_total ? ` · ${q.rejected_total} refused` : ''}`}
                      tone={pct >= 80 ? 'amber' : 'default'} />
                    <Track percent={pct} tone={pct >= 80 ? 'amber' : 'accent'} />
                  </div>
                )
              })}
            </div>
          </div>
        </Panel>
      ) : null}
    </>
  )
}
