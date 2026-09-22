import { useCallback, useState } from 'react'

import { loadWorkflowBoard } from './api'
import { workflowDispatchOf } from './Dispatch'
import { Screen, timeAgo } from './Shell'
import { StopRun } from './StopRun'
import {
  consequenceOf,
  dispatchOf,
  stateGlyph,
  stateTone,
  stepState,
  type StepState,
  type Task,
  type TaskDispatch,
  type Tone,
  type Workflow,
  type WorkflowDrift,
  type WorkflowStep,
} from './types'

/**
 * The DAG view. These are REAL edges -- `depends_on` on each step -- so this is
 * a tree, not a star with the workflow in the middle. Laid out by dependency
 * depth: a step sits one level below its deepest parent.
 *
 * Deliberately SVG-free. A workflow here has a handful of steps, and a column
 * layout with drawn connectors stays readable on a phone in a way a
 * force-directed graph does not. If workflows grow to dozens of steps this is
 * the thing to revisit, and the depth calculation is already the hard part.
 *
 * Step state is JOINED, not read off the step. `GET /v1/workflows` returns
 * steps with no state field at all; it only exists on the task a step created.
 * See `stepState` in types.ts for the three ways that join can come up empty
 * and why they must not render alike.
 */
export function WorkflowsScreen() {
  // Same reason as AgentDetail's: `Screen` keeps its retry nonce to itself, so
  // a mutation inside the board (stopping a step) needs a key bump to make the
  // board re-read. Without it the node a moment ago said RUNNING would keep
  // saying it after the request was recorded.
  const [reloads, setReloads] = useState(0)
  const reload = useCallback(() => setReloads((n) => n + 1), [])

  return (
    <Screen
      key={reloads}
      title="Workflows"
      load={loadWorkflowBoard}
      summary={(d) => `${d.workflows.length} workflow${d.workflows.length === 1 ? '' : 's'}`}
      empty={{
        heading: 'No workflows',
        body: 'The read succeeded and returned nothing. Tasks submitted individually do not belong to a workflow and appear only under Agents.',
      }}
    >
      {(d) => (
        <>
          {d.statesDetail !== null && <StatesUnavailable detail={d.statesDetail} />}
          {d.workflows.map((w) => (
            <WorkflowCard
              key={w.workflow_id}
              workflow={w}
              taskById={d.taskById}
              reload={reload}
            />
          ))}
        </>
      )}
    </Screen>
  )
}

/**
 * The partial state. The workflows read succeeded and the task read did not, so
 * the shape of every graph below is trustworthy and none of the step states
 * are. Saying so is the whole job of this banner -- without it the nodes read
 * as a workflow full of idle steps.
 */
function StatesUnavailable({ detail }: { detail: string }) {
  return (
    <div className="state partial" role="status">
      <h3>Step states could not be read</h3>
      <p>
        The workflows themselves loaded, so the steps, their order and their
        dependencies below are correct. Their <em>states</em> are not shown,
        because the task read failed: {detail}
      </p>
      <p style={{ marginTop: 8 }}>
        Nothing below should be taken as evidence that a step is or is not
        running.
      </p>
    </div>
  )
}

/** Depth of each step: 0 for roots, else 1 + max(depth of dependencies). */
function levelsOf(steps: WorkflowStep[]): WorkflowStep[][] {
  const byId = new Map(steps.map((s) => [s.step_id, s]))
  const depth = new Map<string, number>()

  const resolve = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id)
    if (cached !== undefined) return cached
    // A cycle should be impossible -- the scheduler rejects one at submission --
    // but a UI that hangs on malformed data is worse than one that draws it
    // flat, so this terminates rather than trusting that.
    if (seen.has(id)) return 0
    const step = byId.get(id)
    if (!step || step.depends_on.length === 0) {
      depth.set(id, 0)
      return 0
    }
    seen.add(id)
    const d = 1 + Math.max(...step.depends_on.map((p) => resolve(p, seen)))
    seen.delete(id)
    depth.set(id, d)
    return d
  }

  steps.forEach((s) => resolve(s.step_id, new Set()))
  const max = Math.max(0, ...steps.map((s) => depth.get(s.step_id) ?? 0))
  return Array.from({ length: max + 1 }, (_, lvl) =>
    steps.filter((s) => (depth.get(s.step_id) ?? 0) === lvl),
  )
}

/**
 * The rollup line, read off what the SERVER computed.
 *
 * This used to census the joined tasks here. It no longer does, and the reason
 * is the whole point of the change behind it: the API now derives a workflow's
 * state from its steps on every read, so a second census in the browser would be
 * a restatement of the server's rule with nothing checking the two still agree
 * -- `check-contract-parity.sh` does not cover TypeScript.
 *
 * Counts are still SUPPRESSED when any step state is unknown, because that
 * property belongs to the numbers rather than to where they were computed:
 * "3 succeeded" over a partial read is a wrong number wearing the clothes of a
 * right one. The server reports `complete: false` for exactly that case.
 */
