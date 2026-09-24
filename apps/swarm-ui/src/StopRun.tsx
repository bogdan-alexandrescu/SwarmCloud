import { useState } from 'react'

import { cancelTask } from './api'
import { errorHeading, type ApiError } from './fetch'
import {
  TERMINAL_STATES,
  canBeStopped,
  stoppingEndsALiveAttempt,
  type Task,
  type TaskState,
  type Workflow,
  type WorkflowStep,
} from './types'

/**
 * The stop control. `POST /v1/tasks/{id}/cancel`, which has worked since it was
 * written and which NOTHING in this app called.
 *
 * B28 in docs/web-ui/ui-audit-and-build-prompt.md, and the gap it names is not
 * a missing feature so much as a missing exit: "an operator watching an agent
 * burn tokens on the wrong thing cannot stop it from the console."
 *
 * EVERY SENTENCE THIS FILE PUTS ON SCREEN IS PINNED BY A TEST.
 * `tests/unit/control_plane/test_cancel_semantics.py` exists because a
 * confirmation dialog is a set of claims about what is about to happen, and
 * the first draft of these claims was wrong in two ways that only running the
 * scheduler exposed:
 *
 *   1. STOPPING IS A REQUEST, NOT AN EVENT. For a task holding capacity,
 *      `Store.request_cancel` sets `cancel_requested` and leaves the state
 *      alone -- releasing the lease from the API would decrement a pool that a
 *      live container still occupies. The agent keeps running, and keeps
 *      spending, until its next heartbeat -- WHEN A WORKER IS RUNNING IT. See
 *      `workerHasStarted` for the case where none is, which is the one this
 *      file got wrong (incident wf_ebb3ab2d65664707a559, F-8).
 *   2. DEPENDENTS FALL OVER AFTERWARDS, NOT AT THE SAME TIME. The scheduler's
 *      `_FAILED_PARENT_STATES` tests the parent's STATE, not the flag, so
 *      nothing downstream moves until the worker has actually finished the
 *      attempt as CANCELLED. `test_nothing_downstream_moves_until_the_worker_
 *      has_acted` pins that window.
 *
 * AND THE THING THAT MAKES THE DECISION EASY, which the dialog says because it
 * is true and because it is the kind of fact this product should be stating:
 * the work is KEPT. `lifecycle.py:611-625` terminates the child, takes a
 * checkpoint, uploads the artifacts and the logs, and only then finishes the
 * attempt as CANCELLED. Stopping an agent costs the rest of that attempt, not
 * what it has already done.
 *
 * WHAT IS DELIBERATELY NOT SAID. `Workflow.on_step_failure` is accepted by the
 * API, stored on the workflow and served back -- and READ BY NOTHING. A grep
 * across `apps/` finds no consumer in the scheduler, the reconciler or the
 * rollup. So `continue` and `fail_workflow` behave identically today, and this
 * dialog describes the behaviour rather than the setting. See the module
 * docstring of the test file for the full finding.
 */

/** Which steps of this workflow die with the one being stopped. */
export function dependentsOf(
  step: WorkflowStep,
  steps: WorkflowStep[],
): WorkflowStep[] {
  const byId = new Map(steps.map((s) => [s.step_id, s]))
  const doomed = new Set<string>()
  // Transitive, not just direct children: `join` depends on `left`, and
  // `report` depends on `join`, so stopping `left` takes both. Naming only the
  // direct child would under-report what the button does.
  const walk = (id: string): void => {
    for (const candidate of steps) {
      if (candidate.depends_on.includes(id) && !doomed.has(candidate.step_id)) {
        doomed.add(candidate.step_id)
        walk(candidate.step_id)
      }
    }
  }
  walk(step.step_id)
  doomed.delete(step.step_id)
  return Array.from(doomed)
    .map((id) => byId.get(id))
    .filter((s): s is WorkflowStep => s !== undefined)
}

/**
 * Whether a worker is running THIS attempt, so that "at its next heartbeat"
 * is a promise something will keep.
 *
 * Every sentence below used to promise the heartbeat for any task holding
 * capacity. In incident wf_ebb3ab2d65664707a559, five DISPATCHED tasks whose
 * GKE pods never ran the worker lifecycle were stopped under that promise and
 * stayed DISPATCHED for hours: nothing was ever going to heartbeat.
 *
 * TWO SIGNALS, because either alone is wrong:
 *
 *   * `started_at` is written by the worker's DISPATCHED -> STARTING
 *     transition (`control.py:412`). Null means no worker has ever started
 *     this task;
 *   * but nothing clears it on a retry, so a second attempt waiting
 *     DISPATCHED carries the first attempt's value. The STATE is what says
 *     whether this attempt's worker has started: it moves the task to
 *     STARTING before it runs anything.
 *
 * What is true when this is false, and so what the copy says instead: a worker
 * that does start sees `cancel_requested` before it runs anything, finishes
 * the attempt as CANCELLED and exits (`lifecycle.py:269-278`). If none starts,
 * the reconciler reclaims the attempt once its dispatch deadline passes. No
 * time is given for that. On GKE the reclaim depends on the reconciler being
 * able to read GKE at all, and a number on screen would be a second promise
 * the platform cannot always keep.
 */
