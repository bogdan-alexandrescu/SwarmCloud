import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  loadWorkflowBoard,
  loadWorkflowUsage,
  type StepUsage,
  type WorkflowBoard,
  type WorkflowUsage,
} from './api'
import { dagShape, edgePath, miniMap, type Box, type DagShape } from './dag'
import { workflowDispatchOf } from './Dispatch'
import {
  absentCell,
  costCell,
  countCell,
  durationText,
  measuredCell,
  tokenCell,
  FINISH_NOT_RECORDED,
  NEVER_RAN,
  NO_ATTEMPT_YET,
  STATE_UNREAD,
  TOKENS_NOT_REPORTED,
  USAGE_NOT_READ,
  USAGE_NOT_SAMPLED,
  type Absence,
  type Cell,
} from './measure'
import { Id, Screen, timeAgo } from './Shell'
import { StopRun } from './StopRun'
import {
  TERMINAL_STATES,
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
 * THE DAG VIEW, IN TWO MODES, WITH REAL EDGES.
 *
 * WHAT WAS WRONG, measured on 2026-09-22 against `wf_5e5ad3b6f7da4299a839`
 * (five independent steps and a sixth joining all five):
 *
 *   [cold-start] [fencing] [allornothing] [checkpoints] [absentzero]
 *                       ———— THEN ————
 *                        [synthesis]
 *
 * One `THEN` bar -- the SAME bar a strictly linear workflow draws between two
 * sequential steps. There were no edges at all: siblings were laid out in a row
 * and dependency was implied by vertical order plus one separator, so the view
 * could not distinguish a fan-in of five from a chain of five. That is the one
 * question this screen exists to answer. The only complete statement of the
 * graph on the page was the dependency line under the join, and CSS ellipsed
 * it: `← cold-start, fencing, allorn…`, hiding two of its five parents.
 *
 * SO: one drawn edge per (parent, child) pair, from `dag.ts`, and a dependency
 * list that wraps and is never truncated. Five parents draw five edges; a chain
 * of five draws four, each between one pair. Nothing is shared between two
 * pairs, because a shared mark is how the distinction was lost.
 *
 * THE TWO MODES, and what each is for:
 *
 *   COLLAPSED  for scanning a board of workflows. It still carries TOPOLOGY --
 *              a sentence naming the degree that makes the shape what it is
 *              ("5 join into synthesis", never just "6 steps") and a sparkline
 *              drawn from the SAME edge list the canvas uses, so the two cannot
 *              disagree. A reader tells a fan-out from a chain without
 *              expanding anything.
 *   FULL       one workflow, the whole canvas, at the full content width.
 *
 * EDGES ARE MEASURED, NOT ASSUMED. Node height depends on its text -- the
 * dependency list wraps rather than ellipsing -- so the geometry comes from
 * `getBoundingClientRect` on the rendered nodes and is recomputed on resize.
 * Until the first measurement lands, NO edge is drawn: a path at (0,0) would be
 * a line that claims a dependency it has not located, and the dependency list
 * in each node is a complete statement of the graph either way.
 *
 * STEP STATE IS STILL JOINED, not read off the step. `GET /v1/workflows`
 * returns steps with no state field at all; it only exists on the task a step
 * created. See `stepState` in types.ts for the three ways that join can come up
 * empty and why they must not render alike.
 */
export function WorkflowsScreen() {
  // Same reason as AgentDetail's: `Screen` keeps its retry nonce to itself, so
  // a mutation inside the board (stopping a step) needs a key bump to make the
  // board re-read. Without it the node a moment ago said RUNNING would keep
  // saying it after the request was recorded.
  const [reloads, setReloads] = useState(0)
  const reload = useCallback(() => setReloads((n) => n + 1), [])

  // The BOARD default. Collapsed, because the list answers "which of my
  // workflows should I look at" and a canvas per card would bury that under
  // ten graphs. A card opened to `full` keeps its own mode in `override`.
  const [boardMode, setBoardMode] = useState<Mode>('collapsed')
  const [override, setOverride] = useState<Record<string, Mode>>({})

  const setBoard = useCallback((mode: Mode) => {
    setBoardMode(mode)
    // A board-level choice is the whole board's answer, so per-card overrides
    // are cleared rather than silently winning over the control just clicked.
    setOverride({})
  }, [])

  const toggle = useCallback((id: string, mode: Mode) => {
    setOverride((prev) => ({ ...prev, [id]: mode }))
  }, [])

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
        <Board
          board={d}
          reload={reload}
          boardMode={boardMode}
          setBoard={setBoard}
          override={override}
          toggle={toggle}
        />
      )}
    </Screen>
  )
}

