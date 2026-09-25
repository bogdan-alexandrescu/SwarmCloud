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
// THE BOUNDARIES, AND WHERE EACH ONE IS READ FROM.
//
//   queue       task.created_at (the task's first attempt) or the event
//               that re-queued the task (a retry): `ready`, or the
//               worker's `retrying` to READY (`putBackInLine`)  ->  admission
//   cold start  admission  ->  attempt.started_at
//   run         attempt.started_at  ->  attempt.completed_at
//
// ADMISSION IS THE ATTEMPT'S `lease_acquired` EVENT, NOT `attempt.created_at`.
// The scheduler's `create_attempt` does write admission into `created_at`
// (scheduler/loop.py) -- and then the worker's `record_attempt_start`
// (agent-worker control.py) replaces the whole document with a NON-MERGE
// `.set()` that writes `created_at = utcnow()` beside `started_at = utcnow()`.
// On every attempt whose worker started, `created_at` is therefore the START,
// equal to `started_at` to within microseconds. Read as admission, it drew
// §9's run as a recorded 0s cold start and coloured the whole 3m 09s
// container start as "queue". (The same `.set()` replaces `execution_name`;
// docs/web-ui/04-live-logs.md.)
//
// `lease_acquired` is written by the scheduler, with the attempt's id, right
// after `create_attempt` and before the backend call; nothing rewrites an
// event. `attempt.created_at` is read as admission ONLY for an attempt that
// never started, because only then was the document never replaced. A started
// attempt whose `lease_acquired` is not on the page has no admission here: its
// queue and cold start are drawn absent with that reason, and its run -- both
// of whose ends are the worker's own -- is still measured but is not PLACED,
// because its position on an axis whose zero is admission is exactly the cold
// start that is unknown.
//
// `started_at` is set by the worker (control.py) and `completed_at` by
// `finish()`. Both are on the attempt document, which `GET /v1/tasks/{id}/
// attempts` serves whole.
//
// The events route does not: it orders oldest-first, caps the page and returns
// no page token, so on a long run the TAIL is what falls off -- the terminal
// event, the last checkpoints, the last heartbeats (redesign-v2 §4, seam S1).
// A phase bar built from `/events` alone would show a long agent's beginning
// and imply it never finished. So the events contribute only what the attempt
// document cannot: the `lease_acquired` instant that is admission (among the
// OLDEST events, so the first attempts' survive the cap and a late retry's of
// a long task may not), the `ready` instant that starts a retry's queue, the
// `dispatched` tick inside a cold start, and a LOWER BOUND for a segment that
// has no end.
//
// THE HONESTY RULE THIS MODULE EXISTS FOR. An interval with no recorded end is
// OPEN. It is never closed at "now", never closed at the last event, never
// given a guessed end. Until the events route is paged (another lane's seam),
// the most this page can say about an open segment is "at least this long, as
// far as the events on this page show" -- and that lower bound is carried
// SEPARATELY from the measured total, never summed into it.

import {
  CONCURRENCY_STATES,
  formatDuration,
  TERMINAL_STATES,
  type AttemptRow,
  type Task,
  type TaskEvent,
} from './types'

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
 * Offsets are milliseconds from ADMISSION, so every placed attempt's bar
 * shares one origin and the queue extends to the left of it. On an attempt
 * that is not placed (see `AttemptPhases.placed`) they are from the worker's
 * start, and only `ms` and `atLeastMs` mean anything.
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
  /** 1-based, oldest first, among the documents that came back. */
  readonly ordinal: number
  readonly queue: Segment
  readonly cold: Segment
  /**
   * Null when the attempt never started: it has no run phase at all. That is
   * a fact about the attempt (nothing ran), not a missing measurement, and it
   * is kept apart from `absent` so the two are never drawn alike.
   */
  readonly run: Segment | null
  /**
   * False when this attempt's admission is unknown -- a started attempt whose
   * `lease_acquired` event is not on the page. Its queue and cold start are
   * absent and its run is still measured, but nothing of it has a position
   * on the admission axis, so the chart draws none.
   */
  readonly placed: boolean
  /** `dispatched` event, as an offset from admission, when it is on the page. */
  readonly dispatchedAt: number | null
  /** True when the attempt is over and `started_at` was never written. */
  readonly neverRan: boolean
}

