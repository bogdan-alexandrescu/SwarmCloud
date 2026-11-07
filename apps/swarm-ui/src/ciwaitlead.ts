import { MERGE_PROFILE } from './stepviews'
import type { Task } from './types'

/**
 * THE ONE LINE A RE-CHECKED MERGE STEP'S ATTEMPTS PAGE LEADS WITH (part of #2688).
 *
 * The merge step re-checks CI about every 16 minutes and each re-check is a
 * new attempt, so a step that waited 8 hours (owner, 2026-11-06) showed 28
 * identical attempt cards and nothing that said how long, how often, or why.
 *
 *   * A wait handed to a person (`park_phase` `ci_wait_stuck`, the worker's
 *     `merge._Run.hand_to_person`): the API's own line, `waiting.summary` --
 *     how long, how many re-checks, what fails, the repair rounds, main.
 *   * A CI wait still re-checking, two re-checks or more: its span from
 *     `merge_wait.wait_started_at` (across the heads its repair rounds
 *     pushed) and its re-check count, from the park's own record.
 *
 * Null for any other task: one attempt needs no summary.
 */
export interface CiWaitLead {
  line: string
  /** Handed to a person: the line is a warning. */
  stuck: boolean
  /** The wait's re-checks, when recorded. */
  rechecks: number | null
}

export const CI_STUCK_PHASE = 'ci_wait_stuck'

function rec(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function count(v: unknown): number | null {
  return typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null
}

/** "8 h 2 m", "40 m": the line's span, whole minutes. */
export function waitSpan(ms: number): string {
  const minutes = Math.max(0, Math.floor(ms / 60_000))
  const h = Math.floor(minutes / 60)
  return h > 0 ? `${h} h ${minutes % 60} m` : `${minutes} m`
}

export function ciWaitLead(task: Task | null, now: number): CiWaitLead | null {
  if (task === null || task.runner_profile !== MERGE_PROFILE || task.state !== 'PARKED') return null
  const waiting = rec(task.waiting)
  const detail = rec(waiting?.['detail'])
  const wait = rec(rec(task.metadata)?.['merge_wait'])
  const rechecks = count(wait?.['rechecks'])
  if (detail?.['park_phase'] === CI_STUCK_PHASE) {
    const summary = typeof waiting?.['summary'] === 'string' ? (waiting['summary'] as string) : ''
    if (summary === '') return null
    return { line: summary, stuck: true, rechecks: count(detail['re_checks']) ?? rechecks }
  }
  if (task.park_reason !== 'CI_PENDING' || rechecks === null || rechecks < 2) return null
  const started = typeof wait?.['wait_started_at'] === 'string' ? Date.parse(wait['wait_started_at'] as string) : NaN
  const failed = Object.keys(rec(wait?.['failed_heads']) ?? {}).sort()
  const parts = [
    Number.isNaN(started) ? `CI wait` : `CI wait ${waitSpan(now - started)}`,
    `${rechecks} re-checks`,
  ]
  if (failed.length > 0) parts.push(`failed: ${failed.slice(0, 3).join(', ')}${failed.length > 3 ? ', …' : ''}`)
  return { line: parts.join(' · '), stuck: false, rechecks }
}
