import { loadWorkflows } from './api'
import { Screen, timeAgo } from './Shell'
import type { Workflow, WorkflowStep } from './types'

/**
 * The DAG view. These are REAL edges -- `depends_on` on each step -- so this is
 * a tree, not a star with the workflow in the middle. Laid out by dependency
 * depth: a step sits one level below its deepest parent.
 *
 * Deliberately SVG-free. A workflow here has a handful of steps, and a column
 * layout with drawn connectors stays readable on a phone in a way a
 * force-directed graph does not. If workflows grow to dozens of steps this is
 * the thing to revisit, and the depth calculation is already the hard part.
 */
export function WorkflowsScreen() {
  return (
    <Screen
      title="Workflows"
      load={loadWorkflows}
      summary={(d) => `${d.workflows.length} workflow${d.workflows.length === 1 ? '' : 's'}`}
      empty={{
        heading: 'No workflows',
        body: 'The read succeeded and returned nothing. Tasks submitted individually do not belong to a workflow and appear only under Agents.',
      }}
    >
      {(d) => (
        <>
          {d.workflows.map((w) => (
            <WorkflowCard key={w.workflow_id} workflow={w} />
          ))}
        </>
      )}
    </Screen>
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

function WorkflowCard({ workflow }: { workflow: Workflow }) {
  const levels = levelsOf(workflow.steps)

  return (
    <section className="section">
      <h2>
        {workflow.workflow_id} · {workflow.state.toLowerCase()} · updated{' '}
        {timeAgo(workflow.updated_at)}
      </h2>
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
                <StepNode key={s.step_id} step={s} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function StepNode({ step }: { step: WorkflowStep }) {
  const state = (step.state ?? 'UNKNOWN').toUpperCase()
  const tone =
    state === 'SUCCEEDED' ? 'ok'
    : state === 'FAILED' || state === 'DEAD_LETTERED' ? 'bad'
    : state === 'RUNNING' || state === 'DISPATCHED' || state === 'STARTING' || state === 'LEASED' ? 'live'
    : 'wait'

  return (
    <div className={`node ${tone}`} title={step.task_id ?? 'no task yet'}>
      <div className="node-id">{step.step_id}</div>
      <div className="node-state">{state.toLowerCase()}</div>
      {step.depends_on.length > 0 && (
        <div className="node-dep" title={`depends on ${step.depends_on.join(', ')}`}>
          ← {step.depends_on.join(', ')}
        </div>
      )}
    </div>
  )
}
