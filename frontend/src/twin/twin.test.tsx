import { act, render, screen } from '@testing-library/react'
import { useRef } from 'react'

import { deviceScope, scopePath } from '../services/deviceScope'
import { setTwinResync, useTwinDoc, useTwinValue } from '../stores/twinDocStore'
import type { TwinField, TwinSnapshotMsg } from '../types/twinDoc'
import { formatField } from './format'
import { TwinHeader, TwinMetricBoard } from './TwinPanels'

vi.mock('../services/api', () => ({
  api: {
    deviceHistory: vi.fn(async () => ({ device_id: 'dev', range: '1h', bucket_seconds: 30, series: {} })),
    explainField: vi.fn(),
  },
  ApiError: class extends Error {},
}))

const field = (value: unknown, extra: Partial<TwinField> = {}): TwinField => ({
  value,
  unit: '%',
  label: null,
  reason: null,
  timestamp: new Date().toISOString(),
  interval_s: 5,
  source: { metric_key: 'cpu.usage_percent', origin: 'psutil', batch_id: 'b1', sequence: 1, received_at: new Date().toISOString() },
  status: 'normal',
  freshness: 'LIVE',
  ...extra,
})

const snapshot = (version: number, state: Record<string, unknown>, epoch = 'e1'): TwinSnapshotMsg => ({
  twin_id: 't', device_id: 'dev', twin_version: version, epoch, projected_at: null, restored_from_cache: false, format: 'flat', state,
})

beforeEach(() => {
  useTwinDoc.getState().reset('dev')
})

describe('twin document store', () => {
  it('applies in-order patches, ignores duplicates and resyncs on a gap or a new epoch', () => {
    const resync = vi.fn()
    setTwinResync(resync)
    const s = useTwinDoc.getState()
    s.applySnapshot(snapshot(10, { 'performance.cpu.usage_percent': field(30) }))
    expect(s.applyPatch({ device_id: 'dev', twin_version: 11, base_version: 10, epoch: 'e1', changes: { 'performance.cpu.usage_percent': field(91, { status: 'warning' }) } })).toBe('applied')
    expect((useTwinDoc.getState().fields['performance.cpu.usage_percent'] as TwinField).value).toBe(91)
    expect(s.applyPatch({ device_id: 'dev', twin_version: 11, base_version: 10, epoch: 'e1', changes: {} })).toBe('stale') // duplicate
    expect(s.applyPatch({ device_id: 'dev', twin_version: 14, base_version: 13, epoch: 'e1', changes: {} })).toBe('gap') // missed 12-13
    expect(resync).toHaveBeenCalledWith('dev')
    expect(useTwinDoc.getState().status).toBe('loading')
    useTwinDoc.getState().applySnapshot(snapshot(14, { 'performance.cpu.usage_percent': field(50) }))
    expect(useTwinDoc.getState().applyPatch({ device_id: 'dev', twin_version: 1, base_version: 0, epoch: 'e2', changes: {} })).toBe('gap') // backend restarted
    expect(resync).toHaveBeenCalledTimes(2)
  })

  it('merges partial field updates and resyncs when the base field is missing', () => {
    const resync = vi.fn()
    setTwinResync(resync)
    const s = useTwinDoc.getState()
    s.applySnapshot(snapshot(5, { 'performance.cpu.usage_percent': field(30) }))
    expect(s.applyPatch({ device_id: 'dev', twin_version: 6, base_version: 5, epoch: 'e1', changes: {},
      merge: { 'performance.cpu.usage_percent': { value: 88, status: 'elevated', source: { sequence: 2 } } } })).toBe('applied')
    const f = useTwinDoc.getState().fields['performance.cpu.usage_percent'] as TwinField
    expect(f.value).toBe(88)
    expect(f.unit).toBe('%') // untouched sub-keys kept
    expect(f.source?.sequence).toBe(2)
    expect(f.source?.origin).toBe('psutil') // untouched provenance kept
    expect(useTwinDoc.getState().applyPatch({ device_id: 'dev', twin_version: 7, base_version: 6, epoch: 'e1', changes: {},
      merge: { 'battery.charge_percent': { value: 50 } } })).toBe('gap')
    expect(resync).toHaveBeenCalledWith('dev')
  })

  it('never lets an older snapshot overwrite a newer one, and ignores other devices', () => {
    const s = useTwinDoc.getState()
    s.applySnapshot(snapshot(20, { a: 1 }))
    s.applySnapshot(snapshot(18, { a: 0 }))
    expect(useTwinDoc.getState().version).toBe(20)
    expect(s.applyPatch({ device_id: 'other', twin_version: 21, base_version: 20, epoch: 'e1', changes: { a: 2 } })).toBe('ignored')
  })

  it('re-renders only components whose field changed', () => {
    const renders = { cpu: 0, mem: 0 }
    function Probe({ path, k }: { path: string; k: 'cpu' | 'mem' }) {
      const v = useTwinValue<TwinField>(path)
      const count = useRef(0)
      count.current += 1
      renders[k] = count.current
      return <span>{String(v?.value)}</span>
    }
    useTwinDoc.getState().applySnapshot(snapshot(1, { 'performance.cpu.usage_percent': field(10), 'performance.memory.usage_percent': field(40) }))
    render(<><Probe path="performance.cpu.usage_percent" k="cpu" /><Probe path="performance.memory.usage_percent" k="mem" /></>)
    const before = { ...renders }
    act(() => {
      useTwinDoc.getState().applyPatch({ device_id: 'dev', twin_version: 2, base_version: 1, epoch: 'e1', changes: { 'performance.cpu.usage_percent': field(84) } })
    })
    expect(renders.cpu).toBe(before.cpu + 1)
    expect(renders.mem).toBe(before.mem) // untouched field: no re-render
  })
})

