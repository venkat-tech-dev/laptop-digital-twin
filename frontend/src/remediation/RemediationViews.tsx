import { useState } from 'react'

import { navigate } from '../app/routes'
import { useApi } from '../hooks/useApi'
import { api } from '../services/api'
import { useRemediationEvents } from '../stores/remediationStore'
import { useTwinValue } from '../stores/twinDocStore'
import { STATUS_TEXT, type DryRunPlan, type Remediation } from '../types/remediation'
import { Button, Chip, Panel, PanelHeading, Spec } from '../ui/primitives'
import { CHECK_TEXT, duration, pct, riskTone, rollbackText, statusTone } from './remediationFormat'

/**
 * Phase 8 UI. Consequences are never hidden: before approval the person sees the problem, evidence,
 * the exact action, risk, impact, preconditions, how success is verified and whether it can be undone.
 * Dangerous actions look different from harmless ones and need an explicit confirmation.
 */

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rem-section">
      <p className="eyebrow">{label}</p>
      {children}
    </div>
  )
}

function Result({ r }: { r: Remediation }) {
  const v = r.verification ?? {}
  const failed = r.status === 'FAILED' || r.status === 'ROLLBACK_FAILED'
  const end = r.completed_at ?? r.failed_at
  return (
    <div className={`rem-result ${failed ? 'rem-result--failed' : ''}`}>
      <p className="rem-result__title">{failed ? 'Remediation failed' : r.status === 'PARTIALLY_SUCCEEDED' ? 'Remediation partially succeeded' : r.dry_run ? 'Dry run completed' : 'Remediation completed'}</p>
      {failed ? (
        <>
          <p><b>Reason:</b> {r.failure_reason ?? 'unknown'}</p>
          <p>No additional action was taken.</p>
          <p className="note">Recommended next step: review the device and the application, and retry manually if appropriate.</p>
        </>
      ) : null}
      <div className="anomaly-detail__grid">
        <Spec label="Action" value={r.action_name} />
        <Spec label="Result" value={STATUS_TEXT[r.status]} />
        {typeof v.baseline === 'number' ? <Spec label="Before" value={`${Math.round(v.baseline)}%`} /> : null}
        {typeof v.after === 'number' ? <Spec label="After" value={`${Math.round(v.after)}%`} /> : null}
        <Spec label="Verification" value={v.outcome === 'DRY_RUN' ? 'Dry run (nothing changed)' : failed ? 'Failed' : 'Passed'} />
        <Spec label="Time" value={duration(r.started_at, end)} />
        <Spec label="Approved by" value={r.approved_by ?? '—'} />
        <Spec label="Execution ID" value={<code>{r.execution_id.slice(0, 12)}</code>} />
      </div>
      {v.checks?.length ? (
        <ul className="rem-checks">
          {v.checks.map((c) => <li key={c.check} className={`rem-check rem-check--${(c.state ?? '').toLowerCase()}`}><span>{c.state}</span> {CHECK_TEXT[c.check] ?? c.check}: {c.detail}</li>)}
        </ul>
      ) : null}
    </div>
  )
}

