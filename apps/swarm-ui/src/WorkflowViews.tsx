import { Fragment, useEffect, useRef, useState, type KeyboardEvent, type Ref } from 'react'

import { loadAttempts } from './api'
import { DECLARED_WORDS, NEVER_STARTED_WORD, type ResultUsage, type StepInputs, type StrayInput } from './dag'
import type { Result } from './fetch'
import { NO_ATTEMPT_YET, durationText, type Absence, type Cell } from './measure'
import { Id } from './Shell'
import {
  DEFAULT_SORT,
  VIEW_LABEL,
  WORKFLOW_VIEWS,
  attemptFacts,
  attemptPhase,
  attemptsInOrder,
  nextSort,
  pctOf,
  sortRows,
  type Fact,
  type SortFacts,
  type SortKey,
  type SortSpec,
  type StepTimes,
  type TimelineAxis,
  type WorkflowView,
} from './stepviews'
import { TERMINAL_STATES, bytesLabel, type AttemptRow, type TaskState, type Tone, type WorkflowStep } from './types'

/**
 * A WORKFLOW'S TIMELINE AND TABLE, AND THE INSPECTOR THAT SCRUBS ACROSS THEM.
 *
 * These are PIXELS ONLY. What a step's time was, how rows sort and which order
 * the scrubbers walk are all decided in `stepviews.ts`; which figures a step
 * has and what its state looks like are decided in `Workflows.tsx`, which owns
 * the node and hands every row here already built. Nothing in this file decides
 * whether a value is measured -- it prints the `Cell` it was given, and a
 * `Cell` that is absent carries its own word.
 *
 * NO PROSE ON THE SURFACE (redesign-v2 §9). Every sentence here is an
 * accessible name or a `title`; what is on the glass is marks, words where a
 * figure would be, and column heads. And NO `?` (help-density.md §5): the
 * screen's one glyph is on the board, over `absent-vs-zero`, which is the rule
 * every absent cell in both views obeys.
 */

/** How a step's state is drawn, handed down from the node's own reader so the
 *  graph, the timeline and the table cannot word one state three ways. */
export interface StepLook {
  readonly tone: Tone | 'unknown'
  readonly word: string
  readonly title: string
  /** The `.ctl-dot` class for this state -- the silhouette that survives greyscale. */
  readonly dot: string
}

/** One step, built once, drawn by either view. */
export interface StepRowModel {
  readonly step: WorkflowStep
  readonly taskId: string | null
  readonly look: StepLook
  readonly times: StepTimes
  readonly ran: Cell
  readonly attempts: Cell
  /** Attempts used past the step's ceiling: 0 within it, or with no count. */
  readonly attemptsOver: number
  readonly cost: Cell
  readonly tokens: Cell
  /**
   * Which of `cost` and `tokens` is the step's RESULT's figure rather than its
   * attempts' (WF-5), so every view marks it `from result`. Null for a
   * telemetry figure and for an absence.
   */
  readonly costFrom: 'result' | null
  readonly tokensFrom: 'result' | null
  /**
   * The step's result summary's figures, for the inspector to show on the
   * attempt that wrote them when that attempt's own document has none. Null
   * when the task is not finished (a result belongs to the attempt that
   * finished) or reported nothing.
   */
  readonly result: ResultUsage | null
  /** The attempt read has not landed: the cost and token cells are placeholders, not absences. */
  readonly pending: boolean
  readonly inputs: StepInputs
  readonly sort: SortFacts
}

/**
 * The words on a figure that came from the step's result summary rather than
 * its attempt telemetry (WF-5). ONE SPELLING, used by the node, the table and
 * the inspector, so the three cannot word one source three ways.
 */
export const FROM_RESULT_WORD = 'from result'

/**
 * The source note beside a figure taken from the result. A qualifier in the
 * figure's own slot (design-system.md §8.4), faint and small: the number is
 * the measurement, the note says which record it is from. Its full meaning is
 * the figure's `title`.
 */
export function SourceNote() {
  return <span className="wf-src">{FROM_RESULT_WORD}</span>
}

// ---------------------------------------------------------------------------
// The per-card view control
// ---------------------------------------------------------------------------

/**
 * Graph / Timeline / Table, for ONE open workflow.
 *
 * `.ctl-seg`, the same primitive as the board's Rows/Graph control and the
 * canvas's zoom -- §6.11's one segmented control, not a third kind of switch.
 * The board's control sets every card at once; this one is how a reader who
 * opened a single row from Rows looks at its table without opening all ten.
 */
