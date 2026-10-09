import { useLiveStatus } from '../hooks/useLiveStatus'
import { useTwinValue } from '../stores/twinDocStore'
import { useTwinStore } from '../stores/twinStore'
import { Icon } from '../ui/primitives'
import { deviceSubtitle, deviceTitle, useStreamStats } from './derive'
import { CONNECTIVITY_LABEL, CONNECTIVITY_TONE } from '../twin/format'
import type { Connectivity } from '../types/twinDoc'
import { NotificationBell } from '../alerting/NotificationBell'
import { DeviceSwitcher } from './DeviceSwitcher'
import { navigate } from './routes'

/** Top bar: device identity, honest live status, measured cadence, connection, alerts. */
export function DeviceBar() {
  const device = useTwinStore((s) => s.device)
  const owner = useTwinValue<string>('identity.owner')
  const connectivity = useTwinValue<Connectivity>('connectivity.status')
  const { status, ageMs } = useLiveStatus()
  const stream = useStreamStats()
  const tone = status === 'LIVE' ? '' : status === 'DEGRADED' ? 'chip--amber' : 'chip--critical'

  return (
    <header className="device-bar">
      <DeviceSwitcher>
        <Icon name="laptop" size={22} style={{ color: 'var(--text-2)' }} />
        <span className="device-identity__meta">
          <span className="device-identity__name">{owner ? `${deviceTitle(device)} · ${owner}` : deviceTitle(device)}</span>
          <span className="device-identity__sub">{deviceSubtitle(device)}</span>
        </span>
        <Icon name="chevronDown" size={12} style={{ color: 'var(--text-2)' }} />
      </DeviceSwitcher>
      <div className="device-bar__status">
        {connectivity ? (
          <span className={`chip ${CONNECTIVITY_TONE[connectivity] === 'accent' ? '' : `chip--${CONNECTIVITY_TONE[connectivity]}`}`} role="status" aria-live="polite"
            title="Agent heartbeat + telemetry (digital twin connectivity)">
            <span className="chip__dot" />
            {CONNECTIVITY_LABEL[connectivity]} · {connectivity === 'OFFLINE' || connectivity === 'UNKNOWN' ? 'NO STREAM' : 'REAL HARDWARE'}
          </span>
        ) : (
          <span className={`chip ${tone}`} role="status" aria-live="polite">
            <span className="chip__dot" />
            {status} · {status === 'OFFLINE' ? 'NO STREAM' : 'REAL HARDWARE'}
          </span>
        )}
        <p className="device-bar__cadence">
          {stream.cadenceMs ? `${Math.round(stream.cadenceMs / 50) * 50} ms cadence` : 'cadence —'} ·{' '}
          {ageMs === null ? 'no data' : `${(ageMs / 1000).toFixed(1)} s ago`}
        </p>
        <button type="button" className="btn" onClick={() => navigate('settings')}>
          <Icon name="radio" />
          Connection
        </button>
        <span className="device-bar__divider" />
        <NotificationBell />
      </div>
    </header>
  )
}
