// A workflow's other two views -- Timeline and Table -- and the two scrubbers,
// as arithmetic.
//
// PURE, FOR THE REASON `dag.ts` IS. No React, no DOM, and `now` is always a
// parameter. The hard part of both views is deciding WHAT IS KNOWN about a
// step's time, and that decision has to be assertable without mounting
// anything. `WorkflowViews.tsx` is then only pixels.
//
// WHY THESE VIEWS EXIST (redesign-v2 §2.3): "the view mode is a property of the
// pane, not a route. The same workflow becomes a DAG, a wall-clock timeline and
// a table without navigating." The graph answers "what depended on what"; the
// timeline answers "where did the time go"; the table answers "which of sixty
// steps is the outlier". One object, three questions.
//
// THE RULE THIS FILE KEEPS, which is the one `dag.ts` keeps: an absent
// measurement never becomes a number, and a measured zero stays a digit. Here
// it has a second half that only a TIME AXIS has to face -- viz #2, "that a
// bar's length is agent work: it includes queue and parks. That a task still
// running has an end." So:
//
//   * time WAITED and time RUN are different spans with different marks, never
//     one bar coloured by the step's final state;
//   * a step that is still going has an OPEN span that reaches "now" and no end;
//   * a step that ended without recording when draws NO span past its start --
//     any length would be an invented end, and a span to "now" would say it is
//     still going;
//   * `started_at` is overwritten per attempt (control.py:410-413), so a step
//     that is waiting AFTER an earlier attempt ran still carries a start time.
//     It is drawn as waiting, never as running.

import { NEVER_STARTED_WORD, levelsOf } from './dag'
import {
  absentCell,
  costCell,
  countCell,
  durationText,
  measuredCell,
  tokenCell,
  TOKENS_NOT_REPORTED,
  type Absence,
  type Cell,
} from './measure'
import {
  TERMINAL_STATES,
  type AttemptRow,
  type StepState,
  type TaskState,
  type Workflow,
  type WorkflowStep,
} from './types'

// ---------------------------------------------------------------------------
// The modes
// ---------------------------------------------------------------------------

/** How one open workflow is drawn. The board adds a fourth, `rows`, which is
 *  "none of them open". */
export type WorkflowView = 'graph' | 'timeline' | 'table'

/** In the order the segmented control shows them: the shape first, then time,
 *  then the table you sort to find the outlier. */
export const WORKFLOW_VIEWS: readonly WorkflowView[] = ['graph', 'timeline', 'table']

/** One word each, naming what you get rather than how it is made -- the rule
 *  `Rows`/`Graph` already follow on the board's own control. */
export const VIEW_LABEL: Readonly<Record<WorkflowView, string>> = {
  graph: 'Graph',
  timeline: 'Timeline',
  table: 'Table',
}

// ---------------------------------------------------------------------------
// Where a step's time went
// ---------------------------------------------------------------------------

/**
 * One span on a step's timeline row.
 *
 *   waited   submission to the LATEST start. Time spent queued, parked, being
 *            dispatched and cold-starting -- and, on a retried step, every
 *            earlier attempt, because `started_at` is the latest start only.
 *   waiting  the same, still going: a step that is not running now.
 *   ran      the latest start to the recorded finish.
 *   running  the latest start to now, and it has not ended.
 */
export type SpanKind = 'waited' | 'waiting' | 'ran' | 'running'

export interface Span {
  readonly kind: SpanKind
  readonly from: number
  readonly to: number
  /** No end has been recorded: the span reaches `now` and must not be drawn as closed. */
  readonly open: boolean
}

/**
 * Why a row has fewer spans than it would, in the words the row prints.
 *
 * `never` is a TERMINAL step with no start time: cancelled while it queued, the
 * ordinary case. Its wait is drawn and nothing after it is, and the word says
 * why -- the node, the table and the inspector print the same one.
 */
export type TimesMarkKind = 'unstarted' | 'unread' | 'unrecorded' | 'untimed' | 'never'

export interface TimesMark {
  readonly kind: TimesMarkKind
  readonly text: string
  readonly note: string
}