export interface PhaseRows {
  readonly rows: AttemptPhases[]
  /** Attempts with neither an admission nor a start: nothing to measure from. */
  readonly undatable: number
  /**
   * How many attempts the TASK records (`task.attempt_count`, incremented in
   * the admission transaction). The documents that came back can be fewer:
   * the route is newest-first and capped, and a document can be missing.
   */
  readonly counted: number
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
  if (a < 1000) return `${sign}${Math.round(a)}ms`
  return `${sign}${formatDuration(a)}`
}

// ---------------------------------------------------------------------------
// Has this attempt ended
// ---------------------------------------------------------------------------

/**
 * HAS THIS ATTEMPT ENDED, and on what evidence. One predicate, read by this
 * chart AND by the attempt cards in AgentDetail.tsx: when each kept its own
 * copy, the chart could say "over" directly above a card saying "running".
 *
 * `completed_at` is the obvious test and it is not sufficient. It is written
 * in exactly one place -- `control.record_attempt_end`, reachable only from
 * `finish()` -- and five ordinary paths end an attempt without it:
 *
 *   - a SIGKILL, before `finish()` runs;
 *   - a reconciler reclaim of a stale generation, which repairs the TASK
 *     document (to READY, `current_lease_id` cleared) and never touches the
 *     attempt's;
 *   - the worker's quota `park()` (control.py), CONTRACT invariant 4:
 *     checkpoint, park, release, exit. It writes PARKED and clears
 *     `current_lease_id` on the task and leaves the attempt document alone;
 *   - a failed dispatch: `return_to_ready_after_failed_dispatch` (scheduler
 *     store.py) releases the lease and puts the task back to READY, and no
 *     container ever reports in for that attempt;
 *   - a cancellation before start, which `finish()`es the TASK as CANCELLED.
 *
 * Four facts on this page settle it, each READ rather than inferred:
 *
 *   recorded    `completed_at` is set.
 *   superseded  a later attempt document exists. Attempts are fenced by
 *               generation and run one at a time, so one that is not the
 *               newest is over.
 *   task-ended  the task is terminal. Nothing writes to its attempts again.
 *   released    the task no longer holds THIS attempt's lease: it is outside
 *               LEASED / DISPATCHED / STARTING / RUNNING, or its
 *               `current_lease_id` names another lease. A lease is held only
 *               in those four states (CONTRACT invariant 1), and every path
 *               above that leaves the task non-terminal clears the pointer.
 *
 * What is NOT claimed anywhere is WHY the attempt stopped: this page cannot
 * see a kill, a reclaim or a park as such -- only that the attempt stopped,
 * and that no final figure came with it.
 */
export type AttemptEnd =
  | { readonly over: false }
  | { readonly over: true; readonly by: 'recorded' | 'superseded' | 'task-ended' | 'released' }

export function attemptEnd(a: AttemptRow, task: Task, isLatest: boolean): AttemptEnd {
  if (a.completed_at !== null) return { over: true, by: 'recorded' }
  if (!isLatest) return { over: true, by: 'superseded' }
  if (TERMINAL_STATES.has(task.state)) return { over: true, by: 'task-ended' }
  if (letGo(a, task)) return { over: true, by: 'released' }
  return { over: false }
}

