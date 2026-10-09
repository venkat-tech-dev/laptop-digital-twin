import { useEffect, useMemo, useState } from 'react'

import { utcClock } from '../app/derive'
import { summarize } from '../app/liveData'
import { useApi } from '../hooks/useApi'
import { api } from '../services/api'
import { useTwinStore } from '../stores/twinStore'
import type { SimulationResult } from '../types/telemetry'
import { humanizeState } from '../utils/format'
import { PageHeading } from '../ui/PageHeading'
import { Button, Chip, DataTable, Panel, PanelHeading, Spec } from '../ui/primitives'
import { TimeSeries } from '../ui/TimeSeries'

const DURATIONS = [10, 30, 60, 120]
const SAVED_KEY = 'ldt.scenarios'
const SCENARIO_NAMES: Record<string, string> = {
  cpu_intensive: 'CPU-intensive workload',
  gpu_intensive: 'GPU-intensive workload',
  ram_intensive: 'Memory-intensive workload',
  gaming: 'Gaming session',
  ai_ml: 'AI / ML workload',
}
const scenarioName = (id: string) => SCENARIO_NAMES[id] ?? humanizeState(id)

interface Inputs {
  scenario: string
  minutes: number
  cpu: number
  gpu: number
  ram: number
  onBattery: boolean
  ambient: number
  profile: ThermalProfile
}

type ThermalProfile = 'quiet' | 'balanced' | 'performance'
const PROFILES: { id: ThermalProfile; label: string }[] = [
  { id: 'quiet', label: 'Quiet' },
  { id: 'balanced', label: 'Balanced' },
  { id: 'performance', label: 'Performance' },
]

function Slider({ value, min, max, step, onChange, label }: { value: number; min: number; max: number; step: number; onChange: (v: number) => void; label: string }) {
  return (
    <input className="range" type="range" min={min} max={max} step={step} value={value} aria-label={label}
      style={{ ['--fill' as string]: `${((value - min) / (max - min)) * 100}%` }} onChange={(e) => onChange(Number(e.target.value))} />
  )
}

