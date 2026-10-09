import { act, render, screen } from '@testing-library/react'

import { useAnomalies } from '../stores/anomalyStore'
import { useTwinDoc } from '../stores/twinDocStore'
import type { AnomalyRecord, TwinAlert } from '../types/anomaly'
import { levelTone } from './anomalyUtils'
import { ActiveAnomaliesPanel, ExpectedBand } from './AnomalyViews'

vi.mock('../services/api', () => ({
  api: { anomaly: vi.fn(), deviceAnomalies: vi.fn(), anomalySummary: vi.fn() },
  ApiError: class extends Error {},
}))

const record = (over: Partial<AnomalyRecord> = {}): AnomalyRecord => ({
  anomaly_id: 'a1', device_id: 'dev', detector: 'behavioral', rule_id: 'behavior.cpu', component_id: 'performance',
  metric_key: 'performance.cpu.usage_percent', severity: 'warning', title: 'Unusually high cpu usage', message: 'm',
  value: 85, threshold: 46, started_at: '2026-10-07T10:00:00Z', last_seen_at: '2026-10-07T10:05:00Z', resolved_at: null,
  status: 'active', anomaly_type: 'behavioral_anomaly', category: 'performance', level: 'HIGH', confidence: 0.9,
  lifecycle: 'ONGOING', signal_id: 'cpu', model_version: null, baseline_version: 'v1', expected_value: 20, expected_min: 11,
  expected_max: 29, deviation_score: 8.8, persistence_s: 300, evidence: {}, related: [], correlation_key: null,
  occurrences: 1, updated_at: '2026-10-07T10:05:00Z', feedback: null, ...over,
})

describe('anomaly store', () => {
  beforeEach(() => useAnomalies.setState({ deviceId: 'dev', byId: {}, revision: 0 }))

  it('keeps the newest record, ignores other devices and stale updates', () => {
    const s = useAnomalies.getState()
    s.upsert(record())
    s.upsert(record({ confidence: 0.95, updated_at: '2026-10-07T10:06:00Z' }))
    s.upsert(record({ confidence: 0.1, updated_at: '2026-10-07T10:01:00Z' })) // out of order: ignored
    s.upsert(record({ anomaly_id: 'x', device_id: 'other' })) // not the device on screen
    const st = useAnomalies.getState()
    expect(st.byId.a1.confidence).toBe(0.95)
    expect(st.byId.x).toBeUndefined()
    expect(st.revision).toBe(2)
  })
})

describe('anomaly views', () => {
  it('maps levels to calm tones', () => {
    expect([levelTone('INFO'), levelTone('MEDIUM'), levelTone('CRITICAL'), levelTone(null)]).toEqual(['muted', 'amber', 'critical', 'accent'])
  })

  it('renders observed against the usual range with an accessible description', () => {
    render(<ExpectedBand observed={85} p05={11} p95={29} median={20} trigger={46} unit="%" />)
    expect(screen.getByRole('img').getAttribute('aria-label')).toBe('Observed 85.0%; usual range 11.0% to 29.0%')
    expect(screen.getByText(/trigger 46.0%/)).toBeTruthy()
  })

  it('shows active anomalies from the twin document and updates on a patch', () => {
    useTwinDoc.getState().reset('dev')
    useTwinDoc.getState().applySnapshot({ twin_id: 't', device_id: 'dev', twin_version: 1, epoch: 'e', projected_at: null,
      restored_from_cache: false, format: 'flat', state: { 'alerts.active': [], 'alerts.highest_severity': null } })
    render(<ActiveAnomaliesPanel />)
    expect(screen.getByText('NONE ACTIVE')).toBeTruthy()
    const alert: TwinAlert = { anomaly_id: 'a1', severity: 'warning', level: 'HIGH', type: 'behavioral_anomaly', confidence: 0.91,
      title: 'Unusually high cpu usage', since: new Date().toISOString(), resolved_at: null, lifecycle: 'ONGOING',
      metric_key: 'performance.cpu.usage_percent', signal_id: 'cpu', correlation_key: null }
    act(() => {
      useTwinDoc.getState().applyPatch({ device_id: 'dev', twin_version: 2, base_version: 1, epoch: 'e',
        changes: { 'alerts.active': [alert], 'alerts.highest_severity': 'HIGH' } })
    })
    expect(screen.getByText('1 ACTIVE · HIGH')).toBeTruthy()
    expect(screen.getByText('Unusually high cpu usage')).toBeTruthy()
    expect(screen.getByText(/Unusual for this device · 91%/)).toBeTruthy()
  })
})
