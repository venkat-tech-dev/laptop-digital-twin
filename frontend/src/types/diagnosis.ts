/** Phase-7 diagnosis types (mirror of app/domain/diagnosis/models.py and app/api/v1/diagnosis.py). */

export type DiagnosisStatus = 'GENERATING' | 'AVAILABLE' | 'LOW_CONFIDENCE' | 'INSUFFICIENT_EVIDENCE' | 'SUPERSEDED' | 'EXPIRED' | 'FAILED'
export type ConfidenceLevel = 'HIGH' | 'MEDIUM' | 'LOW' | 'INSUFFICIENT'
export type EvidenceType =
  | 'OBSERVATION' | 'TREND' | 'ANOMALY' | 'PREDICTION' | 'PROCESS' | 'EVENT' | 'CORRELATION' | 'BASELINE'
  | 'ABSENCE_OF_EXPECTED_SIGNAL' | 'TEMPORAL'
export type TriggerKind = 'alert' | 'anomaly' | 'prediction'
export type Verdict = 'HELPFUL' | 'NOT_HELPFUL' | 'CORRECT' | 'INCORRECT' | 'PARTIALLY_CORRECT'

export interface Evidence {
  evidence_id: string
  type: EvidenceType
  source: string
  statement: string
  signal: string | null
  metric: string | null
  observed: number | null
  baseline: number | null
  unit: string | null
  timestamp: string | null
  process: string | null
  strength: number
}

export interface Hypothesis {
  code: string
  category: string
  cause: string
  supporting: string[]
  contradicting: string[]
  missing: string[]
  recommendations: string[]
  confidence: number
  confidence_level: ConfidenceLevel
  factors: Record<string, number>
  origin: 'rules' | 'model'
}

export interface Statement { evidence_id: string; statement: string }

export interface DiagnosisSummary {
  diagnosis_id: string
  series_id: string
  version: number
  device_id: string
  trigger_kind: TriggerKind | 'manual'
  trigger_id: string | null
  alert_id: string | null
  anomaly_id: string | null
  prediction_id: string | null
  status: DiagnosisStatus
  diagnosis_type: string
  category: string
  severity: string | null
  summary: string
  likely_cause: string | null
  confidence: number
  confidence_level: ConfidenceLevel
  reasoning_model: string
  model_version: string
  prompt_version: string | null
  created_at: string
  updated_at: string
  expires_at: string | null
  notices: string[]
  supersedes: string | null
  alternative_causes: string[]
}

export interface DiagnosisDetail extends DiagnosisSummary {
  hypotheses: Hypothesis[]
  evidence: Evidence[]
  explanation: {
    what_happened?: string
    likely_cause?: string | null
    why?: Statement[]
    supporting?: Statement[]
    contradicting?: Statement[]
    missing?: string[]
    investigate?: string[]
    uncertainty?: string
    timeline?: { before: string[]; during: string[]; after: string[] }
    model_claims?: { text: string; evidence_ids: string[] }[]
  }
  related_processes: string[]
  rejected_claims: { kind: string; reason: string }[]
  timings: Record<string, number>
  versions: { diagnosis_id: string; version: number; status: DiagnosisStatus; created_at: string; likely_cause: string | null; confidence_level: ConfidenceLevel; reasoning_model: string }[]
  feedback: { verdict: Verdict; actual_cause: string | null; note: string | null; created_at: string }[]
}

export interface DiagnosisJob {
  job_id: string
  device_id: string
  trigger_kind: string
  trigger_id: string | null
  status: 'QUEUED' | 'RUNNING' | 'DONE' | 'FAILED' | 'CACHED' | 'CANCELLED'
  diagnosis_id: string | null
  error: string | null
}

export const FALLBACK_NOTICE = 'AI reasoning unavailable. Showing deterministic evidence.'

export const EVIDENCE_LABEL: Record<EvidenceType, string> = {
  OBSERVATION: 'Observed',
  TREND: 'Trend',
  ANOMALY: 'Anomaly',
  PREDICTION: 'Forecast',
  PROCESS: 'Process',
  EVENT: 'Event',
  CORRELATION: 'Correlation',
  BASELINE: 'Baseline',
  ABSENCE_OF_EXPECTED_SIGNAL: 'Normal',
  TEMPORAL: 'Timing',
}

export const STATUS_TEXT: Record<DiagnosisStatus, string> = {
  GENERATING: 'Generating',
  AVAILABLE: 'Available',
  LOW_CONFIDENCE: 'Low confidence',
  INSUFFICIENT_EVIDENCE: 'Insufficient evidence',
  SUPERSEDED: 'Superseded',
  EXPIRED: 'Expired',
  FAILED: 'Failed',
}
