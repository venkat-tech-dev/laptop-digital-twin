import { useEffect, useState } from 'react'

import type { IconName } from '../ui/icons'

export type RouteId =
  | 'fleet'
  | 'operations'
  | 'overview'
  | 'twin'
  | 'components'
  | 'telemetry'
  | 'health'
  | 'alerts'
  | 'remediation'
  | 'analytics'
  | 'simulation'
  | 'inventory'
  | 'processes'
  | 'organization'
  | 'settings'

export const NAV: { id: RouteId; label: string; icon: IconName }[] = [
  { id: 'fleet', label: 'Devices & Fleet', icon: 'laptop' },
  { id: 'operations', label: 'Fleet Operations', icon: 'chartNoAxesCombined' },
  { id: 'overview', label: 'Executive Overview', icon: 'layoutDashboard' },
  { id: 'twin', label: 'Digital Twin', icon: 'box' },
  { id: 'components', label: 'Component Intelligence', icon: 'cpu' },
  { id: 'telemetry', label: 'Live Telemetry', icon: 'activity' },
  { id: 'health', label: 'Health & Anomalies', icon: 'shieldCheck' },
  { id: 'alerts', label: 'Alerts & Notifications', icon: 'bell' },
  { id: 'remediation', label: 'Remediation', icon: 'rotateCcw' },
  { id: 'analytics', label: 'Analytics', icon: 'chartNoAxesCombined' },
  { id: 'simulation', label: 'What-If Simulation', icon: 'flaskConical' },
  { id: 'inventory', label: 'Hardware Inventory', icon: 'layers' },
  { id: 'processes', label: 'System Processes', icon: 'listTree' },
  { id: 'organization', label: 'Organization', icon: 'columns2' },
  { id: 'settings', label: 'Settings', icon: 'settings2' },
]

function parse(): { route: RouteId; param: string | null } {
  const [, path = '', param] = location.hash.match(/^#\/([^/?]*)(?:\/([^?]*))?/) ?? []
  const route = NAV.some((n) => n.id === path) ? (path as RouteId) : 'overview'
  return { route, param: param ? decodeURIComponent(param) : null }
}

export function navigate(route: RouteId, param?: string): void {
  location.hash = `#/${route}${param ? `/${encodeURIComponent(param)}` : ''}`
}

export function useRoute(): { route: RouteId; param: string | null } {
  const [state, setState] = useState(parse)
  useEffect(() => {
    const on = () => setState(parse())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return state
}
