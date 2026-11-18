/**
 * What an event records, and which events end a task.
 *
 * ONE LEGACY SHAPE READS DIFFERENTLY FROM ITS FIELD. Until 2026-09-24 the API
 * wrote a flag-only cancel -- a task that still held its lease, so nothing had
 * been cancelled -- as `type: 'cancelled'` with `detail.phase:
 * 'cancel_requested'`. Contract request 17 gave that its own type,
 * `cancel_requested`, which the API writes now and which `swarm_api.codec`
 * serves for stored history too. This screen can still meet the old shape from
 * an API image older than itself, mid-rollout, so it reads it the same way.
 * Only that shape: a `cancelled` with `phase: 'cancelled'`, or with no phase
 * (the scheduler's cascade, the worker, the reconciler), is a real cancel.
 */

import type { TaskEvent } from './types'

/**
 * The event each terminal transition writes -- `control.py`'s state-to-event
 * map, plus the scheduler's cascade cancel and the API's own immediate one. A
 * terminal task has one by construction, so a terminal task whose page
 * carries none is proof the page ends before the run did.
 *
 * `cancel_requested` is deliberately absent: a request is not an ending.
 * tests/unit/control_plane/test_cancel_request_is_not_a_cancel.py asserts
 * this set against the frozen terminal states.
 */
export const TERMINAL_EVENTS: ReadonlySet<string> = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'dead_lettered',
])

/** The type this event records. */
export function eventKind(e: Pick<TaskEvent, 'type' | 'detail'>): string {
  if (e.type === 'cancelled' && e.detail?.['phase'] === 'cancel_requested') return 'cancel_requested'
  return e.type
}

/** Whether this event is the one a terminal transition writes. */
export function isTerminalEvent(e: Pick<TaskEvent, 'type' | 'detail'>): boolean {
  return TERMINAL_EVENTS.has(eventKind(e))
}
