import { Fragment, useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode, type Ref } from 'react'

import { loadAttempts } from './api'
import { DECLARED_WORDS, MONO_ADVANCE_EM, NEVER_STARTED_WORD, type DagShape, type ResultUsage, type StepInputs, type StrayInput } from './dag'
import type { Result } from './fetch'
import { NO_ATTEMPT_YET, durationText, type Absence, type Cell } from './measure'
import type { MarkHue, MarkName } from './marks'
import { NamedMark, Segmented } from './components'
import { Id } from './Shell'
import { agentPath } from './OverviewRegions'
import { addressToPath } from './paths'
import {
  DEFAULT_SORT,
  VIEW_LABEL,
  WORKFLOW_VIEWS,
  attemptFacts,
  attemptPhase,
  attemptsInOrder,
  drawnSpan,
  nextSort,
  pctOf,
  sortRows,
  CLAMP_PERCENTILE,
  type Fact,
  type SortFacts,
  type SortKey,
  type SortSpec,
  type StepTimes,
  type StepWhy,
  type TimelineAxis,
  type WorkflowView,
  MERGE_MAX_HEAD_UPDATES,
  type MergeCard,
} from './stepviews'
import './styles/workflow-views.css'
import { TERMINAL_STATES, bytesLabel, timeAgo, type AttemptRow, type TaskState, type Tone, type WorkflowStep } from './types'

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
  /**
   * THE BRAND STATE MARK (marks.tsx) and its hue: the same silhouette the
   * graph's node draws, so the table, the inspector and the graph on one page
   * draw one state one way (#503: RUNNING was a filled accent-blue dot here and
   * the haloed live disc on the node; PARKED the amber warning triangle here
   * and the violet pause bars there). `skipped` is a step whose verdict gate
   * kept its agent from running; `unknown` a step whose task was not read --
   * the queued ring in amber, the warning colour, never a state's hue.
   */
  readonly mark: MarkName | 'skipped' | 'unknown'
  readonly hue: MarkHue | 'warn'
}

/**
 * A STEP'S BRAND MARK, in the `.sk-st` shape every state mark in the console
 * uses. `word` draws the state word beside it; without it the word is the
 * mark's accessible name.
 */
export function WfStepMark({ look, word = false }: { look: Pick<StepLook, 'mark' | 'hue' | 'word'>; word?: boolean }) {
  return (
    <NamedMark
      className="wf-mk"
      mark={look.mark === 'unknown' ? 'queued' : look.mark}
      dataMark={look.mark}
      hue={look.hue}
      word={look.word}
      bare={!word}
      title=""
    />
  )
}

/**
 * A STEP'S TINT CLASS (#405, owner decision 2026-10-01): `t-<hue>`, the class
 * the graph's node already carries (`dag.ts` `lookClass`), so the table row and
 * the timeline span take their state's pair from the same rules -- the brand
 * tokens `--s-<hue>b` (the tint) and `--s-<hue>` (the edge). An unread step is
 * `t-unk`, which no state's pair is drawn for: it keeps the amber warning.
 */
export function tintClass(look: Pick<StepLook, 'hue'>): string {
  return look.hue === 'warn' ? 't-unk' : `t-${look.hue}`
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
  /** Why the step is not running, or why it failed (#105, #106); null when
   *  there is nothing to explain. `stepWhy`. */
  readonly why: StepWhy | null
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
 * The canonical `Segmented`, the same control as the canvas's zoom -- §6.11's
 * one segmented control, not a third kind of switch.
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
    <Segmented
      className="wf-view-seg"
      label="How to draw this workflow"
      value={view}
      options={WORKFLOW_VIEWS.map((v) => ({ key: v, label: VIEW_LABEL[v] }))}
      onChange={onChoose}
    />
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
      return `agent ${s.source.taskId}, which is not a step of this workflow`
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
      // ESCAPE ON THE OPENED STEP'S NAME CLOSES IT (visual QA pass 1,
      // overview-agents-75): a click leaves focus here, and only the × closed.
      onKeyDown={(e) => {
        if (e.key !== 'Escape' || !picked || e.defaultPrevented) return
        e.preventDefault()
        onPick(row.step.step_id)
      }}
    >
      {withDot && <WfStepMark look={row.look} />}
      <span className="wf-pick-id" title={row.step.step_id}>
        {row.step.step_id}
      </span>
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
  detail = null,
}: {
  rows: readonly StepRowModel[]
  axis: TimelineAxis | null
  picked: string | null
  onPick: (stepId: string) => void
  /**
   * Drawn directly under the picked step's track, across the whole grid: the
   * inspector, below the width where it docks beside the view (#110).
   */
  detail?: ReactNode
}) {
  return (
    <div className="wf-timeline" role="group" aria-label="When each step waited and ran">
      <TimelineKey />
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
            aria-label={`${r.step.step_id}: ${r.times.sentence}${axis === null ? '' : cutSentence(r.times, axis)}`}
          >
            {axis !== null && <Spans row={r} axis={axis} />}
            {r.times.mark !== null && <TrackMark times={r.times} axis={axis} />}
            {axis !== null && axis.now !== null && (
              <i className="wf-tl-now" style={{ left: `${pctOf(axis, axis.now)}%` }} />
            )}
          </div>
          {detail !== null && picked === r.step.step_id && <div className="wf-tl-inline">{detail}</div>}
        </Fragment>
      ))}
    </div>
  )
}

/**
 * WHAT EACH KIND OF BAR IS, IN ONE LINE ABOVE THE AXIS (QA G3-13). A wait, a
 * run, the time on parents and a park were told apart only by a hover; each
 * swatch here is drawn by the same classes the spans are, so the key cannot
 * describe a bar the track does not draw. `parked` and `skipped` are the
 * hatches the Graph's node uses for those states; `running` is the run's core
 * in the live hue with the open end `is-open` draws.
 */
export const TIMELINE_KEY: readonly { kind: string; word: string; title: string }[] = [
  { kind: 'parents', word: 'on parents', title: 'Waiting for a parent step to finish: the step could not start any sooner.' },
  { kind: 'waited', word: 'waited', title: 'Queued, ready, leased or dispatched: time waited, never time run.' },
  { kind: 'parked', word: 'parked', title: 'Parked and holding nothing: the step is waiting on a provider window or capacity, and will resume.' },
  { kind: 'ran', word: 'ran', title: 'From the latest start to the recorded finish, in the step’s state colour.' },
  // A RUN STILL GOING HAS ITS OWN SWATCH (visual QA pass 2, ovag2-09): the
  // key named none, so wf_audit_01's running scan-terraform read as 8 days waited.
  { kind: 'running', word: 'running', title: 'Started and not finished: the run so far, its open end dashed, up to now.' },
  { kind: 'skipped', word: 'skipped by verdict', title: 'The verdict gate kept the step’s agent from running; the attempt only published the reviewed work.' },
]

function TimelineKey() {
  return (
    <ul className="wf-tl-key" aria-label="Timeline key">
      {TIMELINE_KEY.map((k) => (
        <li key={k.kind} className="wf-tl-key-item" title={k.title}>
          <i className={`wf-tl-swatch is-${k.kind}`} aria-hidden />
          {k.word}
        </li>
      ))}
    </ul>
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
      {/* THE SCALE SAYS IT IS BROKEN (#107), where each break took time out. */}
      {axis.breaks.map((b) => (
        <i key={`break-${b.from}`} className="wf-tl-scale-break" style={{ left: `${pctOf(axis, b.from)}%` }}>
          ⫽
        </i>
      ))}
    </>
  )
}

/**
 * What the track's name adds when one of its waits is drawn cut short (#107),
 * so the length the bar no longer shows is still said.
 */
function cutSentence(times: StepTimes, axis: TimelineAxis): string {
  const cut = times.spans.map((s) => drawnSpan(axis, s)).find((d) => d.clampedMs !== null)
  if (cut === undefined || cut.clampedMs === null || axis.clampMs === null) return ''
  return ` Its wait is drawn cut short: it lasted ${durationText(cut.clampedMs)}, beyond the ${durationText(axis.clampMs)} that ${CLAMP_PERCENTILE}% of this workflow's spans stay within.`
}

/** A run narrower than this share of the track is drawn at `SHORT_SPAN_MIN` with its length beside it. */
const SHORT_SPAN_PCT = 1.5
const SHORT_SPAN_MIN = '4px'

/**
 * One row's spans.
 *
 * AN OUTLIER WAIT IS DRAWN CUT SHORT (#107): `axisOf` took the part of it no
 * other span covers out of the axis, so the bar spans the break. The
 * broken-axis mark stands where the time was taken out and the wait's real
 * length is written just after it, inside the track. The bar is not to scale
 * and says so; the number is.
 */
