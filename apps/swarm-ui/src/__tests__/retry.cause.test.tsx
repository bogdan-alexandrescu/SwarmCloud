// AN ATTEMPT THE WORKER SENT BACK TO READY SAYS WHY ON THE AGENTS LIST, AND
// ITS RETRY'S QUEUE STARTS WHERE IT WAS SENT BACK (#149).
//
// The owner decided on #149 (2026-09-25) that an attempt whose runner finished
// cleanly without writing an expected output FAILS, retryably, with the
// missing names as its cause. The worker writes that cause into the task's
// `last_error`, sends the task back to READY and records a `retrying` event
// whose detail says `to_state: "READY"` (agent_worker/control.py
// `fail_retryably`). The CLI prints `last_error` for such a row
// (swarm_mcp/render.py `task_note`, `swarm result`'s "why"). This screen did
// not:
//
//   * THE AGENTS LIST'S "WHY" WAS EMPTY. `whyAgent` answered for PARKED, for a
//     READY task a pool was refusing, for FAILED and for CANCELLED, and for a
//     READY task with nothing refusing it said nothing, so the row read as an
//     agent waiting for no reason although the task recorded one;
//   * THE ATTEMPT CHART SAID A RECORD WAS MISSING THAT WAS NOT. A retry's
//     queue starts at the event that put the task back in line, and the chart
//     looked only for a `ready` event. The worker's requeue is `retrying`, so
//     every such retry's queue was drawn as "not measured", with the sentence
//     "the ready event that put the task back in line for this attempt is not
//     on this page of events".
//
// Each test says what it would take to break it.

import { describe, expect, it } from 'vitest'

import { phasesFor } from '../duration'
import { whyAgent, type AttemptRow, type Task } from '../types'
import { MIN, at, attempt, ev, task } from './runfixture'

/** `last_error` as `expected_outputs.missing_error` writes it. */
const CAUSE =
  'expected outputs missing (not written to $SWARM_ARTIFACTS_DIR: scan-01.md). A later step of ' +
  'this workflow stages them from this task, so the attempt failed; it is retried while the task ' +
  'has attempts left, and the retry must write every expected output again.'

/** An attempt as the API serves it once its worker started (see chart.phases.test.tsx). */
function worked(n: number, start: number, end: number): AttemptRow {
  return attempt(n, {
    created_at: at(start),
    started_at: at(start),
    completed_at: at(end),
    exit_code: 0,
  })
}

function sentBack(over: Partial<Task> = {}): Task {
  return task({
    state: 'READY',
    attempt_count: 1,
    started_at: at(1),
    completed_at: null,
    last_error: CAUSE,
    ...over,
  })
}

describe('a READY task whose last attempt failed says why on the agents list', () => {
  /** MUTATION: drop the READY-with-a-cause branch from `whyAgent`, and this is ''. */
  it('shows the cause the worker recorded when it sent the task back to READY', () => {
    expect(whyAgent(sentBack())).toBe(CAUSE)
  })

  /**
   * The pool is the reason it has not moved NOW. MUTATION: put the new branch
   * above the blocker branch, and this reads the old cause instead.
   */
  it('still names the pool refusing it first, when one is', () => {
    const why = whyAgent(
      sentBack({
        blocked_by: [{ pool: 'resource:standard', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 4 }],
      }),
    )
    expect(why).toBe('This resource class is busy platform-wide. (4/4)')
  })

  /** A READY task nothing has failed stays silent, as before. */
  it('says nothing for a READY task with no recorded error', () => {
    expect(whyAgent(task({ state: 'READY', started_at: null, completed_at: null }))).toBe('')
  })
})

describe('a retry the worker requeued has a measured queue', () => {
  const t = task({ attempt_count: 2, created_at: at(-2) })
  const attempts = [worked(1, 1, 10), worked(2, 23, 30)]

  /**
   * Attempt 1 ended at T+10m and the worker sent the task back to READY at
   * that instant (no delay: `EXPECTED_OUTPUT_RETRY_DELAY_SECONDS = 0`).
   * Attempt 2 was admitted at T+20m. Its queue is 10 minutes. MUTATION: read
   * only `ready` events again, and this queue is absent.
   */
  it('starts the retry’s queue at the worker’s retrying event that sent it back to READY', () => {
    const p = phasesFor(t, attempts, [
      ev('lease_acquired', at(0), 'att_1'),
      ev('retrying', at(10), 'att_1', {
        cause: 'expected_outputs_missing',
        missing: ['scan-01.md'],
        to_state: 'READY',
        attempt_count: 1,
        max_attempts: 3,
      }),
      ev('lease_acquired', at(20), 'att_2'),
    ])
    const q = p.rows[1]!.queue
    expect(q.kind).toBe('closed')
    if (q.kind === 'closed') expect(q.ms).toBe(10 * MIN)
  })

  /**
   * `retrying` is also the worker's IN-PLACE retry: a short provider wait or a
   * credential reload, after which the same attempt carries on and the task
   * never left RUNNING. Neither put the task back in line. MUTATION: take any
   * `retrying` as a requeue, and this queue is measured from T+5m.
   */
  it('does not take an in-place retry, which never left RUNNING, as a requeue', () => {
    const p = phasesFor(t, attempts, [
      ev('lease_acquired', at(0), 'att_1'),
      ev('retrying', at(5), 'att_1', { cause: 'credential_reloaded', provider: 'anthropic', reload: 1 }),
      ev('retrying', at(6), 'att_1', { wait_seconds: 30, attempt: 1 }),
      ev('lease_acquired', at(20), 'att_2'),
    ])
    expect(p.rows[1]!.queue.kind).toBe('absent')
  })
})