function rollupLine(workflow: Workflow): { text: string; trustworthy: boolean } {
  const roll = workflow.rollup
  const total = workflow.steps.length
  if (!roll) {
    // `state_source: 'stored'` -- no read route produces this, but a payload
    // without a rollup must say it has no census rather than invent a zero one.
    return { text: `${total} step${total === 1 ? '' : 's'} · not counted`, trustworthy: false }
  }
  if (!roll.complete) {
    const n = roll.unreadable_steps.length
    return { text: `${n} of ${total} steps: state unread`, trustworthy: false }
  }
  const done = roll.counts.SUCCEEDED ?? 0
  const failed = roll.counts.FAILED ?? 0
  const unstarted = roll.counts.unstarted ?? 0
  const parts = [`${done}/${total} done`]
  if (failed > 0) parts.push(`${failed} failed`)
  if (unstarted > 0) parts.push(`${unstarted} not started`)
  return { text: parts.join(' · '), trustworthy: true }
}

/**
 * The accounting-drift check, for a workflow's two records of its own state.
 *
 * Modelled on `Drift` in Holders.tsx and there for the same reason: the stored
 * `workflow.state` and the state derived from the steps are two records of one
 * fact, so a disagreement is never rounding. It is shown AFTER the server has
 * already repaired it, on purpose -- a repair that leaves no trace is a silent
 * resolution, and the thing worth seeing is that the stored copy was wrong at
 * all, not the instant in which it was wrong.
 *
 * `agrees === null` is rendered as "not checked", never as a disagreement: the
 * derivation was incomplete, so the two records were not compared.
 */
function StateDrift({ drift }: { drift: WorkflowDrift | undefined }) {
  if (!drift || drift.agrees === true) return null
  if (drift.agrees === null) {
    return (
      <p className="rollup untrusted">
        State could not be checked: {drift.unreadable_steps.length} step
        {drift.unreadable_steps.length === 1 ? '' : 's'} unread ({drift.reason}). The
        stored value is <code>{drift.stored}</code> and nothing has confirmed it.
      </p>
    )
  }
  return (
    <p className="rollup untrusted">
      Stored state was <code>{drift.stored}</code>; the steps say{' '}
      <code>{drift.derived}</code>
      {drift.repaired ? ' — the stored copy has been corrected' : ''}.
    </p>
  )
}

