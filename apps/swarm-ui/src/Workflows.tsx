import { loadWorkflowBoard } from './api'
import { Screen, timeAgo } from './Shell'
import {
  stateGlyph,
  stateTone,
  stepState,
  type StepState,
  type Task,
  type Tone,
  type Workflow,
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
  return (
    <Screen
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
            <WorkflowCard key={w.workflow_id} workflow={w} taskById={d.taskById} />
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
 * The computed rollup. Counts are SUPPRESSED when any step state is unknown:
 * "3 succeeded" computed over a partial join is a wrong number wearing the
 * clothes of a right one, and this screen exists partly to not do that.
 */
function rollup(
  steps: WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): { text: string; trustworthy: boolean } {
  const states = steps.map((s) => stepState(s, taskById))
  const unknown = states.filter((s) => s.kind === 'unknown').length
  if (unknown > 0) {
    return { text: `${unknown} of ${steps.length} steps: state unread`, trustworthy: false }
  }
  const done = states.filter((s) => s.kind === 'state' && s.state === 'SUCCEEDED').length
  const failed = states.filter((s) => s.kind === 'state' && s.state === 'FAILED').length
  const unstarted = states.filter((s) => s.kind === 'unstarted').length
  const parts = [`${done}/${steps.length} done`]
  if (failed > 0) parts.push(`${failed} failed`)
  if (unstarted > 0) parts.push(`${unstarted} not started`)
  return { text: parts.join(' · '), trustworthy: true }
}

function WorkflowCard({
  workflow,
  taskById,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
}) {
  const levels = levelsOf(workflow.steps)
  const roll = rollup(workflow.steps, taskById)

  return (
    <section className="section">
      <h2>
        {workflow.workflow_id} · {workflow.state.toLowerCase()} · updated{' '}
        {timeAgo(workflow.updated_at)}
      </h2>
      <p className={`rollup${roll.trustworthy ? '' : ' untrusted'}`}>
        {roll.text}
        {workflow.cancel_requested && ' · cancel requested'}
      </p>
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
                <StepNode key={s.step_id} step={s} state={stepState(s, taskById)} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
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

function StepNode({ step, state }: { step: WorkflowStep; state: StepState }) {
  const p = present(state)

  return (
    <div className={`node ${p.tone}`} title={p.title}>
      <div className="node-id">{step.step_id}</div>
      <div className="node-state">
        <span aria-hidden>{p.glyph}</span> {p.word}
      </div>
      <div className="node-meta">{step.runner_profile}</div>
      {step.depends_on.length > 0 && (
        <div className="node-dep" title={`depends on ${step.depends_on.join(', ')}`}>
          ← {step.depends_on.join(', ')}
        </div>
      )}
    </div>
  )
}
