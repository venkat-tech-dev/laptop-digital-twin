import { useEffect, useState } from 'react'

import { navigate } from '../app/routes'
import { useApi } from '../hooks/useApi'
import { api } from '../services/api'
import { useDiagnosisEvents } from '../stores/diagnosisStore'
import { useSession } from '../stores/sessionStore'
import { useTwinValue } from '../stores/twinDocStore'
import {
  EVIDENCE_LABEL,
  STATUS_TEXT,
  type DiagnosisDetail,
  type DiagnosisJob,
  type DiagnosisSummary,
  type Evidence,
  type TriggerKind,
  type Verdict,
} from '../types/diagnosis'
import { Button, Chip, Panel, PanelHeading } from '../ui/primitives'
import { DiagnosisRemediations } from '../remediation/RemediationViews'
import { levelTone } from './diagnosisFormat'

/**
 * Phase 7: the "why" next to an alert, anomaly or prediction. Every statement shown comes from
 * platform evidence (each with its id); AI wording is shown only after server-side validation, and
 * confidence is the platform's, never the model's. Nothing here can change the device.
 */

const VERDICTS: { id: Verdict; label: string }[] = [
  { id: 'HELPFUL', label: 'Helpful' },
  { id: 'NOT_HELPFUL', label: 'Not helpful' },
  { id: 'CORRECT', label: 'Correct' },
  { id: 'PARTIALLY_CORRECT', label: 'Partially correct' },
  { id: 'INCORRECT', label: 'Incorrect' },
]

const DONE: DiagnosisJob['status'][] = ['DONE', 'CACHED', 'FAILED', 'CANCELLED']

function EvidenceItem({ e }: { e: Evidence | undefined }) {
  if (!e) return null
  return (
    <li className="evidence">
      <span className={`evidence__type evidence__type--${e.type.toLowerCase()}`}>{EVIDENCE_LABEL[e.type] ?? e.type}</span>
      <span className="evidence__text">{e.statement}</span>
      <span className="evidence__id" title={`Source: ${e.source}${e.timestamp ? ` · ${new Date(e.timestamp).toLocaleString()}` : ''} · strength ${Math.round(e.strength * 100)}%`}>{e.evidence_id}</span>
    </li>
  )
}

function EvidenceList({ ids, byId, empty }: { ids: string[]; byId: Map<string, Evidence>; empty: string }) {
  if (!ids.length) return <p className="note note--muted">{empty}</p>
  return <ul className="evidence-list">{ids.map((id) => <EvidenceItem key={id} e={byId.get(id)} />)}</ul>
}

