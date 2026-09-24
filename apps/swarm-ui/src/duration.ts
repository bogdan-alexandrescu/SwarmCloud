// WHERE AN ATTEMPT'S WALL CLOCK WENT: queue, cold start, run.
//
// redesign-v2 §9 measured the first real `claude-code` run
// (task_b208fc8542724268b5f4): 3m 09s between `dispatched` and `starting`,
// 18s of agent. DISPATCHED is where nearly all of that run's wall clock went,
// it is the state a poller almost always sees, and no screen told "waiting for
// a container" apart from "the agent is working". This module is the
// arithmetic that tells them apart; `charts/AttemptPhases.tsx` draws it.
//
// Pure: no React, no charting library, no DOM. Every rule that is about the
// DATA is here, where a test can reach it without rendering anything.
//
// THE BOUNDARIES, AND WHY THEY COME FROM THE ATTEMPT DOCUMENT.
//
//   queue       task.created_at (attempt 1) or the `ready` event that re-queued
//               the task (attempt 2+)  ->  attempt.created_at
//   cold start  attempt.created_at  ->  attempt.started_at
//   run         attempt.started_at  ->  attempt.completed_at
//
// `attempt.created_at` is written by the scheduler's `create_attempt`
// immediately after the lease is acquired and immediately before the backend
// is asked to run the container (scheduler/loop.py), so it IS admission.
// `started_at` is set by the worker's DISPATCHED -> STARTING transition
// (control.py) and `completed_at` by `finish()`. All three are on the attempt
// document, which `GET /v1/tasks/{id}/attempts` serves whole.
//
// The events route does not: it orders oldest-first, caps the page and returns
// no page token, so on a long run the TAIL is what falls off -- the terminal
// event, the last checkpoints, the last heartbeats (redesign-v2 §4, seam S1).
// A phase bar built from `/events` would therefore show a long agent's
// beginning and imply it never finished. So the events contribute only what
// the attempt document cannot: the `ready` instant that starts a retry's
// queue, the `dispatched` tick inside a cold start, and a LOWER BOUND for a
// segment that has no end.
//
// THE HONESTY RULE THIS MODULE EXISTS FOR. An interval with no recorded end is
// OPEN. It is never closed at "now", never closed at the last event, never
// given a guessed end. Until the events route is paged (another lane's seam),
// the most this page can say about an open segment is "at least this long, as
// far as the events on this page show" -- and that lower bound is carried
// SEPARATELY from the measured total, never summed into it.

import { formatDuration, TERMINAL_STATES, type AttemptRow, type Task, type TaskEvent } from './types'

export type Phase = 'queue' | 'cold' | 'run'

/** The words for each phase, once, so the legend and every title agree. */
export const PHASE_LABEL: Readonly<Record<Phase, string>> = {
  queue: 'queue',
  cold: 'cold start',
  run: 'run',
}

/**
 * One phase of one attempt.
 *
 * Offsets are milliseconds from ADMISSION (`attempt.created_at`), so every
 * attempt's bar shares one origin and the queue extends to the left of it.
 *
 *   closed  both ends were recorded. `ms` is a measurement and may be 0.
 *   open    the start was recorded and the end was not. `seen` is the newest
 *           instant this page holds for the attempt -- a LOWER bound on the
 *           end, never an end. `seen === from` when the page holds nothing
 *           newer, and then there is no lower bound beyond the start at all.
 *   absent  a boundary is unknown or unusable, so no extent is claimed.
 *           `why` is the sentence; it is required so an absence cannot be
 *           built without its reason.
 */
export type Segment =
  | { readonly kind: 'closed'; readonly phase: Phase; readonly from: number; readonly to: number; readonly ms: number }
  | {
      readonly kind: 'open'
      readonly phase: Phase
      readonly from: number
      readonly seen: number
      readonly atLeastMs: number
      /** Still going, or over with its end never written. Two different facts. */
      readonly live: boolean
      readonly why: string
    }
  | { readonly kind: 'absent'; readonly phase: Phase; readonly why: string }

