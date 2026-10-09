import { usePrefs } from '../stores/prefsStore'

const ID_KEYS = /^(device_id|serial_number|mac_address|model_number|luid)$/i

function anonymize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(anonymize)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value)) out[k] = ID_KEYS.test(k) && typeof v === 'string' ? '•••' : anonymize(v)
    return out
  }
  return value
}

function stripProcessNames(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripProcessNames)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value)) {
      if (k === 'processes' && Array.isArray(v)) {
        out[k] = v.map((p) => (p && typeof p === 'object' ? { ...(p as object), name: '(excluded)' } : p))
      } else out[k] = stripProcessNames(v)
    }
    return out
  }
  return value
}

/** Downloads JSON produced from real data, honouring the export privacy preferences. */
export function exportJson(name: string, data: unknown): void {
  const prefs = usePrefs.getState().saved
  let payload: unknown = { exported_at: new Date().toISOString(), source: 'Laptop Digital Twin (live telemetry)', data }
  if (prefs.anonymizeExports) payload = anonymize(payload)
  if (!prefs.includeProcessNamesInExports) payload = stripProcessNames(payload)
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${name}-${new Date().toISOString().replace(/[:.]/g, '-')}.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
