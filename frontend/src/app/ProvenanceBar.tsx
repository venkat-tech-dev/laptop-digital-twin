import { useTwinStore } from '../stores/twinStore'
import { useCoverage, utcClock, utcDate } from './derive'

export function ProvenanceBar() {
  const coverage = useCoverage()
  const last = useTwinStore((s) => s.lastTelemetryAt)
  const providers = coverage.providers.filter((p) => p.available > 0).map((p) => p.label)
  return (
    <footer className="provenance">
      <p>LIVE TELEMETRY / {providers.length ? providers.join(' + ') : 'no provider reporting'} / UTC</p>
      <p>
        {last ? `SNAPSHOT ${utcDate(last)} · ${utcClock(last).replace(' UTC', '')}` : 'NO SNAPSHOT'} / {coverage.available} SENSORS AVAILABLE
      </p>
    </footer>
  )
}
