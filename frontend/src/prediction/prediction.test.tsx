import { act, render, screen } from '@testing-library/react'

import { useTwinDoc } from '../stores/twinDocStore'
import type { ForecastCurve, TwinPrediction } from '../types/prediction'
import { ForecastChart, PredictiveInsightsPanel } from './PredictionViews'
import { fmtDuration, severityTone } from './predictionUtils'

vi.mock('../services/api', () => ({
  api: { devicePredictions: vi.fn(), forecastCurve: vi.fn() },
  ApiError: class extends Error {},
}))

describe('prediction views', () => {
  it('formats durations in the unit a person would use and keeps severity calm', () => {
    expect([fmtDuration(45), fmtDuration(27 * 60), fmtDuration(5 * 3600), fmtDuration(9 * 86400)]).toEqual(['45 s', '27 min', '5.0 h', '9 days'])
    expect([severityTone('CRITICAL'), severityTone('HIGH'), severityTone('INFO'), severityTone(null)]).toEqual(['critical', 'amber', 'muted', 'muted'])
  })

  it('draws actual and forecast distinctly with a legend, threshold and accessible label', () => {
    const now = 1_760_000_000
    const curve: ForecastCurve = {
      device_id: 'dev', target_id: 'memory', context: {},
      history: Array.from({ length: 20 }, (_, i) => [now - (20 - i) * 60, 60 + i] as [number, number]),
      forecast: Array.from({ length: 25 }, (_, i) => ({ t: now + i * 60, mean: 80 + i, lower: 78 + i * 0.8, upper: 82 + i * 1.2 })),
    }
    const { container } = render(<ForecastChart curve={curve} threshold={90} unit="%" crossingAt={new Date((now + 600) * 1000).toISOString()} />)
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain('threshold 90')
    expect(container.querySelector('.forecast-chart__actual')).toBeTruthy()
    expect(container.querySelector('.forecast-chart__forecast')).toBeTruthy()
    expect(container.querySelector('.forecast-chart__band')).toBeTruthy()
    expect(container.querySelector('.forecast-chart__cross')).toBeTruthy()
    expect(screen.getByText('Actual (observed)')).toBeTruthy()
    expect(screen.getByText('Forecast (expected)')).toBeTruthy()
  })

  it('shows forecasts from the twin document as estimates, separate from observations', () => {
    useTwinDoc.getState().reset('dev')
    useTwinDoc.getState().applySnapshot({ twin_id: 't', device_id: 'dev', twin_version: 1, epoch: 'e', projected_at: null,
      restored_from_cache: false, format: 'flat', state: { 'predictions.active_count': 0 } })
    render(<PredictiveInsightsPanel />)
    expect(screen.getByText('NO WARNINGS')).toBeTruthy()
    const soon = (s: number) => new Date(Date.now() + s * 1000).toISOString()
    const disk: TwinPrediction = { status: 'AVAILABLE', health: 'GOOD', reason: 'r', title: 'System drive capacity', unit: '%',
      current_value: 78, forecast: null, model: 'trend', prediction_id: 'p1', prediction_status: 'ACTIVE', threshold: 90,
      crossing_at: soon(9 * 86400), crossing_earliest: soon(8 * 86400), crossing_latest: soon(11 * 86400),
      confidence: 0.82, confidence_band: 'HIGH', severity: 'LOW', statement: 'C: may reach 90%' }
    const battery: TwinPrediction = { status: 'NOT_APPLICABLE', health: 'UNAVAILABLE', reason: 'on AC power / charging: no discharge forecast',
      title: 'Battery charge', unit: '%', current_value: 80, forecast: null, model: null }
    act(() => {
      useTwinDoc.getState().applyPatch({ device_id: 'dev', twin_version: 2, base_version: 1, epoch: 'e',
        changes: { 'predictions.disk': disk, 'predictions.battery': battery, 'predictions.active_count': 1, 'predictions.highest_severity': 'LOW' } })
    })
    expect(screen.getByText('1 FORECAST')).toBeTruthy()
    expect(screen.getByText('Estimated')).toBeTruthy()
    expect(screen.getByText(/in ~9 days/)).toBeTruthy()
    expect(screen.getByText('Not applicable')).toBeTruthy()
    expect(screen.getByText(/Estimates, not guarantees/)).toBeTruthy()
  })
})