function WorkflowCard({
  workflow,
  taskById,
  reload,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  reload: () => void
}) {
  const levels = levelsOf(workflow.steps)
  const roll = rollupLine(workflow)
  // Rolled up from the steps' TASKS, exactly as `codec.workflow_dispatch` does
  // server-side -- the frozen `Workflow` has no metadata field, so there is
  // nowhere else it could live. Null when the task join produced nothing to
  // read, which the banner above is already explaining.
  const tasks = workflow.steps
    .map((s) => (s.task_id ? (taskById?.get(s.task_id) ?? null) : null))
    .filter((t): t is Task => t !== null)
  const dispatch = workflowDispatchOf(tasks)

  return (
    <section className="section">
      <h2>
        {workflow.workflow_id} · {workflow.state.toLowerCase()} · updated{' '}
        {timeAgo(workflow.updated_at)}
      </h2>
      <p className={`rollup${roll.trustworthy ? '' : ' untrusted'}`}>
        {roll.text}
        {/* Still a separate annotation, not folded into the state above.
            `cancel_requested` is a REQUEST: a step holding a lease keeps it
            until the worker or the reconciler releases it, so between the
            request and the release the workflow really is still running. */}
        {workflow.cancel_requested && ' · cancel requested'}
      </p>
      <StateDrift drift={workflow.drift} />
      <WorkflowDispatch
        dispatch={dispatch?.dispatch ?? null}
        integratorTaskId={dispatch?.integratorTaskId ?? null}
        steps={workflow.steps.length}
        joined={tasks.length > 0}
      />
      <div className="dag">
        {levels.map((level, i) => (
          <div className="level" key={i} style={{ ['--depth' as string]: i }}>
            {/* A single vertical stalk used to sit here. It was decorative and
                it LIED: one line between levels reads as a linear chain, and
                this is a DAG -- `plan` forks to two children and they join back
                into `report`. Stacked on a phone it was worse, rendering four
                parallel-and-sequential steps as one sequence.
                The honest signal is the level itself, so the level says what it
                is; exact edges stay on each node as `← dependency`. */}
            {i > 0 && (
              <div className="level-label">
                {level.length > 1 ? `then ${level.length} in parallel` : 'then'}
              </div>
            )}
            <div className="level-steps">
              {level.map((s) => (
                <StepNode
                  key={s.step_id}
                  step={s}
                  state={stepState(s, taskById)}
                  workflow={workflow}
                  reload={reload}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

/**
 * HOW MANY PULL REQUESTS THIS WORKFLOW IS GOING TO PRODUCE.
 *
 * The same sentence the submit form showed when it was chosen, computed from
 * the same function over the same step count -- so "I picked one pull request"
 * and "this workflow will open one pull request" cannot come apart.
 *
 * Three absences, and they are not the same:
 *  - no task joined at all: the task read failed or has not reached any step,
 *    and the state banner above is already saying so. Nothing is claimed.
 *  - tasks joined but none carried a dispatch: an API older than the field.
 *  - a dispatch was read: shown.
 */
function WorkflowDispatch({
  dispatch,
  integratorTaskId,
  steps,
  joined,
}: {
  dispatch: TaskDispatch | null
  integratorTaskId: string | null
  steps: number
  joined: boolean
}) {
  if (dispatch === null) {
    return (
      <p className="muted small">
        {joined
          ? 'None of this workflow’s tasks reported a dispatch, so what it publishes is not shown. That is an API older than the field, not a workflow that publishes nothing.'
          : 'No task was joined for this workflow, so what it publishes cannot be read here.'}
      </p>
    )
  }
  const c = consequenceOf(dispatch.strategy, steps)
  return (
    <p className={`rollup dsp-rollup${c.pushes ? '' : ' is-none'}`}>
      <code>{dispatch.strategy}</code> · {c.headline}
      {dispatch.strategy === 'integrate' && integratorTaskId !== null && (
        <span className="muted small"> · integrator {integratorTaskId.slice(-8)}</span>
      )}
    </p>
  )
}

/** How each of the three step-state kinds presents. Kept together so the
 *  difference between "not started" and "not read" stays deliberate. */
function present(state: StepState): { tone: Tone | 'unknown'; glyph: string; word: string; title: string } {
  switch (state.kind) {
    case 'unstarted':
      return {
        tone: 'wait',
        glyph: '◌',
        word: 'not started',
        title: 'This step has no task yet. The workflow has not reached it.',
      }
    case 'unknown':
      return {
        tone: 'unknown',
        glyph: '?',
        word: 'state unread',
        title: `Task ${state.taskId} exists but was not in the task read. Its state is unknown -- this does not mean it is idle.`,
      }
    case 'state':
      return {
        tone: stateTone(state.state),
        glyph: stateGlyph(state.state),
        word: state.state.toLowerCase(),
        title: `Task ${state.task.id}, attempt ${state.task.attempt_count} of ${state.task.max_attempts}`,
      }
  }
}

function StepNode({
  step,
  state,
  workflow,
  reload,
}: {
  step: WorkflowStep
  state: StepState
  workflow: Workflow
  reload: () => void
}) {
  const p = present(state)
  // Read off the step's own task, so the node that opens the pull request is
  // marked in the graph rather than only named in the line above it. Silent on
  // a step with no task and on a `contributor`: every step of an `integrate`
  // workflow but one is a contributor, so a badge on each would mark nothing.
  //
  // Through `dispatchOf`, not off the field: it is the one reader that decides
  // what an absent or unrecognised block means, and a second one here is how
  // two screens start disagreeing about the same task.
  const role = state.kind === 'state' ? (dispatchOf(state.task)?.role ?? null) : null

  return (
    <div className={`node ${p.tone}`} title={p.title}>
      <div className="node-id">
        {step.step_id}
        {role === 'integrator' && (
          <span className="tag ok" title="This step merges the other steps' branches and opens the workflow's single pull request.">
            opens the PR
          </span>
        )}
      </div>
      <div className="node-state">
        <span aria-hidden>{p.glyph}</span> {p.word}
      </div>
      <div className="node-meta">{step.runner_profile}</div>
      {step.depends_on.length > 0 && (
        <div className="node-dep" title={`depends on ${step.depends_on.join(', ')}`}>
          ← {step.depends_on.join(', ')}
        </div>
      )}
      {/* B28, on the node. Only when the step's TASK was actually joined: a
          step whose state is `unknown` was not in the task read, and offering
          to stop something this screen could not read would be acting on a
          guess. `StopRun` then decides for itself whether the state is one the
          cancel route accepts, so a terminal node draws nothing at all. */}
      {state.kind === 'state' && (
        <div className="node-stop">
          <StopRun
            task={state.task}
            what={`step ${step.step_id}`}
            workflow={workflow}
            step={step}
            reload={reload}
            variant="inline"
          />
        </div>
      )}
    </div>
  )
}
