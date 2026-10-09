import { AlertsPage } from './pages/AlertsPage'
import { RemediationPage } from './pages/RemediationPage'
import { useEffect, useState, type FormEvent, type ReactNode } from 'react'

import { AccountGate, MfaSetupGate } from './app/AccountGate'
import { DeviceBar } from './app/DeviceBar'
import { ProvenanceBar } from './app/ProvenanceBar'
import { useRoute, type RouteId } from './app/routes'
import { Sidebar } from './app/Sidebar'
import { ErrorBoundary } from './components/ErrorBoundary'
import { useTwinConnection } from './hooks/useTwinConnection'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { ComponentIntelligencePage } from './pages/ComponentIntelligencePage'
import { DigitalTwinPage } from './pages/DigitalTwinPage'
import { ExecutiveOverviewPage } from './pages/ExecutiveOverviewPage'
import { FleetPage } from './pages/FleetPage'
import { FleetOperationsPage } from './pages/FleetOperationsPage'
import { HardwareInventoryPage } from './pages/HardwareInventoryPage'
import { HealthAnomaliesPage } from './pages/HealthAnomaliesPage'
import { LiveTelemetryPage } from './pages/LiveTelemetryPage'
import { OrganizationPage } from './pages/OrganizationPage'
import { SettingsPage } from './pages/SettingsPage'
import { SimulationPage } from './pages/SimulationPage'
import { SystemProcessesPage } from './pages/SystemProcessesPage'
import './stores/processHistory'
import { useTwinStore } from './stores/twinStore'

function KeyGate({ onSubmit }: { onSubmit: (key: string) => void }) {
  const [key, setKey] = useState('')
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (key.trim()) onSubmit(key.trim())
  }
  return (
    <form className="gate" onSubmit={submit}>
      <p className="panel-title">API key required</p>
      <p className="note">This backend runs with authentication enabled (AUTH_MODE). Enter one of the configured API_KEYS. It is kept in this tab's session storage only.</p>
      <input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} aria-label="API key" />
      <button type="submit" className="btn btn--primary">Connect</button>
    </form>
  )
}

const PAGES: Record<RouteId, (param: string | null) => ReactNode> = {
  fleet: () => <FleetPage />,
  operations: (p) => <FleetOperationsPage param={p} />,
  overview: () => <ExecutiveOverviewPage />,
  twin: (p) => <DigitalTwinPage initial={p} />,
  components: (p) => <ComponentIntelligencePage initial={p} />,
  telemetry: () => <LiveTelemetryPage />,
  health: () => <HealthAnomaliesPage />,
  alerts: (p) => <AlertsPage param={p} />,
  remediation: (p) => <RemediationPage param={p} />,
  analytics: () => <AnalyticsPage />,
  simulation: (p) => <SimulationPage preset={p} />,
  inventory: () => <HardwareInventoryPage />,
  processes: () => <SystemProcessesPage />,
  organization: (p) => <OrganizationPage section={p} />,
  settings: (p) => <SettingsPage section={p} />,
}

export default function App() {
  const { route, param } = useRoute()
  const deviceKey = useTwinStore((s) => s.device?.device_id ?? null)
  const { boot, error, submitKey, signIn, signOut, setupTokenRequired } = useTwinConnection()

  useEffect(() => {
    const out = () => signOut()
    // organization switch: a new session token for another organization (same user)
    const session = (e: Event) => {
      const { token, expiresAt } = (e as CustomEvent<{ token: string; expiresAt: string }>).detail
      signIn(token, expiresAt)
    }
    window.addEventListener('ldt:unauthorized', out)
    window.addEventListener('ldt:signout', out)
    window.addEventListener('ldt:session', session)
    return () => {
      window.removeEventListener('ldt:unauthorized', out)
      window.removeEventListener('ldt:signout', out)
      window.removeEventListener('ldt:session', session)
    }
  }, [signOut, signIn])

  let content: ReactNode
  if (boot === 'needs_key') content = <KeyGate onSubmit={submitKey} />
  else if (boot === 'needs_login' || boot === 'needs_setup') {
    content = <AccountGate setup={boot === 'needs_setup'} tokenRequired={setupTokenRequired} onSession={signIn} />
  }
  else if (boot === 'needs_mfa_setup') content = <MfaSetupGate onDone={signOut} />
  else if (boot === 'error') {
    content = (
      <div className="gate">
        <p className="panel-title">Backend unreachable</p>
        <p className="note">{error}</p>
        <p className="note">Retrying… Start the backend (see README) and this page reconnects automatically.</p>
      </div>
    )
  } else if (boot === 'loading') content = <div className="gate"><p className="eyebrow">CONNECTING TO THE DIGITAL TWIN…</p></div>
  // keyed by device: switching devices remounts the page so every view reloads for that device
  else content = <ErrorBoundary key={`${route}:${deviceKey ?? ''}`} label="Page">{PAGES[route](param)}</ErrorBoundary>

  return (
    <div className="app">
      <Sidebar route={route} />
      <div className="workspace-content">
        <DeviceBar />
        <main className="page">{content}</main>
        <ProvenanceBar />
      </div>
    </div>
  )
}