/**
 * The board, plus the SECOND read the step figures need.
 *
 * Separate from `WorkflowsScreen` because `Screen`'s children is a render prop:
 * hooks may not live inside it. It is also the right seam -- the attempt read
 * is deliberately started only once the board itself has landed, so the graph
 * is on screen while the figures are still arriving rather than after.
 */
function Board({
  board,
  reload,
  boardMode,
  setBoard,
  override,
  toggle,
}: {
  board: WorkflowBoard
  reload: () => void
  boardMode: Mode
  setBoard: (m: Mode) => void
  override: Record<string, Mode>
  toggle: (id: string, mode: Mode) => void
}) {
  // Every task a step points at, in board order. `loadWorkflowUsage` dedupes
  // and caps; the order decides which steps fall inside the cap, so it is the
  // board's own order rather than a set's iteration order.
  const taskIds = useMemo(
    () =>
      board.workflows.flatMap((w) =>
        w.steps.map((s) => s.task_id ?? null).filter((id): id is string => id !== null),
      ),
    [board.workflows],
  )

  const [usage, setUsage] = useState<UsageRead>({ kind: 'reading' })

  useEffect(() => {
    let live = true
    setUsage({ kind: 'reading' })
    loadWorkflowUsage(taskIds).then((r) => {
      if (!live) return
      if (r.status === 'ok') {
        setUsage({ kind: 'ready', usage: r.data })
        return
      }
      // `empty` is a real answer: no step has a task yet, so there is nothing
      // to read and nothing failed. The per-step cells then say "not started",
      // which is what the state join already says.
      if (r.status === 'empty') {
        setUsage({ kind: 'ready', usage: null })
        return
      }
      if (r.status === 'stale') {
        setUsage({ kind: 'ready', usage: r.data })
        return
      }
      if (r.status === 'error') {
        setUsage({ kind: 'failed', detail: r.error.message })
        return
      }
      // `loading` is not a value this promise resolves to, but leaving the
      // nodes in `reading` for ever if it ever did is a silent stall, and a
      // placeholder that never stops moving is the worst of the three states.
      setUsage({ kind: 'failed', detail: 'The attempt read did not complete.' })
    })
    return () => {
      live = false
    }
  }, [taskIds])

  // A running step's duration has no end, so it has to be recomputed rather
  // than only redrawn when a fetch lands.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  return (
    <>
      {board.statesDetail !== null && <StatesUnavailable detail={board.statesDetail} />}
      {usage.kind === 'ready' && usage.usage !== null && <SampleNote usage={usage.usage} />}
      <ModeControl mode={boardMode} onChange={setBoard} count={board.workflows.length} />
      {board.workflows.map((w) => (
        <WorkflowCard
          key={w.workflow_id}
          workflow={w}
          taskById={board.taskById}
          usage={usage}
          now={now}
          reload={reload}
          mode={override[w.workflow_id] ?? boardMode}
          onMode={(m) => toggle(w.workflow_id, m)}
        />
      ))}
    </>
  )
}

/**
 * How the attempt read went, as the node needs to know it.
 *
 * `reading` is NOT an absence and must not render as one: "not reported" is a
 * claim about the platform, and a request still in flight has made no claim at
 * all. The node draws a moving placeholder for it instead -- the same
 * distinction `Overview.tsx` draws between `is-absent` and `ov-reading`.
 */
type UsageRead =
  | { kind: 'reading' }
  | { kind: 'ready'; usage: WorkflowUsage | null }
  | { kind: 'failed'; detail: string }

