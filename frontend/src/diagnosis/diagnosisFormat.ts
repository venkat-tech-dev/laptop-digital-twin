import type { ConfidenceLevel } from '../types/diagnosis'
import type { ChipTone } from '../ui/primitives'

/** Chip tone of a platform confidence level (HIGH = accent, MEDIUM = amber, otherwise muted). */
export function levelTone(level: ConfidenceLevel | string | null | undefined): ChipTone {
  return level === 'HIGH' ? 'accent' : level === 'MEDIUM' ? 'amber' : 'muted'
}
