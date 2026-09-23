import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  loadWorkflowBoard,
  loadWorkflowUsage,
  type StepUsage,
  type WorkflowBoard,
  type WorkflowUsage,
} from './api'
import {
  edgePath,
  layoutOf,
  levelsOf,
  profileMix,
  shapeOf,
  stepDuration,
  workflowSpend,
  NODE_W,
  PAD,
  COL_GAP,
  type DagShape,
  type StepDuration,
  type WorkflowSpend,
} from './dag'
import {
  absentCell,
  costCell,
  countCell,
  durationText,
  measuredCell,
  tokenCell,
  type Absence,
  type Cell,
  FINISH_NOT_RECORDED,
  NEVER_RAN,
  NO_ATTEMPT_YET,
  STATE_UNREAD,
  TOKENS_NOT_REPORTED,
  USAGE_NOT_READ,
  USAGE_NOT_SAMPLED,
} from './measure'
import { workflowDispatchOf } from './Dispatch'
import { Id, Screen, timeAgo } from './Shell'
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
  workflowHeaderState,
  TERMINAL_STATES,
} from './types'

/**
 * The workflow board. TWO FORMS OF ONE THING, and the collapsed one is the
 * default because it is the one a reader lands on.
 *
 * COLLAPSED is a single horizontal bar per workflow, the way a CI list is a
 * row per run: identity, derived state, progress, SHAPE, runner mix, spend and
 * when it last moved. The point of the line is to let somebody pick which of
 * ten workflows to open WITHOUT OPENING ANY, and the field that earns its place
 * hardest is the shape -- `1 → 5 → 1` next to a mini-map of the real edges.
 * Five steps in parallel and five steps in a chain have the same count, the
 * same fraction done and the same cost; they are completely different runs, and
 * a list that renders them identically is the defect the graph work exists to
 * fix. It must survive being collapsed or it has not been fixed.
 *
 * EXPANDED is a canvas: real edges drawn between generous node cards, flowing
 * left to right along dependency depth. Every node keeps the three facts it has
 * always carried -- the step NAME, its STATUS and its RUNNER PROFILE -- and
 * adds the one that was missing, how long it has taken.
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

  // ABOVE THE `key`, DELIBERATELY. `reloads` remounts `Screen`, so anything
  // held inside it is lost on every stop-and-reload; a board that snapped every
  // open workflow shut the moment you stopped one step would be unusable.
  const [mode, setMode] = useState<BoardMode>('collapsed')
  const [open, setOpen] = useState<Record<string, boolean>>({})

  // A board-wide instruction overrules the per-card ones. Keeping stale
  // overrides would make "Collapse all" leave three cards open with no way to
  // tell why.
  const chooseMode = useCallback((m: BoardMode) => {
    setMode(m)
    setOpen({})
  }, [])

  const toggle = useCallback(
    (id: string, expanded: boolean) => setOpen((o) => ({ ...o, [id]: !expanded })),
    [],
  )

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
          mode={mode}
          chooseMode={chooseMode}
          open={open}
          toggle={toggle}
          reload={reload}
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
  mode,
  chooseMode,
  open,
  toggle,
  reload,
}: {
  board: WorkflowBoard
  mode: BoardMode
  chooseMode: (m: BoardMode) => void
  open: Record<string, boolean>
  toggle: (id: string, expanded: boolean) => void
  reload: () => void
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

  return (
    <>
      {board.statesDetail !== null && <StatesUnavailable detail={board.statesDetail} />}
      {usage.kind === 'ready' && usage.usage !== null && <SampleNote usage={usage.usage} />}
      <ModeControl mode={mode} onChoose={chooseMode} />
      <div className="wf-board">
        {board.workflows.map((w) => (
          <WorkflowCard
            key={w.workflow_id}
            workflow={w}
            taskById={board.taskById}
            expanded={open[w.workflow_id] ?? mode === 'full'}
            onToggle={toggle}
            reload={reload}
            usage={usage}
          />
        ))}
      </div>
    </>
  )
}

type BoardMode = 'collapsed' | 'full'

/**
 * The board-wide default. Two states, named for what they show rather than for
 * what they do: "Collapsed" is a list of one-line bars, "Full DAG" opens every
 * canvas at once.
 */