/**
 * The task no longer holds this attempt's lease -- on a task read that is not
 * older than the attempt.
 *
 * THE AGE CHECK. The run screen reads the task and the attempts in parallel.
 * A task read that lands just BEFORE an admission shows READY with no lease,
 * and an attempts read that lands just after it returns the new attempt; the
 * pointer then "names another lease" only because the task document predates
 * the attempt. Such a read says nothing about the attempt, so it is not taken
 * as evidence. Every real release path writes the task AFTER the attempt
 * existed (`park`, the dispatch rollback and the reconciler all stamp
 * `updated_at`), and `attempt.created_at` is admission or, once rewritten,
 * the worker's start -- either one an instant at which the attempt existed.
 *
 * `current_lease_id` is always served (`task_to_api`), but a response from an
 * API that predates it would carry none; then the state alone is read.
 */
function letGo(a: AttemptRow, task: Task): boolean {
  const pointer: string | null | undefined = task.current_lease_id
  const holds = CONCURRENCY_STATES.has(task.state) && (pointer === undefined || pointer === a.lease_id)
  if (holds) return false
  const read = instant(task.updated_at)
  const existed = instant(a.created_at)
  return read === null || existed === null || read >= existed
}

// ---------------------------------------------------------------------------
// Evidence on the event page
// ---------------------------------------------------------------------------

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
    if (e.attempt_id !== attemptId || !ALIVE_IN[phase].has(e.type)) continue
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
 * Whether this event is one that put the task back in line.
 *
 * `ready` is what the scheduler writes when it promotes a parked task and
 * what the reconciler's repair writes. The WORKER's requeue (#149: a runner
 * that finished cleanly without an expected output) is `retrying` with
 * `detail.to_state: "READY"` (`control.fail_retryably`). Every other
 * `retrying` is an IN-PLACE retry (a short provider wait, a credential
 * reload): the same attempt carries on, the task never leaves RUNNING, and
 * nothing was put back in line, so the state it names is what tells the two
 * apart.
 */
function putBackInLine(e: TaskEvent): boolean {
  if (e.type === 'ready') return true
  return e.type === 'retrying' && e.detail?.['to_state'] === 'READY'
}

/**
 * The instant the task was put back in line for THIS attempt: the newest
 * requeue event (`putBackInLine`) after the previous attempt existed and at
 * or before this admission. Null when no such event is on the page.
 */
function requeuedAt(
  after: number,
  upTo: number,
  events: readonly TaskEvent[] | null,
): number | null {
  let best: number | null = null
  for (const e of events ?? []) {
    if (!putBackInLine(e)) continue
    const t = instant(e.at)
    if (t === null || t <= after || t > upTo) continue
    if (best === null || t > best) best = t
  }
  return best
}

/**
 * Admission, from the one record of it nothing rewrites -- or null, and why.
 *
 * See the header: `lease_acquired` first; `attempt.created_at` only when the
 * worker never started and so never replaced the document.
 */
function admissionOf(
  a: AttemptRow,
  events: readonly TaskEvent[] | null,
): { readonly at: number } | { readonly at: null; readonly why: string } {
  const leased = firstOfType(a.attempt_id, 'lease_acquired', events)
  if (leased !== null) return { at: leased }
  if (a.started_at === null) {
    const doc = instant(a.created_at)
    if (doc !== null) return { at: doc }
    return { at: null, why: 'The attempt’s creation time did not parse and its lease_acquired event is not on this page, so when it was admitted is unknown.' }
  }
  return {
    at: null,
    why:
      events === null
        ? 'The event read failed, so this attempt’s lease_acquired event -- the one record of its admission -- could not be looked for. The attempt document’s created_at is not admission: the worker rewrites it to its own start time.'
        : 'This attempt’s lease_acquired event -- the one record of its admission -- is not on this page of events, which is oldest-first and capped. The attempt document’s created_at is not admission: the worker rewrites it to its own start time.',
  }
}

// ---------------------------------------------------------------------------
// Segments
// ---------------------------------------------------------------------------

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