function Feedback({ d }: { d: DiagnosisDetail }) {
  const [verdict, setVerdict] = useState<Verdict | null>(null)
  const [cause, setCause] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const send = async (v: Verdict) => {
    setMsg(null)
    try {
      await api.diagnosisFeedback(d.diagnosis_id, v, cause.trim() || undefined)
      setVerdict(v)
      setMsg('Thanks - recorded for evaluation (the model is not retrained from it).')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <div className="diag-feedback">
      <p className="eyebrow">WAS THIS DIAGNOSIS USEFUL?</p>
      <div className="diag-feedback__row">
        {VERDICTS.map((v) => (
          <Button key={v.id} active={verdict === v.id} onClick={() => void send(v.id)}>{v.label}</Button>
        ))}
      </div>
      <label className="prefs__field">Actual cause (optional)
        <input type="text" maxLength={500} value={cause} onChange={(e) => setCause(e.target.value)} placeholder="e.g. nightly build job" />
      </label>
      {d.feedback.length ? <p className="note note--muted">Earlier feedback: {d.feedback.map((f) => f.verdict.toLowerCase().replace('_', ' ')).join(', ')}</p> : null}
      {msg ? <p className="note">{msg}</p> : null}
    </div>
  )
}

export function DiagnosisBody({ d }: { d: DiagnosisDetail }) {
  const byId = new Map(d.evidence.map((e) => [e.evidence_id, e]))
  const primary = d.hypotheses[0]
  const x = d.explanation
  const tl = x.timeline ?? { before: [], during: [], after: [] }
  const usedModel = d.reasoning_model !== 'rules'
  const aiNotice = d.notices.find((n) => n.includes('AI'))
  return (
    <div className="diag">
      {aiNotice ? <p className="diag__notice">{aiNotice}</p> : null}
      <p className="diag__summary">{d.summary}</p>
      {d.likely_cause ? (
        <p className="diag__cause"><span className="eyebrow">LIKELY CAUSE</span> {d.likely_cause}</p>
      ) : <p className="note">The evidence does not point to a clear cause yet.</p>}
      <div className="anomaly-detail__columns">
        <div>
          <p className="eyebrow">WHY (SUPPORTING EVIDENCE)</p>
          <EvidenceList ids={primary?.supporting ?? []} byId={byId} empty="No supporting evidence." />
          <p className="eyebrow">WHAT DOES NOT FIT</p>
          <EvidenceList ids={primary?.contradicting ?? []} byId={byId} empty="Nothing in the evidence contradicts this explanation." />
          {x.missing?.length ? (
            <>
              <p className="eyebrow">MISSING EVIDENCE</p>
              <ul className="diag__plain">{x.missing.map((m) => <li key={m}>{m}</li>)}</ul>
            </>
          ) : null}
        </div>
        <div>
          <p className="eyebrow">WHAT TO INVESTIGATE (A PERSON DECIDES; NOTHING IS CHANGED AUTOMATICALLY)</p>
          <ul className="diag__plain">{(x.investigate ?? []).map((r) => <li key={r}>{r}</li>)}</ul>
          {d.hypotheses.length > 1 ? (
            <>
              <p className="eyebrow">ALTERNATIVE EXPLANATIONS</p>
              <ul className="diag__alts">
                {d.hypotheses.slice(1).filter((h) => h.code !== 'unknown').map((h) => (
                  <li key={h.code}><Chip tone={levelTone(h.confidence_level)}>{h.confidence_level}</Chip> {h.cause}{h.origin === 'model' ? ' (suggested by AI, scored by the platform)' : ''}</li>
                ))}
              </ul>
            </>
          ) : null}
          {tl.before.length + tl.during.length + tl.after.length ? (
            <>
              <p className="eyebrow">SEQUENCE</p>
              <ul className="diag__plain diag__seq">
                {tl.before.map((s) => <li key={`b${s}`}><b>Before:</b> {s}</li>)}
                {tl.during.map((s) => <li key={`d${s}`}><b>During:</b> {s}</li>)}
                {tl.after.map((s) => <li key={`a${s}`}><b>Next:</b> {s}</li>)}
              </ul>
            </>
          ) : null}
        </div>
      </div>
      {x.model_claims?.length ? (
        <>
          <p className="eyebrow">AI EXPLANATION (VALIDATED AGAINST EVIDENCE)</p>
          <ul className="diag__plain">{x.model_claims.map((c) => <li key={c.text}>{c.text} <span className="evidence__id">{c.evidence_ids.join(', ')}</span></li>)}</ul>
        </>
      ) : null}
      {d.rejected_claims.length ? <p className="note note--muted">{d.rejected_claims.length} AI statement{d.rejected_claims.length > 1 ? 's were' : ' was'} withheld because the evidence did not support {d.rejected_claims.length > 1 ? 'them' : 'it'}.</p> : null}
      <details className="diag__all">
        <summary>All evidence ({d.evidence.length})</summary>
        <ul className="evidence-list">{d.evidence.map((e) => <EvidenceItem key={e.evidence_id} e={e} />)}</ul>
      </details>
      <p className="note note--muted">{x.uncertainty}</p>
      <p className="diag__meta">
        {usedModel ? `Reasoning: ${d.reasoning_model}` : 'Reasoning: deterministic rules'} · prompt {d.prompt_version ?? '—'} · version {d.version} · {new Date(d.created_at).toLocaleString()}
        {d.expires_at ? ` · valid until ${new Date(d.expires_at).toLocaleTimeString()}` : ''}
      </p>
    </div>
  )
}

/** Diagnosis of one alert / anomaly / prediction: latest version, history, request, feedback. */
export function DiagnosisPanel({ kind, id, compact = false }: { kind: TriggerKind; id: string; compact?: boolean }) {
  const revision = useDiagnosisEvents((s) => s.revision)
  const me = useSession((s) => s.me)
  const [job, setJob] = useState<DiagnosisJob | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const list = useApi(() => api.diagnosesFor(kind, id, false), [kind, id, revision, job?.status], 60_000)
  const items: DiagnosisSummary[] = list.data?.items ?? []
  const current = items.find((d) => !['SUPERSEDED', 'EXPIRED'].includes(d.status)) ?? items[0] ?? null
  const shownId = selected ?? current?.diagnosis_id ?? null
  const detail = useApi(() => (shownId ? api.diagnosis(shownId) : Promise.resolve(null)), [shownId, revision], 0)
  const canRequest = Boolean(me && me.role !== 'viewer')

  useEffect(() => {
    if (!job || DONE.includes(job.status)) return
    const t = window.setTimeout(async () => {
      try {
        setJob(await api.diagnosisJob(job.job_id))
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e))
        setJob(null)
      }
    }, 1500)
    return () => window.clearTimeout(t)
  }, [job])

  const request = async (force: boolean) => {
    setErr(null)
    setSelected(null)
    try {
      setJob((await api.requestDiagnosis(kind, id, force)).job)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }
  const running = job && !DONE.includes(job.status)
  const d = detail.data
  return (
    <div className={`diag-panel ${compact ? 'diag-panel--compact' : ''}`}>
      <div className="diag-panel__head">
        <p className="eyebrow">DIAGNOSED · EXPLANATION, NOT A MEASUREMENT</p>
        <div className="diag-panel__chips">
          {d ? <Chip tone={levelTone(d.confidence_level)} title="Platform confidence, computed from evidence">{d.confidence_level} CONFIDENCE</Chip> : null}
          {d ? <Chip tone="muted">{STATUS_TEXT[d.status]}</Chip> : null}
          {canRequest ? (
            <Button disabled={Boolean(running)} onClick={() => void request(Boolean(current))} title={current ? 'Re-run with the latest telemetry (creates a new version)' : 'Explain this event from the evidence'}>
              {running ? 'Diagnosing…' : current ? 'Re-diagnose' : 'Explain this'}
            </Button>
          ) : null}
        </div>
      </div>
      {running ? <p className="note">Collecting evidence and reasoning… ({job?.status.toLowerCase()})</p> : null}
      {job?.status === 'FAILED' ? <p className="note">Diagnosis failed: {job.error}</p> : null}
      {err ? <p className="note">{err}</p> : null}
      {list.error && !items.length ? <p className="note">Diagnosis unavailable: {list.error}</p> : null}
      {!items.length && !running && !list.loading ? <p className="note note--muted">No diagnosis yet.{canRequest ? ' Use “Explain this” to analyse the evidence.' : ''}</p> : null}
      {d?.status === 'GENERATING' ? <p className="note">Generating…</p> : null}
      {d && d.status !== 'GENERATING' ? <DiagnosisBody d={d} /> : null}
      {d && items.length > 1 ? (
        <label className="prefs__field">History
          <select value={shownId ?? ''} onChange={(e) => setSelected(e.target.value)}>
            {items.map((v) => <option key={v.diagnosis_id} value={v.diagnosis_id}>v{v.version} · {STATUS_TEXT[v.status]} · {new Date(v.created_at).toLocaleString()}</option>)}
          </select>
        </label>
      ) : null}
      {d && d.status !== 'GENERATING' ? <DiagnosisRemediations diagnosisId={d.diagnosis_id} /> : null}
      {d && d.status !== 'GENERATING' && d.status !== 'FAILED' ? <Feedback key={d.diagnosis_id} d={d} /> : null}
    </div>
  )
}