function ModeControl({ mode, onChoose }: { mode: BoardMode; onChoose: (m: BoardMode) => void }) {
  return (
    <div className="wf-modebar" role="group" aria-label="How much of each workflow to show">
      {(
        [
          ['collapsed', 'Collapsed'],
          ['full', 'Full DAG'],
        ] as const
      ).map(([value, label]) => (
        <button
          key={value}
          type="button"
          className={`wf-mode${mode === value ? ' is-on' : ''}`}
          aria-pressed={mode === value}
          onClick={() => onChoose(value)}
        >
          {label}
        </button>
      ))}
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
function rollupLine(workflow: Workflow): {
  text: string
  trustworthy: boolean
  done: number
  total: number
} {
  const roll = workflow.rollup
  const total = workflow.steps.length
  if (!roll) {
    // `state_source: 'stored'` -- no read route produces this, but a payload
    // without a rollup must say it has no census rather than invent a zero one.
    return {
      // "not counted" attached to the STEP COUNT is the defect this line
      // was rewritten to remove: it rendered "3 steps - not counted" and
      // told the reader the console could not count to six. The count is
      // the length of an array that was read and is always knowable. What
      // is missing here is the per-step census, so that is what says so.
      text: `${total} step${total === 1 ? '' : 's'} · progress not derived`,
      trustworthy: false,
      done: 0,
      total,
    }
  }
  if (!roll.complete) {
    const n = roll.unreadable_steps.length
    return { text: `${n} of ${total} steps: state unread`, trustworthy: false, done: 0, total }
  }
  const done = roll.counts.SUCCEEDED ?? 0
  const failed = roll.counts.FAILED ?? 0
  const unstarted = roll.counts.unstarted ?? 0
  const parts = [`${done}/${total} done`]
  if (failed > 0) parts.push(`${failed} failed`)
  if (unstarted > 0) parts.push(`${unstarted} not started`)
  return { text: parts.join(' · '), trustworthy: true, done, total }
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

export function WorkflowCard({
  workflow,
  taskById,
  expanded,
  usage,
  onToggle,
  reload,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  expanded: boolean
  usage: UsageRead
  onToggle: (id: string, expanded: boolean) => void
  reload: () => void
}) {
  const roll = rollupLine(workflow)
  const shape = shapeOf(workflow.steps)
  const spend = workflowSpend(workflow.steps, taskById)
  const header = workflowHeaderState(workflow)
  const bodyId = `wf-body-${workflow.workflow_id}`

  return (
    <section className={`section wf-card${expanded ? ' is-open' : ''}`}>
      {/* STILL AN <h2>, and the button is inside it rather than around it. The
          bar is this panel's heading -- it is how the workflow is named on the
          board -- and demoting it to a bare <button> would take the row out of
          the document outline that every other section is in. `.section > h2`
          uppercases, so `.wf-bar` turns that off again for its own contents;
          B17's rule on `.id` is what keeps the id itself lowercase either
          way, and it is asserted on the rendered style in brand.test.tsx. */}
      <h2 className="wf-h">
        <button
          type="button"
          className="wf-bar"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => onToggle(workflow.workflow_id, expanded)}
        >
          <span className="wf-caret" aria-hidden>
            {expanded ? '▾' : '▸'}
          </span>
          {/* B17. This id is lowercase everywhere it actually lives --
              Firestore, the API, the logs and the `#agents/task/<id>` address.
              Printed as WF_BCDC9180… it cannot be pasted anywhere, which is
              the only thing an id is for. */}
          <Id>{workflow.workflow_id}</Id>
          {/* `workflowHeaderState`, NOT `workflow.state`. The same field name
              carries two different meanings depending on which server answered:
              derived from the step tasks on this read, or the Firestore cache
              that nothing advanced before rollup.py existed and which therefore
              reads QUEUED for a workflow's whole life. Printing it raw is how
              this card came to say "queued" in its heading while the step chips
              under it read succeeded and failed -- one card, one typeface, and
              nothing saying which to believe. An underived read claims no state
              at all and names the stored copy as a stored copy. */}
          <span className={`wf-state ${header.tone}`} title={header.title}>
            <span aria-hidden>{header.glyph}</span> {header.word}
          </span>
          <Progress roll={roll} />
          <Shape shape={shape} />
          <Mix steps={workflow.steps} />
          <Spend spend={spend} />
          <span className="wf-when" title={`Last state change: ${workflow.updated_at}`}>
            {timeAgo(workflow.updated_at)}
          </span>
          {/* ALWAYS RENDERED, empty or not. `.wf-bar` is a grid with one column
              per field, and a conditionally-absent child would shift every
              field after it into the wrong column on exactly the rows that
              have something to say. */}
          <span className="wf-flags">
            {/* Still a separate annotation, not folded into the state above.
                `cancel_requested` is a REQUEST: a step holding a lease keeps it
                until the worker or the reconciler releases it, so between the
                request and the release the workflow really is still running. */}
            {workflow.cancel_requested && <span className="tag wait">cancel requested</span>}
          </span>
        </button>
      </h2>

      {expanded && (
        <div className="wf-body" id={bodyId}>
          <StateDrift drift={workflow.drift} />
          <WorkflowDispatchLine workflow={workflow} taskById={taskById} />
          <WorkflowGraph workflow={workflow} taskById={taskById} usage={usage} reload={reload} />
        </div>
      )}
    </section>
  )
}

/**
 * PROGRESS, and the reason it is not always a bar.
 *
 * A meter is a claim that the numbers behind it are a census. When the server
 * reports `complete: false` the census failed, and drawing "2 of 6" as a
 * two-thirds-empty bar would turn a failed read into a measurement -- the
 * exact substitution this console exists to refuse. In that case the words
 * survive, struck through, and no bar is drawn at all.
 */
function Progress({ roll }: { roll: { text: string; trustworthy: boolean; done: number; total: number } }) {
  if (!roll.trustworthy) {
    return <span className="wf-progress untrusted">{roll.text}</span>
  }
  const pct = roll.total === 0 ? 0 : Math.round((roll.done / roll.total) * 100)
  return (
    <span className="wf-progress">
      <span
        className="wf-meter"
        role="img"
        aria-label={`${roll.done} of ${roll.total} steps done`}
        title={roll.text}
      >
        <span className="wf-meter-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="wf-progress-text">{roll.text}</span>
    </span>
  )
}

/**
 * THE TOPOLOGY, COLLAPSED. Two renderings of one layout: the widths as text
 * (`1 → 5 → 1`) and a mini-map drawn from the SAME `layoutOf` the expanded
 * canvas uses, scaled down, with the real edges and one dot per step coloured
 * by that step's state.
 *
 * The text is what a screen reader and a test can read; the map is what the eye
 * gets in 90 pixels. Neither is decoration: without them a fan-out and a chain
 * are the same row.
 */
function Shape({ shape }: { shape: DagShape }) {
  // NO MINI-MAP. The collapsed row draws no graph at all: a 60px thumbnail of
  // a six-node DAG resolves into a smudge at the size a one-line row allows,
  // and a picture too small to read is worse than no picture -- it occupies
  // the space a legible fact would have had. The graph is what expanding is
  // FOR. What the row keeps is the shape as text, `1 -> 5 -> 1`, which tells a
  // fan-out from a chain at a glance, survives a screen reader, and costs one
  // column.
  return (
    <span className="wf-shape" title={shape.label}>
      <span className="wf-shape-text" data-kind={shape.kind}>
        {shape.text}
      </span>
    </span>
  )
}



/**
 * THE RUNNER MIX. Which models this workflow is spending the subscription on,
 * commonest first, two named and the rest counted.
 *
 * It earns the line because it is the field that decides whether a quota park
 * is about to matter to THIS workflow: a twenty-step run that is nine
 * claude-code steps and a browser step behaves nothing like one that is twenty
 * mock steps, and the difference is invisible from the id, the state and the
 * progress. The full per-step profile stays on every expanded node; this never
 * replaces it.
 */
function Mix({ steps }: { steps: WorkflowStep[] }) {
  const mix = profileMix(steps)
  if (mix.length === 0) return <span className="wf-mix" />
  const shown = mix.slice(0, 2)
  const rest = mix.length - shown.length
  const all = mix.map((m) => `${m.profile} ×${m.count}`).join(', ')
  return (
    <span className="wf-mix" title={`Runner profiles: ${all}`}>
      {shown.map((m) => (
        <span className="wf-chip" key={m.profile}>
          {m.profile}
          <span className="wf-chip-n">×{m.count}</span>
        </span>
      ))}
      {rest > 0 && <span className="wf-chip more">+{rest}</span>}
    </span>
  )
}

/**
 * SPEND, and the one rule that outranks everything on this screen.
 *
 * `usd === null` means NO STEP REPORTED A COST. It is rendered "not reported",
 * never `$0.00`: an absent measurement is not a free run. A step that reported
 * `0` -- a mock profile does exactly that -- is a MEASURED zero and renders as
 * a digit.
 *
 * The coverage travels with the figure whenever it is partial, because a total
 * over three of six steps is not the workflow's spend. Four decimals under ten
 * dollars for the same reason Overview uses them: a single attempt is routinely
 * worth $0.0312, and $0.03 loses a third of the figures on this board.
 */
function Spend({ spend }: { spend: WorkflowSpend }) {
  if (spend.usd === null) {
    return (
      <span
        className="wf-spend absent"
        title={
          spend.joined === 0
            ? 'No task was joined for this workflow, so nothing could have reported a cost. This is an absent measurement, not $0.00.'
            : `None of the ${spend.joined} joined step${spend.joined === 1 ? '' : 's'} reported a cost. This is an absent measurement, not $0.00.`
        }
      >
        not reported
      </span>
    )
  }
  const partial = spend.covered < spend.steps
  return (
    <span
      className="wf-spend"
      title={`${spend.covered} of ${spend.steps} steps reported a cost.${
        partial ? ' The rest have not reported one, so this is a floor rather than the total.' : ''
      }`}
    >
      {money(spend.usd)}
      {partial && (
        <span className="wf-spend-cov">
          {spend.covered}/{spend.steps}
        </span>
      )}
    </span>
  )
}

/** Dollars of token cost. Four decimals under ten dollars: a single attempt is
 *  routinely worth $0.0312, and rounding that to $0.03 loses a third of the
 *  figures on this board to "$0.00". */
function money(v: number): string {
  return v < 10 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`
}

/**
 * HOW MANY PULL REQUESTS THIS WORKFLOW IS GOING TO PRODUCE.
 *
 * The same sentence the submit form showed when it was chosen, computed from
 * the same function over the same step count -- so "I picked one pull request"
 * and "this workflow will open one pull request" cannot come apart.
 *
 * EXPANDED ONLY, and that is a judgement rather than an oversight: the strategy
 * does not change while a workflow runs, so it cannot help a reader choose
 * which of ten rows to open. It is a property of what comes out at the end,
 * which is a thing you read once you have opened the one you care about.
 */
function WorkflowDispatchLine({
  workflow,
  taskById,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
}) {
  // Rolled up from the steps' TASKS, exactly as `codec.workflow_dispatch` does
  // server-side -- the frozen `Workflow` has no metadata field, so there is
  // nowhere else it could live. Null when the task join produced nothing to
  // read, which the banner above is already explaining.
  const tasks = workflow.steps
    .map((s) => (s.task_id ? (taskById?.get(s.task_id) ?? null) : null))
    .filter((t): t is Task => t !== null)
  const dispatch = workflowDispatchOf(tasks)
  return (
    <WorkflowDispatch
      dispatch={dispatch?.dispatch ?? null}
      integratorTaskId={dispatch?.integratorTaskId ?? null}
      steps={workflow.steps.length}
      joined={tasks.length > 0}
    />
  )
}

/**
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

/**
 * A clock that ticks, so "running 4m 12s" is true a second later.
 *
 * SCOPED TO THE OPEN CANVAS, following the same rule Overview's `useNow`
 * records: a 1Hz clock held at the top of the board would re-render every
 * collapsed row once a second to move one number inside one expanded card. This
 * hook only exists while a canvas is mounted, which is only while a workflow is
 * open.
 */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  return now
}

/**
 * THE CANVAS. Real edges between real node cards, flowing left to right.
 *
 * The positions come from `layoutOf`, which is pure and tested, so the edges
 * and the cards cannot disagree: both read the same numbers. The SVG holds only
 * the edges -- the cards are HTML on top of it, because a node carries an
 * anchor, a `title` and a stop button, and those are not things to re-implement
 * inside an `<svg>`.
 *
 * It scrolls horizontally rather than shrinking. A twenty-step workflow across
 * six levels is genuinely wider than a phone, and scaling it down to fit turns
 * the step names into texture.
 */
function WorkflowGraph({
  workflow,
  taskById,
  usage,
  reload,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  usage: UsageRead
  reload: () => void
}) {
  const now = useNow()
  const layout = layoutOf(workflow.steps)
  const levels = levelsOf(workflow.steps)

  if (layout.nodes.length === 0) {
    return <p className="muted small">This workflow has no steps.</p>
  }

  return (
    <div className="wf-canvas-wrap">
      {/* The column captions, positioned from the SAME constants the canvas
          lays out with rather than from a number repeated in the stylesheet.
          A caption that drifts one column off the nodes it names is worse
          than no caption. */}
      <ol className="wf-legend" aria-label="Dependency levels" style={{ width: layout.width }}>
        {levels.map((level, i) => (
          <li key={i} style={{ left: PAD + i * (NODE_W + COL_GAP), width: NODE_W }}>
            {i === 0 ? 'starts' : 'then'}
            {level.length > 1 ? ` ${level.length} in parallel` : ''}
          </li>
        ))}
      </ol>
      <div className="wf-canvas" style={{ width: layout.width, height: layout.height }}>
        <svg
          className="wf-edges"
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          aria-hidden
          focusable="false"
        >
          <defs>
            <marker
              id={`arrow-${workflow.workflow_id}`}
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path className="wf-arrowhead" d="M 0 1 L 7 4 L 0 7 z" />
            </marker>
          </defs>
          {layout.edges.map((e) => (
            <path
              key={`${e.from}->${e.to}`}
              className="wf-edge"
              d={edgePath(e)}
              markerEnd={`url(#arrow-${workflow.workflow_id})`}
            />
          ))}
        </svg>
        {layout.nodes.map((n) => (
          <StepNode
            key={n.step.step_id}
            step={n.step}
            state={stepState(n.step, taskById)}
            workflow={workflow}
            now={now}
            usage={usage}
            x={n.x}
            y={n.y}
            h={n.h}
            reload={reload}
          />
        ))}
      </div>
    </div>
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
  now,
  usage,
  x,
  y,
  h,
  reload,
}: {
  step: WorkflowStep
  state: StepState
  workflow: Workflow
  now: number
  usage: UsageRead
  x: number
  y: number
  h: number
  reload: () => void
}) {
  const p = present(state)
  const dur = stepDuration(state, now)
  const f = figuresFor(state, usage, now)
  // A read still IN FLIGHT is not an absence. "not reported" is a claim about
  // the platform; a request that has not landed has made no claim at all, so
  // the cell draws a moving placeholder instead of a sentence.
  const pending = usage.kind === 'reading' && state.kind === 'state'
  // Read off the step's own task, so the node that opens the pull request is
  // marked in the graph rather than only named in the line above it. Silent on
  // a step with no task and on a `contributor`: every step of an `integrate`
  // workflow but one is a contributor, so a badge on each would mark nothing.
  //
  // Through `dispatchOf`, not off the field: it is the one reader that decides
  // what an absent or unrecognised block means, and a second one here is how
  // two screens start disagreeing about the same task.
  const role = state.kind === 'state' ? (dispatchOf(state.task)?.role ?? null) : null
  const taskId = state.kind === 'state' ? state.task.id : state.kind === 'unknown' ? state.taskId : null

  return (
    <div
      className={`node ${p.tone}`}
      title={p.title}
      style={{ left: x, top: y, width: NODE_W, height: h }}
    >
      <div className="node-id">
        {/* The step NAME, and where its run actually lives. An id you cannot
            reach is a label; `#agents/task/<id>` is the address every other
            screen uses for the same task. Only when a task exists: a step the
            workflow has not reached has nothing to open. */}
        {taskId ? (
          <a href={`#agents/task/${encodeURIComponent(taskId)}`}>{step.step_id}</a>
        ) : (
          step.step_id
        )}
        {role === 'integrator' && (
          <span className="tag ok" title="This step merges the other steps' branches and opens the workflow's single pull request.">
            opens the PR
          </span>
        )}
      </div>
      <div className="node-state">
        <span aria-hidden>{p.glyph}</span> {p.word}
      </div>
      {/* The llm used. The owner's "the way it's done today", kept verbatim. */}
      <div className="node-meta">{step.runner_profile}</div>
      <StepTime dur={dur} />

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

      {/* The two things a reader wants from a node that are not "stop it":
          what this step read and wrote, and what it tried. Both are routes the
          console already resolves; a node without them is a dead end, which is
          what it was before the graph work. Only drawn when a task exists --
          a step the workflow has not reached has nothing to open. */}
      {taskId !== null && (
        <div className="node-links">
          <a href={`#agents/task/${encodeURIComponent(taskId)}`}>input &amp; output →</a>
          <a href={`#agents/task/${encodeURIComponent(taskId)}/attempts`}>attempts →</a>
        </div>
      )}

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

/**
 * HOW LONG THIS STEP HAS TAKEN, AND WHAT KIND OF TIME THAT IS.
 *
 * Five different things are rendered five different ways, and the first one is
 * the rule the whole product rests on: a step that has not started HAS NO
 * DURATION. It reads "not started", never `0s`, because `0s` is a measurement
 * and nothing measured it. `stepDuration`'s absent arm carries no number at
 * all, so this component could not print one if it tried.
 *
 * The other four are distinguished because they answer different questions:
 * time queued and time parked are time WAITED and are drawn as waiting; a
 * running step's figure is elapsed and still moving; only a finished step has a
 * duration in the ordinary sense. The measured six-step run this was designed
 * against had five steps waiting exactly 153s and then running 68-92s -- one
 * number covering both would have hidden the entire story of that workflow.
 */
function StepTime({ dur }: { dur: StepDuration }) {
  return (
    <div className={`node-dur is-${dur.kind}`} title={dur.note}>
      {dur.text}
    </div>
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