export interface StepTimes {
  readonly spans: readonly Span[]
  readonly mark: TimesMark | null
  /** `attempt_count` when it is more than one: the waited span covers the earlier ones too. */
  readonly attempts: number | null
  /** Submission to latest start, or to now while still waiting. Null when not measurable. */
  readonly waitedMs: number | null
  readonly waitedOpen: boolean
  /** Latest start to finish, or to now while running. Null when the step is not running and has no recorded run. */
  readonly ranMs: number | null
  /** The whole row as one sentence -- the track's accessible name. */
  readonly sentence: string
}

const at = (v: string | null | undefined): number => (v ? new Date(v).getTime() : NaN)
const finite = (n: number): boolean => Number.isFinite(n)

/** Live: the only two states in which the time since `started_at` is time run. */
const RUNNING_STATES: ReadonlySet<TaskState> = new Set<TaskState>(['STARTING', 'RUNNING'])

function mark(kind: TimesMarkKind, text: string, note: string): TimesMark {
  return { kind, text, note }
}

export function stepTimes(state: StepState, now: number): StepTimes {
  if (state.kind === 'unstarted') {
    const m = mark(
      'unstarted',
      'not started',
      'This step has no task yet: the workflow has not reached it, so there is no time to draw.',
    )
    return { spans: [], mark: m, attempts: null, waitedMs: null, waitedOpen: false, ranMs: null, sentence: m.note }
  }
  if (state.kind === 'unknown') {
    const m = mark(
      'unread',
      'task unread',
      `Task ${state.taskId} was not in the task read, so none of its times were looked at. That is unknown, not idle.`,
    )
    return { spans: [], mark: m, attempts: null, waitedMs: null, waitedOpen: false, ranMs: null, sentence: m.note }
  }

  const task = state.task
  const created = at(task.created_at)
  const started = at(task.started_at)
  const completed = at(task.completed_at)
  const attempts = task.attempt_count > 1 ? task.attempt_count : null
  const retried =
    attempts === null
      ? ''
      : ` This task has had ${attempts} attempts, so the wait runs to its latest start and includes the earlier ones.`

  // FINISHED, WITH A RECORDED END.
  if (finite(completed)) {
    const from = finite(created) ? created : started
    if (!finite(from)) {
      const m = mark(
        'untimed',
        'start not recorded',
        'This step finished, but neither a submission time nor a start time was recorded, so nothing can be placed on the axis.',
      )
      return { spans: [], mark: m, attempts, waitedMs: null, waitedOpen: false, ranMs: null, sentence: m.note }
    }
    if (finite(started) && started >= from && started <= completed) {
      const spans: Span[] = []
      if (started > from) spans.push({ kind: 'waited', from, to: started, open: false })
      spans.push({ kind: 'ran', from: started, to: completed, open: false })
      const waitedMs = finite(created) ? started - created : null
      return {
        spans,
        mark: null,
        attempts,
        waitedMs,
        waitedOpen: false,
        ranMs: completed - started,
        sentence:
          (waitedMs === null
            ? 'No submission time was recorded, so the wait before it started is unknown. '
            : `waited ${durationText(waitedMs)} from submission to its ${attempts ? 'latest ' : ''}start, then `) +
          `ran ${durationText(completed - started)} and ended ${task.state.toLowerCase()}.${retried}`,
      }
    }
    // NO START RECORDED, BUT AN END. A step cancelled before it ever ran is the
    // ordinary case. None of this span is drawn as running, because nothing
    // says any of it was -- and the word after it says so, in the same two
    // words the node and the table print for this step. (A start that IS
    // recorded but falls outside the span -- clock skew -- lands here too, and
    // gets no word: something did record a start.)
    const sentence = `ended ${task.state.toLowerCase()} ${durationText(completed - from)} after submission with no start recorded, so none of that time is shown as running.${retried}`
    return {
      spans: [{ kind: 'waited', from, to: completed, open: false }],
      mark: finite(started) ? null : mark('never', NEVER_STARTED_WORD, sentence),
      attempts,
      waitedMs: completed - from,
      waitedOpen: false,
      ranMs: null,
      sentence,
    }
  }

  // TERMINAL WITHOUT AN END AND WITHOUT A START: it never got as far as
  // STARTING, and nobody wrote when it stopped. No span at all -- the wait has
  // no end to draw to -- and the same word as the arm above.
  if (TERMINAL_STATES.has(task.state) && !finite(started)) {
    const m = mark(
      'never',
      NEVER_STARTED_WORD,
      `This step is ${task.state.toLowerCase()} with no start time and no completion time: it never started, and when it stopped was not recorded.`,
    )
    return { spans: [], mark: m, attempts, waitedMs: null, waitedOpen: false, ranMs: null, sentence: m.note + retried }
  }

  // TERMINAL WITHOUT AN END. It ran, it ended, and nobody wrote when. The wait
  // up to its start is real and is drawn; nothing past the start is.
  if (TERMINAL_STATES.has(task.state)) {
    const spans: Span[] =
      finite(created) && finite(started) && started > created
        ? [{ kind: 'waited', from: created, to: started, open: false }]
        : []
    const m = mark(
      'unrecorded',
      'end not recorded',
      `This step is ${task.state.toLowerCase()} and carries no completion time, so how long it ran was never recorded. No bar is drawn past its start: any length would be an invented end, and a bar to now would say it is still going.`,
    )
    return {
      spans,
      mark: m,
      attempts,
      waitedMs: spans.length > 0 ? started - created : null,
      waitedOpen: false,
      ranMs: null,
      sentence: m.note + retried,
    }
  }

  // RUNNING NOW.
  if (RUNNING_STATES.has(task.state) && finite(started)) {
    const to = Math.max(started, now)
    const spans: Span[] = []
    if (finite(created) && started > created) spans.push({ kind: 'waited', from: created, to: started, open: false })
    spans.push({ kind: 'running', from: started, to, open: true })
    const waitedMs = finite(created) && started >= created ? started - created : null
    return {
      spans,
      mark: null,
      attempts,
      waitedMs,
      waitedOpen: false,
      ranMs: to - started,
      sentence:
        (waitedMs === null ? '' : `waited ${durationText(waitedMs)} from submission to its ${attempts ? 'latest ' : ''}start, then `) +
        `running ${durationText(to - started)} so far, with no end yet.${retried}`,
    }
  }

  // WAITING NOW -- queued, ready, leased, dispatched, or parked. Everything
  // since submission is time waited, including any earlier attempt's run,
  // because the start this task carries belongs to an attempt that is over.
  if (finite(created)) {
    const to = Math.max(created, now)
    const earlier = finite(started)
      ? ` An earlier attempt started ${durationText(now - started)} ago and is over; that run is inside this span and is not shown as running.`
      : ''
    return {
      spans: [{ kind: 'waiting', from: created, to, open: true }],
      mark: null,
      attempts,
      waitedMs: to - created,
      waitedOpen: true,
      ranMs: null,
      sentence: `${task.state.toLowerCase()}: waiting ${durationText(to - created)} since submission, not running.${earlier}${retried}`,
    }
  }
  const m = mark(
    'untimed',
    'times not recorded',
    'No submission time was recorded for this step, so nothing about it can be placed on the axis.',
  )
  return { spans: [], mark: m, attempts, waitedMs: null, waitedOpen: false, ranMs: null, sentence: m.note }
}