export interface AttemptPhases {
  readonly attempt: AttemptRow
  /** 1-based, oldest first. */
  readonly ordinal: number
  readonly queue: Segment
  readonly cold: Segment
  /**
   * Null when the attempt never started: it has no run phase at all. That is
   * a fact about the attempt (nothing ran), not a missing measurement, and it
   * is kept apart from `absent` so the two are never drawn alike.
   */
  readonly run: Segment | null
  /** `dispatched` event, as an offset from admission, when it is on the page. */
  readonly dispatchedAt: number | null
  /** True when the attempt is over and `started_at` was never written. */
  readonly neverRan: boolean
}

export interface PhaseRows {
  readonly rows: AttemptPhases[]
  /** Attempts whose `created_at` did not parse: no origin, so not drawn. */
  readonly undatable: number
}

/** Epoch ms, or null when the string is not an instant. Never 0. */
export function instant(iso: string | null | undefined): number | null {
  if (typeof iso !== 'string' || iso === '') return null
  const t = Date.parse(iso)
  return Number.isFinite(t) ? t : null
}

/**
 * A span as a person reads it. A MEASURED sub-second span keeps its
 * milliseconds.
 *
 * `formatDuration` rounds to whole seconds, which turns the §9 example's
 * `starting -> running +0.1s` into "0s" -- a measured non-zero printed as a
 * zero, the same lie `usdText` refuses for a sub-cent cost, arriving from
 * the other direction. Only a span that IS zero prints "0s".
 */
export function spanText(ms: number): string {
  if (!Number.isFinite(ms)) return '—'
  const a = Math.abs(ms)
  const sign = ms < 0 ? '−' : ''
  if (a === 0) return '0s'
  return `${sign}${formatDuration(a)}`
}

/**
 * WHAT COUNTS AS EVIDENCE THAT A PHASE WAS STILL GOING, and why it is a list.
 *
 * An open segment's lower bound has to be an instant at which the phase was
 * demonstrably still in progress. "The newest event for this attempt" is NOT
 * that: a reconciler that reclaims a dead worker writes its event minutes
 * after the worker stopped, and taking that as "ran at least until here"
 * would stretch a dead attempt by exactly the reclaim delay. So a run is
 * bounded only by what a LIVE worker emits, and a cold start only by what the
 * scheduler writes while the container has not reported in.
 */
const ALIVE_IN: Readonly<Record<'cold' | 'run', ReadonlySet<string>>> = {
  cold: new Set(['lease_acquired', 'dispatched']),
  run: new Set([
    'starting',
    'running',
    'heartbeat',
    'checkpoint_started',
    'checkpoint_completed',
    'checkpoint_restored',
  ]),
}

/** The newest instant on the page at which this attempt was still in `phase`. */
function newestFor(
  attemptId: string,
  phase: 'cold' | 'run',
  events: readonly TaskEvent[] | null,
): number | null {
  let best: number | null = null
  for (const e of events ?? []) {
    if (e.attempt_id !== attemptId || (false && !ALIVE_IN[phase].has(e.type))) continue
    const t = instant(e.at)
    if (t !== null && (best === null || t > best)) best = t
  }
  return best
}

function firstOfType(
  attemptId: string,
  type: string,
  events: readonly TaskEvent[] | null,
): number | null {
  let best: number | null = null
  for (const e of events ?? []) {
    if (e.type !== type || e.attempt_id !== attemptId) continue
    const t = instant(e.at)
    if (t !== null && (best === null || t < best)) best = t
  }
  return best
}

/**
 * The instant the task was put back in line for THIS attempt: the newest
 * `ready` event after the previous admission and at or before this one.
 * Null when no such event is on the page.
 */
function requeuedAt(
  after: number,
  upTo: number,
  events: readonly TaskEvent[] | null,
): number | null {
  let best: number | null = null
  for (const e of events ?? []) {
    if (e.type !== 'ready') continue
    const t = instant(e.at)
    if (t === null || t <= after || t > upTo) continue
    if (best === null || t > best) best = t
  }
  return best
}

const CLOCKS =
  'The recorded end of this interval is earlier than its recorded start, so the two instants disagree and no duration is drawn for it.'

function closedOrAbsent(phase: Phase, from: number, to: number, origin: number): Segment {
  if (to < from) return { kind: 'absent', phase, why: CLOCKS }
  return { kind: 'closed', phase, from: from - origin, to: to - origin, ms: to - from }
}

