import { render, screen } from '@testing-library/react'

import { FleetOperationsPage } from '../pages/FleetOperationsPage'
import { fleetApi } from '../services/fleetApi'

vi.mock('../services/fleetApi', () => ({
  fleetApi: { health: vi.fn(), insights: vi.fn(), capacity: vi.fn(), operations: vi.fn(), models: vi.fn(), slo: vi.fn() },
}))

const health = {
  version: 'fleet-health-v1', status: 'OK', score: 96, band: 'HEALTHY', devices: 20, scored: 20, coverage: 1,
  confidence: 'HIGH', unknown_devices: [], weights: {}, generated_at: '2026-10-09T10:00:00Z', history: [],
  critical_conditions: [{ device_id: 'd019', reasons: ['twin health CRITICAL'] }],
  contributors: [{ factor: 'device_health', avg_points_deducted: 2, devices: 1 }], worst_devices: [],
}

describe('fleet operations', () => {
  beforeEach(() => {
    vi.mocked(fleetApi.health).mockResolvedValue(health as never)
    vi.mocked(fleetApi.insights).mockResolvedValue({
      generated_at: '', scope: { organization: 'acme', devices: 20, anomalies_considered: 0, since_hours: 168 },
      correlation: { status: 'INSUFFICIENT_DATA', reason: '3 device(s) with attributes; at least 5 are needed', insights: [] },
      recurring_issues: [], remediation_outcomes: [], legend: {},
    } as never)
  })

  it('shows a critical device even when the fleet score is healthy', async () => {
    render(<FleetOperationsPage param="executive" />)
    expect(await screen.findByText('96')).toBeInTheDocument()
    expect(await screen.findByText('d019')).toBeInTheDocument()
    expect(screen.getByText('twin health CRITICAL')).toBeInTheDocument()
    expect(screen.getByText(/No financial savings/)).toBeInTheDocument()
  })

  it('says insufficient data instead of inventing insights', async () => {
    render(<FleetOperationsPage param="insights" />)
    expect(await screen.findByText('Insufficient data')).toBeInTheDocument()
    expect(screen.getByText(/at least 5 are needed/)).toBeInTheDocument()
  })
})