// ---------------------------------------------------------------------------
// The axis
// ---------------------------------------------------------------------------

export interface Tick {
  readonly at: number
  readonly label: string
}

export interface TimelineAxis {
  readonly t0: number
  readonly t1: number
  readonly ticks: readonly Tick[]
  /** Where "now" is, when a span is open -- the one place an open span ends. */
  readonly now: number | null
}

/** Round intervals, smallest first. `axisOf` takes the first that gives at most
 *  five intervals, so the axis never carries more than six labels. */
const TICK_STEPS_MS: readonly number[] = [
  ...[1, 2, 5, 10, 15, 30].map((s) => s * 1000),
  ...[1, 2, 5, 10, 15, 30].map((m) => m * 60_000),
  ...[1, 2, 3, 6, 12].map((h) => h * 3_600_000),
  ...[1, 2, 7, 14, 30].map((d) => d * 86_400_000),
]

/** A duration as an axis label: whole units, no zero remainders. `5m`, not `5m 0s`. */
export function spanLabel(ms: number): string {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  if (m < 60) return rs === 0 ? `${m}m` : `${m}m ${rs}s`
  const h = Math.floor(m / 60)
  const rm = m % 60
  if (h < 24) return rm === 0 ? `${h}h` : `${h}h ${rm}m`
  const d = Math.floor(h / 24)
  const rh = h % 24
  return rh === 0 ? `${d}d` : `${d}d ${rh}h`
}

