import { useState } from 'react'

import { cancelTask } from './api'
import { errorHeading, type ApiError } from './fetch'
import {
  TERMINAL_STATES,
  canBeStopped,
  stoppingEndsALiveAttempt,
  type Task,
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
 *      spending, until its next heartbeat.
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

  // A button that 409s is a button that should not have been drawn:
  // `request_cancel` refuses every terminal state. `cancel_requested` already
  // set is the other case -- the request is in, and offering to make it again
  // would imply the first one had not taken.
  if (!canBeStopped(task)) {
    return task.cancel_requested === true && !TERMINAL_STATES.has(task.state) ? (
      <span className="stop-pending" title="The request is recorded. The worker acts on it at its next heartbeat.">
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
    return (
      <p className={variant === 'inline' ? 'stop-note small' : 'stop-note'}>
        {outcome === 'released' ? (
          <>
            <strong>Stopped.</strong> It held no capacity, so it went straight to
            CANCELLED. No attempt had started, so there is nothing to harvest.
          </>
        ) : (
          <>
            <strong>Stop requested.</strong> It is recorded on the task. The
            agent is still running and still spending until its next heartbeat,
            when the worker checkpoints, uploads and exits.
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

  return (
    <div className="stop-confirm" role="group" aria-label={`Stop ${what}`}>
      <h4>Stop {what}?</h4>

      {/* WHAT IT WILL DO, named exactly, and in the tense it happens in. */}
      <p className="stop-what">
        This stops <code>{task.id}</code>
        {step !== null && (
          <>
            {' '}&mdash; step <code>{step.step_id}</code>
          </>
        )}
        . It is irreversible for this attempt: a stopped attempt is not
        resumed, though the task&rsquo;s remaining retries are unaffected by
        this button
        {task.attempt_count > 0 && (
          <>
            {' '}(attempt {task.attempt_count} of {task.max_attempts})
          </>
        )}
        .
      </p>

      {/* THE FACT THAT MAKES IT A SMALL DECISION. */}
      {live ? (
        <p className="stop-keeps">
          <strong>The work so far is kept.</strong> Before it exits the worker
          takes a checkpoint and uploads this attempt&rsquo;s artifacts and
          logs, so everything the agent has produced stays readable on this
          screen and a later attempt can resume from the checkpoint. Stopping
          costs the rest of this attempt, not what it has already done.
        </p>
      ) : (
        <p className="stop-keeps">
          Nothing is executing and no capacity is held, so this takes effect at
          once and there is no attempt to harvest.
        </p>
      )}

      {/* WHEN. The window between the button and the stop is real. */}
      {live && (
        <p className="stop-when">
          It does not stop instantly. The API records the request and the worker
          acts on it at its next heartbeat; until then the agent keeps running
          and the slot stays held, because releasing the lease from here would
          free capacity a live container still occupies.
        </p>
      )}

      {/* WHAT ELSE GOES WITH IT. Named, never counted. */}
      {workflow !== null && step !== null && (
        <div className="stop-blast">
          {doomed.length > 0 ? (
            <p>
              <strong>
                {doomed.length} other step{doomed.length === 1 ? '' : 's'} will be
                cancelled once this one has stopped
              </strong>
              , because {doomed.length === 1 ? 'it depends' : 'they depend'} on
              it:{' '}
              {doomed.map((s, i) => (
                <span key={s.step_id}>
                  {i > 0 && ', '}
                  <code>{s.step_id}</code>
                </span>
              ))}
              . The scheduler does that when the parent reaches CANCELLED, not
              when the request is made.
            </p>
          ) : (
            <p>No other step depends on this one, so nothing else is cancelled.</p>
          )}
          {survivors.length > 0 && (
            <p className="muted small">
              {survivors.length} step{survivors.length === 1 ? '' : 's'}{' '}
              {survivors.length === 1 ? 'does' : 'do'} not depend on it and{' '}
              {survivors.length === 1 ? 'keeps' : 'keep'} running:{' '}
              {survivors.map((s, i) => (
                <span key={s.step_id}>
                  {i > 0 && ', '}
                  <code>{s.step_id}</code>
                </span>
              ))}
              .
            </p>
          )}
        </div>
      )}

      <div className="stop-actions">
        <button type="button" className="danger" disabled={busy} onClick={() => void run()}>
          {busy ? 'stopping…' : `stop ${what}`}
        </button>
        <button type="button" disabled={busy} onClick={() => setOpen(false)}>
          keep running
        </button>
      </div>

      {error && (
        <p className="warn-text">
          {errorHeading(error)} &mdash; {error.message} Nothing was stopped.
        </p>
      )}
    </div>
  )
}
