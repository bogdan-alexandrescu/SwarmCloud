/**
 * WHAT ONE WORKFLOW STEP SPENT AND HOW LONG IT TOOK -- and the four different
 * ways either can be missing.
 *
 * WHY THIS IS A MODULE AND NOT TWO LINES IN THE NODE. The audit's own words
 * about the run detail screen: an absent cost renders as `not reported -- No
 * attempt reported a cost. This is an absent measurement, not $0.00.`, and a
 * checkpoint count of 0 renders as a DIGIT because that zero was MEASURED. The
 * DAG node now carries the same two numbers, and a second, casually-written
 * copy of that rule beside a graph is exactly how the distinction gets lost:
 * `cost ?? 0` reads as a free run, and `$0.00` on five nodes of a six-step
 * workflow is a bill nobody owes.
 *
 * So the rule lives here, once, as a value rather than as JSX, and returns a
 * DISCRIMINATED UNION. There is no branch that yields a number for an absence,
 * because there is no way to write one: `absent` carries a sentence.
 *
 * THE FOUR ABSENCES, which are four different facts:
 *
 *   no-run     the step has no task. The workflow has not reached it, and it
 *              has therefore spent nothing -- a fact about the schedule, not a
 *              failed measurement.
 *   unread     the step has a task id and the task read did not return it. The
 *              platform may well hold a figure; we did not see it.
 *   unreported the task was read and its result summary carries no cost. Only
 *              claude-code and codex report one at all.
 *   running    the task has not finished. `result_summary` is written by
 *              `finish()`, so a running attempt has no figure yet and will get
 *              one; that is not the same as never having had one.
 *
 * WHERE THE NUMBER COMES FROM. `result_summary.runner.usage.total_cost_usd`,
 * which `agent_worker.lifecycle._usage_summary` extracts from the CLI's own
 * JSON before truncation and which `AgentDetail`'s "Spend, from the result
 * summary" panel already renders through the same `usd` rule. It is the LAST
 * attempt's figure -- `finish()` writes the summary once -- and `note` says so
 * rather than letting a retried step's node imply a total.
 *
 * The typed per-attempt `cost_usd` is better and is NOT used here on purpose:
 * reading it needs one `/v1/tasks/{id}/attempts` request per step, and a board
 * of ten workflows would issue sixty. The run panel behind the node does make
 * that read, and says which it is showing.
 */

import { TERMINAL_STATES, elapsed, usageOf } from './types'
import type { StepState } from './types'

/** Why a figure is missing. Never collapse two of these into one sentence. */
export type AbsenceKind = 'no-run' | 'unread' | 'unreported' | 'running'

export type Cell<T> =
  | { kind: 'measured'; value: T; note: string }
  | { kind: 'absent'; absence: AbsenceKind; word: string; note: string }

/**
 * The one sentence this product uses for a cost that was never reported.
 *
 * Exported so `AgentDetail` and the DAG node cannot drift apart on it, and so
 * a test can assert the string rather than a class name. The "$0.00" clause is
 * the load-bearing half: without it a reader supplies the zero themselves.
 */
export const COST_UNREPORTED = 'No attempt reported a cost. This is an absent measurement, not $0.00.'

/** A step with no task has spent nothing because it has not run. Not a zero. */
export const COST_NO_RUN = 'This step has no task yet, so nothing has been spent on it. Not a measured zero.'

export const COST_UNREAD = 'This step has a task that the task read did not return, so its spend is unknown.'

export const COST_RUNNING =
  'The result summary is written when an attempt ends, so a running step has no figure yet.'

/**
 * The step's cost, or the reason there is not one.
 *
 * NOTE what cannot be expressed: there is no `{ kind: 'measured', value: 0 }`
 * reachable from an absent reading. A zero here only ever comes from a
 * `total_cost_usd` the runner actually wrote, which is a real zero -- a mock
 * run costs nothing on purpose -- and renders as `$0.0000`, a digit, exactly
 * as the run panel renders a MEASURED checkpoint count of 0 as a digit.
 */
export function stepCost(state: StepState): Cell<number> {
  if (state.kind === 'unstarted') {
    return { kind: 'absent', absence: 'no-run', word: 'no run yet', note: COST_NO_RUN }
  }
  if (state.kind === 'unknown') {
    return { kind: 'absent', absence: 'unread', word: 'state unread', note: COST_UNREAD }
  }

  const usage = usageOf(state.task)
  const raw = usage?.['total_cost_usd']
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return {
      kind: 'measured',
      value: raw,
      note: `Reported by the last attempt of ${state.task.attempt_count}.`,
    }
  }

  if (!TERMINAL_STATES.has(state.task.state)) {
    return { kind: 'absent', absence: 'running', word: 'not yet', note: COST_RUNNING }
  }
  return { kind: 'absent', absence: 'unreported', word: 'not reported', note: COST_UNREPORTED }
}

/** `$0.0641`. Four places because a step of `wf_5e5ad3b6f7da4299a839` cost
 *  $0.0937 and two places round that to $0.09 -- a 4% error on the one number
 *  the platform exists to account for. */
export function usdLabel(v: number): string {
  return `$${v.toFixed(4)}`
}

/**
 * How long the step has taken, or the reason there is no span.
 *
 * Delegates to `elapsed`, which already distinguishes "queued 4m" from a run
 * time and never emits `0s` for a task that has not started. The wrapper exists
 * so the node's two cells have one shape and the absences stay named.
 */
export function stepDuration(state: StepState, now: number): Cell<string> {
  if (state.kind === 'unstarted') {
    return {
      kind: 'absent',
      absence: 'no-run',
      word: 'not started',
      note: 'The workflow has not reached this step, so no clock has started.',
    }
  }
  if (state.kind === 'unknown') {
    return {
      kind: 'absent',
      absence: 'unread',
      word: 'state unread',
      note: COST_UNREAD,
    }
  }
  const el = elapsed(state.task, now)
  // `elapsed` returns an em dash when neither a created_at nor a started_at
  // parsed. That is an unreadable document, not a zero-length run.
  if (el.text === '—') {
    return {
      kind: 'absent',
      absence: 'unread',
      word: 'no timestamps',
      note: 'The task carries no readable created or started time, so no span can be computed.',
    }
  }
  return {
    kind: 'measured',
    value: el.text,
    note: el.ticking ? 'Still running — this figure is climbing.' : 'Wall time from start to finish.',
  }
}