function Spans({ row, axis }: { row: StepRowModel; axis: TimelineAxis }) {
  return (
    <>
      {row.times.spans.map((s, i) => {
        const d = drawnSpan(axis, s)
        const left = pctOf(axis, d.from)
        const width = pctOf(axis, d.to) - left
        // ONLY A FAILURE OR LIVE WORK TAKES A HUE (WF-11, the #122 hue
        // ruling, kept by the brand pairs of #405). A wait is an outline in
        // every state, because waiting is not the step's verdict; a finished
        // run is the one grey pair whether it succeeded or was cancelled,
        // because finishing is not a verdict either. A FAILED run also takes
        // `is-bad` -- its post -- and a run in flight carries `is-running` on
        // its own kind. The row's state mark still says every state.
        const bad = s.kind === 'ran' && row.look.tone === 'bad' ? ' is-bad' : ''
        // A RUN TAKES ITS STEP'S TINT (#405, owner 2026-10-01; workflows.html
        // D): the state's pair, as on the node and the row. A wait does not --
        // the mock-up draws every wait neutral, for the reason above.
        const tint = s.kind === 'ran' || s.kind === 'running' ? ` ${tintClass(row.look)}` : ''
        const cut = d.clampedMs !== null ? ' is-clamped' : ''
        // A VERDICT-SKIPPED RUN IS THE NODE'S SKIP HATCH, and a step parked
        // now draws its open wait in the node's parked hatch (QA G3-02,
        // G3-13): an outline on the track read as an empty row for a step
        // parked 1h 37m.
        const skip = s.kind === 'ran' && row.look.mark === 'skipped' ? ' is-skipped' : ''
        // A PARKED STEP'S OPEN WAIT ON ITS PARENTS IS PARKED TOO (visual QA
        // pass 1, overview-agents-88): `report`, parked on a dependency, drew
        // the dotted 'on parents' outline, not the key's parked hatch.
        const parkedNow = row.look.mark === 'parked' && (s.kind === 'waiting' || (s.kind === 'parents' && s.open))
        const kind = parkedNow ? 'waiting' : s.kind
        const parked = parkedNow ? ' is-parked' : ''
        // A RUN TOO SHORT TO SEE KEEPS A BAR AND SAYS ITS LENGTH (overview-
        // agents-88): on an eight-day axis `plan` and `scan-scripts` were 1px
        // ticks with no figure. The label stands just after the bar.
        const short = (s.kind === 'ran' || s.kind === 'running') && width < SHORT_SPAN_PCT
        return (
          <Fragment key={i}>
            <i
              className={`wf-tl-span is-${kind}${s.open ? ' is-open' : ''}${bad}${tint}${cut}${skip}${parked}`}
              style={{ left: `${left}%`, width: `${width}%`, minWidth: short ? SHORT_SPAN_MIN : undefined }}
            />
            {short && (
              // PAST THE BAR'S DRAWN EDGE (review of QA1-FIX-29): the bar ends
              // at the larger of its width and SHORT_SPAN_MIN, so their sum is
              // never on it. `.wf-tl-short` drops the cut label's backdrop and
              // its -0.5ch pull (styles/workflow-views.css), which covered it.
              <span className="wf-tl-cut wf-tl-short" style={{ left: `calc(${left + width}% + ${SHORT_SPAN_MIN})` }} aria-hidden>
                {durationText(s.to - s.from)}
              </span>
            )}
            {d.clampedMs !== null &&
              d.breaks.map((b, k) => (
                <span key={b} className="wf-tl-cut" style={{ left: `${pctOf(axis, b)}%` }} aria-hidden>
                  <i className="wf-tl-break">⫽</i>
                  {k === 0 && durationText(d.clampedMs!)}
                </span>
              ))}
          </Fragment>
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
/** A step cost summed over only some attempts: `$0.253 · 1 of 3` (`figuresFor`, overview-agents-69). */
const PARTIAL_COST = /^(.*) · (\d+ of \d+)$/

/**
 * THE COST CELL, WITH A PARTIAL SUM'S MARK ON A LINE OF ITS OWN (QA1B-08
 * re-review, 2026-10-28). `$0.4230 · 1 of 2` is 127px of 12px mono and Cost's
 * ceiling is 100px, so the ellipsis cut `of 2`, the part the mark is for.
 * Widening the column would take Why under the 60px it keeps at the 980px
 * minimum, so the mark wraps instead, as the workflow list's `10 of 12
 * reported` does (V163): `wf-cost-of` is a block in the desktop table and
 * inline in the stacked card, where the line has the room.
 */
function CostCellView({ cell, pending, from }: { cell: Cell; pending: boolean; from: 'result' | null }) {
  const m = pending || from !== null ? null : PARTIAL_COST.exec(cell.text)
  if (m === null) return <CellView cell={cell} pending={pending} from={from} />
  return (
    <>
      <CellView cell={{ ...cell, text: m[1]! }} />{' '}
      <span className="wf-cost-of" title={cell.note || undefined}>· {m[2]}</span>
    </>
  )
}

function CellView({
  cell,
  pending = false,
  from = null,
}: {
  cell: Cell
  pending?: boolean
  from?: 'result' | null
}) {
  // A WORD, NOT ONLY A PULSE (live QA WF-24, 2026-10-11): the pulse bar on
  // the table's ground read as an empty cell beside a total already drawn.
  if (pending) {
    return (
      <span className="wf-cell is-reading" title="The step's attempt read has not landed yet.">
        reading…
      </span>
    )
  }
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
 *  column that is read rather than ranked. Heads in sentence case, as every
 *  table head is (components.html A; visual QA Q1, 2026-10-02). */
const COLUMNS: ReadonlyArray<{ col: string; label: string; sort: SortKey | null; num: boolean }> = [
  { col: 'step', label: 'Step', sort: 'step', num: false },
  { col: 'state', label: 'State', sort: 'state', num: false },
  // WHY IT IS NOT RUNNING, OR WHY IT FAILED (#106). Beside the state it
  // explains; read rather than ranked, as the Agents table's column is.
  { col: 'why', label: 'Why', sort: null, num: false },
  { col: 'runner', label: 'Runner', sort: null, num: false },
  { col: 'waited', label: 'Waited', sort: 'waited', num: true },
  { col: 'ran', label: 'Ran', sort: 'ran', num: true },
  { col: 'attempts', label: 'Attempts', sort: 'attempts', num: true },
  { col: 'cost', label: 'Cost', sort: 'cost', num: true },
  { col: 'tokens', label: 'Tokens', sort: 'tokens', num: true },
  { col: 'inputs', label: 'Inputs', sort: null, num: false },
]

/**
 * THE STEP COLUMN'S WIDTH: ITS LONGEST NAME, OUT OF WHY'S SLACK (V098, #1038).
 *
 * At the sheet's 96px every name past nine characters was cut -- `cold-start`,
 * `scan-scr…` -- while all the slack went to Why. The column now asks for its
 * longest name: the name's characters at the pick's mono advance (DM Mono's
 * measured `MONO_ADVANCE_EM` of `--t-body`, 0.602em; at a rounded 0.6em the
 * face's real advance overran the column and cut the name's tail) plus the
 * cell's and the button's 22px of chrome. It is capped at the floor plus whatever the table's box has over its
 * 980px minimum (`100cqi`: the `wf-steps` container the table sits in), so Why
 * keeps what it has at the minimum and a name longer than that is still cut,
 * whole in its title. The 96px floor is the sheet's `min-width`, which this
 * cannot go under. Inline, on the head cell, because a fixed table takes its
 * column widths from there and the length is this table's own.
 */
export function stepColumnWidth(rows: readonly { step: { step_id: string } }[]): CSSProperties {
  const longest = rows.reduce((n, r) => Math.max(n, r.step.step_id.length), 0)
  return { width: `min(calc(${longest} * ${MONO_ADVANCE_EM} * var(--t-body) + 22px), calc(96px + 100cqi - 980px))` }
}

/**
 * EACH FIGURE COLUMN'S PX CEILING: the width the sheet holds it at, sized to
 * the widest value it can hold -- `skipped by verdict`, `task unread`, `not
 * reported` (styles/workflows.css). Ran is 128 because `between attempts` is
 * 16 characters of 12px mono, 115px, and it was cut at 114; State (136 to
 * 126) and Tokens (104 to 100) gave the room (V202, #1427).
 */
const FIGURE_COLUMN_CAP_PX: Readonly<Record<'state' | 'waited' | 'ran' | 'attempts' | 'cost' | 'tokens', number>> = {
  state: 126,
  waited: 108,
  ran: 128,
  attempts: 92,
  cost: 100,
  tokens: 100,
}

/**
 * THE FIGURE COLUMNS ARE AS WIDE AS WHAT THIS TABLE PRINTS IN THEM, AND WHY
 * TAKES THE REST (V202, #1427).
 *
 * Every figure column was its ceiling, the widest value it could ever hold, in
 * every table: at 1440 a failed step's reason was cut to two ellipsised lines
 * in a 60-75px Why ("exit status / 1: pytest…") while Waited, Ran, Cost and
 * Tokens printed 35-49px of figures in 100-114px cells. Each column now asks for
 * its longest value in this table at the cell's advance -- the figures are
 * 12px mono (`td.is-num`), 0.6em each; the state word is sans at no more than
 * `--t-meta`, measured at the same 0.6em as an upper bound, after its 22px mark
 * and gap -- never less than its head (the label and the sort arrow, at
 * `--t-meta`) and never more than its ceiling. 12px is the cell's padding.
 * Inline on the head cells, as the Step column's width is (`stepColumnWidth`),
 * because a fixed table takes its column widths from there.
 */
export function figureColumnWidths(
  rows: readonly Pick<StepRowModel, 'look' | 'times' | 'ran' | 'attempts' | 'cost' | 'tokens' | 'costFrom' | 'tokensFrom' | 'pending'>[],
): Readonly<Record<keyof typeof FIGURE_COLUMN_CAP_PX, CSSProperties>> {
  const figure = (cell: Cell, from: 'result' | null, pending: boolean): string =>
    pending ? 'reading…' : from === 'result' && cell.kind !== 'absent' ? `${cell.text} ${FROM_RESULT_WORD}` : cell.text
  const longest = (texts: readonly string[]) => texts.reduce((n, t) => Math.max(n, t.length), 0)
  const label = (col: string) => (COLUMNS.find((c) => c.col === col)?.label ?? col).length
  const width = (col: keyof typeof FIGURE_COLUMN_CAP_PX, content: string): CSSProperties => ({
    width: `min(max(calc(${label(col) + 2} * 0.6 * var(--t-meta) + 12px), ${content}), ${FIGURE_COLUMN_CAP_PX[col]}px)`,
  })
  const mono = (n: number) => `calc(${n} * 0.6 * var(--t-micro) + 12px)`
  return {
    state: width('state', `calc(${longest(rows.map((r) => r.look.word))} * 0.6 * var(--t-meta) + 34px)`),
    waited: width('waited', mono(longest(rows.map((r) => waitedCell(r.times).text)))),
    ran: width('ran', mono(longest(rows.map((r) => r.ran.text)))),
    attempts: width('attempts', mono(longest(rows.map((r) => r.attempts.text)))),
    // A partial sum's mark is a line of its own (`CostCellView`): the longer line counts.
    cost: width('cost', mono(longest(rows.flatMap((r) => {
      const t = figure(r.cost, r.costFrom, r.pending)
      const m = r.pending || r.costFrom !== null ? null : PARTIAL_COST.exec(t)
      return m === null ? [t] : [m[1]!, `· ${m[2]}`]
    })))),
    tokens: width('tokens', mono(longest(rows.map((r) => figure(r.tokens, r.tokensFrom, r.pending))))),
  }
}

/** `$0.22`, split into its whole, its decimals and any qualifier after them; no match for a cost that is a word. */
const DOLLARS = /^\$(\d+)\.(\d+)(.*)$/

/** A step whose task ended before it ever started: Waited, Cost and Tokens do not apply to it. */
function neverStarted(r: Pick<StepRowModel, 'ran'>): boolean {
  return r.ran.kind === 'absent' && r.ran.text === NEVER_STARTED_WORD
}

const NOT_APPLICABLE = (what: string): Cell => ({
  kind: 'absent',
  text: '—',
  note: `This step ended without ever starting, so it has no ${what}.`,
})

/**
 * THE ROWS AS THE TABLE PRINTS THEM (visual QA pass 1, overview-agents-70,
 * -71). Two things the row model does not decide, because they are about the
 * column rather than the step:
 *
 *  * ONE PRECISION PER COST COLUMN: `$0.22` beside `$0.231` read as two
 *    precisions of one figure. Every dollar figure is padded with zeros to the
 *    column's most decimals -- never rounded, so no figure changes.
 *  * A STEP THAT NEVER STARTED PRINTS A DASH, with its reason as the title,
 *    for Waited, Cost and Tokens: `0s` and `none` beside `never started`
 *    measured something for a step nothing happened to.
 */
export function tableRows<R extends Pick<StepRowModel, 'ran' | 'cost' | 'tokens' | 'pending' | 'times'>>(rows: readonly R[]): (R & { waited: Cell })[] {
  const places = rows.reduce((n, r) => {
    const m = DOLLARS.exec(r.cost.text)
    return m === null ? n : Math.max(n, m[2]!.length)
  }, 0)
  const pad = (c: Cell): Cell => {
    const m = DOLLARS.exec(c.text)
    return m === null ? c : { ...c, text: `$${m[1]}.${m[2]!.padEnd(places, '0')}${m[3]}` }
  }
  return rows.map((r) =>
    neverStarted(r)
      ? { ...r, waited: NOT_APPLICABLE('wait to time'), cost: NOT_APPLICABLE('cost'), tokens: NOT_APPLICABLE('tokens'), pending: false }
      : { ...r, waited: waitedCell(r.times), cost: pad(r.cost) },
  )
}

/** The columns a table draws: every one, less Why when no row has a why (D12). */
export function tableColumns(rows: readonly Pick<StepRowModel, 'why'>[]): typeof COLUMNS {
  return rows.some((r) => r.why !== null) ? COLUMNS : COLUMNS.filter((c) => c.col !== 'why')
}

/**
 * The `waited` cell: the QUEUE, from the last parent's finish to the start
 * (#107), with the time spent on the parents in its note. A step still waiting
 * on a parent has not queued yet: it prints a word, not a figure, because it
 * has no queue time -- and its sort value (`waitedMs`) is null, so it ranks
 * with the other rows that have no figure. The time on its parents is in the
 * note.
 */
function waitedCell(t: StepTimes): Cell {
  if (t.waitedMs === null && t.parentsMs !== null) {
    return {
      kind: 'absent',
      text: 'on parents',
      note: `Still waiting on a parent step, for ${durationText(t.parentsMs)} so far: it cannot queue until every parent has finished, so there is no queue time yet.`,
    }
  }
  if (t.waitedMs === null) {
    return t.mark !== null
      ? { kind: 'absent', text: t.mark.text, note: t.mark.note }
      : { kind: 'absent', text: 'not recorded', note: 'No submission time was recorded, so the wait cannot be computed.' }
  }
  const text = `${durationText(t.waitedMs)}${t.waitedOpen ? ' so far' : ''}`
  // Where the figure is counted from: the last parent's finish when the wait
  // was split (#107), the submission otherwise.
  const split = t.parentsMs !== null && t.parentsMs > 0
  const parents = split
    ? `Queued from its last parent’s finish; before that it waited ${durationText(t.parentsMs!)} on its parents, which is not in this figure. `
    : ''
  const from = split ? 'Last parent’s finish' : 'Submission'
  return {
    kind: 'measured',
    text,
    note: parents + (t.waitedOpen
      ? 'Still waiting. Time queued, parked or being dispatched -- never time run.'
      : t.attempts !== null
        ? `${from} to the latest start. This agent retried, so the earlier attempts are inside this figure.`
        : `${from} to start: time queued, parked or being dispatched -- never time run.`),
  }
}

/**
 * The `why` cell (#106): one line, the whole of it in the title, in warn ink
 * only when it asks a person to act (`whyNeedsAction`). Empty for a step with
 * nothing to explain.
 */
function WhyCell({ why }: { why: StepWhy | null }) {
  if (why === null) return null
  return (
    <span className={`wf-why${why.warn ? ' is-warn' : ''}`} title={why.full}>
      {why.text}
    </span>
  )
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
 * THE INPUTS COLUMN IS A COUNT (walkthrough A, owner 2026-10-03). The files,
 * one line each, were drawn in the column itself, which at its width stacked
 * every `plan.md ← plan` one word per line into a tower taller than the row.
 * The column says how many files the step reads; the files open in a row
 * under the step (`InputsCell`, unchanged). A step that reads nothing says
 * `none`, which is a fact about its definition, so it has no control.
 */
function inputsCount(inputs: StepInputs): number {
  return inputs.declared.size + inputs.stray.length
}

function InputsSummary({ inputs, open, controls, onToggle }: { inputs: StepInputs; open: boolean; controls: string; onToggle: () => void }) {
  const n = inputsCount(inputs)
  // THE PROMPT IS AN INPUT (visual QA pass 1, overview-agents-72): every
  // prompt-driven step read `none`, as if it had been given nothing.
  if (n === 0 && inputs.malformed === 0) {
    return (
      <span className="wf-cell is-none" title="This step reads its prompt and no file from another step.">
        prompt
      </span>
    )
  }
  const words = [
    n > 0 ? `${n} file${n === 1 ? '' : 's'}` : null,
    inputs.malformed > 0 ? `${inputs.malformed} unreadable` : null,
  ].filter((w): w is string => w !== null)
  return (
    <button
      type="button"
      className={`wf-inputs-toggle${inputs.malformed > 0 ? ' is-unread' : ''}`}
      aria-expanded={open}
      aria-controls={controls}
      title={open ? 'Hide the files this step reads' : 'Show the files this step reads'}
      onClick={onToggle}
    >
      {words.join(' · ')}
    </button>
  )
}

/** A DOM id for a step's row of files: step ids are free text. */
function inputsRowId(stepId: string): string {
  return `wf-in-${stepId.replace(/[^A-Za-z0-9_-]/g, '_')}`
}

/**
 * THE SAME STEPS AS ROWS, sortable by what a reader hunting an outlier sorts by
 * (redesign-v2 §2.3: "duration, cost, attempts, state").
 *
 * `.ctl-table`, the shared table primitive, so the rhythm, the head and the row
 * tones are the ones every other table in this console already draws. The
 * head IS sticky now (QA G3-12), on `thead`: it never stuck while the wrapper
 * was `overflow-x: auto` (WF-21), and the wrapper clips instead, because at a
 * width where the table is drawn as a table it never needs to scroll
 * sideways (styles/workflows.css). The faint seams this table showed at fractional column
 * edges were first put down to that stickiness, then to each head cell
 * painting its own fill; they survived both fixes, because Chrome paints a
 * row group's background into each cell's rect too. `thead` now also paints
 * its `--surface-2` as one inset-shadow rectangle, which Chrome draws once
 * for the whole head (#178, owner decision 2026-09-26; styles.css
 * `.ctl-table thead`). None of the head cells here declares a fill of its
 * own, and shell.test.tsx renders this head -- `data-col`, `aria-sort`, the
 * sort buttons -- to hold that.
 * A failed step's row takes `is-bad` (a full-height rule down its first cell,
 * which survives greyscale); an unread one takes `is-warn`. EVERY ROW also takes
 * its state's tint class (`tintClass`, #405): the tint across its cells and the
 * edge down its first one, in the pair the graph's node is drawn in.
 *
 * THE SORT LIVES HERE, in this component's state, and that is deliberate: the
 * only thing on this board that remounts a card is a stop-and-reload, and the
 * table draws no stop control.
 */
export function WorkflowTable({
  rows,
  picked,
  onPick,
  detail = null,
  head = null,
  title = null,
}: {
  rows: readonly StepRowModel[]
  picked: string | null
  onPick: (stepId: string) => void
  /**
   * What the table says about its own figures -- the sampling mark -- drawn
   * in the table's head row, inside its card (walkthrough A: it floated in a
   * strip above the card, apart from the figures it qualifies).
   */
  head?: ReactNode
  /** The head row's title, where the table sits under another view (the Graph's Steps). */
  title?: string | null
  /**
   * Drawn in a full-width row directly under the picked step's row: the
   * inspector, below the width where it docks beside the view (#110).
   */
  detail?: ReactNode
}) {
  const [sort, setSort] = useState<SortSpec>(DEFAULT_SORT)
  const sorted = tableRows(sortRows(rows, sort))
  const more = useMoreRight<HTMLDivElement>()
  // AN EMPTY COLUMN IS NOT DRAWN (browser QA D12, 2026-10-04): with no step
  // waiting on a reason or failed, Why was ~110px of blank while ten long step
  // names were squeezed beside it. The step column takes that room instead.
  const columns = tableColumns(rows)
  // The Step column grows to its longest name (V098; `stepColumnWidth`), and
  // the figure columns hold their longest value, so Why gets the rest (V202).
  const stepWidth = stepColumnWidth(rows)
  const figureWidths = figureColumnWidths(tableRows(rows))
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set())
  const toggleInputs = (stepId: string) =>
    setOpened((was) => {
      const next = new Set(was)
      if (!next.delete(stepId)) next.add(stepId)
      return next
    })
  return (
    <>
    {(head !== null || title !== null) && (
      <div className="wf-table-head">
        {title !== null && <h3 className="wf-table-title">{title}</h3>}
        {head !== null && <span className="wf-table-marks">{head}</span>}
      </div>
    )}
    {/* `is-scroll` (CH-13): ten columns compared across rows is a DATA table,
        so below 900px it scrolls with the step column held in view rather
        than stacking (design-system.md §7.3). `has-more` is the right-edge
        fade while columns are off that edge (#109). The box around it is the
        size container its stacked form asks about: a container query matches
        only descendants, so the table's own border could not answer it (D12). */}
    <div className="wf-table-box">
    <div ref={more.ref} className={`ctl-table wf-table is-scroll${more.on ? ' has-more' : ''}${columns.length < COLUMNS.length ? ' no-why' : ''}`}>
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.col}
                data-col={c.col}
                className={c.num ? 'is-num' : undefined}
                // With no Why column the sheet's `width: auto` gives Step all the slack already.
                style={
                  c.col === 'step'
                    ? columns.length === COLUMNS.length
                      ? stepWidth
                      : undefined
                    : c.col in figureWidths
                      ? figureWidths[c.col as keyof typeof figureWidths]
                      : undefined
                }
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
            <Fragment key={r.step.step_id}>
            <tr
              data-step={r.step.step_id}
              className={`${tintClass(r.look)}${
                r.look.tone === 'bad'
                  ? ' is-bad'
                  : r.look.tone === 'unknown'
                    ? ' is-warn'
                    : r.look.mark === 'skipped'
                      ? ' is-skipped'
                      : ''
              }`}
            >
              <td data-col="step">
                <PickButton row={r} picked={picked === r.step.step_id} onPick={onPick} withDot={false} />
              </td>
              <td data-col="state">
                <span className="wf-cell-state" title={r.look.title}>
                  <WfStepMark look={r.look} word />
                </span>
              </td>
              {columns.length === COLUMNS.length && (
                // A ROW WITH NO WHY PRINTS NO `Why` LABEL (QA G3-32): stacked,
                // the label is the cell's `::before`, and it stood alone.
                <td data-col="why" data-label={r.why === null ? undefined : 'Why'}>
                  <WhyCell why={r.why} />
                </td>
              )}
              <td data-col="runner" data-label="Runner">{r.step.runner_profile}</td>
              <td data-col="waited" data-label="Waited" className="is-num">
                <CellView cell={r.waited} />
              </td>
              <td data-col="ran" data-label="Ran" className="is-num">
                <CellView cell={r.ran} />
              </td>
              <td data-col="attempts" data-label="Attempts" className="is-num">
                <AttemptsView cell={r.attempts} over={r.attemptsOver} />
              </td>
              <td data-col="cost" data-label="Cost" className="is-num">
                <CostCellView cell={r.cost} pending={r.pending} from={r.costFrom} />
              </td>
              <td data-col="tokens" data-label="Tokens" className="is-num">
                <CellView cell={r.tokens} pending={r.pending} from={r.tokensFrom} />
              </td>
              <td data-col="inputs" data-label="Inputs">
                <InputsSummary
                  inputs={r.inputs}
                  open={opened.has(r.step.step_id)}
                  controls={inputsRowId(r.step.step_id)}
                  onToggle={() => toggleInputs(r.step.step_id)}
                />
              </td>
            </tr>
            {opened.has(r.step.step_id) && inputsCount(r.inputs) + r.inputs.malformed > 0 && (
              <tr className="wf-xrow">
                <td colSpan={columns.length}>
                  <div id={inputsRowId(r.step.step_id)} className="wf-xinputs">
                    <span className="wf-xinputs-h">Reads</span>
                    <InputsCell inputs={r.inputs} />
                  </div>
                </td>
              </tr>
            )}
            {detail !== null && picked === r.step.step_id && (
              <tr className="wf-inline-row">
                <td colSpan={columns.length}>
                  <div className="wf-inline">{detail}</div>
                </td>
              </tr>
            )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
    </div>
    </>
  )
}

/**
 * WHETHER A SIDEWAYS SCROLLER HAS COLUMNS OFF ITS RIGHT EDGE (#109). At 390
 * the step Table showed step, state and why, and waited, ran and cost sat off
 * screen with nothing saying so. The cue is a `mask-image` fade on the right
 * edge, drawn only while there is more to the right: a fade over the last
 * column once it is in view would dim a figure for no reason. A mask, not the
 * `background-attachment` scroll shadow, which was reverted for breaking the
 * contrast probe. Re-measured on scroll and on resize.
 */
export function useMoreRight<T extends HTMLElement>(): { ref: Ref<T>; on: boolean } {
  const ref = useRef<T>(null)
  const [on, setOn] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (el === null) return
    const measure = () => setOn(el.scrollLeft + el.clientWidth < el.scrollWidth - 1)
    measure()
    el.addEventListener('scroll', measure, { passive: true })
    const watch = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    watch?.observe(el)
    return () => {
      el.removeEventListener('scroll', measure)
      watch?.disconnect()
    }
  }, [])
  return { ref, on }
}

/**
 * A sideways scroller that says when columns are off its right edge: the
 * `useMoreRight` cue, mounted WITH its element. A table drawn only once its
 * rows land (an Outcomes card, the workflow list) cannot take the hook from
 * the screen above it -- the hook measures on mount, when there was no
 * element yet -- so it takes this instead (VQA V109).
 */
export function MoreRight({ className, children }: { className: string; children: ReactNode }) {
  const more = useMoreRight<HTMLDivElement>()
  return (
    <div ref={more.ref} className={more.on ? `${className} has-more` : className}>
      {children}
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
  readonly mark: StepLook['mark']
  readonly hue: StepLook['hue']
  readonly word: string
}

/**
 * HOW MANY OCCURRENCES THE SAME-STEP STRIP DRAWS: fifteen around the current
 * one, the rest counted at each end. All 62 of a busy board ran past the
 * card's right edge (#503); the position beside the strip is the fact, and the
 * strip only shows where in the run of workflows this one sits.
 */
export const STRIP_MAX = 15

/** The window of `n` occurrences the strip draws around `at`, and how many fall either side. */
export function stripWindow(n: number, at: number, max = STRIP_MAX): { from: number; to: number } {
  if (n <= max) return { from: 0, to: n }
  const from = Math.min(Math.max(0, at - Math.floor(max / 2)), n - max)
  return { from, to: from + max }
}

type AttemptsRead =
  | { kind: 'reading' }
  | { kind: 'ready'; attempts: AttemptRow[] }
  | { kind: 'failed'; detail: string }

/** Which scrub control focus should land on when an inspector mounts because
 *  the selection moved INTO it from another workflow's card. */
export type ScrubFocus = 'newer' | 'older' | null

/**
 * THE CARD'S FACT LABELS ARE WORDS (visual QA run 2, WG-6, 2026-10-20): the
 * keys `attemptFacts` returns are short ids, and printed as labels they read
 * `gen 3`, `exit 0` and `ckpts 2`, none of which said what it counted. `exit`
 * is `exit code`, so the agent process's code is not read as the step's
 * verdict beside `why it failed` (WG-4). A key with no entry is its own word.
 */
const FACT_LABEL: Readonly<Record<string, string>> = {
  // THE LEASE'S, AND THE ATTEMPT'S (visual QA run 5b, L4 and D2): a bare
  // `generation 3` said nothing of what it numbers, and `cost $0.253` beside
  // the head's workflow total read as a second figure for the same thing.
  gen: 'lease generation',
  exit: 'exit code',
  // SAVED CHECKPOINTS, NOT `checkpoints` (visual QA run 6, F2, #1680): the
  // fixture's failed step is CALLED checkpoints, and 'checkpoints 2' beside
  // its name read as the step rather than a count of what it saved.
  ckpts: 'saved checkpoints',
  cost: 'this attempt’s cost',
  // A failed step's clean exit, said as what it is (`unsaid`).
  process: 'agent process',
}

/**
 * WHAT A FACT'S FIGURE IS FOR, where its label cannot say it (visual QA run
 * 5b, L4): printed after the figure, in the faint meta register. The fencing
 * generation is the one: a number that only means something to a reader told
 * that a stale worker with an older one exits without running (CONTRACT
 * invariant 5).
 */
const FACT_HINT: Readonly<Record<string, string>> = {
  gen: 'raised by every new lease; a worker holding an older one exits without running',
  // THE STEP'S OWN (box 6343767840): the panel's '$0.253' beside the head's
  // '$1.21' read as a second figure for the workflow.
  cost: 'this step’s own, not the workflow’s total',
}

/** `exit status 2: …`, the way a worker writes a non-zero exit into `error`. */
const EXIT_STATUS = /^exit status (\d+):?\s*/

/**
 * WHAT THE CARD ALREADY SAYS IS NOT SAID AGAIN (visual QA run 6, F2, and
 * box 6298221306, #1680). Given the step's failure (`cause`, the text 'Why it
 * failed' prints, or null for a step that did not fail):
 *
 *  * an `error` fact that is the failure itself goes: the card printed it
 *    twice, once under 'Why it failed' and once as 'error exit status 2: …';
 *  * an `error` that only opens with the exit the `exit code` fact states
 *    loses that opening, so the exit is said once;
 *  * an `exit code` of 0 on a failed step is not drawn as 'exit code 0' beside
 *    'Why it failed', where it read as the step having succeeded. It is said
 *    for what it is: the process ended cleanly, and the failure is the step's.
 */
function unsaid(facts: readonly Fact[], cause: string | null): Fact[] {
  const exit = facts.find((f) => f.key === 'exit')
  const code = exit !== undefined && exit.cell.kind !== 'absent' ? exit.cell.text : null
  return facts.flatMap((f): Fact[] => {
    if (f.key === 'exit' && cause !== null && code === '0') {
      return [{ key: 'process', cell: { ...f.cell, text: 'ended cleanly · the step failed on what it reported, not on the process' } }]
    }
    if (f.key !== 'error' || f.cell.kind === 'absent') return [f]
    const whole = f.cell.note || f.cell.text
    const first = whole.split('\n')[0] ?? ''
    if (cause !== null && (whole === cause || cause.startsWith(first))) return []
    const m = EXIT_STATUS.exec(f.cell.text)
    if (m === null || m[1] !== code) return [f]
    const rest = f.cell.text.slice(m[0].length)
    return rest === '' ? [] : [{ ...f, cell: { ...f.cell, text: rest } }]
  })
}

/**
 * `6 steps in 2 stages (5 → 1)`: a workflow's shape as the page head says it
 * (QA-119) and the same-step row repeats it (WG-6) -- the step count, then the
 * stages, then the widths, which alone read as an unexplained `5 → 1`.
 */
export function shapeWords(shape: Pick<DagShape, 'steps' | 'levels' | 'text'>): string {
  return `${shape.steps} step${shape.steps === 1 ? '' : 's'}${shape.levels > 1 ? ` in ${shape.levels} stages (${shape.text})` : ''}`
}

/**
 * The shape's words with its parenthetical in a span of its own, so a phone
 * moves `(1 → 2 → 1 → 1)` to the next line whole rather than breaking inside
 * it (review of U02, #2391 comment 58). The text reads exactly as before.
 */
export function ShapeWords({ words }: { words: string }) {
  const at = words.indexOf(' (')
  if (at < 0) return <>{words}</>
  return (
    <>
      {words.slice(0, at)} <span className="wf-scrub-paren">{words.slice(at + 1)}</span>
    </>
  )
}

/**
 * THE FACTS A READER DEBUGGING THE PLATFORM WANTS, NOT ONE READING THE STEP
 * (visual QA pass 1, overview-agents-66): the lease generation and its hint
 * were the card's jargon. They fold under More about this attempt.
 */
const FOLDED_FACTS: ReadonlySet<string> = new Set(['gen'])

function FactList({ facts, cause = null }: { facts: readonly Fact[]; cause?: string | null }) {
  const all = unsaid(facts, cause)
  const folded = all.filter((f) => FOLDED_FACTS.has(f.key))
  return (
    <>
      <FactItems facts={all.filter((f) => !FOLDED_FACTS.has(f.key))} />
      {folded.length > 0 && (
        <details className="wf-inspect-more">
          <summary>More about this attempt</summary>
          <FactItems facts={folded} />
        </details>
      )}
    </>
  )
}

function FactItems({ facts }: { facts: readonly Fact[] }) {
  return (
    <ul className="ctl-facts wf-inspect-facts">
      {facts.map((f) =>
        f.cell.kind === 'absent' ? (
          <li key={f.key} className="ctl-fact is-absent">
            <b>{FACT_LABEL[f.key] ?? f.key}</b>
            <span className="ctl-mark is-absent" aria-label={f.cell.note}>
              {f.cell.text}
            </span>
          </li>
        ) : (
          <li key={f.key} className="ctl-fact" title={f.cell.note || undefined}>
            <b>{FACT_LABEL[f.key] ?? f.key}</b>
            {f.cell.text}
            {FACT_HINT[f.key] !== undefined && <span className="wf-fact-hint"> · {FACT_HINT[f.key]}</span>}
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
 * WHERE IT LIVES. Next to the picked step, inside the card of the workflow
 * the step belongs to: docked in the right-hand column beside the view at
 * 1100px and up, and directly under the picked row or track below that (#110;
 * it used to mount under the whole view, a screen away from row 10). When the second scrubber moves the selection to the same step
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
  workflowLabel = null,
  workflowStarted = null,
  row,
  taskState,
  siblings,
  siblingIndex,
  shape,
  onSibling,
  focus,
  onFocused,
  onClose,
  now,
  load = loadAttempts,
  children = null,
  changes = null,
  stepHeading = false,
  idInHead = false,
}: {
  workflowId: string
  /**
   * The workflow spec's label, or null (#330). The head names the workflow by
   * it, as the row does, with the id on the line under it. OPTIONAL: a caller
   * with no label draws the id alone.
   */
  workflowLabel?: string | null
  /**
   * The workflow's start, derived from its earliest step (#376), and its
   * submit time, as `workflowStartText` prints them. OPTIONAL: a caller with
   * no step tasks to derive it from draws the head without it.
   */
  workflowStarted?: { text: string; title: string; submitted: string; submittedTitle: string } | null
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
  /**
   * This workflow's shape (#112). The same-step scrubber only walks workflows
   * of this shape, so the shape is printed beside the position: "workflow 2
   * of 3" is two of three `1 → 5 → 1` workflows, not of every workflow on the
   * board that has a step with this id.
   */
  shape: DagShape
  onSibling: (delta: -1 | 1) => void
  focus: ScrubFocus
  onFocused: () => void
  onClose: () => void
  now: number
  load?: AttemptLoader
  /**
   * What the workflow page adds under the head (agent-detail-2.html A): the
   * step's own actions, a review step's verdict card, the merge step's
   * checklist. Null everywhere else.
   */
  children?: ReactNode
  /**
   * The step's changes (diff-viewer.md §2 variant 2): the workflow's Changes
   * tab filtered to this step, and the step's file count -- a dash, with its
   * reason as the title, where nobody counted (`changesCount`). Null: no link.
   */
  changes?: { href: string; count: number | string | null; say: string | null } | null
  /**
   * THE STEP IS THE CARD'S HEADING (visual QA QA-120, run 5b, #1680 comment
   * 6283740207). On a workflow's own page the page is already titled by the
   * workflow, and the card led with that title again, the step a small line
   * under it. True there: the head's first line is the step, as a heading,
   * and the workflow's name and times (the page head's) are not repeated.
   */
  stepHeading?: boolean
  /**
   * THE PAGE HEAD CARRIES THE WORKFLOW'S ID (VQ-015 re-review), behind its
   * Copy id, so the same-step row does not render it. False on a card with
   * no such head, which keeps the id on that row.
   */
  idInHead?: boolean
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
  const strip = stripWindow(siblings.length, siblingIndex)
  const hasNewer = siblingIndex > 0
  const hasOlder = siblingIndex < siblings.length - 1

  // FOCUS FOLLOWS THE SELECTION, once, on arrival, onto the SAME control the
  // reader was pressing. It used to fall back to the other one when this was
  // the end (the last workflow has no older one), because a disabled button
  // cannot hold focus -- which put a reader holding ArrowRight on "newer" with
  // nothing said. `ScrubButton` stays focusable at its end, so there is no
  // fallback to need.
  //
  // HELD UNTIL THE PAGER IS DRAWN (QA1-FIX-29): a list of one draws no
  // pager (overview-agents-67), and the inspector can arrive before the board
  // that tells it the list is longer -- so the control is focused when it
  // first exists, not only on mount. The request is still handed back at once.
  const wantFocus = useRef(focus)
  const paged = siblings.length > 1
  useEffect(() => {
    if (focus !== null) onFocused()
    // On mount only: this is about how the inspector ARRIVED.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (wantFocus.current === null || !paged) return
    ;(wantFocus.current === 'older' ? olderRef : newerRef).current?.focus()
    wantFocus.current = null
  }, [paged])

  const ready = read.kind === 'ready' ? read.attempts : null
  const at = ready === null || ready.length === 0 ? null : (index ?? ready.length - 1)
  const current = ready !== null && at !== null ? (ready[at] ?? null) : null
  const newest = ready !== null && at === ready.length - 1
  const toAttempt = (delta: -1 | 1) => {
    if (ready === null || at === null) return
    const n = at + delta
    if (n >= 0 && n < ready.length) setIndex(n)
  }
  const prevAttempt = ready !== null && at !== null && at > 0 ? () => toAttempt(-1) : null
  const nextAttempt = ready !== null && at !== null && at < ready.length - 1 ? () => toAttempt(1) : null
  // The stepper's records and the table's counted attempts differ (a CI park
  // is refunded): the stepper says `run` and names the counted figure (WF-6).
  const counted = ready !== null && row.sort.attempts !== null && row.sort.attempts !== ready.length
  const countedTitle =
    ready === null || at === null
      ? undefined
      : `Run ${at + 1} of the ${ready.length} attempt records this step has. ` +
        `${row.attempts.text} count against its attempts, as the table says: a wait for CI is refunded and not counted.`

  // ESCAPE CLOSES IT (visual QA pass 1, overview-agents-75), and focus goes
  // back to the step's name in the view that opened it.
  const closeOnEscape = (e: KeyboardEvent<HTMLElement>) => {
    // The graph's docked panel has its own Escape, which puts focus on the node.
    if (e.key !== 'Escape' || e.defaultPrevented || e.currentTarget.closest('.wf-panel') !== null) return
    e.preventDefault()
    const scope = e.currentTarget.closest('.wf-card') ?? e.currentTarget.ownerDocument
    const name = [...scope.querySelectorAll<HTMLButtonElement>('button.wf-pick')].find((b) => b.dataset.step === row.step.step_id)
    onClose()
    name?.focus()
  }

  return (
    <section className="wf-inspect" aria-label={`Step ${row.step.step_id} of ${workflowId}`} onKeyDown={closeOnEscape}>
      <div className="wf-inspect-head">
        {/* WHICH WORKFLOW, BY NAME (#330): the label the row leads with, and
            the id one line down; the id alone when the spec gave no label.
            ON THE WORKFLOW'S OWN PAGE THE STEP LEADS INSTEAD (QA-120): the
            page is titled by the workflow, so the card's heading is the step
            -- its mark, its id and its state -- on the line the × ends. */}
        {stepHeading ? (
          <h3 className="wf-inspect-wf wf-inspect-steph">
            {/* Hidden from the heading's name, which the state word after
                the id already ends: 'checkpoints failed', not 'failed
                checkpoints failed'. */}
            <span className="wf-inspect-steph-mk" aria-hidden>
              <WfStepMark look={row.look} />
            </span>
            <span className="wf-inspect-id">{row.step.step_id}</span>
            <span className="wf-inspect-state" title={row.look.title}>
              {row.look.word}
            </span>
          </h3>
        ) : (
          <span className={`wf-inspect-wf${workflowLabel !== null ? ' has-label' : ''}`}>
            {workflowLabel !== null && (
              <span className="wf-inspect-label" title={workflowLabel}>
                {workflowLabel}
              </span>
            )}
            <Id title={workflowId}>{workflowId}</Id>
          </span>
        )}
        {/* THE CLOSE BUTTON ENDS THE HEAD'S FIRST LINE (V100, #1038). It was
            the head's last item, after Open agent and Changes, so in a panel
            or a narrow pane the row wrapped and the × sat alone on a line of
            its own under the step name, tied to nothing. Beside the workflow's
            name it is the panel's top-right corner, where a close goes. */}
        <button type="button" className="wf-inspect-close" aria-label="Stop inspecting this step" onClick={onClose}>
          ×
        </button>
        {/* THE WORKFLOW'S START AND SUBMIT (#376), on their own line under
            its name: derived from the earliest step, as the row prints it.
            `submitted`, not `sub` (visual QA run 2, WG-6): the short form
            said nothing a reader could expand. */}
        {!stepHeading && workflowStarted !== null && (
          <span className="wf-inspect-times" title={workflowStarted.title}>
            started {workflowStarted.text} · submitted {workflowStarted.submitted}
          </span>
        )}
        {!stepHeading && (
          <>
            <WfStepMark look={row.look} />
            <span className="wf-inspect-id">{row.step.step_id}</span>
            <span className="wf-inspect-state" title={row.look.title}>
              {row.look.word}
            </span>
          </>
        )}
        {/* `.ctl-link`: ink plus an underline, the accent only on hover and
            focus. Unclassed, this anchor fell back to the browser's own blue --
            and to visited purple once the run had been opened.

            THE ONE WAY FROM THE WORKFLOW TO THE RUN (WF-7). Picking a step --
            on the Graph, the Timeline or the Table -- fills this inspector and
            navigates nowhere; this link opens the step's agent in the Work ›
            Agents drawer. A step with no task has nothing to open, so it gets
            no link rather than a dead one. */}
        {taskId !== null && (
          // THE AGENT'S OWN PATH, UNDER THE LIST THAT HOLDS IT (live QA WF-21,
          // 2026-10-11): the legacy `#work/task/<id>` resolved every state to
          // `/agents/live/<id>`, a succeeded step's included. By the rule Run
          // steps' Open agent uses (`agentPath`, QA G2-17); a task the read
          // did not carry keeps the Live address, having no state to route by.
          <a
            className="ctl-link wf-inspect-run"
            href={
              taskState === null
                ? addressToPath(`work/task/${encodeURIComponent(taskId)}`)
                : agentPath({ id: taskId, state: taskState })
            }
          >
            Open agent →
          </a>
        )}
        {/* THE STEP'S LOGS, ONE PRESS AWAY (visual QA QA-121, 2026-10-20):
            a failed step's card said why in one line ('exit status 1: pytest
            collected 2 failures') and the only way to what the agent printed
            was Open agent, then its Logs tab. The agent's own path, as Open
            agent's, with its Logs pane. */}
        {taskId !== null && (
          <a
            className="ctl-link wf-inspect-logs"
            href={
              taskState === null
                ? addressToPath(`work/task/${encodeURIComponent(taskId)}/logs`)
                : `${agentPath({ id: taskId, state: taskState })}/logs`
            }
          >
            Logs ›
          </a>
        )}
        {changes !== null && (
          <a
            className="ctl-link wf-inspect-changes"
            href={changes.href}
            title={changes.say ?? 'This workflow’s Changes tab, showing this step’s column only. Its agent’s own Changes tab holds the whole patch.'}
          >
            {/* UNCOUNTED, THE LINK SAYS NO COUNT AT ALL (V111, #1038). It
                printed `Changes · — ›`, a dash where a figure goes, which read
                as a broken number; and a 0 would claim a count nobody took.
                The reason nothing was counted is the link's title. */}
            {/* THE LINK NAMES ITS SCOPE (visual QA run 2, WG-7, 2026-10-20): it
                read `Changes ›`, the page's own tab's name, for that tab
                filtered to this one step. AND WHERE IT OPENS (run 5b, L5):
                beside the Changes page's `Agent’s changes ›`, which is the
                agent's own patch, 'This step’s changes' did not say it is the
                workflow's tab, one column of it. AND WHICH STEP (run 6, F4,
                #1680 box 6298221770): 'This step’s' named none, beside the
                tab's 'Changes' and the banner's 'agent, logs, changes', so
                no control said it led to this step's changes. The name comes
                after the scope: "<step>’s" reads 'checkpoints’s' (F5), and a
                link opening 'Changes' reads as the tab (WG-7). */}
            {changes.count === null
              ? `This step’s changes in this workflow: ${row.step.step_id} ›`
              : `This step’s changes in this workflow: ${row.step.step_id} · ${changes.count} ${changes.count === 1 ? 'file' : 'files'} ›`}
          </a>
        )}
      </div>

      {/* NO LIVE THUMBNAIL HERE (LB-UX2b, docs/design/live-browser.md §6).
          The running browser step's card is inline in its graph NODE
          (`StepNode` in Workflows.tsx), as the owner's chosen mockup draws
          it; a second one here would be a second stream of the same session. */}

      {children}

      {/* ITS OWN LINE, UNDER THE REASON IT QUALIFIES (review of QA1-FIX-29):
          inside the attempt row it took `.wf-scrub`'s one line, clipped at
          390, and on a wide card it sat beside the closed fold as its label.
          A block of its own in the card's column wraps. */}
      {ready !== null && at !== null && !newest && row.why !== null && row.why.kind === 'cause' && (
        <span className="wf-scrub-note">
          Why it failed is the step’s final reason, from attempt {ready.length}; the attempt {at + 1} figures below are its own.
        </span>
      )}

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
          <span className="ctl-mark is-absent" aria-label="This step has no agent yet, so it has no attempts to step through.">
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
            {/* ◀ POSITION ▶ IS ONE UNIT THAT NEVER WRAPS, as the same-step
                stepper's is (V200, #1427): at 400 `run 3 of 3 · 1 of 3
                counted` filled the line and ▶ wrapped alone onto the next.
                The counted figure follows the unit and wraps on its own. */}
            <span className="wf-scrub-nav">
              <ScrubButton label="Previous attempt" glyph="◀" onPress={prevAttempt} />
              {/* ONE COUNT, NAMED FOR WHAT IT COUNTS (live QA WF-6, 2026-10-11).
                  The stepper walks the step's attempt DOCUMENTS; the table's
                  Attempts is `attempt_count`, which a merge step's CI park
                  refunds (agent_worker control.py). A merge that parked for CI
                  four times read `attempt 5 of 5` here and `1 of 10` in the
                  table, on one screen. Where the two differ the stepper says
                  `run`, and the counted figure beside it is the table's. */}
              {counted ? (
                <span className="wf-scrub-pos" title={countedTitle}>
                  run {at + 1} of {ready.length}
                </span>
              ) : (
                // 'OF N' IS ATTEMPTS MADE, AND SAYS SO (visual QA pass 1,
                // overview-agents-74): the table's '1 of 3' is attempts used of
                // those allowed, and 'attempt 1 of 1' beside it read as the
                // same ratio.
                <span className="wf-scrub-pos" title={`Attempt ${at + 1} of the ${ready.length} this step has made. The table’s Attempts is attempts used of those allowed.`}>
                  attempt {at + 1} of {ready.length} made
                </span>
              )}
              <ScrubButton label="Next attempt" glyph="▶" onPress={nextAttempt} />
            </span>
            {counted && (
              <span className="wf-scrub-counted" title={countedTitle}>
                {row.attempts.text} counted
              </span>
            )}
            <FactList
              facts={attemptFacts(
                current,
                attemptPhase(taskState, at === ready.length - 1),
                now,
                // THE RESULT BELONGS TO THE ATTEMPT THAT WROTE IT: the newest
                // attempt of a finished task (WF-5). Any other attempt keeps
                // its own absences.
                at === ready.length - 1 && taskState !== null && TERMINAL_STATES.has(taskState) ? row.result : null,
                // ONE DURATION FOR ONE RUN (QA G3-26): the newest attempt of a
                // finished task is the run the Graph and the Table time.
                at === ready.length - 1 && taskState !== null && TERMINAL_STATES.has(taskState) ? row.times.ranMs : null,
              )}
              // THE STEP'S REASON IS THE NEWEST ATTEMPT'S (visual QA pass 1,
              // overview-agents-68): stepping back to attempt 2 kept attempt
              // 3's 'exit status 2' under Why it failed. An older attempt
              // keeps its own error, and the card says whose reason that is.
              cause={newest && row.why !== null && row.why.kind === 'cause' ? row.why.full : null}
            />
          </>
        )}
      </div>

      <div
        className="wf-scrub"
        role="group"
        aria-label="The same step in other workflows of the same shape on this board, newest first"
        aria-keyshortcuts="ArrowLeft ArrowRight"
        data-scrub="workflow"
        onKeyDown={scrubKeys(hasNewer ? () => onSibling(-1) : null, hasOlder ? () => onSibling(1) : null)}
      >
        {/* WHAT IT COMPARES, IN WORDS (visual QA run 2 WG-6, run 5b L4):
            `same step` over `workflow 1 of 1` did not say it walks this step
            across the other workflows of this shape. */}
        <span className="ctl-eyebrow wf-scrub-key">same step in other workflows</span>
        {/* ◀ POSITION ▶ ARE ONE UNIT THAT NEVER WRAPS (browser QA D30,
            2026-10-04): the shape sat between the position and ▶, and a long
            shape pushed the arrow onto a line of its own. The shape follows
            the unit and is cut, whole in its title. */}
        {/* NOTHING TO PAGE, NO PAGER (visual QA pass 1, overview-agents-67):
            'no other workflow of this shape' sat between two disabled arrows
            over a lone strip mark. One sentence, the shape in its title. */}
        {siblings.length <= 1 ? (
          <span className="wf-scrub-pos" title={`This workflow’s shape: ${shapeWords(shape)}. Only workflows of this shape are compared.`}>
            No other workflow of this shape on this board
          </span>
        ) : (
        <>
        <span className="wf-scrub-nav">
          <ScrubButton
            buttonRef={newerRef}
            label="Same step, newer workflow"
            glyph="◀"
            onPress={hasNewer ? () => onSibling(-1) : null}
          />
          {/* A LIST OF ONE SAYS SO: `workflow 1 of 1` was a position with
              nowhere to go, the arrows both at their ends. */}
          <span className="wf-scrub-pos">{`workflow ${siblingIndex + 1} of ${siblings.length}`}</span>
          <ScrubButton
            buttonRef={olderRef}
            label="Same step, older workflow"
            glyph="▶"
            onPress={hasOlder ? () => onSibling(1) : null}
          />
        </span>
        {/* THE SHAPE IN THE HEAD'S WORDS (visual QA run 2, WG-6): it printed
            `5 → 1 · 6 steps`, the bare widths PR 1719 had explained in the
            page head (QA-119) and left unexplained here. */}
        <span className="wf-scrub-shape" title={`Only workflows of this shape are compared. ${shape.label}`}>
          shape: <ShapeWords words={shapeWords(shape)} />
        </span>
        {/* THE STRIP: every occurrence, in scrub order, in its own state's
            silhouette, with this one ringed. Decoration for a sighted reader
            -- the position above is the fact, and each workflow's state is one
            press away -- so it is hidden from assistive technology rather than
            announced as a list of unlabelled dots. */}
        {strip.from > 0 && (
          <span className="wf-scrub-more" aria-hidden>
            +{strip.from}
          </span>
        )}
        <ol className="wf-scrub-strip" aria-hidden>
          {siblings.slice(strip.from, strip.to).map((s, k) => (
            <li
              key={s.workflowId}
              className={strip.from + k === siblingIndex ? 'is-here' : undefined}
              title={`${s.workflowId}: ${s.word}`}
            >
              <WfStepMark look={s} />
            </li>
          ))}
        </ol>
        {strip.to < siblings.length && (
          <span className="wf-scrub-more" aria-hidden>
            +{siblings.length - strip.to}
          </span>
        )}
        </>
        )}
        {/* THE ID ONCE ON A WORKFLOW'S PAGE (VQ-015 re-review): the page
            head's Copy id carries it, so this row does not render it again.
            The position names the workflow and the strip's titles name each
            one. A card with no such head keeps it here. */}
        {idInHead ? null : <Id title={workflowId}>{workflowId}</Id>}
      </div>
    </section>
  )
}

/** `abc1234`, with the whole sha as its title. */
function Sha({ sha }: { sha: string }) {
  return (
    <span className="mono" title={sha}>
      {sha.length > 12 ? sha.slice(0, 7) : sha}
    </span>
  )
}

/** What a CI park waits on, by the park's code (`agent_worker.merge._with_forge`). */
function waitsOn(code: string | null, pending: readonly string[]): string {
  if (pending.length > 0) return `pending: ${pending.join(', ')}`
  if (code === 'mergeability_unknown') return 'GitHub has not computed mergeability yet'
  if (code === 'no_checks') return 'no check has reported yet; it waits for a first check'
  return 'the pull request\'s checks'
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`

/**
 * THE MERGE STEP'S CARD (docs/merge-step.md "Revised 2026-10-06" §6 MS4), on
 * the run page's Steps card and in the workflow page's step inspector: the
 * state -- waiting for CI with the pending checks and the head, behind and
 * updated (n of 12), merged with the commit and the issues closed, or refused
 * with the code and the reason -- the pull request, and when the step first
 * parked for CI. Every fact is `mergeCardOf`'s; this draws them.
 *
 * A CI_PENDING park is "waiting for CI". It holds no lease, no pool count and
 * no Job (invariant 1), and swarm-api's tick or the fallback instant wakes it,
 * so it is never worded as stalled or blocked.
 */
export function MergeStepCard({ card, now }: { card: MergeCard; now: number }) {
  const s = card.state
  let head: string
  let lines: ReactNode[] = []
  switch (s.kind) {
    case 'waiting':
      head = 'waiting for CI'
      lines = [
        <>{waitsOn(s.code, s.pending)}{s.head !== null && <> · at <Sha sha={s.head} /></>}</>,
        'Holds no capacity. Woken when the checks settle.',
      ]
      break
    case 'updated':
      head =
        s.updates === null
          ? `behind, updated (count not recorded) · at most ${MERGE_MAX_HEAD_UPDATES}`
          : `behind, updated ${s.updates} of ${MERGE_MAX_HEAD_UPDATES}`
      lines = [
        <>GitHub merged the base into the branch; the checks run again{s.head !== null && <> at <Sha sha={s.head} /></>}</>,
        'Holds no capacity. Woken when the checks settle.',
      ]
      break
    case 'merged':
      head = 'merged'
      lines = [
        <>
          {s.byThisTask === true
            ? 'by this step'
            : s.already
              ? 'already merged at the pinned head, not by this step'
              : s.byThisTask === false
                ? 'not by this step'
                : 'whether this step merged was not recorded'}
          {s.commit !== null && <> · commit <Sha sha={s.commit} /></>}
          {s.updates !== null && s.updates > 0 && <> · after {plural(s.updates, 'branch update')}</>}
        </>,
        <>
          {s.closed.length === 0 ? 'no issue closed' : <>closed {s.closed.map((n) => `#${n}`).join(', ')}</>}
          {s.notClosed > 0 && <> · {plural(s.notClosed, 'issue')} not closed</>}
          {s.beyondPage && <> · more references than one page: the rest were not closed</>}
        </>,
      ]
      break
    case 'refused':
      head = s.failed ? 'merge failed' : 'refused'
      lines = [
        <>
          {s.code !== null && <span className="mono">{s.code}</span>}
          {s.code !== null && s.reason !== null && ': '}
          {s.reason ?? (s.code === null ? 'no reason recorded' : null)}
        </>,
      ]
      break
    case 'not-yet':
      head = `${TERMINAL_STATES.has(s.state) ? 'not merged' : 'not merged yet'} · ${s.state.toLowerCase().replace(/_/g, ' ')}`
      break
  }
  const kind = s.kind
  const pr = card.pullRequest
  return (
    <section className="wf-merge" aria-label="Merge" data-merge={kind}>
      <div className="wf-card-h">
        <b>Merge</b>
        <span className={`wf-merge-out${kind === 'refused' ? ' is-bad' : ''}`}>{head}</span>
        <span className="wf-card-r">
          {pr === null ? (
            'pull request not recorded'
          ) : pr.href !== null ? (
            <a className="ctl-link" data-fact="pull-request" href={pr.href} target="_blank" rel="noopener noreferrer">
              #{pr.number}
            </a>
          ) : (
            <span data-fact="pull-request">#{pr.number}</span>
          )}
        </span>
      </div>
      {lines.map((line, i) => (
        <p key={i} className="wf-merge-head">
          {line}
        </p>
      ))}
      {/* THE CI CLOCK'S START, NOT THE FIRST PARK (live QA WF-5, 2026-10-11).
          `first_parked_at` is carried from park to park only while the step
          waits on CI; a wait for the merge slot clears it (agent_worker
          control.py, the CI park), so the next CI wait starts it afresh. A
          step that first parked at 10:35 read "first parked for CI 17m ago"
          off a clock restarted at 11:08. The words say what the field is. */}
      {card.firstParkedAt !== null && (
        <p className="wf-card-foot" data-fact="first-parked">
          current CI wait began{' '}
          <time
            dateTime={card.firstParkedAt}
            title={`${card.firstParkedAt}. The CI clock restarts after a wait for the merge slot, so an earlier park can predate it; the step's events list every park.`}
          >
            {timeAgo(card.firstParkedAt, now)}
          </time>
        </p>
      )}
    </section>
  )
}
