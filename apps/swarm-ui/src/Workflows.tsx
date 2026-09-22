import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { loadWorkflowBoard } from './api'
import { workflowDispatchOf } from './Dispatch'
import { Screen, timeAgo } from './Shell'
import {
  consequenceOf,
  dispatchOf,
  stateGlyph,
  stateTone,
  stepState,
  workflowHeaderState,
  type StepState,
  type Task,
  type TaskDispatch,
  type Tone,
  type Workflow,
  type WorkflowDrift,
  type WorkflowStep,
} from './types'

/**
 * The DAG view. These are REAL edges -- `depends_on` on each step -- drawn as
 * one curve per parent/child pair, so the topology is on the screen rather than
 * implied by vertical order.
 *
 * WHAT WAS HERE BEFORE AND WHY IT HAD TO GO. Siblings were laid out in a row and
 * a single "THEN" bar sat between one row and the next. That bar is the SAME
 * mark whether the next row joins all five of its predecessors or continues from
 * one of them, so the view could not express a fan-in -- proven live on
 * 2026-09-22 with `wf_5e5ad3b6f7da4299a839`, five independent steps and a sixth
 * joining all five, which drew identically to a chain. "What depends on what" is
 * the one question this screen exists to answer and it was the one it could not.
 *
 * HAND-ROLLED SVG, NO CHARTING DEPENDENCY. The layout is a layered DAG with a
 * handful of nodes; a graph library would add a bundle and a layout engine to
 * draw curves between boxes the browser has already positioned. The node boxes
 * stay real HTML -- they hold selectable text, titles and a tag -- and the SVG
 * is an aria-hidden layer behind them, which is why the geometry is MEASURED
 * rather than assumed: the dependency line under a node wraps to as many lines
 * as it needs (it must never be ellipsed), so no fixed row height is honest.
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

// ---------------------------------------------------------------------------
// The graph
// ---------------------------------------------------------------------------

/** One drawn edge: a parent step, and the child that names it in `depends_on`. */
export interface DagEdge {
  from: string
  to: string
}

/**
 * EVERY edge in the graph: one per `depends_on` entry, never one per level.
 *
 * The count is the point. A join of five parents produces five edges and a chain
 * of five produces four, which is exactly the distinction the old level
 * separator collapsed.
 *
 * A parent naming a step that is not in the workflow is kept here rather than
 * dropped. The scheduler rejects an unknown dependency at submission
 * (`test_dag_validation`), so it should be unreachable; if it ever is not, the
 * edge has no box to land on and draws nothing, and the dependency line under
 * the node marks that parent as dangling and says why. What must not happen is
 * the edge disappearing from the model as well as from the drawing -- a graph
 * that silently omits what it could not place is the failure this whole change
 * is about.
 */
export function edgesOf(steps: WorkflowStep[]): DagEdge[] {
  const edges: DagEdge[] = []
  for (const step of steps) {
    for (const parent of step.depends_on) {
      edges.push({ from: parent, to: step.step_id })
    }
  }
  return edges
}