/** Full preview / decision / progress / result for one remediation. */
export function RemediationDetail({ id }: { id: string }) {
  const revision = useRemediationEvents((s) => s.revision)
  const q = useApi(() => api.remediation(id), [id, revision], 15_000)
  const [confirm, setConfirm] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [plan, setPlan] = useState<DryRunPlan | null>(null)
  const r = q.data
  if (!r) return <Panel style={{ flex: '868 0 0' }}><PanelHeading title={q.error ? 'Remediation unavailable' : 'Loading…'} />{q.error ? <p className="note">{q.error}</p> : null}</Panel>
  const a = r.action
  const changes = a?.changes_state ?? true
  const decide = async (d: 'approve' | 'reject' | 'cancel') => {
    setBusy(true)
    setMsg(null)
    try {
      await api.remediationDecision(r.id, d, note)
      q.reload()
      useRemediationEvents.getState().bump()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  const dryRun = async () => {
    setMsg(null)
    try {
      setPlan(await api.remediationDryRun(r.id))
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }
  const terminal = ['SUCCEEDED', 'PARTIALLY_SUCCEEDED', 'FAILED', 'ROLLED_BACK', 'ROLLBACK_FAILED'].includes(r.status)
  const app = r.verification_rules?.application
  return (
    <Panel style={{ flex: '868 0 0' }} className={`anomaly-detail rem-detail rem-detail--${r.risk_level.toLowerCase()}`}>
      <PanelHeading eyebrow={`REMEDIATION · ${r.device_id}${r.dry_run ? ' · DRY RUN' : ''}`} title={r.action_name}
        right={<div className="anomaly-detail__chips"><Chip tone={riskTone(r.risk_level)}>{r.risk_level} RISK</Chip><Chip tone={statusTone(r.status)}>{STATUS_TEXT[r.status]}</Chip></div>} />
      {terminal || r.dry_run && r.status === 'SUCCEEDED' ? <Result r={r} /> : null}
      <div className="rem-grid">
        <Section label="WHAT IS WRONG">
          <p>{r.reason}</p>
          {r.diagnosis_id ? <p className="note note--muted">Diagnosis confidence {pct(r.diagnosis_confidence)} · action confidence {pct(r.action_confidence)} · expected success {pct(r.expected_success_probability)} <span title="Three different measures: how sure the cause is, how likely this action addresses it, and how often this action has worked.">ⓘ</span></p> : null}
          {r.evidence?.length ? <ul className="evidence-list">{r.evidence.map((e) => <li key={e.evidence_id} className="evidence"><span className="evidence__type">{e.evidence_id}</span><span className="evidence__text">{e.statement}</span></li>)}</ul> : null}
        </Section>
        <Section label="WHAT EXACTLY WILL HAPPEN">
          <p>{r.description}</p>
          {app ? <p className="note">Application: <b>{app.name}</b> ({app.executables.join(', ')})</p> : null}
        </Section>
        <Section label="WHAT COULD BE AFFECTED">
          <p>{a?.impact ?? '—'}</p>
          {app?.impact ? <p className="note">{app.impact}</p> : null}
        </Section>
        <Section label="HOW SUCCESS IS VERIFIED">
          <p>{r.verification_rules?.description ?? '—'}</p>
          <p className="note note--muted">Success is decided from telemetry after the action, not from the device saying "done".</p>
        </Section>
        <Section label="CAN IT BE REVERSED?">
          <p className={a?.reversible ? '' : 'rem-irreversible'}>{rollbackText(a?.rollback ?? r.rollback_strategy)}</p>
        </Section>
        <Section label="DURATION & PERMISSIONS">
          <p>About {a?.estimated_duration_s ?? '?'} s; times out after {a?.timeouts_s.execution ?? '?'} s. The device's agent validates the signed action against its own allowlist before running it.</p>
          <p className="note note--muted">Policy: {r.approval_policy.replace(/_/g, ' ').toLowerCase()} · requested by {r.requested_by}{r.recommendation_source === 'ai' ? ' (AI suggestion, validated against the action catalog)' : ''}</p>
        </Section>
      </div>

      {r.status === 'PENDING_APPROVAL' ? (
        <div className={`rem-approval rem-approval--${r.risk_level.toLowerCase()}`}>
          <p className="recommendation__label">DECISION {r.approval_expires_at ? `· VALID UNTIL ${new Date(r.approval_expires_at).toLocaleTimeString()}` : ''}</p>
          {changes ? (
            <label className="rem-confirm"><input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} /> I understand: {a?.impact} {a?.reversible ? '' : 'This cannot be undone.'}</label>
          ) : null}
          <label className="prefs__field">Note (optional)<input type="text" maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} /></label>
          <div className="recommendation__actions">
            <Button className={changes ? 'btn--danger' : ''} primary={!changes} disabled={busy || !r.allowed?.approve || (changes && !confirm)} onClick={() => void decide('approve')}
              title={r.allowed?.approve ? undefined : 'Not permitted for your account (role, risk level or four-eyes rule)'}>Approve Action</Button>
            <Button disabled={busy || !r.allowed?.reject} onClick={() => void decide('reject')}>Reject Action</Button>
            <Button disabled={busy} onClick={() => void dryRun()}>Dry run</Button>
          </div>
          {!r.allowed?.approve ? <p className="note note--muted">You cannot approve this request (role, risk level, or the requester may not approve their own request).</p> : null}
        </div>
      ) : null}
      {r.allowed?.cancel && r.status !== 'PENDING_APPROVAL' ? <Button disabled={busy} onClick={() => void decide('cancel')}>Cancel Execution</Button> : null}
      {r.execution?.waiting_on && ['QUEUED', 'APPROVED'].includes(r.status) ? <p className="note">Waiting: {r.execution.waiting_on.replace(/_/g, ' ')}</p> : null}
      {msg ? <p className="note">{msg}</p> : null}
      {plan ? (
        <div className="rem-dryrun">
          <p className="eyebrow">DRY RUN · NO CHANGES WERE MADE</p>
          <p>{plan.expected_effect} (about {plan.estimated_duration_s} s)</p>
          <ul className="rem-checks">{plan.preconditions.map((c) => <li key={c.check} className={`rem-check rem-check--${c.ok ? 'pass' : c.kind === 'wait' ? 'pending' : 'fail'}`}><span>{c.ok ? 'OK' : c.kind === 'wait' ? 'WAIT' : 'NO'}</span> {c.check.replace(/_/g, ' ')}: {c.detail}</li>)}</ul>
          <p className="note note--muted">{plan.note}</p>
        </div>
      ) : null}
      <details className="diag__all">
        <summary>History ({r.audit?.length ?? 0}) · correlation {r.correlation_id.slice(0, 8)}</summary>
        <ol className="audit">
          {(r.audit ?? []).map((e, i) => <li key={`${e.at}-${i}`}><span className="audit__time">{new Date(e.at).toLocaleTimeString()}</span> <b>{e.action.replace(/_/g, ' ')}</b> by {e.actor}{e.to_status && e.to_status !== e.from_status ? ` → ${e.to_status}` : ''}{e.detail.reason ? ` — ${String(e.detail.reason)}` : ''}</li>)}
        </ol>
      </details>
    </Panel>
  )
}

/** Compact list of remediations (used in the diagnosis panel and the twin page). */
export function RemediationList({ items, onOpen }: { items: Remediation[]; onOpen?: (id: string) => void }) {
  if (!items.length) return null
  return (
    <ul className="rem-list">
      {items.map((r) => (
        <li key={r.id}>
          <button type="button" className={`alert-row rem-row--${r.risk_level.toLowerCase()}`} onClick={() => (onOpen ? onOpen(r.id) : navigate('remediation', r.id))}>
            <Chip tone={riskTone(r.risk_level)}>{r.risk_level}</Chip>
            <span className="alert-row__title">{r.action_name}</span>
            <span className="alert-row__meta">{STATUS_TEXT[r.status]} · {r.device_id} · {new Date(r.updated_at).toLocaleString()}</span>
          </button>
        </li>
      ))}
    </ul>
  )
}

/** Inside a diagnosis: the recommended remediation(s) for it. */
export function DiagnosisRemediations({ diagnosisId }: { diagnosisId: string }) {
  const revision = useRemediationEvents((s) => s.revision)
  const q = useApi(() => api.remediations({ diagnosis_id: diagnosisId, limit: 5 }), [diagnosisId, revision], 60_000)
  const items = q.data?.items ?? []
  if (!items.length) return null
  return (
    <div className="rem-in-diagnosis">
      <p className="eyebrow">RECOMMENDED REMEDIATION (NEEDS APPROVAL; NOTHING RUNS AUTOMATICALLY BY DEFAULT)</p>
      <RemediationList items={items} />
    </div>
  )
}

/** Twin page: remediation state of the device (never replaces observed state). */
export function TwinRemediationPanel() {
  const status = useTwinValue<string>('remediation.status') ?? 'NONE'
  const pending = useTwinValue<number>('remediation.pending_count') ?? 0
  const latest = useTwinValue<Remediation | null>('remediation.latest') ?? null
  return (
    <Panel className="twin-side">
      <PanelHeading eyebrow="REMEDIATION · HUMAN-APPROVED ACTIONS" title="Remediation"
        right={<Chip tone={statusTone(status)}>{pending ? `${pending} PENDING` : status.replace('_', ' ')}</Chip>} />
      {latest ? (
        <>
          <p className="diag__summary">{latest.action_name}: {STATUS_TEXT[latest.status]}</p>
          <p className="note note--muted">{latest.risk_level} risk · {new Date(latest.updated_at).toLocaleString()}</p>
          <Button onClick={() => navigate('remediation', latest.remediation_id)}>Open remediation</Button>
        </>
      ) : <p className="note note--muted">No remediation for this device. Actions are proposed from diagnoses and always need approval unless an explicit policy allows a low-risk action.</p>}
    </Panel>
  )
}
