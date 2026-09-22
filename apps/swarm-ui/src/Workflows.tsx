import { useEffect, useMemo, useState } from 'react'
import { loadWorkflowBoard, loadWorkflowUsage, type StepUsage, type WorkflowBoard, type WorkflowUsage } from './api'
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
import { Screen, timeAgo } from './Shell'
import {
  TERMINAL_STATES,
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
 *
 * WHAT EACH STEP PRODUCED IS ALSO ON THE NODE, and it used to be nowhere. The
 * node carried name, state, runner profile and dependency -- four facts about
 * the PLAN and none about the RUN -- so the one screen in the product about a
 * multi-agent run reported neither duration, nor cost, nor tokens, nor a way to
 * reach what the step read or wrote. All four exist: duration comes off the
 * joined task, the other three off the attempts (`codec.attempt_from_dict`
 * decodes `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
 * `cache_creation_input_tokens` and `cost_usd`), and the step id is now a link
 * into the task's own screen, where its input and output are.
 *
 * Every one of those figures is nullable and NOT ONE of them may render as a
 * zero it did not measure. `measure.ts` holds that rule; this screen holds only
 * the four different reasons a workflow board can lack a figure, which are four
 * different sentences: the step has no task, the task was not in the task read,
 * the task was outside the attempt sample, or the attempt read failed.
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
      {(d) => <Board board={d} />}
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
function Board({ board }: { board: WorkflowBoard }) {
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
      {board.workflows.map((w) => (
        <WorkflowCard
          key={w.workflow_id}
          workflow={w}
          taskById={board.taskById}
          usage={usage}
          now={now}
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
  usage,
  now,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  usage: UsageRead
  now: number
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
                  usage={usage}
                  now={now}
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
}: {
  step: WorkflowStep
  state: StepState
  usage: UsageRead
  now: number
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
  const taskId = state.kind === 'state' ? state.task.id : state.kind === 'unknown' ? state.taskId : null
  const f = figuresFor(state, usage, now)
  const pending = usage.kind === 'reading' && state.kind === 'state'

  return (
    <div className={`node ${p.tone}`}>
      <div className="node-id">
        {/* THE NODE IS A DOORWAY, not a picture. The task id used to live only
            in a native `title`, so from "draft is parked" there was no click
            that reached draft: the route was Agents → Waiting → find the row.
            The step's own screen is where its INPUT and its OUTPUT are. */}
        {taskId === null ? (
          <span title={p.title}>{step.step_id}</span>
        ) : (
          <a
            className="node-open"
            href={`#agents/task/${encodeURIComponent(taskId)}`}
            title={`${p.title}\nOpen this step: its input, its output and every attempt.`}
          >
            {step.step_id}
          </a>
        )}
        {role === 'integrator' && (
          <span className="tag ok" title="This step merges the other steps' branches and opens the workflow's single pull request.">
            opens the PR
          </span>
        )}
      </div>
      <div className="node-state" title={p.title}>
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
        <div className="node-dep" title={`depends on ${step.depends_on.join(', ')}`}>
          ← {step.depends_on.join(', ')}
        </div>
      )}
      {taskId !== null && (
        <div className="node-links">
          <a href={`#agents/task/${encodeURIComponent(taskId)}`}>input &amp; output →</a>
          <a href={`#agents/task/${encodeURIComponent(taskId)}/attempts`}>attempts →</a>
        </div>
      )}
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