/**
 * What the sample covered, said once at the top rather than implied per node.
 *
 * Silent when every task on the board was read: a line saying "12 of 12" on
 * every refresh is noise, and the per-node "not sampled" already carries the
 * case that matters.
 */
function SampleNote({ usage }: { usage: WorkflowUsage }) {
  const uncovered = usage.notSampled.size
  const failed = usage.failed.size
  if (uncovered === 0 && failed === 0) return null
  return (
    <p className="rollup untrusted">
      Step figures cover {usage.byTaskId.size} of {usage.tasksRequested} tasks on this board.
      {uncovered > 0 && (
        <>
          {' '}
          {uncovered} {uncovered === 1 ? 'is' : 'are'} outside the {usage.sampleLimit}-task
          read ceiling — their cost and tokens are unknown here, not zero.
        </>
      )}
      {failed > 0 && <> {failed} attempt read{failed === 1 ? '' : 's'} failed.</>}
    </p>
  )
}

export type Mode = 'collapsed' | 'full'

/**
 * The board-level mode switch.
 *
 * `aria-pressed` rather than a `<select>`: there are two states, both are one
 * click away, and the current one is legible without opening anything.
 */
function ModeControl({
  mode,
  onChange,
  count,
}: {
  mode: Mode
  onChange: (m: Mode) => void
  count: number
}) {
  return (
    <div className="dagx-modes">
      <span className="dagx-modes-label">Graph</span>
      <button
        type="button"
        aria-pressed={mode === 'collapsed'}
        className={mode === 'collapsed' ? 'on' : ''}
        onClick={() => onChange('collapsed')}
      >
        Collapsed
      </button>
      <button
        type="button"
        aria-pressed={mode === 'full'}
        className={mode === 'full' ? 'on' : ''}
        onClick={() => onChange('full')}
      >
        Full DAG
      </button>
      <span className="dagx-modes-note">
        {mode === 'collapsed'
          ? `Shape and counts for ${count} workflow${count === 1 ? '' : 's'}. Open one to draw its edges.`
          : 'Every edge drawn, at full width. Click a node to open that agent run.'}
      </span>
    </div>
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
  usage,
  now,
  reload,
  mode,
  onMode,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  usage: UsageRead
  // TICKING, not `Date.now()` per render. A running step's duration has no end,
  // so the figure has to be recomputed on a timer rather than only when a fetch
  // lands -- the board owns that interval so every card shares one.
  now: number
  reload: () => void
  mode: Mode
  onMode: (m: Mode) => void
}) {
  const shape = dagShape(workflow.steps)
  // THE HEADING'S STATE IS DERIVED OR IT IS NOT PRINTED. `workflow.state` looks
  // like the answer and is not: on an API older than `rollup.py` it is the
  // Firestore cache, which reads QUEUED for the workflow's whole life. See
  // `workflowHeaderState` in types.ts.
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
        {/* B17. `.section > h2` uppercases, and this id is lowercase
            everywhere it actually lives -- Firestore, the API, the logs and
            the `#agents/task/<id>` address. Printed as WF_BCDC9180… it cannot
            be pasted anywhere, which is the only thing an id is for. */}
        <Id>{workflow.workflow_id}</Id>
        {/* NOT the workflow's own state field, lower-cased. That field is the
            derived rollup on a current API and the stale Firestore cache on an
            older one, and the two are indistinguishable from here -- which is
            how a heading came to read QUEUED over steps reading succeeded,
            failed and cancelled. `workflowHeaderState` prints a state only when
            THIS read derived one, and says so plainly when it did not. */}
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

      <Topology shape={shape} taskById={taskById} mode={mode} onMode={onMode} />

      {mode === 'full' && (
        <DagCanvas
          workflowId={workflow.workflow_id}
          workflow={workflow}
          shape={shape}
          taskById={taskById}
          usage={usage}
          now={now}
          reload={reload}
        />
      )}

      {shape.dangling.length > 0 && <Dangling shape={shape} />}
    </section>
  )
}