/** Why an attempt that never started is over, in the words of what was read. */
function neverStartedWhy(end: AttemptEnd, task: Task): string {
  if (end.over && end.by === 'released') {
    return `Admitted and never started. The task is ${task.state} and no longer holds this attempt’s lease — the shape a failed dispatch or a reclaim leaves — so no container is coming for it, and no end was recorded.`
  }
  return 'Admitted and never started. This attempt is over and no end was recorded for it.'
}

/** Why a started attempt's run has no end, in the words of what was read. */
function openRunWhy(end: AttemptEnd, task: Task): string {
  const bound = 'It ran at least until its newest heartbeat or checkpoint on this page.'
  if (!end.over) {
    return 'Running, with no end yet. It has run at least until its newest heartbeat or checkpoint on this page; the events route is capped and newer ones may exist.'
  }
  if (end.by === 'released') {
    return `This attempt is over: the task is ${task.state} and no longer holds its lease, and no finish time was ever written — the shape a quota park or a reconciler reclaim leaves. ${bound}`
  }
  return `This attempt is over and no finish time was ever written — the shape a kill or a reconciler reclaim leaves. ${bound}`
}

/**
 * Every attempt, oldest first, split into its three phases.
 *
 * `attempts` may arrive in any order; they are sorted on `created_at`
 * (ISO-8601 UTC, so lexicographic), the order the attempt cards use. The
 * rewrite does not disturb it: an attempt's worker starts after its own
 * admission and before the next one's, and a stale worker is fenced before
 * it writes anything.
 */