/**
 * The time axis every row of one workflow shares.
 *
 * `origin` is the workflow's own submission time. It starts the axis when it is
 * earlier than every step, because a workflow whose first step waited before
 * its task existed has spent that time too.
 *
 * NULL WHEN NOTHING HAS A TIME, which is a real answer (every step unstarted or
 * unread) and not an axis of zero width.
 */
export function axisOf(rows: readonly StepTimes[], now: number, origin: number | null): TimelineAxis | null {
  let t0 = Infinity
  let t1 = -Infinity
  let open = false
  for (const r of rows) {
    for (const s of r.spans) {
      t0 = Math.min(t0, s.from)
      t1 = Math.max(t1, s.to)
      if (s.open) open = true
    }
  }
  if (!finite(t0) || !finite(t1)) return null
  if (origin !== null && finite(origin) && origin < t0) t0 = origin
  // A second is the floor: a workflow whose every step took no measurable time
  // still gets an axis a reader can read, and nothing divides by zero.
  if (t1 - t0 < 1000) t1 = t0 + 1000
  const span = t1 - t0
  const step = TICK_STEPS_MS.find((s) => span / s <= 5) ?? TICK_STEPS_MS[TICK_STEPS_MS.length - 1]!
  const ticks: Tick[] = []
  for (let k = 0; k * step <= span; k++) {
    ticks.push({ at: t0 + k * step, label: k === 0 ? '0' : `+${spanLabel(k * step)}` })
  }
  return { t0, t1, ticks, now: open ? now : null }
}

/** Where `t` sits on the axis, as a percentage of the track, clamped. */
export function pctOf(axis: TimelineAxis, t: number): number {
  const p = ((t - axis.t0) / (axis.t1 - axis.t0)) * 100
  return Math.min(100, Math.max(0, p))
}

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------

/** Every step of a workflow, keyed by id, in the order the graph reads them:
 *  level by level, left to right within a level. */
export function stepOrder(steps: readonly WorkflowStep[]): Map<string, { order: number; level: number }> {
  const out = new Map<string, { order: number; level: number }>()
  let i = 0
  levelsOf(steps).forEach((lvl, level) => {
    for (const s of lvl) out.set(s.step_id, { order: i++, level })
  })
  return out
}

/**
 * A state as a sort key: what needs a human first.
 *
 * Failures, then cancellations, then live work, then work waiting on capacity,
 * then work waiting on anything else, then done, then the steps the workflow
 * has not reached. NULL for a state nobody read -- an unread state is not a
 * rank, and sorting it among the failures or the successes would be a claim.
 */
export function stateRankOf(state: StepState): number | null {
  if (state.kind === 'unknown') return null
  if (state.kind === 'unstarted') return 6
  switch (state.state) {
    case 'FAILED':
    case 'DEAD_LETTERED':
      return 0
    case 'CANCELLED':
      return 1
    case 'STARTING':
    case 'RUNNING':
      return 2
    case 'LEASED':
    case 'DISPATCHED':
      return 3
    case 'SUBMITTED':
    case 'QUEUED':
    case 'READY':
    case 'PARKED':
      return 4
    case 'SUCCEEDED':
      return 5
  }
}

export type SortKey = 'step' | 'state' | 'waited' | 'ran' | 'attempts' | 'cost'
export type SortDir = 'ascending' | 'descending'

export interface SortSpec {
  readonly key: SortKey
  readonly dir: SortDir
}

/** The graph's own order, which is what the table opens in. */
export const DEFAULT_SORT: SortSpec = { key: 'step', dir: 'ascending' }

/** What a row sorts on. Every figure is nullable, and null means NOT MEASURED. */
export interface SortFacts {
  readonly order: number
  readonly stateRank: number | null
  readonly waitedMs: number | null
  readonly ranMs: number | null
  readonly attempts: number | null
  readonly costUsd: number | null
}

/**
 * The rows, sorted.
 *
 * AN ABSENCE SORTS LAST IN BOTH DIRECTIONS. A cost nobody reported is not a
 * small cost, so it may not lead an ascending sort -- and it is not a large one
 * either, so flipping the column may not bring it to the top. Treating null as
 * 0 would do the first; treating it as Infinity would do the second. Neither is
 * a number, so neither takes part in the comparison.
 *
 * Ties, including every pair of absences, keep the graph's order, so the sort
 * is stable across polls whatever order the rows arrived in.
 */