export function SimulationPage({ preset }: { preset: string | null }) {
  const components = useTwinStore((s) => s.components)
  const scenarios = useApi(() => api.scenarios(), [])
  const [inputs, setInputs] = useState<Inputs | null>(null)
  const [menu, setMenu] = useState(false)
  const [result, setResult] = useState<SimulationResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const s = summarize(components)

  const apply = (id: string, base?: Partial<Inputs>) => {
    const sc = scenarios.data?.find((x) => x.scenario === id)
    if (!sc) return
    setInputs({
      scenario: id, minutes: base?.minutes ?? 30, cpu: Math.round(sc.cpu_load * 100), gpu: Math.round(sc.gpu_load * 100), ram: sc.ram_gb,
      onBattery: sc.on_battery, ambient: base?.ambient ?? 25, profile: base?.profile ?? 'balanced',
    })
  }

  useEffect(() => {
    if (scenarios.data && !inputs) apply(preset && scenarios.data.some((x) => x.scenario === preset) ? preset : 'ai_ml')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scenarios.data])

  const run = async () => {
    if (!inputs) return
    setBusy(true)
    setError(null)
    try {
      setResult(await api.simulate({
        scenario: inputs.scenario, duration_minutes: inputs.minutes, cpu_load: inputs.cpu / 100, gpu_load: inputs.gpu / 100, ram_gb: inputs.ram,
        on_battery: inputs.onBattery, ambient_c: inputs.ambient, thermal_profile: inputs.profile,
      }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const save = () => {
    if (!inputs) return
    try {
      const list = JSON.parse(localStorage.getItem(SAVED_KEY) ?? '[]') as unknown[]
      localStorage.setItem(SAVED_KEY, JSON.stringify([{ ...inputs, saved_at: new Date().toISOString() }, ...list].slice(0, 20)))
      setToast('Scenario saved in this browser')
    } catch {
      setToast('Could not save: browser storage unavailable')
    }
    setTimeout(() => setToast(null), 2500)
  }

  const rows = useMemo(() => {
    if (!result) return []
    const c = result.current
    const p = result.predicted
    const memTotal = s.memTotal ? s.memTotal / 1024 ** 3 : null
    const f = (v: unknown, unit: string, d = 0) => (typeof v === 'number' ? `${v.toFixed(d)}${unit}` : '—')
    const diff = (a: unknown, b: unknown, unit: string, d = 0) => (typeof a === 'number' && typeof b === 'number' ? `${b - a >= 0 ? '+' : '−'}${Math.abs(b - a).toFixed(d)}${unit}` : '—')
    const memNow = typeof c.memory_used_gb === 'number' ? c.memory_used_gb : null
    const memPred = typeof p.memory_used_gb === 'number' ? p.memory_used_gb : null
    return [
      { m: 'CPU utilization', a: f(c.cpu_usage_percent, '%'), b: f(p.cpu_usage_percent, '%'), d: diff(c.cpu_usage_percent, p.cpu_usage_percent, ' pts') },
      { m: 'GPU utilization', a: f(c.gpu_usage_percent, '%'), b: f(p.gpu_usage_percent, '%'), d: diff(c.gpu_usage_percent, p.gpu_usage_percent, ' pts') },
      { m: 'Memory in use', a: f(memNow, ' GB', 1), b: `${f(memPred, ' GB', 1)}${memTotal && memPred !== null && memPred >= memTotal * 0.96 ? ' (limit)' : ''}`, d: diff(memNow, memPred, ' GB', 1) },
      { m: c.temperature_sensor && String(c.temperature_sensor).includes('ACPI') ? 'CPU-area temperature (ACPI zone)' : 'CPU package temperature', a: f(c.temperature_c, '°C'), b: f(p.temperature_c, '°C'), d: diff(c.temperature_c, p.temperature_c, '°C') },
      { m: 'Estimated package power', a: f(c.estimated_package_power_w, ' W', 1), b: f(p.estimated_package_power_w, ' W', 1), d: diff(c.estimated_package_power_w, p.estimated_package_power_w, ' W', 1) },
      { m: 'CPU temperature 95% interval', a: '—', b: typeof p.temperature_low_c === 'number' && typeof p.temperature_high_c === 'number' ? `${p.temperature_low_c.toFixed(0)}–${p.temperature_high_c.toFixed(0)}°C` : '—', d: typeof p.temperature_sigma_c === 'number' ? `σ ${p.temperature_sigma_c.toFixed(1)}°C` : '—' },
      { m: 'Fan duty (estimated)', a: s.fanRpm !== null ? `${s.fanRpm.toFixed(0)} RPM` : 'RPM not exposed', b: f(p.fan_duty_percent_est, '%'), d: '—' },
      p.power_source === 'battery'
        ? { m: 'Battery at end / runtime', a: f(c.battery_percent, '%'), b: `${f(p.battery_percent_at_end, '%')} / ${f(p.battery_runtime_h, ' h', 1)}${typeof p.battery_runtime_h_low === 'number' ? ` (${p.battery_runtime_h_low.toFixed(1)}–${f(p.battery_runtime_h_high, ' h', 1)})` : ''}`, d: diff(c.battery_percent, p.battery_percent_at_end, ' pts') }
        : { m: 'Battery at end / charging power', a: f(c.battery_percent, '%'), b: `${f(p.battery_percent_at_end, '%')} / ${f(p.charge_power_w, ' W', 0)}`, d: diff(c.battery_percent, p.battery_percent_at_end, ' pts') },
    ]
  }, [result, s.memTotal, s.fanRpm])

  const traj = result?.trajectory ?? []
  const t0 = 0
  const t1 = (result?.duration_s ?? 1800) * 1000
  const tempNow = typeof result?.current.temperature_c === 'number' ? result.current.temperature_c : null
  const predTemp = traj.filter((p) => p.temperature_c !== undefined).map((p) => ({ t: p.t_s * 1000, v: p.temperature_c as number }))
  const bandLow = traj.filter((p) => p.temperature_low_c !== undefined).map((p) => ({ t: p.t_s * 1000, v: p.temperature_low_c as number }))
  const bandHigh = traj.filter((p) => p.temperature_high_c !== undefined).map((p) => ({ t: p.t_s * 1000, v: p.temperature_high_c as number }))
  const baseline = tempNow !== null ? [{ t: t0, v: tempNow }, { t: t1, v: tempNow }] : []
  const sc = scenarios.data?.find((x) => x.scenario === inputs?.scenario)

  return (
    <>
      <PageHeading title="What-If Simulation" subtitle="Explore workload impact without changing the physical device."
        action={<Button icon="arrowRight" onClick={save} disabled={!inputs}>Save scenario</Button>} />

      <div className="sim-boundary">
        <Chip>SIMULATION</Chip>
        <p>Predicted values are model estimates, not live measurements. No workload or configuration is applied to the laptop.</p>
        <p className="sim-boundary__meta">FIRST-ORDER THERMAL MODEL / GENERATED DATA</p>
      </div>

      <div className="split split--rev">
        <div className="sim-config">
          <Panel>
            <PanelHeading eyebrow="INPUTS" title="Workload scenario" />
            <p className="note">Preset</p>
            <div className="dropdown">
              <Button primary icon="chevronDown" onClick={() => setMenu(!menu)} disabled={!scenarios.data}>{inputs ? scenarioName(inputs.scenario) : 'Loading…'}</Button>
              {menu ? (
                <div className="dropdown__menu" role="listbox">
                  {(scenarios.data ?? []).map((x) => (
                    <button key={x.scenario} type="button" role="option" aria-selected={x.scenario === inputs?.scenario} className="dropdown__item"
                      onClick={() => { apply(x.scenario, { minutes: inputs?.minutes, ambient: inputs?.ambient, profile: inputs?.profile }); setMenu(false) }}>
                      <span>{scenarioName(x.scenario)}</span>
                      <span className="note--muted note">{x.description}</span>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
            {inputs ? (
              <>
                <button type="button" className="spec spec--button" onClick={() => setInputs({ ...inputs, minutes: DURATIONS[(DURATIONS.indexOf(inputs.minutes) + 1) % DURATIONS.length] })}>
                  <span className="spec__label">Duration</span><span className="spec__value">{inputs.minutes} minutes</span>
                </button>
                <Spec label="CPU target load" value={`${inputs.cpu}%`} />
                <Slider value={inputs.cpu} min={0} max={100} step={5} label="CPU target load" onChange={(v) => setInputs({ ...inputs, cpu: v })} />
                <Spec label="GPU target load" value={`${inputs.gpu}%`} />
                <Slider value={inputs.gpu} min={0} max={100} step={5} label="GPU target load" onChange={(v) => setInputs({ ...inputs, gpu: v })} />
                <Spec label="Additional memory" value={`${inputs.ram.toFixed(1)} GB`} />
                <Slider value={inputs.ram} min={0} max={16} step={0.5} label="Additional memory" onChange={(v) => setInputs({ ...inputs, ram: v })} />
                <Spec label="Ambient temperature" value={`${inputs.ambient}°C`} />
                <Slider value={inputs.ambient} min={10} max={40} step={1} label="Ambient temperature" onChange={(v) => setInputs({ ...inputs, ambient: v })} />
                <button type="button" className="spec spec--button" onClick={() => setInputs({ ...inputs, onBattery: !inputs.onBattery })}>
                  <span className="spec__label">Power source</span><span className="spec__value">{inputs.onBattery ? 'Battery' : 'AC'}</span>
                </button>
                <div className="spec">
                  <p className="spec__label">Thermal profile</p>
                  <div className="segmented" role="radiogroup" aria-label="Thermal profile">
                    {PROFILES.map((pr) => (
                      <button key={pr.id} type="button" role="radio" aria-checked={inputs.profile === pr.id}
                        className={`segmented__item ${inputs.profile === pr.id ? 'is-active' : ''}`} onClick={() => setInputs({ ...inputs, profile: pr.id })}>{pr.label}</button>
                    ))}
                  </div>
                </div>
              </>
            ) : null}
            <Button primary icon="play" onClick={run} disabled={busy || !inputs} style={{ alignSelf: 'flex-start' }}>{busy ? 'Simulating…' : 'Run simulation'}</Button>
            {error ? <p className="note" style={{ color: 'var(--amber)' }}>{error}</p> : null}
          </Panel>
          <Panel>
            <PanelHeading title="Model assumptions" />
            <p className="note">{sc?.description ? `${sc.description}. ` : ''}Starts from the live state captured when you run it. Load levels are held constant for the whole duration.</p>
            <Spec label="Confidence" value={result ? `${result.confidence} (${result.confidence_score})` : '—'} tone={result?.confidence === 'low' ? 'amber' : 'default'} />
            <Spec label="Temperature interval (95%)" value={typeof result?.predicted.temperature_sigma_c === 'number' ? `±${(1.96 * result.predicted.temperature_sigma_c).toFixed(1)}°C at steady state` : '—'}
              title="From the residuals of the live temperature-vs-load fit when calibrated, otherwise a documented heuristic (see assumptions)" />
            {(result?.assumptions ?? []).map((a) => <p key={a} className="note--muted note">• {a}</p>)}
            <p className="note--muted note">Estimates do not replace hardware validation.</p>
          </Panel>
        </div>

        <div className="sim-outcome">
          <Panel>
            <PanelHeading title="Current physical state vs. simulated workload" right={<Chip tone={result ? 'accent' : 'muted'}>{busy ? 'RUNNING' : result ? 'SIMULATION COMPLETE' : 'NOT RUN'}</Chip>} />
            <div className="compare-provenance">
              <p style={{ color: 'var(--accent)' }}>MEASURED BASELINE / {result?.baseline_captured_at ? utcClock(Date.parse(result.baseline_captured_at)).replace(' UTC', '') : '—'}</p>
              <p style={{ color: 'var(--secondary)' }}>PREDICTED / +{result ? Math.round(result.duration_s / 60) : inputs?.minutes ?? '—'} MINUTES</p>
            </div>
            <DataTable rowKey={(r) => r.m} rows={rows} empty="Run a simulation to compare the live baseline with the predicted state."
              columns={[
                { key: 'm', header: 'METRIC', width: 250, kind: 'primary', render: (r) => r.m },
                { key: 'a', header: 'MEASURED SAMPLE', width: 238, render: (r) => r.a },
                { key: 'b', header: 'PREDICTED ESTIMATE', width: 258, render: (r) => r.b },
                { key: 'd', header: 'CHANGE', grow: true, render: (r) => r.d },
              ]} />
          </Panel>
          <Panel>
            <PanelHeading title="Predicted thermal response" right={<p className="panel-meta">{result ? `${Math.round(result.duration_s / 60)} MIN HORIZON / ${result.confidence.toUpperCase()} CONFIDENCE` : 'NO RESULT'}</p>} />
            <TimeSeries series={[{ points: baseline }, { points: predTemp, tone: 'secondary', dashed: true, band: bandLow.length ? { low: bandLow, high: bandHigh } : undefined }]} start={t0} end={t1} height={105}
              axis={['NOW', `+${Math.round(t1 / 180_000)} MIN`, `+${Math.round((2 * t1) / 180_000)} MIN`, `+${Math.round(t1 / 60_000)} MIN`]}
              reference={{ value: 85, label: '85°C ADVISORY' }} maxGapMs={t1} showLatest={false} emptyText="RUN A SIMULATION" />
            <div className="legend-row">
              <p className="legend legend--primary">— MEASURED BASELINE {tempNow !== null ? `${tempNow.toFixed(0)}°C` : ''}</p>
              <p className="legend legend--secondary">- - PREDICTED TEMPERATURE {typeof result?.predicted.temperature_c === 'number' ? `${result.predicted.temperature_c.toFixed(0)}°C` : ''}</p>
              {bandLow.length ? <p className="legend legend--secondary">▒ 95% INTERVAL</p> : null}
            </div>
          </Panel>
          {result ? (
            <div className={`predicted-risk ${result.warnings.length ? '' : 'predicted-risk--ok'}`}>
              <div className="predicted-risk__head">
                <Chip tone={result.warnings.length ? 'amber' : 'accent'}>{result.warnings.length ? 'PREDICTED ADVISORY' : 'NO ADVISORY'}</Chip>
                <p className="predicted-risk__title">{result.warnings[0] ?? 'The model predicts the workload stays within thermal and memory limits.'}</p>
              </div>
              {result.warnings.slice(1).map((w) => <p key={w} className="note">{w}</p>)}
            </div>
          ) : null}
        </div>
      </div>
      {toast ? <div className="toast" role="status">{toast}</div> : null}
    </>
  )
}