export function phasesFor(
  task: Task,
  attempts: readonly AttemptRow[],
  events: readonly TaskEvent[] | null,
): PhaseRows {
  const ordered = [...attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  const counted = Number.isFinite(task.attempt_count) ? Math.max(0, task.attempt_count) : 0
  // Whether the oldest document that came back is the task's FIRST attempt.
  // Only then does its queue start at submission; with documents missing, the
  // oldest one returned may be attempt 34 of 83, and a queue from submission
  // would fold every earlier attempt into "queue".
  const complete = ordered.length >= counted
  const rows: AttemptPhases[] = []
  let undatable = 0
  // An instant at which the previous attempt already existed: the floor for
  // the event that re-queued this one.
  let previousExisted: number | null = null
  const eventsRead = events !== null

  ordered.forEach((a, i) => {
    const admission = admissionOf(a, events)
    const admitted = admission.at
    const started = instant(a.started_at)
    if (admitted === null && started === null) {
      // Nothing to measure from and nothing measured: not drawn, and counted.
      undatable += 1
      previousExisted = instant(a.created_at) ?? previousExisted
      return
    }
    const placed = admitted !== null
    // Offsets are from admission when it is known, and from the worker's
    // start otherwise (then the row is not placed and only lengths are read).
    const origin = admitted ?? (started as number)
    const isLatest = i === ordered.length - 1
    const end = attemptEnd(a, task, isLatest)
    const over = end.over
    const coldSeen = newestFor(a.attempt_id, 'cold', events)
    const runSeen = newestFor(a.attempt_id, 'run', events)

    // QUEUE. The first attempt waited from submission; a retry waited from
    // the event that put the task back in line (`putBackInLine`). With no
    // such event on this page, a retry's queue has no start and is drawn as an absence --
    // NOT as starting at the previous attempt's end, which would fold the
    // retry back-off and any park into "queue".
    let queue: Segment
    if (admitted === null) {
      queue = {
        kind: 'absent',
        phase: 'queue',
        why: 'When this attempt was admitted is unknown (see its cold start), so the wait before admission has no end.',
      }
    } else if (i === 0 && complete) {
      const submitted = instant(task.created_at)
      queue =
        submitted === null
          ? { kind: 'absent', phase: 'queue', why: 'The task’s submission time did not parse, so the wait before admission has no start.' }
          : closedOrAbsent('queue', submitted, admitted, origin)
    } else {
      const from = requeuedAt(previousExisted ?? -Infinity, admitted, events)
      queue =
        from !== null
          ? closedOrAbsent('queue', from, admitted, origin)
          : {
              kind: 'absent',
              phase: 'queue',
              why:
                i === 0
                  ? `The task records ${counted} attempts and ${ordered.length} documents came back, so this may not be its first attempt, and the ready or retrying event that put it in line is not on this page. The wait before admission has no start.`
                  : eventsRead
                    ? 'The ready or retrying event that put the task back in line for this attempt is not on this page of events, so the wait before admission has no start.'
                    : 'The event read failed, so the ready or retrying event that put the task back in line for this attempt could not be looked for.',
            }
    }
    previousExisted = admitted ?? instant(a.created_at) ?? previousExisted

    // COLD START, and the attempt that never started.
    let cold: Segment
    let run: Segment | null = null
    let neverRan = false
    if (admission.at === null) {
      cold = { kind: 'absent', phase: 'cold', why: admission.why }
    } else if (started !== null) {
      cold = closedOrAbsent('cold', admission.at, started, origin)
    } else if (a.started_at !== null) {
      cold = { kind: 'absent', phase: 'cold', why: 'The worker start time did not parse, so where admission ended and the run began is unknown.' }
    } else if (!over) {
      cold = open('cold', admission.at, coldSeen, origin, true, 'Admitted and not started yet: the container has not reported in.')
    } else {
      // OVER, AND NEVER STARTED. The admission is recorded; the end of this
      // wait is not -- nothing writes one for an attempt that never ran. Open,
      // and marked as not live, so it is never read as still coming up.
      neverRan = true
      cold = open('cold', admission.at, coldSeen, origin, false, neverStartedWhy(end, task))
    }

    if (started !== null) {
      const completed = instant(a.completed_at)
      if (completed !== null) {
        run = closedOrAbsent('run', started, completed, origin)
      } else if (a.completed_at !== null) {
        run = { kind: 'absent', phase: 'run', why: 'The finish time did not parse, so how long this attempt ran is unknown.' }
      } else {
        run = open('run', started, runSeen, origin, !over, openRunWhy(end, task))
      }
    }

    const dispatched = firstOfType(a.attempt_id, 'dispatched', events)
    const dispatchedAt =
      admitted !== null && dispatched !== null && dispatched >= admitted && (started === null || dispatched <= started)
        ? dispatched - admitted
        : null

    rows.push({ attempt: a, ordinal: i + 1, queue, cold, run, placed, dispatchedAt, neverRan })
  })

  return { rows, undatable, counted }
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
 *
 * `attempts` is what the TASK records, not how many documents came back. The
 * `/attempts` route is newest-first and capped, and a document can be missing;
 * a total over the documents alone would call a partial read complete.
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
  /** Attempts the task records whose documents did not come back. */
  readonly missing: number
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
    } else {
      absent += 1
    }
  }
  const known = closed + neverRan
  const documents = p.rows.length + p.undatable
  const attempts = Math.max(p.counted, documents)
  const missing = attempts - documents
  return {
    ms: known === 0 ? null : ms,
    attempts,
    closed,
    open: openN,
    openAtLeastMs,
    neverRan,
    absent,
    undatable: p.undatable,
    missing,
    partial: openN > 0 || absent > 0 || p.undatable > 0 || missing > 0,
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
 * `seen`, never further, so no guessed end can widen the axis -- and a row
 * that is not placed contributes nothing, because none of its offsets are
 * from admission.
 */
export function phaseExtent(rows: readonly AttemptPhases[]): { lo: number; hi: number; degenerate: boolean } | null {
  if (rows.length === 0) return null
  let lo = 0
  let hi = 0
  for (const r of rows) {
    if (!r.placed) continue
    if (r.queue.kind === 'closed') lo = Math.min(lo, r.queue.from)
    hi = Math.max(hi, rowRight(r))
  }
  return { lo, hi, degenerate: lo === hi }
}