export function sortRows<T extends { readonly sort: SortFacts }>(rows: readonly T[], spec: SortSpec): T[] {
  const value = (r: T): number | null => {
    switch (spec.key) {
      case 'step':
        return r.sort.order
      case 'state':
        return r.sort.stateRank
      case 'waited':
        return r.sort.waitedMs
      case 'ran':
        return r.sort.ranMs
      case 'attempts':
        return r.sort.attempts
      case 'cost':
        return r.sort.costUsd
    }
  }
  const sign = spec.dir === 'ascending' ? 1 : -1
  return [...rows].sort((a, b) => {
    const va = value(a)
    const vb = value(b)
    if (va === null && vb === null) return a.sort.order - b.sort.order
    if (va === null) return 1
    if (vb === null) return -1
    return (va - vb) * sign || a.sort.order - b.sort.order
  })
}

/** The next sort after a column head is pressed: a new column starts ascending,
 *  the same column flips. */
export function nextSort(current: SortSpec, key: SortKey): SortSpec {
  if (current.key !== key) return { key, dir: 'ascending' }
  return { key, dir: current.dir === 'ascending' ? 'descending' : 'ascending' }
}

// ---------------------------------------------------------------------------
// The scrubbers
// ---------------------------------------------------------------------------
//
// redesign-v2 §2.3: "Scrubbers ... next attempt of this task, the same step
// across the last ten workflows. Keyboard movement between comparable objects
// is how you avoid building a comparison screen for every pair." Two orders,
// both decided here so the buttons and the arrow keys cannot disagree.

/** One occurrence of a step id on the board. */
export interface SameStep {
  readonly workflowId: string
  readonly createdAt: string
  readonly step: WorkflowStep
}

/**
 * Every workflow on this board that has a step with this id, NEWEST FIRST.
 *
 * By the workflow's submission time, not by the board's order: the board is
 * whatever order the route returned, and "the same step across the last ten
 * workflows" is a statement about time. A workflow with no readable
 * `created_at` goes last rather than being guessed into place.
 *
 * THE BOARD IS THE SCOPE. It is one page of `GET /v1/workflows` (100 rows), so
 * a workflow older than that page is not here -- the position reads "n of m"
 * against what was read, never against a total nobody counted.
 */
export function sameStepAcross(workflows: readonly Workflow[], stepId: string): SameStep[] {
  const found: SameStep[] = []
  for (const w of workflows) {
    const s = w.steps.find((x) => x.step_id === stepId)
    if (s) found.push({ workflowId: w.workflow_id, createdAt: w.created_at, step: s })
  }
  const t = (v: string) => {
    const n = at(v)
    return finite(n) ? n : -Infinity
  }
  return found.sort((a, b) => t(b.createdAt) - t(a.createdAt) || a.workflowId.localeCompare(b.workflowId))
}

/**
 * A task's attempts, OLDEST FIRST, so "next" means the one after in time.
 *
 * The route serves them newest first. Sorted by creation, then by the fencing
 * generation each was minted with, then by id -- a total order, so two reads of
 * the same attempts put them in the same places.
 */
export function attemptsInOrder(attempts: readonly AttemptRow[]): AttemptRow[] {
  const t = (v: string | null) => {
    const n = at(v)
    return finite(n) ? n : Infinity
  }
  return [...attempts].sort(
    (a, b) =>
      t(a.created_at) - t(b.created_at) ||
      a.generation - b.generation ||
      a.attempt_id.localeCompare(b.attempt_id),
  )
}

/** One labelled figure in the inspector. */
export interface Fact {
  readonly key: string
  readonly cell: Cell
}