export function ViewControl({
  view,
  onChoose,
}: {
  view: WorkflowView
  onChoose: (v: WorkflowView) => void
}) {
  return (
    <div className="ctl-seg wf-view-seg" role="group" aria-label="How to draw this workflow">
      {WORKFLOW_VIEWS.map((v) => (
        <button key={v} type="button" aria-pressed={view === v} onClick={() => onChoose(v)}>
          {VIEW_LABEL[v]}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Inputs nothing on the graph can carry
// ---------------------------------------------------------------------------

function strayFrom(s: StrayInput): string {
  switch (s.source.kind) {
    case 'submission':
      return 'the submission'
    case 'outside':
      return `task ${s.source.taskId}, which is not a step of this workflow`
    case 'undeclared':
      return `step ${s.source.stepId}, which this step did not declare an input from`
  }
}

/**
 * THE ONE MARK FOR STAGED FILES NO EDGE CAN CARRY, on the card rather than on a
 * node.
 *
 * A file that landed in a workspace and appears nowhere on screen is the
 * silent drop this console refuses. On the graph it cannot be an edge -- there
 * is no step at the other end -- and adding a row to a node would change a
 * height `layoutOf` has already committed to. So it is counted here, in the
 * chrome, once for the workflow, with every file named in the accessible name;
 * the table lists each one in its own row as well.
 *
 * A NEUTRAL mark, not an absence: nothing is missing. Something arrived from
 * outside the picture.
 */
export function StrayMark({
  strays,
}: {
  strays: readonly { readonly stepId: string; readonly input: StrayInput }[]
}) {
  if (strays.length === 0) return null
  const n = strays.length
  const list = strays
    .map((s) => `${s.input.file} into ${s.stepId}, from ${strayFrom(s.input)}`)
    .join('; ')
  return (
    <span
      className="ctl-mark wf-strays"
      role="note"
      aria-label={`${n} staged input${n === 1 ? '' : 's'} with no edge in this graph: ${list}.`}
    >
      {n} input{n === 1 ? '' : 's'} off-graph
    </span>
  )
}

/**
 * STAGED ENTRIES NOBODY COULD READ, COUNTED ON THE CARD.
 *
 * `stagedInputsOf` counts an entry it cannot read as a file rather than
 * dropping it, and the table cell printed that count -- but only the table, so
 * the graph and the timeline of the same workflow showed nothing, and the edge
 * the entry may belong to said the result "lists no such file". The count is a
 * property of the workflow's READ, so it sits beside the off-graph mark in the
 * strip every view shares, in the unread treatment: something was reported and
 * this reader could not read it.
 */
export function UnreadableMark({ counts }: { counts: readonly { readonly stepId: string; readonly n: number }[] }) {
  if (counts.length === 0) return null
  const n = counts.reduce((t, c) => t + c.n, 0)
  const list = counts.map((c) => `${c.n} in ${c.stepId}`).join('; ')
  return (
    <span
      className="ctl-mark is-unread wf-unreadable"
      role="note"
      aria-label={`${n} staged-input entr${n === 1 ? 'y' : 'ies'} could not be read as a file: ${list}. Each was reported and is counted here rather than dropped.`}
    >
      {n} input{n === 1 ? '' : 's'} unreadable
    </span>
  )
}

// ---------------------------------------------------------------------------
// Selecting a step
// ---------------------------------------------------------------------------

/**
 * THE ROW'S NAME IS A SELECTION, NOT A LINK (redesign-v2 §2.3: "Selecting any
 * node in any mode fills the inspector. Nothing navigates.").
 *
 * The name picks the step into the inspector under the view, and the
 * inspector carries the link to the run (`open agent →`). The graph's nodes
 * used to be that link instead, so the Graph was the one view where picking a
 * step navigated away; since the owner's WF-7 decision (epic #83) a node picks
 * exactly as this button does. `aria-pressed` because it is a toggle: pressing
 * it again puts the step down.
 */
function PickButton({
  row,
  picked,
  onPick,
  withDot,
}: {
  row: StepRowModel
  picked: boolean
  onPick: (stepId: string) => void
  withDot: boolean
}) {
  return (
    <button
      type="button"
      className="wf-pick"
      data-step={row.step.step_id}
      aria-pressed={picked}
      title={row.look.title}
      onClick={() => onPick(row.step.step_id)}
    >
      {withDot && <i className={row.look.dot} aria-hidden />}
      <span className="wf-pick-id">{row.step.step_id}</span>
    </button>
  )
}

// ---------------------------------------------------------------------------
// The timeline
// ---------------------------------------------------------------------------

/**
 * WHERE THE TIME WENT, ON ONE WALL-CLOCK AXIS (viz #2).
 *
 * One row per step, in the graph's order. Each row draws the step's SPANS --
 * waited, then ran -- rather than one bar coloured by the outcome, because a
 * single bar would say that a step which queued for three minutes and ran for
 * eighteen seconds "took" three minutes eighteen, and that is the misreading
 * this view exists to stop (redesign-v2 §9's measured cold start is exactly
 * that shape).
 *
 * POSITIONS ARE PERCENTAGES OF THE TRACK, so the view needs no measurement and
 * cannot disagree with itself between a phone and a monitor. The track is
 * `role="img"` with the row's whole story as its name; the name button beside
 * it is the only control on the row.
 */
export function WorkflowTimeline({
  rows,
  axis,
  picked,
  onPick,
}: {
  rows: readonly StepRowModel[]
  axis: TimelineAxis | null
  picked: string | null
  onPick: (stepId: string) => void
}) {
  return (
    <div className="wf-timeline" role="group" aria-label="When each step waited and ran">
      <span className="wf-tl-corner" aria-hidden />
      <div className="wf-tl-scale" aria-hidden>
        {axis !== null && <Ticks axis={axis} />}
      </div>
      {rows.map((r) => (
        <Fragment key={r.step.step_id}>
          <PickButton row={r} picked={picked === r.step.step_id} onPick={onPick} withDot />
          <div
            className="wf-tl-track"
            data-step={r.step.step_id}
            role="img"
            aria-label={`${r.step.step_id}: ${r.times.sentence}`}
          >
            {axis !== null && <Spans row={r} axis={axis} />}
            {r.times.mark !== null && <TrackMark times={r.times} axis={axis} />}
            {axis !== null && axis.now !== null && (
              <i className="wf-tl-now" style={{ left: `${pctOf(axis, axis.now)}%` }} />
            )}
          </div>
        </Fragment>
      ))}
    </div>
  )
}

/**
 * The axis labels, each at its own percentage of the track.
 *
 * EACH LABEL IS AN ELEMENT OF ITS OWN INSIDE ITS TICK (WF-12), so the sheet can
 * hide a label while the tick keeps its rule line: at 560px and below the axis
 * prints every other label counting back from the last, which is §7.2's rule
 * that a phone chart is drawn for the phone rather than scaled. And only a
 * tick `axisOf` marks `end` hangs its label left of its line -- the last one,
 * in the last quarter of the track. Nothing here is measured.
 */
function Ticks({ axis }: { axis: TimelineAxis }) {
  return (
    <>
      {axis.ticks.map((t) => (
        <span
          key={t.at}
          className={`wf-tl-tick${t.end ? ' is-end' : ''}`}
          style={{ left: `${pctOf(axis, t.at)}%` }}
        >
          <span className="wf-tl-tick-label">{t.label}</span>
        </span>
      ))}
    </>
  )
}

/** One row's spans. */
function Spans({ row, axis }: { row: StepRowModel; axis: TimelineAxis }) {
  return (
    <>
      {row.times.spans.map((s, i) => {
        const left = pctOf(axis, s.from)
        const width = pctOf(axis, s.to) - left
        // ONLY A FAILURE OR LIVE WORK IS COLOURED (WF-11, the #122 hue
        // ruling). A wait is an outline in every state, because waiting is not
        // the step's verdict; a finished run is one grey whether it succeeded
        // or was cancelled, because finishing is not a verdict either. A FAILED
        // run is the one outcome that takes a class -- its fill and its post --
        // and a run in flight carries `is-running` on its own kind. The row's
        // state dot still says every state.
        const bad = s.kind === 'ran' && row.look.tone === 'bad' ? ' is-bad' : ''
        return (
          <i
            key={i}
            className={`wf-tl-span is-${s.kind}${s.open ? ' is-open' : ''}${bad}`}
            style={{ left: `${left}%`, width: `${width}%` }}
          />
        )
      })}
    </>
  )
}

/**
 * The word a track prints where a bar would be.
 *
 * PLACED AFTER WHATEVER TIME IS DRAWN. A step that ended without recording when
 * still has a real wait up to its start; the word sits where that wait stops,
 * which is exactly where the missing part begins. Past the middle of the track
 * it hangs to the left of that point instead, so it cannot run off the card.
 */
function TrackMark({ times, axis }: { times: StepTimes; axis: TimelineAxis | null }) {
  if (times.mark === null) return null
  let drawnTo = 0
  if (axis !== null && times.spans.length > 0) {
    const ax: TimelineAxis = axis
    drawnTo = Math.max(...times.spans.map((s) => pctOf(ax, s.to)))
  }
  const end = drawnTo > 50
  return (
    <span
      className={`wf-tl-mark is-${times.mark.kind}${end ? ' is-end' : ''}`}
      style={{ left: `${drawnTo}%` }}
      title={times.mark.note}
    >
      {times.mark.text}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------

/** A figure in a cell: the value, a word where a value would be, or a
 *  placeholder while the read that would supply it is still in flight. A
 *  figure taken from the step's result carries the source note beside it
 *  (WF-5). */
function CellView({
  cell,
  pending = false,
  from = null,
}: {
  cell: Cell
  pending?: boolean
  from?: 'result' | null
}) {
  if (pending) return <span className="node-reading" role="img" aria-label="reading" />
  const figure = (
    <span className={cell.kind === 'absent' ? 'wf-cell is-absent' : 'wf-cell'} title={cell.note || undefined}>
      {cell.text}
    </span>
  )
  if (from !== 'result' || cell.kind === 'absent') return figure
  return (
    <>
      {figure} <SourceNote />
    </>
  )
}

/**
 * The attempts cell, which is the one figure in the table with a ceiling.
 *
 * PAST THE CEILING IT IS DRAWN AS PAST THE CEILING. `4 of 3` rendered in the
 * same ink as `1 of 3`, so the one row in a table of twenty whose count the
 * ceiling was meant to make impossible read as ordinary. `.is-over` is the
 * over-ceiling treatment (the bad tone and weight -- `.is-over` is what the
 * sheet already calls "more held than the ceiling allows" on a track), and the
 * overage is in the accessible name, because a tone is never the only signal.
 */
function AttemptsView({ cell, over }: { cell: Cell; over: number }) {
  if (over <= 0) return <CellView cell={cell} />
  return (
    <span
      className="wf-cell is-over"
      role="img"
      aria-label={`${cell.text} attempts used: ${over} over the ceiling`}
      title={cell.note || undefined}
    >
      {cell.text}
    </span>
  )
}

/** The columns, in order. `sort` is the key a head sorts on, or null for a
 *  column that is read rather than ranked. */
const COLUMNS: ReadonlyArray<{ col: string; label: string; sort: SortKey | null; num: boolean }> = [
  { col: 'step', label: 'step', sort: 'step', num: false },
  { col: 'state', label: 'state', sort: 'state', num: false },
  { col: 'runner', label: 'runner', sort: null, num: false },
  { col: 'waited', label: 'waited', sort: 'waited', num: true },
  { col: 'ran', label: 'ran', sort: 'ran', num: true },
  { col: 'attempts', label: 'attempts', sort: 'attempts', num: true },
  { col: 'cost', label: 'cost', sort: 'cost', num: true },
  { col: 'tokens', label: 'tokens', sort: null, num: true },
  { col: 'inputs', label: 'inputs', sort: null, num: false },
]

function waitedCell(t: StepTimes): Cell {
  if (t.waitedMs === null) {
    return t.mark !== null
      ? { kind: 'absent', text: t.mark.text, note: t.mark.note }
      : { kind: 'absent', text: 'not recorded', note: 'No submission time was recorded, so the wait cannot be computed.' }
  }
  const text = `${durationText(t.waitedMs)}${t.waitedOpen ? ' so far' : ''}`
  return {
    kind: 'measured',
    text,
    note: t.waitedOpen
      ? 'Still waiting. Time queued, parked or being dispatched -- never time run.'
      : t.attempts !== null
        ? 'Submission to the latest start. This task retried, so the earlier attempts are inside this figure.'
        : 'Submission to start: time queued, parked or being dispatched -- never time run.',
  }
}

/**
 * What a step read, one entry per file, ONE LINE PER FILE.
 *
 * `plan.md ← plan` and then either its size (it arrived, measured) or the word
 * for which kind of not-yet it is. A step that declares nothing and staged
 * nothing says `none` -- that is a fact about the step's definition, not an
 * absent measurement, so it is plain text rather than the absence treatment.
 *
 * WHY A LINE EACH, and why in `depends_on` order (`inputsByStep` sorts them).
 * The entries were one comma-joined run in a `nowrap` cell, so a step with two
 * inputs showed the first and clipped the second -- and which one was first
 * followed the Firestore map, so it changed between reads. merge-1..4 each
 * declared two files and failed on the SECOND; the table showed one input,
 * clipped, and a different one on the next refresh. A break between entries
 * keeps every file on screen without the cell's `nowrap` cutting any of them.
 */
function InputsCell({ inputs }: { inputs: StepInputs }) {
  const declared = [...inputs.declared.entries()]
  if (declared.length === 0 && inputs.stray.length === 0 && inputs.malformed === 0) {
    return <span className="wf-cell is-none">none</span>
  }
  return (
    <span className="wf-inputs">
      {declared.map(([parent, p], i) => (
        <span key={`d-${parent}`} className="wf-input">
          {i > 0 && <br />}
          {p.file} ← {parent}{' '}
          {p.kind === 'staged' ? (
            <span
              className="wf-cell"
              title={p.fromCheckpoint ? 'Already in the restored checkpoint, so this attempt did not fetch it again.' : 'Staged into the workspace before the agent ran.'}
            >
              {p.bytes === null ? 'size not reported' : bytesLabel(p.bytes)}
              {p.fromCheckpoint ? ' · from checkpoint' : ''}
            </span>
          ) : (
            <span className="wf-cell is-absent" title={DECLARED_WORDS[p.why].note}>
              {DECLARED_WORDS[p.why].text}
            </span>
          )}
        </span>
      ))}
      {inputs.stray.map((s, i) => (
        <span key={`s-${i}`} className="wf-input is-stray">
          {declared.length + i > 0 && <br />}
          {s.file} ← {s.source.kind === 'submission' ? 'submission' : s.source.kind === 'outside' ? s.source.taskId : `${s.source.stepId} (undeclared)`}{' '}
          <span className="wf-cell">{s.bytes === null ? 'size not reported' : bytesLabel(s.bytes)}</span>
        </span>
      ))}
      {inputs.malformed > 0 && (
        <>
          {declared.length + inputs.stray.length > 0 && <br />}
          <span
            className="ctl-mark is-unread"
            aria-label={`${inputs.malformed} entr${inputs.malformed === 1 ? 'y' : 'ies'} in this step's staged-input report could not be read as a file.`}
          >
            {inputs.malformed} unreadable
          </span>
        </>
      )}
    </span>
  )
}

/**
 * THE SAME STEPS AS ROWS, sortable by what a reader hunting an outlier sorts by
 * (redesign-v2 §2.3: "duration, cost, attempts, state").
 *
 * `.ctl-table`, the shared table primitive, so the rhythm, the head and the row
 * tones are the ones every other table in this console already draws. The
 * head is NOT sticky (WF-21): the wrapper scrolls sideways only, so a sticky
 * head never stuck, and making each head cell a layer of its own is the likely
 * source of the faint seams this table showed at fractional column edges.
 * A failed step's row takes `is-bad` (a full-height rule down its first cell,
 * which survives greyscale); an unread one takes `is-warn`.
 *
 * THE SORT LIVES HERE, in this component's state, and that is deliberate: the
 * only thing on this board that remounts a card is a stop-and-reload, and the
 * table draws no stop control.
 */
export function WorkflowTable({
  rows,
  picked,
  onPick,
}: {
  rows: readonly StepRowModel[]
  picked: string | null
  onPick: (stepId: string) => void
}) {
  const [sort, setSort] = useState<SortSpec>(DEFAULT_SORT)
  const sorted = sortRows(rows, sort)
  return (
    <div className="ctl-table wf-table">
      <table>
        <thead>
          <tr>
            {COLUMNS.map((c) => (
              <th
                key={c.col}
                data-col={c.col}
                className={c.num ? 'is-num' : undefined}
                aria-sort={c.sort !== null && sort.key === c.sort ? sort.dir : undefined}
              >
                {c.sort === null ? (
                  c.label
                ) : (
                  <button type="button" className="wf-sort" onClick={() => setSort((s) => nextSort(s, c.sort!))}>
                    {c.label}
                    <span className="wf-sort-dir" aria-hidden>
                      {sort.key === c.sort ? (sort.dir === 'ascending' ? ' ▲' : ' ▼') : ''}
                    </span>
                  </button>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr
              key={r.step.step_id}
              data-step={r.step.step_id}
              className={r.look.tone === 'bad' ? 'is-bad' : r.look.tone === 'unknown' ? 'is-warn' : undefined}
            >
              <td data-col="step">
                <PickButton row={r} picked={picked === r.step.step_id} onPick={onPick} withDot={false} />
              </td>
              <td data-col="state">
                <span className="wf-cell-state" title={r.look.title}>
                  <i className={r.look.dot} aria-hidden />
                  {r.look.word}
                </span>
              </td>
              <td data-col="runner">{r.step.runner_profile}</td>
              <td data-col="waited" className="is-num">
                <CellView cell={waitedCell(r.times)} />
              </td>
              <td data-col="ran" className="is-num">
                <CellView cell={r.ran} />
              </td>
              <td data-col="attempts" className="is-num">
                <AttemptsView cell={r.attempts} over={r.attemptsOver} />
              </td>
              <td data-col="cost" className="is-num">
                <CellView cell={r.cost} pending={r.pending} from={r.costFrom} />
              </td>
              <td data-col="tokens" className="is-num">
                <CellView cell={r.tokens} pending={r.pending} from={r.tokensFrom} />
              </td>
              <td data-col="inputs">
                <InputsCell inputs={r.inputs} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The inspector and its two scrubbers
// ---------------------------------------------------------------------------

/**
 * What the attempt scrubber says when the attempt read came back empty.
 *
 * `no attempt yet` is right for a step that is still waiting -- the read
 * succeeded, nothing has run, something may. For a step that is OVER it is
 * wrong twice: "yet" promises an attempt that is not coming, and the Table and
 * the Timeline beside it already say `never started` for the same step. A
 * terminal task with no attempt document never got an attempt at all, so it
 * takes the word every other view prints for it (`NEVER_STARTED_WORD`).
 */
const NO_ATTEMPT_EVER: Absence = {
  text: NEVER_STARTED_WORD,
  note: 'The attempt read succeeded and returned none, and this step is over: it ended without any attempt being made, so there is nothing to step through.',
}

function noAttempt(taskState: TaskState | null): Absence {
  return taskState !== null && TERMINAL_STATES.has(taskState) ? NO_ATTEMPT_EVER : NO_ATTEMPT_YET
}

/** The attempt read, injectable so a test can hand it rows without HTTP. */
export type AttemptLoader = (taskId: string) => Promise<Result<{ attempts: AttemptRow[] }>>

/** One occurrence of the picked step on the board, as the scrubber shows it. */
export interface SiblingRef {
  readonly workflowId: string
  readonly dot: string
  readonly word: string
}

type AttemptsRead =
  | { kind: 'reading' }
  | { kind: 'ready'; attempts: AttemptRow[] }
  | { kind: 'failed'; detail: string }

/** Which scrub control focus should land on when an inspector mounts because
 *  the selection moved INTO it from another workflow's card. */
export type ScrubFocus = 'newer' | 'older' | null

function FactList({ facts }: { facts: readonly Fact[] }) {
  return (
    <ul className="ctl-facts wf-inspect-facts">
      {facts.map((f) =>
        f.cell.kind === 'absent' ? (
          <li key={f.key} className="ctl-fact is-absent">
            <b>{f.key}</b>
            <span className="ctl-mark is-absent" aria-label={f.cell.note}>
              {f.cell.text}
            </span>
          </li>
        ) : (
          <li key={f.key} className="ctl-fact" title={f.cell.note || undefined}>
            <b>{f.key}</b>
            {f.cell.text}
            {/* THE SAME NOTE THE TABLE PRINTS for the same figure (WF-5), so
                the inspector and the table word one source one way. */}
            {f.from === 'result' && (
              <>
                {' '}
                <SourceNote />
              </>
            )}
          </li>
        ),
      )}
    </ul>
  )
}

/**
 * ONE SCRUB CONTROL, WHICH STAYS FOCUSABLE AT ITS END.
 *
 * It was `disabled` at its end, and a browser takes focus off a control the
 * moment it becomes disabled (the HTML focus fixup rule). The arrow keys are
 * heard on the scrubber's group, so a reader who walked to the first attempt
 * with ArrowLeft was left on <body>, and ArrowRight did nothing until they
 * tabbed back in -- "arrow keys move either one" held for every press but the
 * one that mattered.
 *
 * `aria-disabled` instead: the end is still announced as unavailable, the
 * press does nothing, and focus stays where the reader put it, so the other
 * direction is one key away. The keyboard sweep counts `aria-disabled` as
 * switched off, as it does `disabled`.
 */
function ScrubButton({
  label,
  glyph,
  onPress,
  buttonRef,
}: {
  label: string
  glyph: string
  /** Null at the end: nothing that way. */
  onPress: (() => void) | null
  buttonRef?: Ref<HTMLButtonElement>
}) {
  const off = onPress === null
  return (
    <button
      ref={buttonRef}
      type="button"
      className="wf-scrub-btn"
      aria-label={label}
      aria-disabled={off ? true : undefined}
      onClick={off ? undefined : onPress}
    >
      {glyph}
    </button>
  )
}

/** ArrowLeft / ArrowRight inside a scrubber move it, and nothing else is
 *  touched -- Tab in particular, which the keyboard sweep checks no control on
 *  a plain route swallows. */
function scrubKeys(prev: (() => void) | null, next: (() => void) | null) {
  return (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === 'ArrowLeft' && prev !== null) {
      e.preventDefault()
      prev()
    } else if (e.key === 'ArrowRight' && next !== null) {
      e.preventDefault()
      next()
    }
  }
}

/**
 * THE INSPECTOR, AND THE TWO SCRUBBERS redesign-v2 §2.3 ASKS FOR: "next attempt
 * of this task" and "the same step across the last ten workflows".
 *
 * WHERE IT LIVES. Under the view, inside the card of the workflow the picked
 * step belongs to. When the second scrubber moves the selection to the same step
 * in another workflow, the inspector moves WITH it -- into that workflow's card
 * -- because the thing being inspected is that workflow's step, and an
 * inspector for workflow B drawn under workflow A's timeline would put two
 * workflows' facts side by side with nothing saying which is which. Focus goes
 * with it, to the same control, so pressing the key again keeps walking.
 *
 * THE ATTEMPT READ IS STARTED HERE AND ONLY HERE, when a step with a task is
 * picked. `GET /v1/tasks/{id}/attempts` is the per-step read the board already
 * rations to twelve tasks (`loadWorkflowUsage`); one more for the step a reader
 * has asked about is the cheapest way to answer them, and it is never started
 * for a step nobody picked.
 */
export function StepInspector({
  workflowId,
  row,
  taskState,
  siblings,
  siblingIndex,
  onSibling,
  focus,
  onFocused,
  onClose,
  now,
  load = loadAttempts,
}: {
  workflowId: string
  row: StepRowModel
  /**
   * The step's task's state, or null when the task was not in the read. With
   * which attempt is the newest, it decides what a missing start or end means
   * (`attemptPhase`) -- only the newest attempt of a STARTING or RUNNING task
   * is "so far". It was a boolean, "not terminal", which timed a PARKED task's
   * attempt as running while the node and the timeline said waiting.
   */
  taskState: TaskState | null
  siblings: readonly SiblingRef[]
  siblingIndex: number
  onSibling: (delta: -1 | 1) => void
  focus: ScrubFocus
  onFocused: () => void
  onClose: () => void
  now: number
  load?: AttemptLoader
}) {
  const taskId = row.taskId
  const [read, setRead] = useState<AttemptsRead>({ kind: 'reading' })
  // Null until the attempts land; then the newest, because that is the one a
  // reader who picked this step was asking about.
  const [index, setIndex] = useState<number | null>(null)

  useEffect(() => {
    if (taskId === null) return
    let alive = true
    setRead({ kind: 'reading' })
    setIndex(null)
    load(taskId).then((r) => {
      if (!alive) return
      if (r.status === 'ok' || r.status === 'stale') {
        setRead({ kind: 'ready', attempts: attemptsInOrder(r.data.attempts) })
        return
      }
      if (r.status === 'empty') {
        setRead({ kind: 'ready', attempts: [] })
        return
      }
      // `loading` is not a value this promise resolves to; if it ever did, a
      // placeholder that never stops moving is a silent stall, so it is named.
      setRead({ kind: 'failed', detail: r.status === 'error' ? r.error.message : 'The attempt read did not complete.' })
    })
    return () => {
      alive = false
    }
  }, [taskId, load])

  const newerRef = useRef<HTMLButtonElement | null>(null)
  const olderRef = useRef<HTMLButtonElement | null>(null)
  const hasNewer = siblingIndex > 0
  const hasOlder = siblingIndex < siblings.length - 1

  // FOCUS FOLLOWS THE SELECTION, once, on arrival, onto the SAME control the
  // reader was pressing. It used to fall back to the other one when this was
  // the end (the last workflow has no older one), because a disabled button
  // cannot hold focus -- which put a reader holding ArrowRight on "newer" with
  // nothing said. `ScrubButton` stays focusable at its end, so there is no
  // fallback to need.
  useEffect(() => {
    if (focus === null) return
    const want = focus === 'older' ? olderRef : newerRef
    want.current?.focus()
    onFocused()
    // On mount only: this is about how the inspector ARRIVED.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const ready = read.kind === 'ready' ? read.attempts : null
  const at = ready === null || ready.length === 0 ? null : (index ?? ready.length - 1)
  const current = ready !== null && at !== null ? (ready[at] ?? null) : null
  const toAttempt = (delta: -1 | 1) => {
    if (ready === null || at === null) return
    const n = at + delta
    if (n >= 0 && n < ready.length) setIndex(n)
  }
  const prevAttempt = ready !== null && at !== null && at > 0 ? () => toAttempt(-1) : null
  const nextAttempt = ready !== null && at !== null && at < ready.length - 1 ? () => toAttempt(1) : null

  return (
    <section className="wf-inspect" aria-label={`Step ${row.step.step_id} of ${workflowId}`}>
      <div className="wf-inspect-head">
        <i className={row.look.dot} aria-hidden />
        <span className="wf-inspect-id">{row.step.step_id}</span>
        <span className="wf-inspect-state" title={row.look.title}>
          {row.look.word}
        </span>
        {/* `.ctl-link`: ink plus an underline, the accent only on hover and
            focus. Unclassed, this anchor fell back to the browser's own blue --
            and to visited purple once the run had been opened.

            THE ONE WAY FROM THE WORKFLOW TO THE RUN (WF-7). Picking a step --
            on the Graph, the Timeline or the Table -- fills this inspector and
            navigates nowhere; this link opens the step's agent in the Work ›
            Agents drawer. A step with no task has nothing to open, so it gets
            no link rather than a dead one. */}
        {taskId !== null && (
          <a className="ctl-link wf-inspect-run" href={`#work/task/${encodeURIComponent(taskId)}`}>
            open agent →
          </a>
        )}
        <button type="button" className="wf-inspect-close" aria-label="Stop inspecting this step" onClick={onClose}>
          ×
        </button>
      </div>

      <div
        className="wf-scrub"
        role="group"
        aria-label="Attempts of this step"
        aria-keyshortcuts="ArrowLeft ArrowRight"
        data-scrub="attempt"
        onKeyDown={scrubKeys(prevAttempt, nextAttempt)}
      >
        <span className="ctl-eyebrow wf-scrub-key">attempt</span>
        {taskId === null ? (
          <span className="ctl-mark is-absent" aria-label="This step has no task yet, so it has no attempts to step through.">
            not started
          </span>
        ) : read.kind === 'reading' ? (
          <span className="ctl-mark is-pending" aria-label="The attempts of this step are still being read.">
            reading
          </span>
        ) : read.kind === 'failed' ? (
          <span className="ctl-mark is-unread" aria-label={`The attempts of this step could not be read: ${read.detail}`}>
            attempts unread
          </span>
        ) : current === null || at === null || ready === null ? (
          <span className="ctl-mark is-absent" aria-label={noAttempt(taskState).note}>
            {noAttempt(taskState).text}
          </span>
        ) : (
          <>
            <ScrubButton label="Previous attempt" glyph="◀" onPress={prevAttempt} />
            <span className="wf-scrub-pos">
              attempt {at + 1} of {ready.length}
            </span>
            <ScrubButton label="Next attempt" glyph="▶" onPress={nextAttempt} />
            <FactList
              facts={attemptFacts(
                current,
                attemptPhase(taskState, at === ready.length - 1),
                now,
                // THE RESULT BELONGS TO THE ATTEMPT THAT WROTE IT: the newest
                // attempt of a finished task (WF-5). Any other attempt keeps
                // its own absences.
                at === ready.length - 1 && taskState !== null && TERMINAL_STATES.has(taskState) ? row.result : null,
              )}
            />
          </>
        )}
      </div>

      <div
        className="wf-scrub"
        role="group"
        aria-label="The same step in other workflows on this board, newest first"
        aria-keyshortcuts="ArrowLeft ArrowRight"
        data-scrub="workflow"
        onKeyDown={scrubKeys(hasNewer ? () => onSibling(-1) : null, hasOlder ? () => onSibling(1) : null)}
      >
        <span className="ctl-eyebrow wf-scrub-key">same step</span>
        <ScrubButton
          buttonRef={newerRef}
          label="Same step, newer workflow"
          glyph="◀"
          onPress={hasNewer ? () => onSibling(-1) : null}
        />
        <span className="wf-scrub-pos">
          workflow {siblingIndex + 1} of {siblings.length}
        </span>
        <ScrubButton
          buttonRef={olderRef}
          label="Same step, older workflow"
          glyph="▶"
          onPress={hasOlder ? () => onSibling(1) : null}
        />
        {/* THE STRIP: every occurrence, in scrub order, in its own state's
            silhouette, with this one ringed. Decoration for a sighted reader
            -- the position above is the fact, and each workflow's state is one
            press away -- so it is hidden from assistive technology rather than
            announced as a list of unlabelled dots. */}
        <ol className="wf-scrub-strip" aria-hidden>
          {siblings.map((s, i) => (
            <li key={s.workflowId} className={i === siblingIndex ? 'is-here' : undefined} title={`${s.workflowId}: ${s.word}`}>
              <i className={s.dot} />
            </li>
          ))}
        </ol>
        <Id title={workflowId}>{workflowId}</Id>
      </div>
    </section>
  )
}
