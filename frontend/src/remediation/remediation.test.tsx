import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { api } from '../services/api'
import type { Remediation } from '../types/remediation'
import { RemediationDetail } from './RemediationViews'
import { riskTone, rollbackText, statusTone } from './remediationFormat'

vi.mock('../services/api', () => ({
  api: { remediation: vi.fn(), remediationDecision: vi.fn(async () => ({})), remediationDryRun: vi.fn() },
  ApiError: class extends Error {},
}))

const rem = (over: Partial<Remediation> = {}): Remediation => ({
  id: 'r1', remediation_id: 'r1', device_id: 'laptop-001', alert_id: 'a1', diagnosis_id: 'd1',
  action_type: 'RESTART_KNOWN_APPLICATION', action_name: 'Restart an approved application: Slack',
  description: 'The agent asks the application to close and starts it again. It never force-kills.',
  risk_level: 'MEDIUM', requires_approval: true, approval_policy: 'MANUAL_APPROVAL', parameters: { application_id: 'slack.desktop' },
  status: 'PENDING_APPROVAL', requested_by: 'system', approved_by: null, approval_at: null,
  approval_expires_at: new Date(Date.now() + 1800_000).toISOString(), rejected_by: null, created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(), scheduled_at: null, mode: 'IMMEDIATE', started_at: null, completed_at: null,
  failed_at: null, result: null, failure_reason: null, correlation_id: 'c1abcdef', execution_id: 'e1abcdef1234567',
  reason: 'Sustained CPU usage by slack.exe is a likely contributor.', diagnosis_confidence: 0.87, action_confidence: 0.78,
  expected_success_probability: 0.6, recommendation_source: 'rules', rollback_strategy: 'NOT_AVAILABLE', dry_run: false,
  action: { risk_level: 'MEDIUM', impact: 'The application closes and restarts; unsaved work in it may be lost.', rollback: 'NOT_AVAILABLE',
    reversible: false, changes_state: true, estimated_duration_s: 45, timeouts_s: { validation: 30, execution: 120, verification: 300 } },
  allowed: { approve: true, reject: true, cancel: true },
  verification_rules: { checks: [], timeout_s: 300, target_signal: 'cpu', target_below: 70, description: 'The application is running again and CPU drops below 70 % within 5 minutes.' },
  evidence: [{ evidence_id: 'E1', statement: 'CPU is 92% (usual up to 45%).' }],
  execution: {}, verification: {}, audit: [],
  ...over,
})

describe('remediation approval', () => {
  beforeEach(() => vi.mocked(api.remediationDecision).mockClear())

  it('shows every consequence and needs explicit confirmation for a state-changing action', async () => {
    vi.mocked(api.remediation).mockResolvedValue(rem())
    render(<RemediationDetail id="r1" />)
    expect(await screen.findByText('MEDIUM RISK')).toBeInTheDocument()
    for (const label of ['WHAT IS WRONG', 'WHAT EXACTLY WILL HAPPEN', 'WHAT COULD BE AFFECTED', 'HOW SUCCESS IS VERIFIED', 'CAN IT BE REVERSED?', 'DURATION & PERMISSIONS']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.getByText('This action cannot be undone.')).toBeInTheDocument()
    expect(screen.getByText(/CPU drops below 70 %/)).toBeInTheDocument()
    const approve = screen.getByRole('button', { name: 'Approve Action' })
    expect(approve).toBeDisabled() // not before confirming the consequences
    expect(approve.className).toContain('btn--danger') // visually distinct from harmless actions
    fireEvent.click(screen.getByRole('checkbox'))
    expect(approve).not.toBeDisabled()
    fireEvent.click(approve)
    await waitFor(() => expect(api.remediationDecision).toHaveBeenCalledWith('r1', 'approve', ''))
  })

  it('does not let an account approve what it may not (four-eyes, role, risk)', async () => {
    vi.mocked(api.remediation).mockResolvedValue(rem({ allowed: { approve: false, reject: false, cancel: false } }))
    render(<RemediationDetail id="r1" />)
    const approve = await screen.findByRole('button', { name: 'Approve Action' })
    fireEvent.click(screen.getByRole('checkbox'))
    expect(approve).toBeDisabled()
    expect(screen.getByText(/You cannot approve this request/)).toBeInTheDocument()
  })

  it('reports a failure honestly', async () => {
    vi.mocked(api.remediation).mockResolvedValue(rem({ status: 'FAILED', failure_reason: 'the application did not close within 20s; it was not forced', failed_at: new Date().toISOString(), started_at: new Date().toISOString() }))
    render(<RemediationDetail id="r1" />)
    expect(await screen.findByText('Remediation failed')).toBeInTheDocument()
    expect(screen.getByText('No additional action was taken.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve Action' })).toBeNull()
  })

  it('shows a verified result with before and after', async () => {
    vi.mocked(api.remediation).mockResolvedValue(rem({ status: 'SUCCEEDED', approved_by: 'admin', started_at: new Date(Date.now() - 102_000).toISOString(), completed_at: new Date().toISOString(),
      verification: { baseline: 92, after: 47, outcome: 'SUCCEEDED', checks: [{ check: 'target_improved', state: 'PASS', detail: 'cpu 92% -> 47%' }] } }))
    render(<RemediationDetail id="r1" />)
    expect(await screen.findByText('Remediation completed')).toBeInTheDocument()
    expect(screen.getByText('92%')).toBeInTheDocument()
    expect(screen.getByText('47%')).toBeInTheDocument()
    expect(screen.getByText('1m 42s')).toBeInTheDocument()
  })

  it('formats risk, status and rollback', () => {
    expect(riskTone('LOW')).toBe('accent')
    expect(riskTone('HIGH')).toBe('critical')
    expect(statusTone('FAILED')).toBe('critical')
    expect(rollbackText('NOT_NEEDED')).toMatch(/nothing to undo/)
  })
})
