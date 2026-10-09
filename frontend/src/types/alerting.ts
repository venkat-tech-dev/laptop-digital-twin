/** Phase-6 alerting types (mirror of app/domain/alerting/models.py). */

export type AlertSeverity = 'INFO' | 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
export type AlertStatus = 'OPEN' | 'ONGOING' | 'ACKNOWLEDGED' | 'RESOLVED' | 'SUPPRESSED' | 'EXPIRED'
export type AlertCategory = 'anomaly' | 'prediction' | 'connectivity' | 'security' | 'system'
export type Channel = 'in_app' | 'browser' | 'windows' | 'email' | 'webhook'

export interface AuditEntry { at: string; actor: string; action: string; from_status: string | null; to_status: string | null; detail: string | null }

export interface AlertRecord {
  alert_id: string
  device_id: string
  event_id: string
  source_type: string
  alert_type: string
  category: AlertCategory
  severity: AlertSeverity
  title: string
  summary: string
  description: string | null
  status: AlertStatus
  priority: number
  confidence: number | null
  correlation_key: string | null
  first_detected_at: string
  last_updated_at: string
  acknowledged_at: string | null
  acknowledged_by: string | null
  resolved_at: string | null
  resolved_by: string | null
  suppressed_at: string | null
  suppressed_by: string | null
  suppressed_until: string | null
  escalation_level: number
  next_escalation_at: string | null
  occurrences: number
  metadata: {
    metric?: string | null
    threshold?: number | null
    observed?: number | null
    expected?: number | null
    duration_s?: number
    recurrence?: boolean
    evidence?: Record<string, unknown>
  }
  created_at: string
  updated_at: string
  audit?: AuditEntry[]
  deliveries?: { notification_id: string; user_id: string; channel: Channel; status: string; attempt_count: number; provider: string | null; delivered_at: string | null; read_at: string | null; failed_at: string | null; failure_reason: string | null; created_at: string; escalation_level: number }[]
}

export interface NotificationRecord {
  notification_id: string
  alert_id: string | null
  user_id: string
  device_id: string | null
  channel: Channel
  status: string
  priority: number
  severity: AlertSeverity
  category: AlertCategory
  title: string
  body: string
  payload: Record<string, unknown>
  delivered_at: string | null
  read_at: string | null
  created_at: string
  updated_at: string
}

export interface NotificationPage { items: NotificationRecord[]; unread: number; unread_by_severity: Record<string, number>; limit: number; offset: number; server_time: string }

export interface QuietHours { start: string; end: string; high: 'immediate' | 'defer'; medium: 'immediate' | 'defer' }
export interface NotificationPreferences {
  channels: Channel[]
  severities: AlertSeverity[]
  categories: AlertCategory[]
  frequency: 'immediate' | 'grouped' | 'digest'
  digest_hour: number
  timezone: string
  quiet_hours: QuietHours | null
  email: string | null
}
export interface PreferencesResponse {
  preferences: NotificationPreferences
  channels: Record<Channel, { available: boolean; reason: string | null }>
  frequencies: string[]
  safeguards: { critical_always_in_app: boolean }
}

export const SEVERITY_ORDER: AlertSeverity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO']
export const CATEGORY_LABEL: Record<AlertCategory, string> = {
  anomaly: 'Anomaly', prediction: 'Prediction', connectivity: 'Connectivity', security: 'Security', system: 'System',
}
export const STATUS_LABEL: Record<AlertStatus, string> = {
  OPEN: 'Open', ONGOING: 'Ongoing', ACKNOWLEDGED: 'Acknowledged', RESOLVED: 'Resolved', SUPPRESSED: 'Suppressed', EXPIRED: 'Expired',
}