/** Depth of each step: 0 for roots, else 1 + max(depth of dependencies). */
export function levelsOf(steps: WorkflowStep[]): WorkflowStep[][] {
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

/** Where one node box sits, in pixels relative to the graph frame. */
interface NodeBox {
  cx: number
  top: number
  bottom: number
}

interface Geometry {
  width: number
  height: number
  boxes: Map<string, NodeBox>
}

const NO_GEOMETRY: Geometry = { width: 0, height: 0, boxes: new Map() }

function sameGeometry(a: Geometry, b: Geometry): boolean {
  if (a.width !== b.width || a.height !== b.height || a.boxes.size !== b.boxes.size) {
    return false
  }
  for (const [id, box] of b.boxes) {
    const prev = a.boxes.get(id)
    if (!prev || prev.cx !== box.cx || prev.top !== box.top || prev.bottom !== box.bottom) {
      return false
    }
  }
  return true
}

/**
 * Measure the node boxes so the edges can be drawn between them.
 *
 * MEASURED, NOT ASSUMED, and the reason is a requirement rather than taste: the
 * dependency line under a node must never be ellipsed, so a node with five
 * parents is taller than a node with one and no constant row height is correct.
 * Measuring is also what makes a skip-level edge -- a step depending on a
 * grandparent -- draw as one curve instead of needing a routing pass.
 *
 * The state update is guarded by `sameGeometry` because the observer watches the
 * same elements this hook's output renders over. The SVG layer is absolutely
 * positioned and `pointer-events: none`, so it cannot change layout and the
 * guard should never be the thing that stops a loop -- it is here so that if it
 * ever could, it does.
 */
function useGraphGeometry(signature: string) {
  const frame = useRef<HTMLDivElement | null>(null)
  const nodes = useRef(new Map<string, HTMLElement>())
  const [geometry, setGeometry] = useState<Geometry>(NO_GEOMETRY)

  const measure = useCallback(() => {
    const el = frame.current
    if (!el) return
    const base = el.getBoundingClientRect()
    const next: Geometry = { width: base.width, height: base.height, boxes: new Map() }
    nodes.current.forEach((node, id) => {
      const r = node.getBoundingClientRect()
      next.boxes.set(id, {
        cx: r.left - base.left + r.width / 2,
        top: r.top - base.top,
        bottom: r.bottom - base.top,
      })
    })
    setGeometry((prev) => (sameGeometry(prev, next) ? prev : next))
  }, [])

  const register = useCallback((id: string) => (el: HTMLElement | null) => {
    if (el) nodes.current.set(id, el)
    else nodes.current.delete(id)
  }, [])

  useLayoutEffect(() => {
    measure()
    // A browser without ResizeObserver still gets edges; they are re-measured on
    // a window resize instead of on a content reflow. Both listeners are
    // registered, so a webfont swapping in after first paint moves the curves.
    window.addEventListener('resize', measure)
    if (typeof ResizeObserver === 'undefined') {
      return () => window.removeEventListener('resize', measure)
    }
    const observer = new ResizeObserver(measure)
    if (frame.current) observer.observe(frame.current)
    nodes.current.forEach((node) => observer.observe(node))
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [measure, signature])

  return { frame, register, geometry }
}

/**
 * The curve for one edge, parent bottom-centre to child top-centre.
 *
 * A cubic with vertical control handles, so the line leaves a parent and enters
 * a child straight down: at a five-into-one join that makes the five arrivals
 * fan into the child's top edge rather than crossing it at five angles.
 */
function edgePath(from: NodeBox, to: NodeBox): string {
  const x1 = from.cx
  const y1 = from.bottom
  const x2 = to.cx
  const y2 = to.top
  const reach = Math.max(12, (y2 - y1) / 2)
  return `M ${x1} ${y1} C ${x1} ${y1 + reach}, ${x2} ${y2 - reach}, ${x2} ${y2}`
}

function WorkflowGraph({
  workflowId,
  steps,
  taskById,
}: {
  workflowId: string
  steps: WorkflowStep[]
  taskById: ReadonlyMap<string, Task> | null
}) {
  const levels = levelsOf(steps)
  const edges = edgesOf(steps)
  const known = new Set(steps.map((s) => s.step_id))
  // The measurement has to re-run when the set of nodes changes, and a workflow
  // card is keyed by id, so the step ids are the whole signature.
  const { frame, register, geometry } = useGraphGeometry(
    `${steps.length}:${steps.map((s) => s.step_id).join(',')}`,
  )
  const arrow = `dag-arrow-${workflowId}`

  return (
    <div className="dag" ref={frame}>
      {/* aria-hidden: the curves restate `depends_on`, and every node already
          prints its parents in full underneath. A screen reader gets the list,
          not a description of a drawing. */}
      <svg
        className="dag-edges"
        width={geometry.width}
        height={geometry.height}
        viewBox={`0 0 ${Math.max(geometry.width, 1)} ${Math.max(geometry.height, 1)}`}
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          {/* Scoped to the workflow: several cards are on the page at once and a
              duplicated SVG id resolves to whichever came first in the document. */}
          <marker
            id={arrow}
            viewBox="0 0 8 8"
            refX="6.5"
            refY="4"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 8 4 L 0 8 z" className="dag-arrowhead" />
          </marker>
        </defs>
        {edges.map((edge) => {
          const from = geometry.boxes.get(edge.from)
          const to = geometry.boxes.get(edge.to)
          if (!from || !to) return null
          return (
            <path
              key={`${edge.from}->${edge.to}`}
              className="dag-edge"
              d={edgePath(from, to)}
              markerEnd={`url(#${arrow})`}
            />
          )
        })}
      </svg>
      {levels.map((level, i) => (
        <div className="dag-row" key={i}>
          {level.map((s) => (
            <StepNode
              key={s.step_id}
              step={s}
              state={stepState(s, taskById)}
              nodeRef={register(s.step_id)}
              known={known}
            />
          ))}
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------

/**
 * The step CENSUS, read off what the SERVER computed. Not the workflow's state
 * and not its size -- both of those are printed separately and plainly.
 *
 * This used to census the joined tasks here. It no longer does, and the reason
 * is the whole point of the change behind it: the API now derives a workflow's
 * state from its steps on every read, so a second census in the browser would be
 * a restatement of the server's rule with nothing checking the two still agree
 * -- `check-contract-parity.sh` does not cover TypeScript.
 *
 * Counts are marked UNCONFIRMED when any step state is unknown, because that
 * property belongs to the numbers rather than to where they were computed:
 * "3 succeeded" over a partial read is a wrong number wearing the clothes of a
 * right one. The server reports `complete: false` for exactly that case.
 *
 * The step COUNT is deliberately not in here. It used to be, as
 * "3 steps · not counted" struck through in amber -- so the only statement of
 * how big the workflow is looked retracted, while three nodes were drawn from
 * that very number a few pixels below. The count is known whenever the steps
 * are; "not counted" is a fact about the census and stays with the census.
 */
export function censusLine(workflow: Workflow): { text: string; confirmed: boolean } {
  const roll = workflow.rollup
  const total = workflow.steps.length
  if (!roll) {
    return { text: 'no census: this read did not derive one', confirmed: false }
  }
  if (!roll.complete) {
    const n = roll.unreadable_steps.length
    return { text: `${n} of ${total} steps: state unread`, confirmed: false }
  }
  const done = roll.counts.SUCCEEDED ?? 0
  const failed = roll.counts.FAILED ?? 0
  const unstarted = roll.counts.unstarted ?? 0
  const parts = [`${done}/${total} done`]
  if (failed > 0) parts.push(`${failed} failed`)
  if (unstarted > 0) parts.push(`${unstarted} not started`)
  return { text: parts.join(' · '), confirmed: true }
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
 * This is the ONE place the stored value appears, and it appears named as the
 * stored value inside a sentence about the two records disagreeing. The heading
 * never prints it; see `workflowHeaderState`.
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

/**
 * Said out loud when the API did not derive: the heading has no state to print,
 * and the reader needs to know that is a property of the server rather than of
 * this workflow.
 */
function StateNotDerived({ workflow }: { workflow: Workflow }) {
  return (
    <p className="rollup untrusted">
      This API did not derive the state from the steps on this read, so the
      heading claims none. The stored copy reads{' '}
      <code>{workflow.stored_state ?? workflow.state}</code>, which nothing has
      confirmed — before <code>rollup.py</code> a workflow kept the value it was
      created with for its whole life. The step chips below are live; read those.
    </p>
  )
}

function WorkflowCard({
  workflow,
  taskById,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
}) {
  const header = workflowHeaderState(workflow)
  const census = censusLine(workflow)
  const total = workflow.steps.length
  // Rolled up from the steps' TASKS, exactly as `codec.workflow_dispatch` does
  // server-side -- the frozen `Workflow` has no metadata field, so there is
  // nowhere else it could live. Null when the task join produced nothing to
  // read, which the banner above is already explaining.
  const tasks = workflow.steps
    .map((s) => (s.task_id ? (taskById?.get(s.task_id) ?? null) : null))
    .filter((t): t is Task => t !== null)
  const dispatch = workflowDispatchOf(tasks)

  return (
    <section className="section wf-card">
      <h2 className="wf-head">
        {/* `.ident` and not a bare string: `.section > h2` uppercases, and an
            uppercased id is a string that differs from the real one and cannot
            be pasted. Same rule, same reason, as QuotaDetail.tsx:94-96. */}
        <span className="ident">{workflow.workflow_id}</span>
        <span className={`wf-state ${header.tone}`} title={header.title}>
          <span aria-hidden>{header.glyph}</span> {header.word}
        </span>
        <span className="wf-when">updated {timeAgo(workflow.updated_at)}</span>
      </h2>
      <p className="rollup wf-size">
        {/* Plain. Always. This is the only place the size of the workflow
            appears and it is a number the page has already drawn nodes from. */}
        {total} step{total === 1 ? '' : 's'}
        {' · '}
        <span className={census.confirmed ? undefined : 'unconfirmed'}>{census.text}</span>
        {/* Still a separate annotation, not folded into the state above.
            `cancel_requested` is a REQUEST: a step holding a lease keeps it
            until the worker or the reconciler releases it, so between the
            request and the release the workflow really is still running. */}
        {workflow.cancel_requested && ' · cancel requested'}
      </p>
      {!header.derived && <StateNotDerived workflow={workflow} />}
      <StateDrift drift={workflow.drift} />
      <WorkflowDispatch
        dispatch={dispatch?.dispatch ?? null}
        integratorTaskId={dispatch?.integratorTaskId ?? null}
        steps={workflow.steps.length}
        joined={tasks.length > 0}
      />
      <WorkflowGraph
        workflowId={workflow.workflow_id}
        steps={workflow.steps}
        taskById={taskById}
      />
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
  nodeRef,
  known,
}: {
  step: WorkflowStep
  state: StepState
  nodeRef: (el: HTMLElement | null) => void
  /** Every step id in this workflow, so a dangling dependency can be marked. */
  known: ReadonlySet<string>
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
    <div className={`node ${p.tone}`} ref={nodeRef} title={p.title}>
      <div className="node-id">
        <span className="ident">{step.step_id}</span>
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
        /* NEVER ELLIPSED. This wraps to as many lines as it needs, and the node
           grows with it. The line under a five-parent join used to read
           "← cold-start, fencing, allorn…", hiding two of the five: the only
           complete statement of the graph on the page, cut off mid-word. The
           curves above are the picture; this is the text, and the text has to be
           whole for the picture to be checkable. */
        <div className="node-dep">
          <span aria-hidden>←</span>{' '}
          {step.depends_on.map((parent, i) => (
            <span key={parent}>
              {i > 0 && ', '}
              <span
                className={known.has(parent) ? 'ident' : 'ident dangling'}
                title={
                  known.has(parent)
                    ? undefined
                    : `${parent} is named as a dependency but is not a step of this workflow, so no edge could be drawn for it.`
                }
              >
                {parent}
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
