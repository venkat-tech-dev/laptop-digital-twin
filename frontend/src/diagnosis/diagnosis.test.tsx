import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { api } from '../services/api'
import { useSession } from '../stores/sessionStore'
import type { DiagnosisDetail } from '../types/diagnosis'
import { DiagnosisBody, DiagnosisPanel } from './DiagnosisPanel'
import { levelTone } from './diagnosisFormat'

vi.mock('../services/api', () => ({
  api: {
    diagnosesFor: vi.fn(),
    diagnosis: vi.fn(),
    requestDiagnosis: vi.fn(),
    diagnosisJob: vi.fn(),
    diagnosisFeedback: vi.fn(async () => ({ verdict: 'CORRECT' })),
  },
  ApiError: class extends Error {},
}))

const detail = (over: Partial<DiagnosisDetail> = {}): DiagnosisDetail => ({
  diagnosis_id: 'd1', series_id: 's1', version: 1, device_id: 'dev', trigger_kind: 'alert', trigger_id: 'a1',
  alert_id: 'a1', anomaly_id: null, prediction_id: null, status: 'AVAILABLE', diagnosis_type: 'CPU_PRESSURE',
  category: 'cpu_pressure', severity: 'HIGH', summary: 'Sustained CPU usage by builder.exe is a likely contributor (high confidence).',
  likely_cause: 'Sustained CPU usage by builder.exe is a likely contributor', confidence: 0.82, confidence_level: 'HIGH',
  reasoning_model: 'rules', model_version: 'rules-1', prompt_version: null, created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(), expires_at: null, notices: ['AI reasoning unavailable. Showing deterministic evidence.'],
  supersedes: null, alternative_causes: ['Several background tasks together are keeping the CPU busy'],
  hypotheses: [
    { code: 'cpu.process', category: 'CPU_PRESSURE', cause: 'Sustained CPU usage by builder.exe is a likely contributor',
      supporting: ['E1', 'E2'], contradicting: [], missing: [], recommendations: [], confidence: 0.82, confidence_level: 'HIGH', factors: {}, origin: 'rules' },
    { code: 'cpu.background', category: 'CPU_PRESSURE', cause: 'Several background tasks together are keeping the CPU busy',
      supporting: ['E1'], contradicting: ['E2'], missing: [], recommendations: [], confidence: 0.31, confidence_level: 'LOW', factors: {}, origin: 'rules' },
  ],
  evidence: [
    { evidence_id: 'E1', type: 'OBSERVATION', source: 'twin', statement: 'CPU is 90% (usual up to 45%).', signal: 'cpu', metric: 'CPU', observed: 90, baseline: 45, unit: '%', timestamp: null, process: null, strength: 0.9 },
    { evidence_id: 'E2', type: 'PROCESS', source: 'process_snapshots', statement: 'builder.exe averaged 62.0% CPU during the window (before: 1.0%).', signal: 'cpu', metric: 'process CPU', observed: 62, baseline: 1, unit: '% CPU', timestamp: null, process: 'builder.exe', strength: 0.8 },
  ],
  explanation: {
    missing: ['temperature telemetry is not available'],
    investigate: ['Check whether builder.exe is expected to be busy right now (a build, scan, update or sync).'],
    uncertainty: 'Platform confidence is HIGH (82%), computed from the evidence, not from the AI model.',
    timeline: { before: ['builder.exe was at 1.0% CPU before the window and 62.0% during it.'], during: [], after: [] },
    model_claims: [],
  },
  related_processes: ['builder.exe'], rejected_claims: [{ kind: 'recommendation', reason: 'FORBIDDEN_ACTION' }], timings: {},
  versions: [], feedback: [],
  ...over,
})

describe('diagnosis view', () => {
  it('shows the likely cause, traceable evidence, alternatives, the fallback notice and withheld AI statements', () => {
    render(<DiagnosisBody d={detail()} />)
    expect(screen.getByText('AI reasoning unavailable. Showing deterministic evidence.')).toBeInTheDocument()
    expect(screen.getAllByText(/builder\.exe is a likely contributor/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('CPU is 90% (usual up to 45%).').length).toBeGreaterThan(0)
    expect(screen.getAllByText('E1').length).toBeGreaterThan(0) // every statement carries its evidence id
    expect(screen.getByText(/Several background tasks/)).toBeInTheDocument()
    expect(screen.getByText(/1 AI statement was withheld/)).toBeInTheDocument()
    expect(screen.getByText(/A PERSON DECIDES; NOTHING IS CHANGED AUTOMATICALLY/)).toBeInTheDocument()
    expect(screen.getByText(/temperature telemetry is not available/)).toBeInTheDocument()
  })

  it('maps confidence levels to tones', () => {
    expect(levelTone('HIGH')).toBe('accent')
    expect(levelTone('MEDIUM')).toBe('amber')
    expect(levelTone('INSUFFICIENT')).toBe('muted')
  })
})

describe('diagnosis panel', () => {
  beforeEach(() => {
    vi.mocked(api.diagnosesFor).mockReset()
    vi.mocked(api.diagnosis).mockReset()
  })

  it('offers "Explain this" when nothing exists, and polls the job until done', async () => {
    useSession.setState({ me: { username: 'ops', display_name: null, role: 'operator', method: 'account', auth_mode: 'accounts', can_operate: true, can_admin: false } })
    vi.mocked(api.diagnosesFor).mockResolvedValue({ device_id: 'dev', items: [] })
    vi.mocked(api.requestDiagnosis).mockResolvedValue({ job: { job_id: 'j1', device_id: 'dev', trigger_kind: 'alert', trigger_id: 'a1', status: 'QUEUED', diagnosis_id: null, error: null } })
    render(<DiagnosisPanel kind="alert" id="a1" />)
    const btn = await screen.findByRole('button', { name: 'Explain this' })
    fireEvent.click(btn)
    await waitFor(() => expect(api.requestDiagnosis).toHaveBeenCalledWith('alert', 'a1', false))
    expect(await screen.findByText(/Collecting evidence and reasoning/)).toBeInTheDocument()
  })

  it('is read-only for viewers and shows the current version with feedback', async () => {
    useSession.setState({ me: { username: 'vic', display_name: null, role: 'viewer', method: 'account', auth_mode: 'accounts', can_operate: false, can_admin: false } })
    const d = detail()
    vi.mocked(api.diagnosesFor).mockResolvedValue({ device_id: 'dev', items: [d] })
    vi.mocked(api.diagnosis).mockResolvedValue(d)
    render(<DiagnosisPanel kind="alert" id="a1" />)
    expect(await screen.findByText('HIGH CONFIDENCE')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Explain this|Re-diagnose/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Correct' }))
    await waitFor(() => expect(api.diagnosisFeedback).toHaveBeenCalledWith('d1', 'CORRECT', undefined))
    expect(await screen.findByText(/recorded for evaluation/)).toBeInTheDocument()
  })
})