function open(
  phase: Phase,
  from: number,
  newest: number | null,
  origin: number,
  live: boolean,
  why: string,
): Segment {
  // The lower bound is only ever a RECORDED instant, and only when it is
  // later than the start. The client's own clock is not a measurement of the
  // attempt, so `Date.now()` never appears here: an attempt that was killed an
  // hour ago would otherwise grow by an hour every time the page is opened.
  const seen = newest !== null && newest > from ? newest : from
  return {
    kind: 'open',
    phase,
    from: from - origin,
    seen: seen - origin,
    atLeastMs: seen - from,
    live,
    why,
  }
}

/**
 * Has this attempt ended, on the evidence this page holds.
 *
 * The same three facts `attemptEnd` in AgentDetail.tsx reads, for the same
 * reason: `completed_at` is written only by `finish()`, so a SIGKILL or a
 * reconciler reclaim leaves it null for ever. A later attempt existing, or the
 * task being terminal, is what says such an attempt is over.
 */
function isOver(a: AttemptRow, task: Task, isLatest: boolean): boolean {
  return a.completed_at !== null || !isLatest || TERMINAL_STATES.has(task.state)
}

/**
 * Every attempt, oldest first, split into its three phases.
 *
 * `attempts` may arrive in any order; they are sorted on `created_at`
 * (ISO-8601 UTC, so lexicographic), which is the order they happened in.
 */