/** Twin page: the latest diagnosis of the device on screen (twin field diagnoses.latest). */
export function DiagnosedPanel() {
  const latest = useTwinValue<DiagnosisSummary | null>('diagnoses.latest') ?? null
  const count = useTwinValue<number>('diagnoses.active_count') ?? 0
  const generating = useTwinValue<boolean>('diagnoses.generating') ?? false
  return (
    <Panel className="twin-side">
      <PanelHeading eyebrow="DIAGNOSED · EXPLANATION FROM EVIDENCE" title="Diagnosis"
        right={<Chip tone={latest ? levelTone(latest.confidence_level) : 'accent'}>{generating ? 'GENERATING' : count ? `${count} CURRENT` : 'NONE'}</Chip>} />
      {latest ? (
        <>
          <p className="diag__summary">{latest.summary}</p>
          <p className="note note--muted">{latest.confidence_level.toLowerCase()} confidence · {latest.reasoning_model === 'rules' ? 'deterministic rules' : latest.reasoning_model} · {new Date(latest.created_at).toLocaleTimeString()}</p>
          {latest.alternative_causes.length ? <p className="note note--muted">Alternatives: {latest.alternative_causes.join('; ')}</p> : null}
          {latest.alert_id ? <Button onClick={() => navigate('alerts', `alert:${latest.alert_id}`)}>Open the evidence</Button> : null}
        </>
      ) : <p className="note note--muted">No current diagnosis. High-severity alerts are explained automatically; any alert can be explained on request.</p>}
    </Panel>
  )
}
