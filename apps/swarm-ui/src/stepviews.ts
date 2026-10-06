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

import { NEVER_STARTED_WORD, hasTokenKind, levelsOf, shapeOf, type ResultUsage } from './dag'
import {
  absentCell,
  costCell,
  countCell,
  durationText,
  measuredCell,
  tokenCount,
  TOKENS_NOT_REPORTED,
  type Absence,
  type Cell,
} from './measure'
import {
  TERMINAL_STATES,
  dispatchOf,
  stepState,
  whyAgent,
  whyNeedsAction,
  whyNotRunning,
  type AttemptRow,
  type StepState,
  type Task,
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
 *   parents  submission to the moment its LAST parent finished (#107): time
 *            the step could not have started whatever the platform did, so it
 *            is drawn lighter than a queue. Open while a parent is still going.
 *   waited   the rest of the wait, to the LATEST start: time spent queued,
 *            parked, being dispatched and cold-starting -- and, on a retried
 *            step, every earlier attempt, because `started_at` is the latest
 *            start only. From submission when the step has no parents, or when
 *            a parent's finish was not read and the split cannot be placed.
 *   waiting  the same, still going: a step that is not running now.
 *   ran      the latest start to the recorded finish.
 *   running  the latest start to now, and it has not ended.
 */
export type SpanKind = 'parents' | 'waited' | 'waiting' | 'ran' | 'running'

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
  /**
   * The QUEUE: from the moment the last parent finished (from submission for a
   * step with no parents, or whose parents' finish was not read) to the latest
   * start, or to now while still waiting. It is what the table's `waited`
   * column prints and sorts on (#107), because the queue is the part of a wait
   * that belongs to this step. Null when not measurable, and while a parent is
   * still going -- a step that has not queued yet has no queue to rank.
   */
  readonly waitedMs: number | null
  readonly waitedOpen: boolean
  /** Submission to the last parent's finish (#107), or to now while a parent is
   *  still going. Null for a step with no parents, or none read. */
  readonly parentsMs: number | null
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

/**
 * WHEN A STEP'S PARENTS WERE DONE (#107), which is where its wait splits.
 *
 *   none     it has no parents: all of its wait is queue.
 *   at       every parent has finished, the last of them at `at` --
 *            max(parent.completed_at), since the step could start no earlier.
 *   pending  a parent has not finished yet; `stepIds` names those.
 *   unknown  a parent's finish was not read (its task was not in the read, or
 *            it ended without writing when), so there is nowhere to split.
 *
 * A parent that FAILED counts as finished at its `completed_at`: a cascade
 * cancels the child at that moment, so the wait ends there too.
 */
export type ParentsDone =
  | { readonly kind: 'none' }
  | { readonly kind: 'at'; readonly at: number }
  | { readonly kind: 'pending'; readonly stepIds: readonly string[] }
  | { readonly kind: 'unknown' }

const UNKNOWN_PARENTS: ParentsDone = { kind: 'unknown' }

/** `step`'s parents, read out of the same workflow's steps and task read. */
export function parentsDoneOf(
  step: WorkflowStep,
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): ParentsDone {
  if (step.depends_on.length === 0) return { kind: 'none' }
  const pending: string[] = []
  let unknown = false
  let last = -Infinity
  for (const id of step.depends_on) {
    const parent = steps.find((s) => s.step_id === id)
    if (parent === undefined) {
      unknown = true
      continue
    }
    const st = stepState(parent, taskById)
    if (st.kind === 'unstarted' || (st.kind === 'state' && !TERMINAL_STATES.has(st.state))) {
      pending.push(id)
      continue
    }
    const done = st.kind === 'state' ? at(st.task.completed_at) : NaN
    if (!finite(done)) {
      unknown = true
      continue
    }
    last = Math.max(last, done)
  }
  // A PARENT STILL GOING OUTRANKS ONE UNREAD: whatever the unread one did, the
  // step cannot start until the live one finishes.
  if (pending.length > 0) return { kind: 'pending', stepIds: pending }
  if (unknown) return UNKNOWN_PARENTS
  return { kind: 'at', at: last }
}

interface WaitSplit {
  readonly spans: Span[]
  /** Null when there is no split: no parents, or their finish unread. */
  readonly parentsMs: number | null
  /** Null while a parent is still going: the step has not queued yet. */
  readonly queuedMs: number | null
}

/**
 * One wait, `from` submission `to` its end (a start, an end, or now when
 * `live`), split at the parents' finish. A closed piece of no length draws no
 * span; the open piece of a live wait always does, because it is what reaches
 * "now".
 */
function splitWait(from: number, to: number, live: boolean, parents: ParentsDone): WaitSplit {
  const kind: SpanKind = live ? 'waiting' : 'waited'
  if (parents.kind === 'pending' && live) {
    return { spans: [{ kind: 'parents', from, to, open: true }], parentsMs: to - from, queuedMs: null }
  }
  if (parents.kind === 'at') {
    // Clamped into the wait: a parent recorded as finishing before this step
    // was submitted cost it nothing, and one after its start (clock skew) is
    // the whole wait.
    const p = Math.min(Math.max(parents.at, from), to)
    const spans: Span[] = []
    if (p > from) spans.push({ kind: 'parents', from, to: p, open: false })
    if (to > p || live) spans.push({ kind, from: p, to, open: live })
    return { spans, parentsMs: p - from, queuedMs: to - p }
  }
  return {
    spans: to > from || live ? [{ kind, from, to, open: live }] : [],
    parentsMs: null,
    queuedMs: to - from,
  }
}

/** The wait before a start, as the head of a row's sentence. */
function waitPhrase(w: WaitSplit, latest: boolean): string {
  const start = `its ${latest ? 'latest ' : ''}start`
  if (w.parentsMs !== null && w.parentsMs > 0) {
    return `waited ${durationText(w.parentsMs)} on its parents, then queued ${durationText(w.queuedMs ?? 0)} until ${start}, then `
  }
  return `waited ${durationText(w.queuedMs ?? 0)} from submission to ${start}, then `
}

/**
 * `parents` is when the step's parents were done (`parentsDoneOf`). Unknown by
 * default, which draws the wait unsplit exactly as before #107.
 */
export function stepTimes(state: StepState, now: number, parents: ParentsDone = UNKNOWN_PARENTS): StepTimes {
  if (state.kind === 'unstarted') {
    const m = mark(
      'unstarted',
      'not started',
      'This step has no task yet: the workflow has not reached it, so there is no time to draw.',
    )
    return { spans: [], mark: m, attempts: null, waitedMs: null, waitedOpen: false, parentsMs: null, ranMs: null, sentence: m.note }
  }
  if (state.kind === 'unknown') {
    const m = mark(
      'unread',
      'task unread',
      `Task ${state.taskId} was not in the task read, so none of its times were looked at. That is unknown, not idle.`,
    )
    return { spans: [], mark: m, attempts: null, waitedMs: null, waitedOpen: false, parentsMs: null, ranMs: null, sentence: m.note }
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
      return { spans: [], mark: m, attempts, waitedMs: null, waitedOpen: false, parentsMs: null, ranMs: null, sentence: m.note }
    }
    if (finite(started) && started >= from && started <= completed) {
      const w = finite(created) ? splitWait(created, started, false, parents) : null
      const spans: Span[] = [...(w?.spans ?? []), { kind: 'ran', from: started, to: completed, open: false }]
      return {
        spans,
        mark: null,
        attempts,
        waitedMs: w === null ? null : w.queuedMs,
        waitedOpen: false,
        parentsMs: w === null ? null : w.parentsMs,
        ranMs: completed - started,
        sentence:
          (w === null ? 'No submission time was recorded, so the wait before it started is unknown. ' : waitPhrase(w, attempts !== null)) +
          `ran ${durationText(completed - started)} and ended ${task.state.toLowerCase()}.${retried}`,
      }
    }
    // NO START RECORDED, BUT AN END. A step cancelled before it ever ran is the
    // ordinary case. None of this span is drawn as running, because nothing
    // says any of it was -- and the word after it says so, in the same two
    // words the node and the table print for this step. (A start that IS
    // recorded but falls outside the span -- clock skew -- lands here too, and
    // gets no word: something did record a start.)
    // Split at the parents' finish like any wait (#107): a step cancelled by
    // a cascade waited on its parents until the one that failed ended.
    const w = splitWait(from, completed, false, finite(created) ? parents : UNKNOWN_PARENTS)
    const onParents =
      w.parentsMs !== null && w.parentsMs > 0 ? ` ${durationText(w.parentsMs)} of that was waiting on its parents.` : ''
    const sentence = `ended ${task.state.toLowerCase()} ${durationText(completed - from)} after submission with no start recorded, so none of that time is shown as running.${onParents}${retried}`
    return {
      spans: w.spans.length > 0 ? w.spans : [{ kind: 'waited', from, to: completed, open: false }],
      mark: finite(started) ? null : mark('never', NEVER_STARTED_WORD, sentence),
      attempts,
      waitedMs: w.queuedMs,
      waitedOpen: false,
      parentsMs: w.parentsMs,
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
    return { spans: [], mark: m, attempts, waitedMs: null, waitedOpen: false, parentsMs: null, ranMs: null, sentence: m.note + retried }
  }

  // TERMINAL WITHOUT AN END. It ran, it ended, and nobody wrote when. The wait
  // up to its start is real and is drawn; nothing past the start is.
  if (TERMINAL_STATES.has(task.state)) {
    const w = finite(created) && finite(started) && started > created ? splitWait(created, started, false, parents) : null
    const spans: Span[] = w === null ? [] : w.spans
    const m = mark(
      'unrecorded',
      'end not recorded',
      `This step is ${task.state.toLowerCase()} and carries no completion time, so how long it ran was never recorded. No bar is drawn past its start: any length would be an invented end, and a bar to now would say it is still going.`,
    )
    return {
      spans,
      mark: m,
      attempts,
      waitedMs: w === null ? null : w.queuedMs,
      waitedOpen: false,
      parentsMs: w === null ? null : w.parentsMs,
      ranMs: null,
      sentence: m.note + retried,
    }
  }

  // RUNNING NOW.
  if (RUNNING_STATES.has(task.state) && finite(started)) {
    const to = Math.max(started, now)
    const w = finite(created) && started >= created ? splitWait(created, started, false, parents) : null
    const spans: Span[] = [...(w?.spans ?? []), { kind: 'running', from: started, to, open: true }]
    return {
      spans,
      mark: null,
      attempts,
      waitedMs: w === null ? null : w.queuedMs,
      waitedOpen: false,
      parentsMs: w === null ? null : w.parentsMs,
      ranMs: to - started,
      sentence:
        (w === null ? '' : waitPhrase(w, attempts !== null)) +
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
    const w = splitWait(created, to, true, parents)
    const word = task.state.toLowerCase()
    const head =
      parents.kind === 'pending'
        ? `${word}: waiting ${durationText(to - created)} on its parents (${parents.stepIds.join(', ')}) since submission, not running.`
        : w.parentsMs !== null && w.parentsMs > 0
          ? `${word}: waited ${durationText(w.parentsMs)} on its parents, then waiting ${durationText(w.queuedMs ?? 0)} since they finished, not running.`
          : `${word}: waiting ${durationText(to - created)} since submission, not running.`
    return {
      spans: w.spans,
      mark: null,
      attempts,
      waitedMs: w.queuedMs,
      waitedOpen: w.queuedMs !== null,
      parentsMs: w.parentsMs,
      ranMs: null,
      sentence: `${head}${earlier}${retried}`,
    }
  }
  const m = mark(
    'untimed',
    'times not recorded',
    'No submission time was recorded for this step, so nothing about it can be placed on the axis.',
  )
  return { spans: [], mark: m, attempts, waitedMs: null, waitedOpen: false, parentsMs: null, ranMs: null, sentence: m.note }
}

// ---------------------------------------------------------------------------
// The axis
// ---------------------------------------------------------------------------

export interface Tick {
  readonly at: number
  readonly label: string
  /**
   * The label hangs LEFT of its line (WF-12). True only for the LAST tick, and
   * only when it sits in the last quarter of the track; never for the first.
   *
   * It was every last tick, whatever its position, so a last line at 70% of
   * the track printed its label pointing back into the tick before it. A last
   * label that faces right is always the step or twice the step -- four
   * characters or fewer, about 33px at 12px mono -- and the 7-character forms
   * (`+1h 30m`) only ever fall on a last tick at 75% or more, so the quarter
   * of a 390px track (about 54px) holds anything that faces right. Nothing is
   * measured: the position is a percentage, as every position on this axis is.
   */
  readonly end: boolean
}

/** Where the last tick has to sit for its label to hang left of its line. */
export const TICK_END_AT_PCT = 75

export interface TimelineAxis {
  readonly t0: number
  readonly t1: number
  readonly ticks: readonly Tick[]
  /** Where "now" is, when a span is open -- the one place an open span ends. */
  readonly now: number | null
  /**
   * The longest a WAIT is drawn (#107), when some wait on this axis is longer:
   * the 95th percentile of every span's real length (`CLAMP_PERCENTILE`).
   * Null when nothing was cut.
   */
  readonly clampMs: number | null
  /**
   * THE BREAKS IN THE AXIS (#107): stretches of real time taken out of the
   * drawing, in time order and never overlapping. `pctOf` maps through them,
   * so everything after a break is drawn that much earlier -- in every row.
   * Empty when nothing was cut.
   */
  readonly breaks: readonly AxisBreak[]
}

/** Real time `from` to `to` that the axis does not draw. */
export interface AxisBreak {
  readonly from: number
  readonly to: number
}

/**
 * WHERE AN OUTLIER WAIT IS CUT (#107): beyond the 95th PERCENTILE of the real
 * lengths of every span on the axis -- waits and runs alike, nearest rank.
 *
 * One step that queued for three hours in a workflow whose other steps took
 * minutes made the axis three hours long, and every other bar a sliver. So
 * the AXIS IS BROKEN: the part of such a wait beyond the threshold, where no
 * other span on the axis is drawn, is taken out of time itself (`breaks`), and
 * everything after it moves left by that much -- the slow step's own run, and
 * any row that happened later. The wait keeps its last `clampMs` whole, carries
 * a broken-axis mark where the time was taken out, and has its real length
 * written as text.
 *
 * ONLY TIME NOTHING ELSE COVERS IS TAKEN OUT. Another step's run (or its
 * ordinary wait) inside the outlier's wait is drawn at its real length, and
 * the outlier is drawn that much longer: a run drawn short would be a false
 * measurement, and a run is never cut.
 *
 * Nearest rank means that below twenty spans the 95th percentile IS the
 * longest span, so a small workflow is never cut -- there is no crowd for a
 * span to be an outlier from.
 */
export const CLAMP_PERCENTILE = 95

const WAIT_KINDS: ReadonlySet<SpanKind> = new Set<SpanKind>(['parents', 'waited', 'waiting'])

/** The nearest-rank `CLAMP_PERCENTILE`th percentile of these lengths, or null. */
function clampThreshold(rows: readonly StepTimes[]): number | null {
  const lengths = rows.flatMap((r) => r.spans.map((s) => s.to - s.from)).sort((a, b) => a - b)
  if (lengths.length === 0) return null
  const rank = Math.ceil((CLAMP_PERCENTILE / 100) * lengths.length)
  const t = lengths[Math.max(0, rank - 1)]!
  // A zero threshold would cut every wait to nothing: a workflow of instant
  // steps has no scale to call anything an outlier against.
  return t > 0 ? t : null
}

/** Sorted, merged intervals. */
function merged(intervals: readonly AxisBreak[]): AxisBreak[] {
  const out: AxisBreak[] = []
  for (const iv of [...intervals].sort((x, y) => x.from - y.from)) {
    const last = out[out.length - 1]
    if (last !== undefined && iv.from <= last.to) out[out.length - 1] = { from: last.from, to: Math.max(last.to, iv.to) }
    else out.push(iv)
  }
  return out
}

/** `cuts` minus `covered`, both sorted and merged. */
function without(cuts: readonly AxisBreak[], covered: readonly AxisBreak[]): AxisBreak[] {
  const out: AxisBreak[] = []
  for (const c of cuts) {
    let from = c.from
    for (const k of covered) {
      if (k.to <= from || k.from >= c.to) continue
      if (k.from > from) out.push({ from, to: k.from })
      from = Math.max(from, k.to)
    }
    if (from < c.to) out.push({ from, to: c.to })
  }
  return out
}

/** The breaks for these rows at this threshold (see `CLAMP_PERCENTILE`). */
function breaksOf(rows: readonly StepTimes[], clampMs: number | null): AxisBreak[] {
  if (clampMs === null) return []
  const spans = rows.flatMap((r) => r.spans)
  const cuts: AxisBreak[] = []
  const covered: AxisBreak[] = []
  for (const s of spans) {
    if (WAIT_KINDS.has(s.kind) && s.to - s.from > clampMs) {
      cuts.push({ from: s.from, to: s.to - clampMs })
      // The last `clampMs` of an outlier is drawn, so another outlier's cut
      // cannot take it out.
      covered.push({ from: s.to - clampMs, to: s.to })
    } else {
      covered.push({ from: s.from, to: s.to })
    }
  }
  return without(merged(cuts), merged(covered)).filter((b) => b.to > b.from)
}

/** How much of `from`..`to` the breaks take out. */
function takenOut(breaks: readonly AxisBreak[], from: number, to: number): number {
  let ms = 0
  for (const b of breaks) ms += Math.max(0, Math.min(to, b.to) - Math.max(from, b.from))
  return ms
}

/** `t` on the drawn (broken) clock: real time less every break before it. */
function drawnAt(breaks: readonly AxisBreak[], t: number): number {
  return t - takenOut(breaks, -Infinity, t)
}

/** A span as it is drawn. `clampedMs` is its real length when a break cuts
 *  it (null when drawn whole), and `breaks` the real instants it is cut at. */
export interface DrawnSpan {
  readonly from: number
  readonly to: number
  readonly clampedMs: number | null
  readonly breaks: readonly number[]
}

export function drawnSpan(axis: Pick<TimelineAxis, 'breaks'>, s: Span): DrawnSpan {
  const inside = axis.breaks.filter((b) => b.to > s.from && b.from < s.to)
  if (!WAIT_KINDS.has(s.kind) || inside.length === 0) return { from: s.from, to: s.to, clampedMs: null, breaks: [] }
  return { from: s.from, to: s.to, clampedMs: s.to - s.from, breaks: inside.map((b) => Math.max(b.from, s.from)) }
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
 *
 * `t0`/`t1` and every tick's `at` are REAL times; the drawn width between them
 * is less by the `breaks`. The tick interval is chosen on the drawn width, and
 * every label is the REAL time since `t0`, so a label after a break reads the
 * hours the break took out. Ticks keep at least one interval apart on the
 * drawing, so a broken axis still carries no more than six labels.
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
  const threshold = clampThreshold(rows)
  const breaks = breaksOf(rows, threshold)
  // A second is the floor: a workflow whose every step took no measurable time
  // still gets an axis a reader can read, and nothing divides by zero.
  if (drawnAt(breaks, t1) - drawnAt(breaks, t0) < 1000) t1 = t0 + 1000 + takenOut(breaks, t0, t1)
  const d0 = drawnAt(breaks, t0)
  const span = drawnAt(breaks, t1) - d0
  const step = TICK_STEPS_MS.find((s) => span / s <= 5) ?? TICK_STEPS_MS[TICK_STEPS_MS.length - 1]!
  // Each unbroken stretch of real time carries its own round ticks.
  const pieces: AxisBreak[] = []
  let from = t0
  for (const b of breaks) {
    if (b.to <= t0 || b.from >= t1) continue
    if (b.from > from) pieces.push({ from, to: b.from })
    from = Math.max(from, b.to)
  }
  if (from <= t1) pieces.push({ from, to: t1 })
  const at: number[] = []
  let lastDrawn = -Infinity
  for (const p of pieces) {
    for (let k = Math.ceil((p.from - t0) / step); t0 + k * step <= p.to; k++) {
      const t = t0 + k * step
      const d = drawnAt(breaks, t) - d0
      if (d - lastDrawn < step) continue
      at.push(t)
      lastDrawn = d
    }
  }
  const ticks: Tick[] = at.map((t, k) => {
    // THE LAST TICK HANGS LEFT ONLY IN THE LAST QUARTER (WF-12), and the
    // first never does: '0' at the left edge has nothing to its left.
    const last = k === at.length - 1 && k > 0
    return {
      at: t,
      label: k === 0 ? '0' : `+${spanLabel(t - t0)}`,
      end: last && ((drawnAt(breaks, t) - d0) / span) * 100 >= TICK_END_AT_PCT,
    }
  })
  return {
    t0,
    t1,
    ticks,
    now: open ? now : null,
    clampMs: breaks.length > 0 ? threshold : null,
    breaks,
  }
}

/** Where `t` sits on the axis, as a percentage of the track, clamped. Through
 *  the breaks: a time inside one sits at the break. */
export function pctOf(axis: TimelineAxis, t: number): number {
  const d0 = drawnAt(axis.breaks, axis.t0)
  const p = ((drawnAt(axis.breaks, t) - d0) / (drawnAt(axis.breaks, axis.t1) - d0)) * 100
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
 * A workflow's SHAPE, as the key the same-step scrubber compares (#112): the
 * `shapeOf` level widths and the step count. `levelsOf` places every step on
 * some level, so today the count is the widths' sum; it is part of the key so
 * that the key does not rest on that.
 */
export function shapeSignature(steps: readonly WorkflowStep[]): string {
  const s = shapeOf(steps)
  return `${s.text} · ${s.steps}`
}

/**
 * Every workflow on this board that has a step with this id AND THE SAME SHAPE
 * (`shape`, a `shapeSignature`), NEWEST FIRST.
 *
 * THE SHAPE, BECAUSE A STEP ID IS NOT A COMPARISON (#112). `synthesis` in a
 * 30-step fan and `synthesis` in a 5 -> 1 join share a name and nothing else:
 * one joins twenty-eight branches, the other five, and stepping from one to the
 * other compares two different jobs. So an occurrence counts only in a
 * workflow of the same shape as the one the step was picked in.
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
export function sameStepAcross(workflows: readonly Workflow[], stepId: string, shape: string): SameStep[] {
  const found: SameStep[] = []
  for (const w of workflows) {
    const s = w.steps.find((x) => x.step_id === stepId)
    if (s && shapeSignature(w.steps) === shape) found.push({ workflowId: w.workflow_id, createdAt: w.created_at, step: s })
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
  /**
   * Set when a measured figure is the step's RESULT's rather than this
   * attempt's own telemetry, so the inspector marks it `from result` exactly
   * as the table does (WF-5). Absent for every other figure.
   */
  readonly from?: 'result'
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

/**
 * What `took` measures, and why it need not equal the table's `ran` (WF-21).
 *
 * TWO DIFFERENT FACTS, not one figure disagreeing with itself. `took` is ONE
 * ATTEMPT from its own start to its own finish: the attempt document's
 * `started_at` (control.py:816) and `completed_at` (control.py:930). The
 * table's, the node's and the timeline's `ran` is the TASK from its latest
 * start to its completion: the task's `started_at`, rewritten at each
 * attempt's DISPATCHED -> STARTING (control.py:794), and its `completed_at`
 * (control.py:1073). Those are separate `utcnow()` reads, so even a single
 * attempt's two figures can differ by about a second. Each note names its own
 * timestamps and points at the other, so the second of difference reads as
 * what it is.
 */
const TOOK_NOTE =
  'Start to finish of this attempt alone: the attempt’s own started_at to its own completed_at. The table’s “ran” times the task instead, from its latest start to its completion -- separate writes, so for a single attempt the two can differ by about a second.'

/** How long one attempt took, or which kind of nothing that is. */
function tookCell(started: number, completed: number, phase: AttemptPhase, now: number): Cell {
  if (finite(started) && finite(completed)) {
    return measuredCell(durationText(completed - started), TOOK_NOTE)
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
 * The note on a figure taken from the step's result summary (WF-5). It says
 * which record the number is from, because the two records are not the same
 * measurement: the attempt document is typed telemetry, the result is what the
 * worker wrote when this attempt finished.
 */
export const FROM_RESULT_NOTE =
  'From the step’s result summary -- what the worker wrote when this attempt finished. This attempt’s own document carries no typed figure for it, so this is the result’s record, not the attempt telemetry. Token cost only; no infrastructure cost is recorded anywhere.'

/**
 * Why the BOARD -- the Graph's nodes and the Table -- has no attempt figure of
 * its own for a step whose result it shows instead (WF-5).
 *
 *   `not-sampled`  the step is outside the attempts the board samples
 *   `not-read`     the board's attempt read failed, for the board or this task
 *   `no-attempt`   the board read the step's attempts and there were none
 *   `untyped`      the board read them and none carries a typed figure
 */
export type BoardTelemetryGap = 'not-sampled' | 'not-read' | 'no-attempt' | 'untyped'

const RESULT_LEAD =
  'From the step’s result summary -- what the worker wrote when the step’s last attempt finished, so it covers that attempt alone.'
const COST_ONLY = 'Token cost only; no infrastructure cost is recorded anywhere.'

/**
 * The note on a result figure THE BOARD draws (#160 review, finding 1).
 *
 * `FROM_RESULT_NOTE` says the attempt's own document carries no typed figure.
 * That is a claim about a document, and the inspector can make it because the
 * inspector READ the document. The board borrowed the same note outside its
 * sample and where its attempt read failed -- paths on which it read nothing --
 * so its title stated a fact about the platform nobody had measured, and the
 * inspector could then read that attempt and show a figure the title had just
 * said was not there. Only where the board did read the attempts and found no
 * typed figure is the inspector's note the board's too.
 */
export function boardResultNote(gap: BoardTelemetryGap): string {
  switch (gap) {
    case 'untyped':
      return FROM_RESULT_NOTE
    case 'not-sampled':
      return `${RESULT_LEAD} This board did not read this step’s attempts -- the step is outside the attempts it samples -- so it says nothing about what their documents carry; picking the step reads them. ${COST_ONLY}`
    case 'not-read':
      return `${RESULT_LEAD} This board could not read this step’s attempts -- the read failed -- so it says nothing about what their documents carry. ${COST_ONLY}`
    case 'no-attempt':
      return `${RESULT_LEAD} This board read the step’s attempts and found no attempt document, so the result is the only record of a figure. ${COST_ONLY}`
  }
}

/**
 * The four kinds of token a step can report, each its own sum: null is "no
 * attempt reported this kind", never zero.
 */
export interface TokenKindCounts {
  readonly input: number | null | undefined
  readonly output: number | null | undefined
  readonly cacheRead: number | null | undefined
  readonly cacheWrite: number | null | undefined
}

/** A count is a finite number; anything else is a kind nobody reported. */
function reportedCount(v: number | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * `in 52 · out 9,955 · cache read 1.34M · write 59.7k`, leaving out what
 * nobody reported -- Agent › Details' caption, word for word (#322).
 */
export function tokenKindsCaption(k: TokenKindCounts): string {
  const parts: string[] = []
  const add = (label: string, v: number | null | undefined) => {
    const n = reportedCount(v)
    if (n !== null) parts.push(`${label} ${tokenCount(n)}`)
  }
  add('in', k.input)
  add('out', k.output)
  add('cache read', k.cacheRead)
  add('write', k.cacheWrite)
  return parts.join(' · ')
}

/**
 * A step's or an attempt's tokens as one cell (#322): THE TOTAL OF EVERY KIND
 * THAT WAS REPORTED, and each kind's count in the note.
 *
 * `X in · Y out` was the shape before, and it left the cache out: a step that
 * read 1.34M cached tokens printed `52 in · 9,955 out` beside a cost that paid
 * for all of them, so the cost read as wrong when it was right. The owner's
 * decided shape for Agent › Details is the total as the headline with each
 * kind's count underneath; a node is budgeted for a 20-character figure, so
 * here the headline is the cell and the kinds are its note (the `title` and
 * the node's `?`).
 *
 * A KIND NO ATTEMPT REPORTED IS LEFT OUT, of the total and of the caption --
 * never added as a zero. The 5m/1h write split is Agent › Details' alone: it
 * comes from the CLI's own `usage.cache_creation`, which neither the attempt
 * documents nor `runner.usage` carry.
 */
export function tokenKindsCell(k: TokenKindCounts, note: string): Cell {
  const total = [k.input, k.output, k.cacheRead, k.cacheWrite]
    .map(reportedCount)
    .reduce<number | null>((t, v) => (v === null ? t : t === null ? v : t + v), null)
  if (total === null) return absentCell(TOKENS_NOT_REPORTED)
  return measuredCell(
    tokenCount(total),
    `${tokenKindsCaption(k)}. ${note} A kind left out was not reported, which is not the same as none.`,
  )
}

/** A step result's four kinds, in the shape `tokenKindsCell` reads. */
export function resultTokenKinds(r: ResultUsage): TokenKindCounts {
  return { input: r.inputTokens, output: r.outputTokens, cacheRead: r.cacheReadTokens, cacheWrite: r.cacheCreationTokens }
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
 *
 * `result` IS THE STEP'S RESULT SUMMARY'S FIGURES, handed in only for the
 * attempt that wrote it -- the newest attempt of a task that has finished
 * (WF-5). Where that attempt's own document carries no typed cost or no token
 * count, the result's figure is shown in its place and marked `from result`,
 * so the inspector and the table say the same thing about the same step in the
 * same words. Every other attempt keeps its own absences: a result describes
 * one attempt, and borrowing it for another would be a figure for the wrong
 * run.
 */
export function attemptFacts(
  a: AttemptRow,
  phase: AttemptPhase,
  now: number,
  result: ResultUsage | null = null,
): Fact[] {
  const started = at(a.started_at)
  const completed = at(a.completed_at)
  const took = tookCell(started, completed, phase, now)
  const exit: Cell =
    typeof a.exit_code === 'number' && Number.isFinite(a.exit_code)
      ? measuredCell(`${a.exit_code}`, 'The agent process’s exit code.')
      : absentCell(exitAbsence(started, completed, phase))
  const own = tokenKindsCell(
    {
      input: a.input_tokens,
      output: a.output_tokens,
      cacheRead: a.cache_read_input_tokens,
      cacheWrite: a.cache_creation_input_tokens,
    },
    'This attempt’s own token counts.',
  )
  const borrowed =
    own.kind === 'absent' && result !== null && hasTokenKind(result)
      ? tokenKindsCell(resultTokenKinds(result), FROM_RESULT_NOTE)
      : null
  const ownCost = costCell(a.cost_usd, 'This attempt’s own cost. Token cost only; no infrastructure cost is recorded anywhere.')
  const borrowedCost =
    ownCost.kind === 'absent' && result !== null && result.usd !== null ? costCell(result.usd, FROM_RESULT_NOTE) : null
  const facts: Fact[] = [
    { key: 'gen', cell: measuredCell(`${a.generation}`, 'The fencing generation this attempt was minted with.') },
    { key: 'took', cell: took },
    { key: 'exit', cell: exit },
    borrowedCost === null ? { key: 'cost', cell: ownCost } : { key: 'cost', cell: borrowedCost, from: 'result' },
    borrowed === null ? { key: 'tokens', cell: own } : { key: 'tokens', cell: borrowed, from: 'result' },
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

// ---------------------------------------------------------------------------
// Why a step is not running, and why one failed (#105, #106)
// ---------------------------------------------------------------------------
//
// NO WORKFLOW VIEW SAID WHY. The Agents list reads `whyAgent`/`whyNotRunning`
// off every row; the graph, the timeline and the table read none of it, and a
// failed node showed no cause until it was picked into the inspector. These
// are the same readers, with the one thing only a workflow has in hand: the
// PARENTS' states, which turn the cascade's "an upstream step did not succeed"
// into the step that did not (docs/web-ui/03-agents-and-workflows.md §2.3).

/** The first line of a text, untrimmed of nothing but its line break. */
export function firstLine(text: string): string {
  return text.split(/\r?\n/, 1)[0] ?? ''
}

/**
 * A failure's CAUSE, normalised so the same failure groups on the row header:
 * the first line's head before its first `: ` (the part that names the kind of
 * failure rather than the file or the trace), quoted values and platform ids
 * reduced to `…`, lower-cased. `input collision: plan.md ...` and `Input
 * collision: notes.md ...` are one cause, `input collision`. Null when there is
 * no error text at all.
 */
export function failureCause(lastError: string | null | undefined): string | null {
  if (!lastError) return null
  const line = firstLine(lastError).trim()
  if (line === '') return null
  const head = line.split(/:\s/, 1)[0]!.trim()
  const cause = (head === '' ? line : head)
    .replace(/`[^`]*`|'[^']*'|"[^"]*"/g, '…')
    .replace(/\b(?:tsk|task|wf|att|lease)_[A-Za-z0-9_-]+/g, '…')
    .replace(/\b[0-9a-f]{8,}\b/gi, '…')
    .replace(/\s+/g, ' ')
    .replace(/[.\s]+$/, '')
    .toLowerCase()
  return cause === '' ? null : cause
}

/** How many FAILED steps share a cause. `cause` is null for a failure whose
 *  task carried no error text. */
export interface CauseGroup {
  readonly cause: string | null
  readonly n: number
}

/**
 * A workflow's FAILED steps grouped by `failureCause`, largest group first
 * (ties by cause), the failures with no cause last. Only steps whose task was
 * read: a step the read did not return has no error to group.
 */
export function failureGroups(
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): CauseGroup[] {
  const counts = new Map<string | null, number>()
  for (const step of steps) {
    const st = stepState(step, taskById)
    if (st.kind !== 'state' || st.state !== 'FAILED') continue
    const c = failureCause(st.task.last_error)
    counts.set(c, (counts.get(c) ?? 0) + 1)
  }
  return [...counts.entries()]
    .map(([cause, n]) => ({ cause, n }))
    .sort((a, b) =>
      a.cause === null ? 1 : b.cause === null ? -1 : b.n - a.n || a.cause.localeCompare(b.cause),
    )
}

/** Parent states the cascade treats as "did not succeed" -- scheduler/loop.py
 *  `_FAILED_PARENT_STATES`. ANY one of them cancels the child. */
const FAILED_PARENT_STATES: ReadonlySet<TaskState> = new Set<TaskState>(['FAILED', 'CANCELLED', 'DEAD_LETTERED'])

/** The first parent, in `depends_on` order, whose task did not succeed. */
function failedParentOf(
  step: WorkflowStep,
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): string | null {
  for (const id of step.depends_on) {
    const parent = steps.find((s) => s.step_id === id)
    if (parent === undefined) continue
    const st = stepState(parent, taskById)
    if (st.kind === 'state' && FAILED_PARENT_STATES.has(st.state)) return id
  }
  return null
}

/** The parents, in `depends_on` order, that have not succeeded yet. */
function unfinishedParentsOf(
  step: WorkflowStep,
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): string[] {
  return step.depends_on.filter((id) => {
    const parent = steps.find((s) => s.step_id === id)
    if (parent === undefined) return false
    const st = stepState(parent, taskById)
    return st.kind === 'unstarted' || (st.kind === 'state' && st.state !== 'SUCCEEDED')
  })
}

/**
 * What a step says when nothing more specific does, by state. `whyAgent`
 * writes nothing for these -- it has nothing to name -- but a waiting node
 * with no line reads as a node with nothing to wait for.
 */
const WAITING_WORDS: Partial<Record<TaskState, string>> = {
  SUBMITTED: 'submitted; not queued yet',
  QUEUED: 'queued; not yet evaluated for admission',
  READY: 'ready; waiting for the next admission pass',
  LEASED: 'capacity reserved; being dispatched',
  DISPATCHED: 'dispatched; its container has not started',
}

/**
 * One step's "why", for the table's `why` column and the graph node's line.
 *
 *   text  one line, what the surface prints
 *   full  the whole of it: a failure's complete `last_error`, for the `title`
 *         and the card's description on focus
 *   warn  whether it asks a person to act -- `whyNeedsAction`, the rule the
 *         Agents list inks its own why line by (AG-14)
 *   kind  `cause` for a failure (#105), `why` for everything else (#106)
 */
export interface StepWhy {
  readonly kind: 'cause' | 'why'
  readonly text: string
  readonly full: string
  readonly warn: boolean
}

function why(text: string, warn: boolean): StepWhy {
  return { kind: 'why', text, full: text, warn }
}

/**
 * Why `step` is not running, or why it failed; null when there is nothing to
 * explain (running, succeeded, or its task not read).
 *
 * First match wins, as in the Agents table's column:
 *
 *   FAILED / DEAD_LETTERED  the first line of `last_error`; the whole error is `full`
 *   CANCELLED               a cascade (no `cancel_requested`, has parents) names the
 *                           parent that failed -- ANY parent, the scheduler's rule --
 *                           and falls back to `whyAgent`'s copy when none is in hand
 *   no task yet             a failed parent, then the parents still going
 *   waiting                 a failed parent; `whyAgent` (park and admission blockers,
 *                           weighed with `units`); `whyNotRunning`; the parents still
 *                           going; the state's own words
 *
 * `units` is the task's weight from `classUnits` (Blockers.tsx) -- null when
 * the catalogue was not read, which keeps the pre-#66 reading.
 */
export function stepWhy(
  step: WorkflowStep,
  state: StepState,
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
  units: number | null,
): StepWhy | null {
  if (state.kind === 'unknown') return null
  const failedParent = failedParentOf(step, steps, taskById)
  if (state.kind === 'unstarted') {
    if (failedParent !== null) return why(`blocked: ${failedParent} failed`, false)
    const pending = unfinishedParentsOf(step, steps, taskById)
    return pending.length > 0 ? why(`waiting on ${pending.join(', ')}`, false) : null
  }
  const task = state.task
  switch (task.state) {
    case 'SUCCEEDED':
    case 'STARTING':
    case 'RUNNING':
      return null
    case 'FAILED':
    case 'DEAD_LETTERED': {
      const err = task.last_error ?? ''
      if (err.trim() === '') {
        return task.state === 'FAILED'
          ? { kind: 'cause', text: 'no error recorded', full: 'This step failed and its task carries no error text.', warn: whyNeedsAction(task, units) }
          : null
      }
      return { kind: 'cause', text: firstLine(err), full: err, warn: whyNeedsAction(task, units) }
    }
    case 'CANCELLED':
      if (!task.cancel_requested && step.depends_on.length > 0 && failedParent !== null) {
        return why(`blocked: ${failedParent} failed`, false)
      }
      return why(whyAgent(task, units), false)
    default: {
      const warn = whyNeedsAction(task, units)
      if (failedParent !== null) return why(`blocked: ${failedParent} failed`, warn)
      const agent = whyAgent(task, units)
      if (agent !== '') return why(agent, warn)
      const blocked = whyNotRunning(task)
      if (blocked !== null) return why(blocked, warn)
      const pending = unfinishedParentsOf(step, steps, taskById)
      if (pending.length > 0) return why(`waiting on ${pending.join(', ')}`, warn)
      const words = WAITING_WORDS[task.state]
      return words === undefined ? null : why(words, warn)
    }
  }
}

/**
 * The line a GRAPH NODE draws: a failure's cause (#105) or why a waiting step
 * is not running (#106). A cancelled step draws none -- it is over, and its
 * why is the table's column -- so only a failed or a waiting node grows.
 */
export function nodeNote(
  step: WorkflowStep,
  state: StepState,
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
  units: number | null,
): StepWhy | null {
  if (state.kind === 'state' && state.state === 'CANCELLED') return null
  return stepWhy(step, state, steps, taskById, units)
}

// ---------------------------------------------------------------------------
// What a row is called, the pull request it opened, and which rows are chains
// (#330)
// ---------------------------------------------------------------------------

/**
 * The workflow spec's `label`, read off its step tasks, or null.
 *
 * WHERE IT LIVES. The frozen `Workflow` has no metadata and `workflow_to_api`
 * serves no label, but a submission's `label` is written into the workflow's
 * metadata as `unit` (`swarm_mcp/workflows.py` `submit`, and direct dispatch
 * does the same for its one-step wrapper), and every step task carries the
 * workflow's metadata (`codec.workflow_to_api`'s docstring). So the label is
 * whatever the first joined step task's `metadata.unit` says. It is MASKED
 * metadata, like everything on a task: a label the masker caught reads as its
 * mask, never as the literal.
 *
 * Null when no step task was joined or none carries a non-blank `unit`; the row
 * then leads with the id alone, which is what it always did.
 */
export function workflowLabel(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): string | null {
  if (taskById === null) return null
  for (const s of workflow.steps) {
    const t = s.task_id ? taskById.get(s.task_id) : undefined
    const unit = t?.metadata?.unit
    if (typeof unit === 'string' && unit.trim() !== '') return unit.trim()
  }
  return null
}

/** The pull request a workflow opened, as the row prints it. */
export interface WorkflowPullRequest {
  readonly number: number
  /** The result's URL when it is http(s), else null: a number with no link. */
  readonly href: string | null
  /** The step whose result carries it. */
  readonly stepId: string
}

/**
 * The pull request this workflow's run opened, from the step results the board
 * already read -- the console never calls GitHub.
 *
 * ONLY FROM A STEP THAT OPENS ONE: the `integrate` workflow's integrator, or a
 * `direct-pr` step. A contributor's result can name a branch; it is not the
 * workflow's pull request. The integrator wins over a `direct-pr` step because
 * it is the one pull request the whole workflow opens.
 *
 * NO STATE. `git.pull_request.state` is what the worker recorded AT THE MOMENT
 * IT OPENED the pull request (agent_worker/forge.py), so it says `open` for
 * ever, including after the merge. The API serves no live state, so the row
 * prints the number alone rather than a word that goes stale the first time
 * somebody merges.
 *
 * A URL that is not http(s) keeps the number and loses the link: an href is
 * the one place a stored string becomes something a click executes.
 */
export function workflowPullRequest(
  workflow: Workflow,
  taskById: ReadonlyMap<string, Task> | null,
): WorkflowPullRequest | null {
  if (taskById === null) return null
  let found: WorkflowPullRequest | null = null
  for (const s of workflow.steps) {
    const t = s.task_id ? taskById.get(s.task_id) : undefined
    if (t === undefined) continue
    const git = (t.result_summary as { git?: unknown } | null)?.git
    const g = typeof git === 'object' && git !== null ? (git as Record<string, unknown>) : null
    const d = dispatchOf(t)
    const role = d?.role ?? (g?.role === 'integrator' ? 'integrator' : null)
    const strategy = d?.strategy ?? (typeof g?.strategy === 'string' ? g.strategy : null)
    if (role !== 'integrator' && strategy !== 'direct-pr') continue
    const pr = g?.pull_request
    if (typeof pr !== 'object' || pr === null) continue
    const { number, url } = pr as Record<string, unknown>
    if (typeof number !== 'number' || !Number.isInteger(number) || number <= 0 || typeof url !== 'string') continue
    const here: WorkflowPullRequest = {
      number,
      href: /^https?:\/\//i.test(url) ? url : null,
      stepId: s.step_id,
    }
    if (role === 'integrator') return here
    found ??= here
  }
  return found
}