describe('twin UI', () => {
  it('shows unknown and unsupported values explicitly, never as 0', async () => {
    useTwinDoc.getState().applySnapshot(snapshot(3, {
      'connectivity.status': 'ONLINE',
      'performance.cpu.usage_percent': field(72, { status: 'elevated' }),
      'sections.cpu': { visual: 'elevated' },
      'thermal.temperature_c': field(null, { unit: '°C', freshness: 'UNSUPPORTED', reason: 'Needs LibreHardwareMonitor', source: null }),
      'sections.thermal': { visual: 'unknown' },
      health: { state: 'HEALTHY', reasons: [], evaluated_at: null },
    }))
    await act(async () => {
      render(<><TwinHeader /><TwinMetricBoard /></>)
    })
    expect(screen.getByTestId('value-performance.cpu.usage_percent').textContent).toBe('72')
    expect(screen.getByTestId('value-thermal.temperature_c').textContent).toBe('UNSUPPORTED')
    expect(screen.getByTestId('value-battery.charge_percent').textContent).toBe('NO DATA')
    expect(screen.queryByText('0°C')).toBeNull()
    expect(document.querySelector('[data-field="performance.cpu.usage_percent"]')?.getAttribute('data-visual')).toBe('elevated')
    expect(screen.getByText('Needs LibreHardwareMonitor')).toBeInTheDocument()
  })

  it('visual state follows patches (CPU 42% -> 97% critical) without a reload', async () => {
    useTwinDoc.getState().applySnapshot(snapshot(1, {
      'connectivity.status': 'ONLINE',
      'performance.cpu.usage_percent': field(42),
      'sections.cpu': { visual: 'normal' },
    }))
    await act(async () => {
      render(<TwinMetricBoard />)
    })
    const card = () => document.querySelector('[data-field="performance.cpu.usage_percent"]')
    expect(card()?.getAttribute('data-visual')).toBe('normal')
    act(() => {
      useTwinDoc.getState().applyPatch({ device_id: 'dev', twin_version: 2, base_version: 1, epoch: 'e1', changes: {
        'performance.cpu.usage_percent': field(97, { status: 'critical' }), 'sections.cpu': { visual: 'critical' },
      } })
    })
    expect(screen.getByTestId('value-performance.cpu.usage_percent').textContent).toBe('97')
    expect(card()?.getAttribute('data-visual')).toBe('critical')
  })

  it('formats real values only', () => {
    expect(formatField(field(null))).toBeNull()
    expect(formatField(field(3 * 1024 ** 3, { unit: 'bytes' }))).toEqual({ value: '3.0', unit: 'GB' })
    expect(formatField(field(true, { unit: 'bool' }))).toEqual({ value: 'On', unit: '' })
  })
})

describe('device scope', () => {
  afterEach(() => deviceScope.set(null))
  it('adds device_id to device-scoped GET requests only', () => {
    deviceScope.set('dev-9')
    expect(scopePath('/api/v1/telemetry/history?keys=a')).toBe('/api/v1/telemetry/history?keys=a&device_id=dev-9')
    expect(scopePath('/api/v1/twin')).toBe('/api/v1/twin?device_id=dev-9')
    expect(scopePath('/api/v1/device/list')).toBe('/api/v1/device/list')
    expect(scopePath('/api/v1/devices')).toBe('/api/v1/devices')
    expect(scopePath('/api/v1/anomalies/abc/analysis')).toBe('/api/v1/anomalies/abc/analysis')
    expect(scopePath('/api/v1/anomalies/x/acknowledge', 'POST')).toBe('/api/v1/anomalies/x/acknowledge')
  })
})