/**
 * WHERE AN ATTEMPT STANDS, which is not where its task stands.
 *
 * An attempt's missing start or end means different things depending on
 * whether it is the task's CURRENT attempt and what the task is doing now --
 * and the inspector got this wrong by asking only "is the task terminal?".
 * What writes each field, measured against the worker and the scheduler:
 *
 *   * `scheduler.store.create_attempt` writes the attempt at DISPATCH with
 *     `started_at: null`; the worker fills it in `record_attempt_start`, just
 *     before STARTING -> RUNNING. So the newest attempt of a LEASED,
 *     DISPATCHED or STARTING task has no start because it has not started
 *     YET.
 *   * `control.finish()` writes the end through `record_attempt_end`.
 *     `control.park()` does NOT: it transitions to PARKED, releases the lease
 *     and exits, so a parked attempt keeps `completed_at` and `exit_code` null
 *     for good. A reclaimed attempt is the same. Its missing end is "never
 *     written", not "still going".
 *
 * So only ONE attempt can be timed "so far": the newest attempt of a task that
 * is STARTING or RUNNING. Every other missing end is an end nobody wrote.
 *
 *   running    the newest attempt of a STARTING / RUNNING task.
 *   admitting  the newest attempt of a LEASED / DISPATCHED task: its container
 *              has not started yet.
 *   waiting    the newest attempt of a SUBMITTED / QUEUED / READY / PARKED
 *              task: it is over, and the task is waiting for the next one.
 *   unread     the newest attempt of a task whose state was not in the read:
 *              whether it is still going is unknown.
 *   over       any earlier attempt, and every attempt of a terminal task.
 */
export type AttemptPhase =
  | { readonly kind: 'running' }
  | { readonly kind: 'admitting' }
  | { readonly kind: 'waiting'; readonly state: TaskState }
  | { readonly kind: 'unread' }
  | { readonly kind: 'over' }

const ADMITTING_STATES: ReadonlySet<TaskState> = new Set<TaskState>(['LEASED', 'DISPATCHED'])

/**
 * The phase of one attempt: `taskState` is the step's task's state, or null
 * when the task was not in the read; `latest` is whether this is the newest
 * attempt the attempt read returned.
 */
export function attemptPhase(taskState: TaskState | null, latest: boolean): AttemptPhase {
  if (!latest) return { kind: 'over' }
  if (taskState === null) return { kind: 'unread' }
  if (TERMINAL_STATES.has(taskState)) return { kind: 'over' }
  if (RUNNING_STATES.has(taskState)) return { kind: 'running' }
  if (ADMITTING_STATES.has(taskState)) return { kind: 'admitting' }
  return { kind: 'waiting', state: taskState }
}

const NEVER_STARTED: Absence = {
  text: NEVER_STARTED_WORD,
  note: 'This attempt carries no start time and is over: it was fenced, parked, refused at dispatch or failed before its container started.',
}

const NOT_STARTED_YET: Absence = {
  text: 'not started yet',
  note: 'This is the task’s current attempt and its container has not recorded a start yet. The scheduler writes the attempt at dispatch; the start is written when the container begins.',
}

const STILL_RUNNING: Absence = {
  text: 'still running',
  note: 'This is the current attempt of a running task, so it has no exit code yet.',
}

const NO_EXIT_YET: Absence = {
  text: 'no exit yet',
  note: 'This attempt has not started, so it has no exit code yet.',
}

const END_NOT_WRITTEN: Absence = {
  text: 'end not written',
  note: 'This attempt stopped without writing an end. A park or a reclaim records none, so how long it ran is unknown -- and it is not running.',
}

const STATE_UNREAD_HERE: Absence = {
  text: 'task unread',
  note: 'This is the newest attempt, and its task was not in the task read, so whether it is still to start or still going is unknown.',
}

/** The newest attempt of a waiting task: over, with no end written, and the
 *  word names what the task is doing now, so it cannot be read as running. */
function waitingEnd(state: TaskState): Absence {
  return {
    text: `${state.toLowerCase()}, end not written`,
    note: `The task is ${state.toLowerCase()}, so this attempt is over. It stopped without writing an end -- a park or a reclaim records none -- so how long it ran is unknown.`,
  }
}

/** How long one attempt took, or which kind of nothing that is. */
function tookCell(started: number, completed: number, phase: AttemptPhase, now: number): Cell {
  if (finite(started) && finite(completed)) {
    return measuredCell(durationText(completed - started), 'Start to finish of this attempt alone.')
  }
  if (!finite(started)) {
    // An END WITH NO START is over whatever the task is doing: something wrote
    // that it stopped, and nothing wrote that it began.
    if (finite(completed)) return absentCell(NEVER_STARTED)
    switch (phase.kind) {
      case 'running':
      case 'admitting':
        return absentCell(NOT_STARTED_YET)
      case 'unread':
        return absentCell(STATE_UNREAD_HERE)
      case 'waiting':
      case 'over':
        return absentCell(NEVER_STARTED)
    }
  }
  switch (phase.kind) {
    case 'running':
      return measuredCell(`${durationText(now - started)} so far`, 'Still running. This figure moves.')
    case 'unread':
      return absentCell(STATE_UNREAD_HERE)
    case 'waiting':
      return absentCell(waitingEnd(phase.state))
    case 'admitting':
    case 'over':
      return absentCell(END_NOT_WRITTEN)
  }
}