/**
 * A `depends_on` entry naming a step this workflow does not contain.
 *
 * Shown rather than filtered. `validate_dag` rejects one at submission
 * (tests/unit/control_plane/test_dag_validation.py), so a workflow that has one
 * is evidence about the API or the payload, and a graph that quietly drew one
 * fewer edge would look complete and be wrong.
 */
function Dangling({ shape }: { shape: DagShape }) {
  return (
    <p className="rollup untrusted">
      {shape.dangling.length} dependenc{shape.dangling.length === 1 ? 'y names a step' : 'ies name steps'} this
      workflow does not contain:{' '}
      {shape.dangling.map((e) => `${e.to} ← ${e.from}`).join(', ')}. No edge is drawn for{' '}
      {shape.dangling.length === 1 ? 'it' : 'them'}, and the graph above is therefore incomplete.
    </p>
  )
}

/**
 * THE COLLAPSED MODE, and the header of the full one.
 *
 * `shape.summary` names the degree rather than the count -- "5 join into
 * synthesis", not "6 steps" -- because a count is exactly what a chain of six
 * and a fan-in of five have in common, and telling them apart is the point.
 *
 * The sparkline beside it is drawn from `shape.edges`, the same list the canvas
 * draws, so the collapsed and expanded views cannot disagree about the shape of
 * the same workflow. Its dots carry state tone, so "which of these is red" is
 * answerable while scanning.
 */