export function phasesFor(
  task: Task,
  attempts: readonly AttemptRow[],
  events: readonly TaskEvent[] | null,
): PhaseRows {
  const ordered = [...attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  const rows: AttemptPhases[] = []
  let undatable = 0
  let previousAdmission: number | null = null
  const eventsRead = events !== null

  ordered.forEach((a, i) => {
    const admitted = instant(a.created_at)
    if (admitted === null) {
      undatable += 1
      return
    }
    const isLatest = i === ordered.length - 1
    const over = isOver(a, task, isLatest)
    const coldSeen = newestFor(a.attempt_id, 'cold', events)
    const runSeen = newestFor(a.attempt_id, 'run', events)

    // QUEUE. The first attempt waited from submission; a retry waited from
    // the `ready` event that put the task back in line. With no such event on
    // this page, a retry's queue has no start and is drawn as an absence --
    // NOT as starting at the previous attempt's end, which would fold the
    // retry back-off and any park into "queue".
    let queue: Segment
    if (i === 0) {
      const submitted = instant(task.created_at)
      queue =
        submitted === null
          ? { kind: 'absent', phase: 'queue', why: 'The task’s submission time did not parse, so the wait before admission has no start.' }
          : closedOrAbsent('queue', submitted, admitted, admitted)
    } else {
      const from = requeuedAt(previousAdmission ?? -Infinity, admitted, events) ?? previousAdmission
      queue =
        from !== null
          ? closedOrAbsent('queue', from, admitted, admitted)
          : {
              kind: 'absent',
              phase: 'queue',
              why: eventsRead
                ? 'The ready event that put the task back in line for this attempt is not on this page of events, so the wait before admission has no start.'
                : 'The event read failed, so the ready event that put the task back in line for this attempt could not be looked for.',
            }
    }
    previousAdmission = admitted

    // COLD START, and the attempt that never started.
    const started = instant(a.started_at)
    let cold: Segment
    let run: Segment | null = null
    let neverRan = false
    if (started !== null) {
      cold = closedOrAbsent('cold', admitted, started, admitted)
    } else if (a.started_at !== null) {
      cold = { kind: 'absent', phase: 'cold', why: 'The worker start time did not parse, so where admission ended and the run began is unknown.' }
    } else if (!over) {
      cold = open('cold', admitted, coldSeen, admitted, true, 'Admitted and not started yet: the container has not reported in.')
    } else {
      // OVER, AND NEVER STARTED. The admission is recorded; the end of this
      // wait is not -- nothing writes one for an attempt that never ran. Open,
      // and marked as not live, so it is never read as still coming up.
      neverRan = true
      cold = open('cold', admitted, coldSeen, admitted, false, 'Admitted and never started. This attempt is over and no end was recorded for it.')
    }

    if (started !== null) {
      const completed = instant(a.completed_at)
      if (completed !== null) {
        run = closedOrAbsent('run', started, completed, admitted)
      } else if (a.completed_at !== null) {
        run = { kind: 'absent', phase: 'run', why: 'The finish time did not parse, so how long this attempt ran is unknown.' }
      } else {
        run = open(
          'run',
          started,
          runSeen,
          admitted,
          !over,
          over
            ? 'This attempt is over and no finish time was ever written — the shape a kill or a reconciler reclaim leaves. It ran at least until its newest heartbeat or checkpoint on this page.'
            : 'Running, with no end yet. It has run at least until its newest heartbeat or checkpoint on this page; the events route is capped and newer ones may exist.',
        )
      }
    }

    const dispatched = firstOfType(a.attempt_id, 'dispatched', events)
    const dispatchedAt =
      dispatched !== null && dispatched >= admitted && (started === null || dispatched <= started)
        ? dispatched - admitted
        : null

    rows.push({ attempt: a, ordinal: i + 1, queue, cold, run, dispatchedAt, neverRan })
  })

  return { rows, undatable }
}

/**
 * The summed agent work, and exactly what it covers.
 *
 * redesign-v2 Panel 5's trap: `task.started_at` is overwritten on every
 * attempt, so `completed_at - started_at` on the TASK is the last attempt's
 * runtime and `completed_at - created_at` is wall time including every queue
 * and park. Neither is how long the agent worked; only the sum of per-attempt
 * run intervals is.
 *
 * `ms` is null when no attempt contributed a measurement -- a sum of nothing
 * is unknown, not zero. An attempt that never started contributes a known
 * nothing and is counted apart, because "it did no work" is a fact about it.
 * An OPEN attempt contributes NOTHING to `ms`: its lower bound is in
 * `openAtLeastMs`, stated beside the total and never added into it.
 */
export interface WorkSum {
  readonly ms: number | null
  readonly attempts: number
  readonly closed: number
  readonly open: number
  readonly openAtLeastMs: number
  readonly neverRan: number
  readonly absent: number
  readonly undatable: number
  /** True when the total covers fewer attempts than there are. */
  readonly partial: boolean
}

export function workSum(p: PhaseRows): WorkSum {
  let ms = 0
  let closed = 0
  let openN = 0
  let openAtLeastMs = 0
  let neverRan = 0
  let absent = 0
  for (const r of p.rows) {
    if (r.run === null) {
      // Not started. Over -> a known nothing. Not over -> still coming up,
      // which is an OPEN attempt whose run has not begun.
      if (r.neverRan) neverRan += 1
      else openN += 1
      continue
    }
    if (r.run.kind === 'closed') {
      ms += r.run.ms
      closed += 1
    } else if (r.run.kind === 'open') {
      openN += 1
      openAtLeastMs += r.run.atLeastMs
      ms += r.run.atLeastMs
    } else {
      absent += 1
    }
  }
  const known = closed + neverRan
  return {
    ms: known === 0 ? null : ms,
    attempts: p.rows.length + p.undatable,
    closed,
    open: openN,
    openAtLeastMs,
    neverRan,
    absent,
    undatable: p.undatable,
    partial: openN > 0 || absent > 0 || p.undatable > 0,
  }
}

/** The right-hand extent of one row, from recorded instants only. */
function rowRight(r: AttemptPhases): number {
  let hi = 0
  for (const s of [r.cold, r.run]) {
    if (s === null) continue
    if (s.kind === 'closed') hi = Math.max(hi, s.to)
    if (s.kind === 'open') hi = Math.max(hi, s.seen)
  }
  if (r.dispatchedAt !== null) hi = Math.max(hi, r.dispatchedAt)
  return hi
}

/**
 * The domain of the phase chart, in ms from admission, or null with nothing
 * to draw. Built from recorded instants alone: an open segment reaches its
 * `seen`, never further, so no guessed end can widen the axis.
 */
export function phaseExtent(rows: readonly AttemptPhases[]): { lo: number; hi: number; degenerate: boolean } | null {
  if (rows.length === 0) return null
  let lo = 0
  let hi = 0
  for (const r of rows) {
    if (r.queue.kind === 'closed') lo = Math.min(lo, r.queue.from)
    hi = Math.max(hi, rowRight(r))
  }
  return { lo, hi, degenerate: lo === hi }
}