/** Which absence a missing exit code is. Only a running attempt's is "not yet". */
function exitAbsence(started: number, completed: number, phase: AttemptPhase): Absence {
  if (finite(completed)) return EXIT_NOT_RECORDED
  switch (phase.kind) {
    case 'running':
      return finite(started) ? STILL_RUNNING : NO_EXIT_YET
    case 'admitting':
      return finite(started) ? EXIT_NOT_RECORDED : NO_EXIT_YET
    case 'unread':
      return STATE_UNREAD_HERE
    case 'waiting':
    case 'over':
      return EXIT_NOT_RECORDED
  }
}

/** Unreachable while `checkpoints` is a list, which the type says it always is.
 *  Named rather than borrowed from another absence so that, if the field ever
 *  arrives missing, the word it prints is about checkpoints. */
const CKPTS_UNLISTED: Absence = {
  text: 'not listed',
  note: 'This attempt’s document carries no checkpoint list.',
}

const EXIT_NOT_RECORDED: Absence = {
  text: 'not recorded',
  note: 'This attempt ended without an exit code being written.',
}

/**
 * One attempt's figures, through `measure.ts` like every other figure on this
 * board.
 *
 * `phase` is where this attempt stands (`attemptPhase`). It used to be a
 * boolean, "the task has not finished", and that was the defect: it was true
 * for a PARKED task, whose newest attempt then read `2h 0m so far` and `still
 * running` beside a node saying `between attempts` and a timeline drawing it
 * waiting. Only the newest attempt of a STARTING or RUNNING task is timed "so
 * far" now, so the inspector says what the node and the timeline say.
 */
export function attemptFacts(a: AttemptRow, phase: AttemptPhase, now: number): Fact[] {
  const started = at(a.started_at)
  const completed = at(a.completed_at)
  const took = tookCell(started, completed, phase, now)
  const exit: Cell =
    typeof a.exit_code === 'number' && Number.isFinite(a.exit_code)
      ? measuredCell(`${a.exit_code}`, 'The agent process’s exit code.')
      : absentCell(exitAbsence(started, completed, phase))
  const tin = tokenCell(a.input_tokens, '')
  const tout = tokenCell(a.output_tokens, '')
  const tokens: Cell =
    tin.kind === 'absent' && tout.kind === 'absent'
      ? absentCell(TOKENS_NOT_REPORTED)
      : measuredCell(
          [tin.kind === 'measured' ? `${tin.text} in` : null, tout.kind === 'measured' ? `${tout.text} out` : null]
            .filter((x): x is string => x !== null)
            .join(' · '),
          'This attempt’s own token counts. A half that is missing was not reported, which is not the same as none.',
        )
  const facts: Fact[] = [
    { key: 'gen', cell: measuredCell(`${a.generation}`, 'The fencing generation this attempt was minted with.') },
    { key: 'took', cell: took },
    { key: 'exit', cell: exit },
    {
      key: 'cost',
      cell: costCell(a.cost_usd, 'This attempt’s own cost. Token cost only; no infrastructure cost is recorded anywhere.'),
    },
    { key: 'tokens', cell: tokens },
    {
      key: 'ckpts',
      cell: countCell(
        a.checkpoints.length,
        CKPTS_UNLISTED,
        a.checkpoints.length === 0
          ? 'This attempt’s document lists none. This zero was counted, not assumed.'
          : 'Listed on this attempt’s document.',
      ),
    },
  ]
  if (a.error !== null && a.error !== '') {
    const lines = a.error.split('\n')
    const first = lines[0] ?? ''
    facts.push({
      key: 'error',
      cell: measuredCell(lines.length > 1 ? `${first} (+${lines.length - 1} lines)` : first, a.error),
    })
  }
  return facts
}