function Topology({
  shape,
  taskById,
  mode,
  onMode,
}: {
  shape: DagShape
  taskById: ReadonlyMap<string, Task> | null
  mode: Mode
  onMode: (m: Mode) => void
}) {
  const mini = miniMap(shape, 132, 34)
  const toneOf = new Map<string, string>()
  for (const level of shape.levels) {
    for (const step of level) toneOf.set(step.step_id, present(stepState(step, taskById)).tone)
  }

  return (
    <div className="dagx-topo">
      <svg
        className="dagx-mini"
        width={mini.width}
        height={mini.height}
        viewBox={`0 0 ${mini.width} ${mini.height}`}
        role="img"
        aria-label={shape.summary}
      >
        {mini.lines.map((l) => (
          <line
            key={`${l.from}->${l.to}`}
            data-edge={`${l.from}->${l.to}`}
            x1={l.x1}
            y1={l.y1}
            x2={l.x2}
            y2={l.y2}
          />
        ))}
        {mini.dots.map((d) => (
          <circle key={d.id} className={`t-${toneOf.get(d.id) ?? 'unknown'}`} cx={d.cx} cy={d.cy} r={2.6} />
        ))}
      </svg>
      <div className="dagx-topo-text">
        <span className="dagx-shape">{shape.summary}</span>
        <span className="dagx-topo-sub">
          {shape.depth} level{shape.depth === 1 ? '' : 's'} · {shape.edges.length} edge
          {shape.edges.length === 1 ? '' : 's'} · widest {shape.widest}
        </span>
      </div>
      <button
        type="button"
        className="dagx-open"
        aria-expanded={mode === 'full'}
        onClick={() => onMode(mode === 'full' ? 'collapsed' : 'full')}
      >
        {mode === 'full' ? 'Collapse' : 'Open DAG'}
      </button>
    </div>
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

// ---------------------------------------------------------------------------
// The canvas
// ---------------------------------------------------------------------------

type Geometry = { boxes: Map<string, Box>; width: number; height: number }

/**
 * The full graph: nodes in normal flow, edges in an absolutely-positioned SVG
 * underneath them.
 *
 * WHY NOT LAY THE NODES OUT IN THE SVG TOO. A node carries its id, its state,
 * its runner profile, its duration, its cost and its complete dependency list,
 * and the dependency list wraps. Text in SVG does not wrap, and a fixed node
 * height would put every edge a few pixels into the wrong place the first time
 * a step id grew. So the browser lays the nodes out, the boxes are measured,
 * and the edges are drawn to what is actually there.
 *
 * `overflow-x: auto` on the host with a min-width per level means a workflow
 * twelve wide scrolls rather than crushing its nodes to unreadable slivers, and
 * the SVG spans the scrolled width, not the visible one.
 */
function DagCanvas({
  workflowId,
  workflow,
  shape,
  taskById,
  usage,
  now,
  reload,
}: {
  workflowId: string
  workflow: Workflow
  shape: DagShape
  taskById: ReadonlyMap<string, Task> | null
  usage: UsageRead
  now: number
  reload: () => void
}) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const nodeRefs = useRef(new Map<string, HTMLElement>())
  const [geom, setGeom] = useState<Geometry | null>(null)

  const register = useCallback((id: string, el: HTMLElement | null) => {
    if (el) nodeRefs.current.set(id, el)
    else nodeRefs.current.delete(id)
  }, [])

  useLayoutEffect(() => {
    const host = hostRef.current
    if (host === null) return

    const measure = () => {
      const hb = host.getBoundingClientRect()
      const boxes = new Map<string, Box>()
      nodeRefs.current.forEach((el, id) => {
        const b = el.getBoundingClientRect()
        boxes.set(id, {
          // Relative to the host's PADDING BOX and un-scrolled, so an edge stays
          // attached to its node when the canvas is scrolled sideways.
          x: b.left - hb.left + host.scrollLeft,
          y: b.top - hb.top + host.scrollTop,
          w: b.width,
          h: b.height,
        })
      })
      const next: Geometry = { boxes, width: host.scrollWidth, height: host.scrollHeight }
      // Compared before storing. The observer below fires on every layout of
      // every node, and a new object each time would re-render on a resize that
      // moved nothing.
      setGeom((prev) => (sameGeometry(prev, next) ? prev : next))
    }

    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(host)
    nodeRefs.current.forEach((el) => ro.observe(el))
    return () => ro.disconnect()
    // The node set changes only when the steps do; `shape` is recomputed per
    // render, so its identity is not usable as a dependency and the step ids
    // are used instead.
  }, [shape.levels.map((l) => l.map((s) => s.step_id).join(',')).join('|')])

  // Unique per card: two workflow cards on one board would otherwise share a
  // marker id, and the second one's arrowheads would resolve to the first's.
  const marker = `dagx-arrow-${cssId(workflowId)}`

  return (
    <div className="dagx" ref={hostRef}>
      {/* aria-hidden: every edge this draws is also stated in words inside the
          node it points at ("← plan, scan-scripts"), so a reader who cannot see
          the curve still gets the complete graph rather than a duplicate of it. */}
      <svg
        className="dagx-edges"
        width={geom?.width ?? 0}
        height={geom?.height ?? 0}
        aria-hidden
        focusable="false"
      >
        <defs>
          <marker
            id={marker}
            markerWidth="7"
            markerHeight="7"
            refX="5.4"
            refY="3"
            orient="auto"
            markerUnits="userSpaceOnUse"
          >
            <path d="M 0 0 L 6 3 L 0 6 z" />
          </marker>
        </defs>
        {/* No geometry yet means no line. A path drawn at the origin would
            claim a dependency it has not located. */}
        {geom !== null &&
          shape.edges.map((e) => {
            const from = geom.boxes.get(e.from)
            const to = geom.boxes.get(e.to)
            if (!from || !to) return null
            return (
              <path
                key={`${e.from}->${e.to}`}
                className="dagx-edge"
                data-edge={`${e.from}->${e.to}`}
                d={edgePath(from, to)}
                markerEnd={`url(#${marker})`}
              />
            )
          })}
      </svg>

      <div className="dagx-levels" style={{ ['--widest' as string]: shape.widest }}>
        {shape.levels.map((level, i) => (
          <div className="dagx-level" key={i}>
            {level.map((s) => (
              <StepNode
                key={s.step_id}
                step={s}
                state={stepState(s, taskById)}
                usage={usage}
                now={now}
                workflow={workflow}
                reload={reload}
                register={register}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

function sameGeometry(a: Geometry | null, b: Geometry): boolean {
  if (a === null) return false
  if (a.width !== b.width || a.height !== b.height || a.boxes.size !== b.boxes.size) return false
  for (const [id, box] of b.boxes) {
    const prev = a.boxes.get(id)
    if (!prev) return false
    if (prev.x !== box.x || prev.y !== box.y || prev.w !== box.w || prev.h !== box.h) return false
  }
  return true
}

/** A workflow id is `[a-z0-9_]`, but an id used in a CSS `url(#...)` reference
 *  must survive whatever a future id scheme allows, so it is narrowed here. */
function cssId(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, '_')
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

/**
 * THE NODE IS THE WAY IN.
 *
 * This was a plain `<div>` with no href and no onClick, so from "draft is
 * parked" there was no click that reached `draft`. It is now an `<a>` to
 * `#agents/task/<id>` -- the route App.tsx already resolves to the full agent
 * run: runtime environment, attempts, spend, duration, logs, checkpoints,
 * artifacts and outputs. An anchor rather than a click handler on purpose: it
 * is middle-clickable, copyable, and reachable by keyboard without this file
 * reimplementing any of that.
 *
 * A step with NO TASK is not a link, and says why. A dead link to a task that
 * does not exist would be the same defect one level down.
 *
 * A step whose task id we hold but whose task the read did not return IS a
 * link: the task exists, the run page fetches it by id, and the fact that this
 * board's page of 200 tasks did not include it says nothing about whether the
 * run can be opened.
 */
/**
 * HOW LONG THE STEP HAS BEEN RUNNING, or the reason that is not a number.
 *
 * `started_at` is written on DISPATCHED -> STARTING, so a QUEUED, LEASED or
 * DISPATCHED step legitimately has none and "0s" for it would be a lie in the
 * most literal sense. `completed_at` can also be missing on a task that went
 * terminal between polls, which is a THIRD case: it ran, it ended, and the
 * finish was never written -- "not recorded", exactly as the attempts drawer
 * says for peak memory.
 */
function ranCell(task: Task, now: number): Cell {
  const ms = (v: string | null) => (v ? new Date(v).getTime() : NaN)
  const started = ms(task.started_at)
  const completed = ms(task.completed_at)
  const terminal = TERMINAL_STATES.has(task.state)

  if (!Number.isFinite(started)) {
    const created = ms(task.created_at)
    return absentCell({
      text: 'not started',
      note: Number.isFinite(created)
        ? `started_at is written on DISPATCHED → STARTING. This step has not begun; it was submitted ${durationText(now - created)} ago.`
        : 'started_at is written on DISPATCHED → STARTING. This step has not begun.',
    })
  }
  if (Number.isFinite(completed)) {
    return measuredCell(durationText(completed - started), 'Start to finish, including any provider wait and any park.')
  }
  if (terminal) {
    return absentCell(FINISH_NOT_RECORDED)
  }
  return measuredCell(`${durationText(now - started)} so far`, 'Still running. This figure moves.')
}

/** Every figure one node shows, and what to say where there is none. */
interface StepFigures {
  ran: Cell
  cost: Cell
  tokens: Cell
  checkpoints: Cell
  /** The single sentence explaining the usage absences, or null if measured. */
  why: string | null
}

/**
 * The four figures, for one step.
 *
 * FOUR DIFFERENT ABSENCES, and the whole value of this function is keeping them
 * apart. A step with no task has nothing to measure; a step whose task was not
 * in the task read has figures nobody fetched; a step outside the attempt
 * sample has figures this board chose not to fetch; a step whose attempt read
 * failed has figures that could not be fetched. One "—" for all four sends an
 * operator to four different places at random.
 */
function figuresFor(state: StepState, usage: UsageRead, now: number): StepFigures {
  const allAbsent = (a: Absence): StepFigures => ({
    ran: absentCell(a),
    cost: absentCell(a),
    tokens: absentCell(a),
    checkpoints: absentCell(a),
    why: a.note,
  })

  if (state.kind === 'unstarted') return allAbsent(NEVER_RAN)
  if (state.kind === 'unknown') return allAbsent(STATE_UNREAD)

  const ran = ranCell(state.task, now)
  const taskId = state.task.id

  // The read has not landed. NOT an absence: the node draws a placeholder for
  // these rather than claiming the platform reported nothing.
  if (usage.kind === 'reading') {
    const pending: Absence = {
      text: 'reading',
      note: 'The attempt read for this step is still in flight.',
    }
    return { ran, cost: absentCell(pending), tokens: absentCell(pending), checkpoints: absentCell(pending), why: null }
  }

  const absentUsage = (a: Absence): StepFigures => ({
    ran,
    cost: absentCell(a),
    tokens: absentCell(a),
    checkpoints: absentCell(a),
    why: a.note,
  })

  if (usage.kind === 'failed') {
    return absentUsage({ text: USAGE_NOT_READ.text, note: `${USAGE_NOT_READ.note} (${usage.detail})` })
  }
  if (usage.usage === null) return absentUsage(USAGE_NOT_SAMPLED)

  const failed = usage.usage.failed.get(taskId)
  if (failed !== undefined) {
    return absentUsage({ text: USAGE_NOT_READ.text, note: `${USAGE_NOT_READ.note} (${failed})` })
  }
  const u = usage.usage.byTaskId.get(taskId)
  if (u === undefined) return absentUsage(USAGE_NOT_SAMPLED)
  // The read succeeded and there is nothing to sum. A FOURTH thing, and not
  // "the runner reported no cost": nothing has run.
  if (u.attempts === 0) return absentUsage(NO_ATTEMPT_YET)

  return {
    ran,
    cost: costCell(
      u.costUsd,
      `Summed over ${u.attemptsWithCost} of ${u.attempts} attempt${u.attempts === 1 ? '' : 's'} that reported one. Token cost only — no infrastructure cost is recorded anywhere.`,
    ),
    tokens: tokensOf(u),
    // COUNTED, not reported: the attempt document carries its own list of
    // checkpoint ids, so this figure is never absent once the attempts are in
    // hand, and a zero here IS the measurement. It renders as a digit while its
    // neighbours render as sentences, because its neighbours were not measured.
    checkpoints: countCell(
      u.checkpoints,
      USAGE_NOT_SAMPLED,
      u.checkpoints === 0
        ? 'No attempt document lists one. This zero was counted, not assumed.'
        : `Across ${u.attempts} attempt${u.attempts === 1 ? '' : 's'}. Contents are not recorded.`,
    ),
    why: null,
  }
}

/**
 * Input and output tokens as one cell.
 *
 * EACH HALF SUMS SEPARATELY, so a step whose runner reported input and no
 * output shows the half it has and says which -- `(input ?? 0) + (output ?? 0)`
 * counts the missing half as a zero, which is the same defect
 * `AgentDetail.tsx:408` records having already been fixed once at the tile
 * level.
 */
function tokensOf(u: StepUsage): Cell {
  const tin = tokenCell(u.inputTokens, '')
  const tout = tokenCell(u.outputTokens, '')
  if (tin.kind === 'absent' && tout.kind === 'absent') return absentCell(TOKENS_NOT_REPORTED)
  const parts: string[] = []
  if (tin.kind === 'measured') parts.push(`${tin.text} in`)
  if (tout.kind === 'measured') parts.push(`${tout.text} out`)
  const both = tin.kind === 'measured' && tout.kind === 'measured'
  return measuredCell(
    parts.join(' · '),
    both
      ? `Summed over ${u.attemptsWithTokens} of ${u.attempts} attempt${u.attempts === 1 ? '' : 's'} that reported tokens.`
      : tin.kind === 'measured'
        ? 'Input only. No attempt reported an output count, which is not the same as none.'
        : 'Output only. No attempt reported an input count, which is not the same as none.',
  )
}

function StepNode({
  step,
  state,
  usage,
  now,
  workflow,
  reload,
  register,
}: {
  step: WorkflowStep
  state: StepState
  usage: UsageRead
  now: number
  workflow: Workflow
  reload: () => void
  register: (id: string, el: HTMLElement | null) => void
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
  const taskId =
    state.kind === 'state' ? state.task.id : state.kind === 'unknown' ? state.taskId : null
  const f = figuresFor(state, usage, now)
  const pending = usage.kind === 'reading' && state.kind === 'state'

  const body = (
    <>
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

      {/* The run, as four figures. A placeholder while the attempt read is in
          flight -- a request that has not landed has made no claim, and
          "not reported" is a claim about the platform. */}
      <dl className="node-nums">
        <NodeNum label="ran" cell={f.ran} pending={false} />
        <NodeNum label="cost" cell={f.cost} pending={pending} />
        <NodeNum label="tokens" cell={f.tokens} pending={pending} />
        <NodeNum label="ckpts" cell={f.checkpoints} pending={pending} />
      </dl>
      {f.why !== null && <p className="node-why">{f.why}</p>}

      {step.depends_on.length > 0 && (
        /* NEVER ELLIPSED. This list is the complete statement of the graph in
           words, and the CSS that used to truncate it hid two of the five
           parents of the one node the whole screen was about. It wraps. */
        <div className="node-dep">
          <span aria-hidden>←</span>{' '}
          <span className="node-dep-list">{step.depends_on.join(', ')}</span>
        </div>
      )}
    </>
  )

  /*
   * THE NODE IS A DOORWAY AND IT ALSO CARRIES A CONTROL, and those are two
   * elements rather than one. Making the whole node an `<a>` is what gives the
   * graph its click-through -- from "draft is parked" straight to draft's own
   * screen, where its input and its output are. B28 puts a stop control on
   * every non-terminal step. A `<button>` INSIDE an `<a>` is invalid HTML and
   * the two activations fight each other, so they are SIBLINGS inside a
   * wrapper: the anchor carries the node body, the stop control sits beside it,
   * and neither swallows the other's click.
   *
   * `register` goes on the WRAPPER, not on the anchor: the edges are drawn to
   * the box a reader sees, and that box includes the control.
   */
  const stop =
    // Only when the step's TASK was actually joined: a step whose state is
    // `unknown` was not in the task read, and offering to stop something this
    // screen could not read would be acting on a guess. `StopRun` then decides
    // for itself whether the state is one the cancel route accepts, so a
    // terminal node draws nothing at all.
    state.kind === 'state' ? (
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
    ) : null

  if (taskId === null) {
    return (
      <div
        className={`node ${p.tone} is-unreachable`}
        title={p.title}
        ref={(el) => register(step.step_id, el)}
      >
        {body}
        <span className="node-go is-none">no run to open yet</span>
        {stop}
      </div>
    )
  }

  return (
    <div className={`node-wrap ${p.tone}`} ref={(el) => register(step.step_id, el)}>
      <a
        className={`node ${p.tone}`}
        href={`#agents/task/${encodeURIComponent(taskId)}`}
        title={`${p.title} — opens this agent run`}
      >
        {body}
        <span className="node-go">open run →</span>
      </a>
      {/* Outside the anchor, deliberately. The two things a reader might want
          from a node -- "show me this run" and "stop this run" -- are different
          actions, and only one of them may be reachable by clicking the box. */}
      {stop}
      <div className="node-links">
        <a href={`#agents/task/${encodeURIComponent(taskId)}`}>input &amp; output →</a>
        <a href={`#agents/task/${encodeURIComponent(taskId)}/attempts`}>attempts →</a>
      </div>
    </div>
  )
}

/**
 * One figure on a node.
 *
 * `is-absent` is the dashed, faint treatment the metric tiles use, and it is
 * applied to ABSENCE only. A read still in flight gets `is-reading` and a
 * moving bar instead, because the two say different things: one is a statement
 * about the platform, the other is a statement about this request.
 */
function NodeNum({ label, cell, pending }: { label: string; cell: Cell; pending: boolean }) {
  const cls = pending ? 'is-reading' : cell.kind === 'absent' ? 'is-absent' : ''
  return (
    <div className={`node-num ${cls}`.trimEnd()} title={cell.note || undefined}>
      <dt>{label}</dt>
      <dd>{pending ? <span className="node-reading" aria-label="reading" /> : cell.text}</dd>
    </div>
  )
}