export function workerHasStarted(task: {
  state: TaskState
  started_at?: string | null
}): boolean {
  return (task.state === 'STARTING' || task.state === 'RUNNING') && Boolean(task.started_at)
}

/** Steps that share the workflow and do not depend on the one being stopped. */
export function survivorsOf(
  step: WorkflowStep,
  steps: WorkflowStep[],
): WorkflowStep[] {
  const doomed = new Set(dependentsOf(step, steps).map((s) => s.step_id))
  return steps.filter((s) => s.step_id !== step.step_id && !doomed.has(s.step_id))
}

export interface StopRunProps {
  task: Task
  /** A label for the thing being stopped, as the operator names it. */
  what: string
  /** The workflow this task is a step of, when it is one. */
  workflow?: Workflow | null
  step?: WorkflowStep | null
  /** Re-read the screen after the request is recorded. */
  reload: () => void
  /** `inline` sits on a graph node; `panel` sits in the run detail. */
  variant?: 'inline' | 'panel'
}

export function StopRun({
  task,
  what,
  workflow = null,
  step = null,
  reload,
  variant = 'panel',
}: StopRunProps) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [outcome, setOutcome] = useState<'requested' | 'released' | null>(null)
  const started = workerHasStarted(task)

  // A button that 409s is a button that should not have been drawn:
  // `request_cancel` refuses every terminal state. `cancel_requested` already
  // set is the other case -- the request is in, and offering to make it again
  // would imply the first one had not taken.
  if (!canBeStopped(task)) {
    return task.cancel_requested === true && !TERMINAL_STATES.has(task.state) ? (
      <span
        className="stop-pending"
        title={
          started
            ? 'The request is recorded. The worker acts on it at its next heartbeat.'
            : 'The request is recorded. No worker has started this attempt: one that starts ' +
              'stops before running anything, and if none does, the reconciler finishes it.'
        }
      >
        stop requested
      </span>
    ) : null
  }

  const live = stoppingEndsALiveAttempt(task)
  const doomed = workflow && step ? dependentsOf(step, workflow.steps) : []
  const survivors = workflow && step ? survivorsOf(step, workflow.steps) : []

  const run = async () => {
    setBusy(true)
    setError(null)
    const res = await cancelTask(task.id)
    setBusy(false)
    if (res.status === 'ok') {
      // The FIELD, not the status. A 200 means the request was recorded; only
      // `released_immediately` says whether anything stopped.
      setOutcome(res.data?.released_immediately === true ? 'released' : 'requested')
      setOpen(false)
      reload()
    } else if (res.status === 'error' || res.status === 'stale') {
      setError(res.error)
    }
  }

  if (outcome !== null) {
    // STOPPED AND STOP-REQUESTED ARE DIFFERENT FACTS AND STAY DIFFERENT WORDS.
    // `released_immediately` is the only thing that says which, and the two
    // must not converge -- that is the whole point of reading the field rather
    // than the status. What went is the second sentence of each: the long form
    // is the accessible name.
    return (
      <p
        className={variant === 'inline' ? 'stop-note small' : 'stop-note'}
        aria-label={
          outcome === 'released'
            ? 'Stopped. It held no capacity, so it went straight to CANCELLED. No attempt had started, so there is nothing to harvest.'
            : started
              ? 'Stop requested. It is recorded on the task. The agent is still running and still spending until its next heartbeat, when the worker checkpoints, uploads and exits.'
              : 'Stop requested. It is recorded on the task. No worker has started this attempt, so ' +
                'nothing is running to act on it yet: a worker that starts sees the request before it ' +
                'runs anything and stops, and if none starts, the reconciler reclaims the attempt and ' +
                'finishes it. Until one of those happens the slot stays held.'
        }
      >
        {outcome === 'released' ? (
          <>
            <strong>Stopped.</strong> It held no capacity, so nothing to harvest.
          </>
        ) : started ? (
          <>
            <strong>Stop requested.</strong> Still running until the next
            heartbeat.
          </>
        ) : (
          <>
            <strong>Stop requested.</strong> No worker has started; the reconciler
            finishes it if none does.
          </>
        )}
      </p>
    )
  }

  if (!open) {
    return (
      <button
        type="button"
        className={variant === 'inline' ? 'stop-btn inline' : 'stop-btn'}
        onClick={() => setOpen(true)}
      >
        stop
      </button>
    )
  }

  // FOUR PARAGRAPHS BECAME FOUR FACTS, AND EVERY CLAIM SURVIVES.
  //
  // This dialog's claims are pinned by `test_cancel_semantics.py` for a real
  // reason -- the first draft of them was wrong twice, and only running the
  // scheduler showed it. What that file holds is that the dialog does not
  // OVERSTATE: not "this cancels the following steps" but "these are cancelled
  // once this one has stopped"; not "it stops" but "the request is recorded".
  // Those distinctions are in the values below, word for word.
  //
  // What went is the connective tissue: "because releasing the lease from here
  // would free capacity a live container still occupies" is the REASON the
  // stop is a request rather than an event, and it is the same reason on every
  // task, every time. A keyed fact whose value is `at the worker's next
  // heartbeat` makes the same promise in four words and cannot be skimmed past
  // the way the fourth line of a paragraph can. The full sentences are the
  // strip's accessible name, so nothing is lost to a screen reader either.
  //
  // THREE CASES, NOT TWO. That four-word promise, and "the work so far is
  // kept", are true only while a worker is running this attempt. For an
  // attempt no worker has started (`workerHasStarted`) nothing will heartbeat
  // and nothing has been done, so that case gets its own values, and they name
  // the two things that can actually finish it: a worker that starts, or the
  // reconciler. Neither is given a time.
  const stops =
    step === null ? task.id : `${task.id} — step ${step.step_id}`
  return (
    <div className="stop-confirm" role="group" aria-label={`Stop ${what}`}>
      <h4>Stop {what}?</h4>

      <ul
        className="ctl-facts stop-facts"
        aria-label={
          `Stopping ${stops} is irreversible for this attempt: a stopped attempt is not resumed, ` +
          `though the task's remaining retries are unaffected by this button. ` +
          (!live
            ? 'Nothing is executing and no capacity is held, so this takes effect at once and there is ' +
              'no attempt to harvest.'
            : started
              ? 'The work so far is kept: before it exits the worker takes a checkpoint and uploads ' +
                "this attempt's artifacts and logs, so stopping costs the rest of this attempt and not " +
                'what it has already done. It does not stop instantly — the API records the request and ' +
                'the worker acts on it at its next heartbeat; until then the agent keeps running and the ' +
                'slot stays held, because releasing the lease from here would free capacity a live ' +
                'container still occupies.'
              : 'Nothing has run yet: no worker has started this attempt, so there is no work to ' +
                'harvest. It does not stop instantly — the API records the request, and the slot stays ' +
                'held until a worker starts and stops before running anything or, if none starts, the ' +
                'reconciler reclaims the attempt and finishes it. Releasing the lease from here would ' +
                'free capacity a starting container may still occupy.')
        }
      >
        <li className="ctl-fact">
          <b>stops</b>
          <code>{task.id}</code>
          {step !== null && (
            <>
              {' '}
              step <code>{step.step_id}</code>
            </>
          )}
        </li>
        {task.attempt_count > 0 && (
          <li className="ctl-fact">
            <b>attempt</b>
            {task.attempt_count} of {task.max_attempts}, not resumed
          </li>
        )}
        <li className="ctl-fact">
          <b>work so far</b>
          {!live
            ? 'none to harvest'
            : started
              ? 'kept — checkpoint, artifacts and logs are uploaded first'
              : 'none — no worker has started'}
        </li>
        <li className="ctl-fact">
          <b>takes effect</b>
          {!live
            ? 'at once'
            : started
              ? "at the worker's next heartbeat, not now"
              : 'when a worker starts, or when the reconciler finishes it — not now'}
        </li>
      </ul>

      {/* WHAT ELSE GOES WITH IT. Named, never counted -- and the tense is the
          part `test_cancel_semantics.py` holds: the scheduler cancels these
          when the parent reaches CANCELLED, which is after the worker has
          acted, not when the button is pressed. So the key is `once stopped`
          and not `also cancels`. */}
      {workflow !== null && step !== null && (
        <ul className="ctl-facts stop-facts stop-blast">
          <li className={doomed.length > 0 ? 'ctl-fact' : 'ctl-fact is-absent'}>
            <b>once stopped, also cancels</b>
            {doomed.length === 0 ? (
              'nothing — no other step depends on this one'
            ) : (
              doomed.map((s, i) => (
                <span key={s.step_id}>
                  {i > 0 && ', '}
                  <code>{s.step_id}</code>
                </span>
              ))
            )}
          </li>
          {survivors.length > 0 && (
            <li className="ctl-fact">
              <b>keeps running</b>
              {survivors.map((s, i) => (
                <span key={s.step_id}>
                  {i > 0 && ', '}
                  <code>{s.step_id}</code>
                </span>
              ))}
            </li>
          )}
        </ul>
      )}

      <div className="stop-actions">
        <button type="button" className="danger" disabled={busy} onClick={() => void run()}>
          {busy ? 'stopping…' : `stop ${what}`}
        </button>
        <button type="button" disabled={busy} onClick={() => setOpen(false)}>
          keep running
        </button>
      </div>

      {/* "Nothing was stopped" stays: an absent side effect has nothing to put
          a mark on, and it is the fact that decides whether to press again. */}
      {error && (
        <p className="warn-text">
          {errorHeading(error)} &mdash; {error.message} Nothing was stopped.
        </p>
      )}
    </div>
  )
}
