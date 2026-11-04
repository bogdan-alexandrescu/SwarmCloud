/**
 * THE STAGE GRID: a workflow's steps as a row per stage and a column per
 * feature, the Work redesign's workflow graph (the owner's pick, "Graph with a
 * docked inspector", 2026-11-03).
 *
 * Each step is a chip: its state mark, its time, its pull request number and a
 * "?" when its agent asks the owner a question. Each stage row carries its own
 * tally. A 40-step workflow reads as 4 by 10 with nothing drawn over anything
 * else, so the page can keep it on the left while the inspector on the right
 * changes subject.
 *
 * WHERE A STEP GOES IS THE WORKFLOW'S OWN DATA. The row is the step's stage,
 * `levelsOf` (dag.ts) over its `depends_on`, the same level the console's
 * graph and Table read. The column is the feature: a step whose name is
 * `<stage>-<feature>` sits under that feature only when its dependencies agree
 * (it depends on the step above it in that column); when they say otherwise,
 * the dependency wins and the step sits under the step it depends on. The name
 * is a label, never the authority.
 *
 * NOTHING IS COUNTED BY HAND AND NOTHING UNKNOWN READS AS 0. A step whose task
 * was not in the read is drawn "not known" and counted as that, never as
 * waiting or done (stepState's rule, types.ts).
 *
 * AS THE GRID NARROWS it drops the pull request number, then the times (both
 * stay in the tooltip), then shows the state marks only with the column labels
 * turned vertical. The width is measured, not guessed from the viewport: the
 * same grid sits beside a wide inspector at 1440 and alone at 1280.
 *
 * Mounted by the Work pages; it owns no route, no fetch and no selection
 * state of its own. The page says which step is selected and which to keep
 * bright, and hears which step was picked and which stage's count was opened.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'

import { PARK_WORD } from '../components'
import { expiresAtOf, expiresWords, isWaitingForMember } from '../awaitInput'
import { SKIPPED_WORD, levelsOf } from '../dag'
import { NamedMark, STATE_MARK, StateMark } from '../marks'
import { ownerQuestionsHold } from '../Handoff'
import { questionCount } from '../QuestionsCard'
import { spanLabel } from '../stepviews'
import {
  CONCURRENCY_STATES,
  stepState,
  TERMINAL_STATES,
  type ParkReason,
  type ResultSummary,
  type StepState,
  type Task,
  type Workflow,
  type WorkflowStep,
} from '../types'
import { useNow } from '../useNow'
import { skippedByVerdict } from '../wfreview'
import './stage-grid.css'

// ---------------------------------------------------------------------------
// Placement
// ---------------------------------------------------------------------------

export interface GridColumn {
  /** Unique within the grid. */
  readonly key: string
  /** The feature's name, or the step id that opened the column. */
  readonly label: string
}

export interface GridStage {
  readonly index: number
  /** The stage's name when every step in it is `<name>-…`, else `Stage n`. */
  readonly label: string
  /** One entry per column: the step id there, or null. */
  readonly cells: readonly (string | null)[]
  /** Every step of the stage, in the workflow's order. */
  readonly stepIds: readonly string[]
}

export interface StageGridShape {
  readonly columns: readonly GridColumn[]
  readonly stages: readonly GridStage[]
  /** step id -> its row and column. */
  readonly at: ReadonlyMap<string, { readonly stage: number; readonly column: number }>
}

/**
 * The stage's name from its steps' names: a prefix, ending before a `-`, that
 * every step of the stage shares. A name may hold a `-` itself (`re-review`),
 * so the candidates are tried in this order: the longest that leaves every step
 * a feature the grid already has a column for; else, for a stage of two or
 * more steps, the longest they share; else the first word. Null when the steps
 * share none.
 */
function stageNameOf(steps: readonly WorkflowStep[], known: ReadonlySet<string>): string | null {
  const firstId = steps[0]?.step_id
  if (firstId === undefined) return null
  const candidates: string[] = []
  for (let i = firstId.indexOf('-'); i > 0; i = firstId.indexOf('-', i + 1)) {
    const p = firstId.slice(0, i)
    if (steps.every((s) => s.step_id.startsWith(`${p}-`) && s.step_id.length > p.length + 1)) candidates.push(p)
  }
  if (candidates.length === 0) return null
  const fits = candidates.filter((p) => steps.every((s) => known.has(s.step_id.slice(p.length + 1))))
  if (fits.length > 0) return fits[fits.length - 1]!
  return steps.length > 1 ? candidates[candidates.length - 1]! : candidates[0]!
}

/**
 * The grid of a workflow's steps. Rows: `levelsOf`. Columns: in four passes
 * per stage, so a name can never overrule the dependency data.
 *
 *  1. A step named `<stage>-<feature>` takes the `<feature>` column when the
 *     data agrees: it has no dependencies, or it depends on the step already
 *     in that column.
 *  2. Otherwise it takes the column of the step it depends on (the nearest
 *     stage first), when that step has no other dependant in this stage --
 *     a fan-out does not pile onto its parent's column.
 *  3. Anything left opens a column of its own, named by its feature or its id.
 */
export function stageGridOf(steps: readonly WorkflowStep[]): StageGridShape {
  const levels = levelsOf(steps)
  const columns: { key: string; label: string }[] = []
  const byKey = new Map<string, number>()
  const at = new Map<string, { stage: number; column: number }>()
  const stages: GridStage[] = []

  const open = (label: string): number => {
    let key = label
    for (let n = 2; byKey.has(key); n += 1) key = `${label}#${n}`
    columns.push({ key, label })
    byKey.set(key, columns.length - 1)
    return columns.length - 1
  }

  levels.forEach((level, k) => {
    const name = stageNameOf(level, new Set(columns.map((c) => c.label)))
    const featureOf = (s: WorkflowStep): string | null => (name === null ? null : s.step_id.slice(name.length + 1))
    const taken = new Map<number, string>()
    const place = (s: WorkflowStep, c: number) => {
      taken.set(c, s.step_id)
      at.set(s.step_id, { stage: k, column: c })
    }
    const inLevel = new Set(level.map((s) => s.step_id))
    // How many steps of this stage depend on each step: a parent with two or more is a fan-out.
    const dependants = new Map<string, number>()
    for (const s of level) for (const p of new Set(s.depends_on)) dependants.set(p, (dependants.get(p) ?? 0) + 1)

    // 1. The name, where the data agrees.
    for (const s of level) {
      const f = featureOf(s)
      const c = f === null ? undefined : byKey.get(f)
      if (c === undefined || taken.has(c)) continue
      const agrees = s.depends_on.length === 0 || s.depends_on.some((p) => at.get(p)?.column === c)
      if (agrees) place(s, c)
    }
    // 2. The lineage: the column of the step it depends on, nearest stage first.
    for (const s of level) {
      if (at.has(s.step_id)) continue
      const parents = s.depends_on
        .filter((p) => !inLevel.has(p) && at.has(p) && dependants.get(p) === 1)
        .sort((a, b) => (at.get(b)?.stage ?? 0) - (at.get(a)?.stage ?? 0))
      const c = parents.map((p) => at.get(p)!.column).find((col) => !taken.has(col))
      if (c !== undefined) place(s, c)
    }
    // 3. A column of its own.
    for (const s of level) {
      if (at.has(s.step_id)) continue
      const f = featureOf(s)
      const free = f === null ? undefined : byKey.get(f)
      place(s, free !== undefined && !taken.has(free) && s.depends_on.length === 0 ? free : open(f ?? s.step_id))
    }

    stages.push({
      index: k,
      label: name ?? `Stage ${k + 1}`,
      cells: [],
      stepIds: level.map((s) => s.step_id),
    })
  })

  // Cells once every column exists, so every row is as wide as the grid.
  const full = stages.map((st) => {
    const cells: (string | null)[] = columns.map(() => null)
    for (const id of st.stepIds) {
      const p = at.get(id)
      if (p) cells[p.column] = id
    }
    return { ...st, cells }
  })
  return { columns, stages: full, at }
}

/**
 * The step a stage above (`-1`) or below (`+1`) the given one: the same column
 * when that stage has a step there, else the nearest column that has one.
 * Null at the first or last stage. The page's inspector walks with this too.
 */
export function walkStage(shape: StageGridShape, stepId: string, dir: -1 | 1): string | null {
  const here = shape.at.get(stepId)
  if (!here) return null
  for (let k = here.stage + dir; k >= 0 && k < shape.stages.length; k += dir) {
    const cells = shape.stages[k]!.cells
    let best: string | null = null
    let bestD = Infinity
    cells.forEach((id, c) => {
      const d = Math.abs(c - here.column)
      if (id !== null && d < bestD) {
        best = id
        bestD = d
      }
    })
    if (best !== null) return best
  }
  return null
}

/** The next step left (`-1`) or right (`+1`) in the same stage, or null at its end. */
function walkRow(shape: StageGridShape, stepId: string, dir: -1 | 1): string | null {
  const here = shape.at.get(stepId)
  if (!here) return null
  const cells = shape.stages[here.stage]!.cells
  for (let c = here.column + dir; c >= 0 && c < cells.length; c += dir) {
    const id = cells[c]
    if (id) return id
  }
  return null
}

// ---------------------------------------------------------------------------
// One step's facts
// ---------------------------------------------------------------------------

/** How a chip is drawn and which tally counts it. */
export type ChipKind = 'done' | 'failed' | 'running' | 'waiting' | 'parked' | 'stopped' | 'unknown'

export function chipKindOf(state: StepState): ChipKind {
  if (state.kind === 'unstarted') return 'waiting'
  if (state.kind === 'unknown') return 'unknown'
  const s = state.state
  if (s === 'SUCCEEDED') return 'done'
  if (s === 'FAILED' || s === 'DEAD_LETTERED') return 'failed'
  if (s === 'CANCELLED') return 'stopped'
  if (CONCURRENCY_STATES.has(s)) return 'running'
  if (s === 'PARKED' && state.task.park_reason !== 'DEPENDENCY_INCOMPLETE') return 'parked'
  return 'waiting'
}

/**
 * The questions each step's agent asks its owner, by task id, from the two
 * records the console reads: the agent's own (its posted questions less those
 * answered, `questionCount`) and a merge of this workflow held by the owner's
 * questions (`ownerQuestionsHold`, #2313), which names each asking task.
 */
export function openQuestionsByTask(workflowId: string, taskById: ReadonlyMap<string, Task> | null): Map<string, number> {
  const out = new Map<string, number>()
  if (taskById === null) return out
  const held = new Map<string, Set<number>>()
  for (const t of taskById.values()) {
    if (t.workflow_id !== workflowId) continue
    const own = Math.max(0, questionCount(t) - (t.owner_answers?.length ?? 0))
    if (own > 0) out.set(t.id, own)
    for (const q of ownerQuestionsHold(t)?.questions ?? []) {
      const set = held.get(q.task_id) ?? new Set<number>()
      set.add(q.question_index)
      held.set(q.task_id, set)
    }
  }
  for (const [id, qs] of held) out.set(id, Math.max(out.get(id) ?? 0, qs.size))
  return out
}

/** The pull request the step's run opened, from its result (`result_summary.git.pull_request`). */
export function pullRequestOf(task: Task | null): number | null {
  const git = (task?.result_summary as ResultSummary | null | undefined)?.git
  const n = typeof git === 'object' && git !== null ? git.pull_request?.number : undefined
  return typeof n === 'number' ? n : null
}

const at = (v: string | null | undefined): number => (v ? Date.parse(v) : NaN)

/** A chip's time: how long it has run, or ran. Null when it never started or nothing was recorded. */
export function chipTimeOf(state: StepState, now: number): string | null {
  if (state.kind !== 'state') return null
  const t = state.task
  const started = at(t.started_at)
  if (!Number.isFinite(started)) return null
  const end = TERMINAL_STATES.has(t.state) ? at(t.completed_at) : CONCURRENCY_STATES.has(t.state) ? now : NaN
  if (!Number.isFinite(end)) return null
  const ms = Math.max(0, end - started)
  // Whole minutes past the first: a chip has room for `12m`, not `12m 30s`.
  return spanLabel(ms < 60_000 ? ms : Math.round(ms / 60_000) * 60_000)
}

/** Why a waiting step waits, in the console's words; null for a step that does not wait. */
function waitWordOf(step: WorkflowStep, state: StepState): string | null {
  if (state.kind === 'unstarted') {
    return step.depends_on.length > 0 ? `starts after ${step.depends_on.join(', ')}` : null
  }
  if (state.kind !== 'state') return null
  const t = state.task
  if (t.state === 'PARKED') {
    const r = t.park_reason
    if (!r) return 'parked, no reason recorded'
    // A reason this build does not know is printed verbatim (StatePill's rule).
    return Object.prototype.hasOwnProperty.call(PARK_WORD, r) ? `parked: ${PARK_WORD[r as ParkReason]}` : `parked: ${r}`
  }
  if (t.state === 'QUEUED') return 'in line'
  if (t.state === 'READY') return 'waiting for a slot'
  return null
}

function stateWordOf(state: StepState, kind: ChipKind): string {
  if (state.kind === 'unknown') return 'state not known: its agent was not in the read'
  if (state.kind === 'unstarted') return 'not started'
  if (kind === 'waiting' || kind === 'parked') return state.state.toLowerCase()
  return state.state.toLowerCase().replace('_', '-')
}

interface Chip {
  readonly step: WorkflowStep
  readonly state: StepState
  readonly kind: ChipKind
  readonly time: string | null
  readonly pr: number | null
  readonly asks: number
  /** Its verdict gate kept its agent from running (#264): SUCCEEDED, but never read as a step that ran. */
  readonly skipped: boolean
  /** The tooltip and the accessible name: everything the chip drops when narrow. */
  readonly tip: string
}

function chipOf(step: WorkflowStep, stage: string, taskById: ReadonlyMap<string, Task> | null, asking: ReadonlyMap<string, number>, now: number): Chip {
  const state = stepState(step, taskById)
  const kind = chipKindOf(state)
  const task = state.kind === 'state' ? state.task : null
  const skipped = task !== null && task.state === 'SUCCEEDED' && skippedByVerdict(task)
  const time = skipped ? null : chipTimeOf(state, now)
  const pr = pullRequestOf(task)
  const asks = step.task_id ? asking.get(step.task_id) ?? 0 : 0
  const why = waitWordOf(step, state)
  const parts = [
    `${step.step_id} (${stage}): ${skipped ? SKIPPED_WORD : stateWordOf(state, kind)}`,
    skipped ? 'agent not run' : null,
    why,
    // A step waiting for a member carries the deadline the scheduler wrote at the park (human-steps.md §4.5).
    task !== null && isWaitingForMember(task) ? expiresWords(expiresAtOf(task), now) : null,
    step.waiting?.summary ?? null,
    time === null ? null : TERMINAL_STATES.has(task!.state) ? `ran ${time}` : `running ${time}`,
    pr === null ? null : `pull request #${pr}`,
    asks > 0 ? `asks you ${asks === 1 ? 'a question' : `${asks} questions`}` : null,
  ]
  return { step, state, kind, time, pr, asks, skipped, tip: parts.filter((p): p is string => p !== null).join(' · ') }
}

// ---------------------------------------------------------------------------
// Tallies
// ---------------------------------------------------------------------------

export interface StageTally {
  readonly done: number
  readonly failed: number
  readonly running: number
  /** Not started, queued, ready, or parked for any reason. */
  readonly waiting: number
  readonly stopped: number
  /** Steps whose task was not in the read: the tally is short by exactly this many. */
  readonly unknown: number
}

export function tallyOf(kinds: readonly ChipKind[]): StageTally {
  const n = (k: ChipKind) => kinds.filter((x) => x === k).length
  return {
    done: n('done'),
    failed: n('failed'),
    running: n('running'),
    waiting: n('waiting') + n('parked'),
    stopped: n('stopped'),
    unknown: n('unknown'),
  }
}

function tallySentence(t: StageTally): string {
  const parts = [
    `${t.done} done`,
    `${t.failed} failed`,
    `${t.running} running`,
    `${t.waiting} waiting`,
    t.stopped > 0 ? `${t.stopped} cancelled` : null,
    t.unknown > 0 ? `${t.unknown} not known` : null,
  ]
  return parts.filter((p): p is string => p !== null).join(', ')
}

// ---------------------------------------------------------------------------
// Narrowing
// ---------------------------------------------------------------------------

/** What a chip still shows: everything, no PR number, no time, the mark alone. */
export type GridDensity = 'full' | 'no-pr' | 'no-time' | 'marks'

/** The width of the stage column, in px at the console's 14px type (6.5em, 5.5em when narrow). */
const STAGE_COL_PX = 91
const STAGE_COL_NARROW_PX = 77
const GAP_PX = 4

/**
 * The density for a grid `width` px wide with `columns` feature columns, from
 * the room one chip gets. A chip wraps to two lines, so a mark with `12m` over
 * `#2403` fits 56px; a mark and a time 44px; a mark alone needs 28px, and
 * under that the column labels turn vertical too. These are the picked
 * design's breakpoints (d.css: the PR number goes at a 700px main column, the
 * time at 560px, with ten features) as room per chip, so a grid of four
 * features keeps its times where one of ten cannot. Null width (not measured
 * yet) is `full`: the first paint is what a wide page shows.
 */
export function densityAt(width: number | null, columns: number): GridDensity {
  if (width === null || width <= 0 || columns <= 0) return 'full'
  const per = (stageCol: number) => (width - stageCol - GAP_PX * columns) / columns
  if (per(STAGE_COL_PX) >= 56) return 'full'
  if (per(STAGE_COL_NARROW_PX) >= 44) return 'no-pr'
  if (per(STAGE_COL_NARROW_PX) >= 28) return 'no-time'
  return 'marks'
}

function useWidth(): [(el: HTMLDivElement | null) => void, number | null] {
  const [el, setEl] = useState<HTMLDivElement | null>(null)
  const [width, setWidth] = useState<number | null>(null)
  useLayoutEffect(() => {
    if (el === null) return
    const w = el.getBoundingClientRect().width
    if (w > 0) setWidth(w)
  }, [el])
  useEffect(() => {
    if (el === null || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      const w = entries[entries.length - 1]?.contentRect.width
      if (typeof w === 'number' && w > 0) setWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [el])
  return [setEl, width]
}

// ---------------------------------------------------------------------------
// The component
// ---------------------------------------------------------------------------

export interface StageGridProps {
  readonly workflow: Workflow
  /** The step tasks the page read; null when it read none (every step then reads "not known"). */
  readonly taskById: ReadonlyMap<string, Task> | null
  /** The step open in the inspector. */
  readonly selected?: string | null
  /** A chip was clicked, or the keyboard walked to it: the page opens it in the inspector. */
  readonly onSelect: (stepId: string) => void
  /** A stage's tally was clicked: the page opens the Table view on that stage. Absent: the tally is plain text. */
  readonly onOpenStage?: (stage: { readonly index: number; readonly label: string; readonly stepIds: readonly string[] }) => void
  /** Steps to keep bright (the Needs-you row); every other chip is dimmed. Null or absent: none dimmed. */
  readonly bright?: ReadonlySet<string> | null
  /** The page's clock, so every chip on the page ages together; else the grid's own. */
  readonly now?: number
}

const MARK_OF: Readonly<Record<ChipKind, Parameters<typeof NamedMark>[0]['mark']>> = {
  done: STATE_MARK.SUCCEEDED.mark,
  failed: STATE_MARK.FAILED.mark,
  running: STATE_MARK.RUNNING.mark,
  waiting: STATE_MARK.QUEUED.mark,
  parked: STATE_MARK.PARKED.mark,
  stopped: STATE_MARK.CANCELLED.mark,
  unknown: 'warn',
}

function ChipMark({ chip }: { chip: Chip }) {
  if (chip.skipped) return <NamedMark mark="skipped" hue="neu" word={SKIPPED_WORD} bare />
  if (chip.state.kind === 'state') return <StateMark state={chip.state.task.state} bare />
  if (chip.state.kind === 'unknown') return <NamedMark mark="warn" hue="warn" word="not known" bare />
  return <NamedMark mark={MARK_OF.waiting} hue="neu" word="not started" bare />
}

export function StageGrid({ workflow, taskById, selected = null, onSelect, onOpenStage, bright = null, now: pageNow }: StageGridProps) {
  const ownNow = useNow()
  const now = pageNow ?? ownNow
  const shape = useMemo(() => stageGridOf(workflow.steps), [workflow.steps])
  const asking = useMemo(() => openQuestionsByTask(workflow.workflow_id, taskById), [workflow.workflow_id, taskById])
  const stepById = useMemo(() => new Map(workflow.steps.map((s) => [s.step_id, s])), [workflow.steps])
  const [measure, width] = useWidth()
  const density = densityAt(width, shape.columns.length)
  const chipRefs = useRef(new Map<string, HTMLButtonElement>())

  // The chip that takes the tab stop: the selected one, else the first.
  const first = shape.stages[0]?.cells.find((c): c is string => c !== null) ?? null
  const tabStop = selected !== null && shape.at.has(selected) ? selected : first

  if (workflow.steps.length === 0) {
    return <p className="sg-empty">This workflow has no steps.</p>
  }

  const go = (to: string | null) => {
    if (to === null) return
    onSelect(to)
    chipRefs.current.get(to)?.focus()
  }
  const onKey = (stepId: string) => (e: KeyboardEvent<HTMLButtonElement>) => {
    const to =
      e.key === 'ArrowUp' ? walkStage(shape, stepId, -1)
        : e.key === 'ArrowDown' ? walkStage(shape, stepId, 1)
          : e.key === 'ArrowLeft' ? walkRow(shape, stepId, -1)
            : e.key === 'ArrowRight' ? walkRow(shape, stepId, 1)
              : undefined
    if (to === undefined) return
    e.preventDefault()
    go(to)
  }

  return (
    <div className={`sg is-${density}${bright !== null ? ' is-focused' : ''}`} ref={measure} data-density={density}>
      <div className="sg-legend" aria-label="Legend">
        <span><StateMark state="SUCCEEDED" bare /> done</span>
        <span><StateMark state="RUNNING" bare /> running</span>
        <span><NamedMark mark={MARK_OF.waiting} hue="neu" word="waiting" bare /> waiting</span>
        <span><StateMark state="PARKED" bare /> parked</span>
        <span><StateMark state="FAILED" bare /> failed</span>
        <span><span className="sg-ask is-inline" aria-hidden="true">?</span> asks you</span>
      </div>
      <div
        className="sg-grid"
        role="group"
        aria-label={`${workflow.steps.length} steps: a row per stage, a column per feature`}
        style={{ ['--sg-cols' as string]: Math.max(1, shape.columns.length) }}
      >
        <div className="sg-row is-head" aria-hidden="true">
          <span className="sg-stage" />
          {shape.columns.map((c) => (
            <span key={c.key} className="sg-col" title={c.label}>{c.label}</span>
          ))}
        </div>
        {shape.stages.map((st) => {
          const chips = st.cells.map((id) => (id === null ? null : chipOf(stepById.get(id)!, st.label, taskById, asking, now)))
          const tally = tallyOf(chips.filter((c): c is Chip => c !== null).map((c) => c.kind))
          const sentence = tallySentence(tally)
          const counts = (
            <>
              {tally.done > 0 && <span className="sg-n is-done"><StateMark state="SUCCEEDED" bare />{tally.done}</span>}
              {tally.failed > 0 && <span className="sg-n is-failed"><StateMark state="FAILED" bare />{tally.failed}</span>}
              {tally.running > 0 && <span className="sg-n is-running"><StateMark state="RUNNING" bare />{tally.running}</span>}
              {tally.waiting > 0 && <span className="sg-n is-waiting"><NamedMark mark={MARK_OF.waiting} hue="neu" word="waiting" bare />{tally.waiting}</span>}
              {tally.stopped > 0 && <span className="sg-n is-stopped"><StateMark state="CANCELLED" bare />{tally.stopped}</span>}
              {tally.unknown > 0 && <span className="sg-n is-unknown">{tally.unknown} not known</span>}
            </>
          )
          return (
            <div key={st.index} className="sg-row" role="group" aria-label={`${st.label}: ${sentence}`} data-stage={st.label}>
              <span className="sg-stage">
                <b className="sg-stage-name" title={st.label}>{st.label}</b>
                {onOpenStage ? (
                  <button
                    type="button"
                    className="sg-tally"
                    title={`${sentence}. Open the Table on the ${st.label} stage`}
                    aria-label={`${st.label}: ${sentence}. Open the Table on this stage`}
                    onClick={() => onOpenStage({ index: st.index, label: st.label, stepIds: st.stepIds })}
                  >
                    {counts}
                  </button>
                ) : (
                  <span className="sg-tally" title={sentence}>{counts}</span>
                )}
              </span>
              {chips.map((chip, c) =>
                chip === null ? (
                  <span key={`empty-${c}`} className="sg-gap" />
                ) : (
                  <button
                    key={chip.step.step_id}
                    ref={(el) => {
                      if (el) chipRefs.current.set(chip.step.step_id, el)
                      else chipRefs.current.delete(chip.step.step_id)
                    }}
                    type="button"
                    className={`sg-chip is-${chip.kind}${chip.skipped ? ' is-skipped' : ''}${selected === chip.step.step_id ? ' is-selected' : ''}${bright !== null && !bright.has(chip.step.step_id) ? ' is-dimmed' : ''}`}
                    data-step={chip.step.step_id}
                    title={chip.tip}
                    aria-label={chip.tip}
                    aria-pressed={selected === chip.step.step_id}
                    tabIndex={chip.step.step_id === tabStop ? 0 : -1}
                    onClick={() => onSelect(chip.step.step_id)}
                    onKeyDown={onKey(chip.step.step_id)}
                  >
                    <span className="sg-chip-mark" aria-hidden="true"><ChipMark chip={chip} /></span>
                    {density !== 'marks' && density !== 'no-time' && chip.time !== null && (
                      <span className="sg-chip-time" aria-hidden="true">{chip.time}</span>
                    )}
                    {density !== 'marks' && density !== 'no-time' && chip.state.kind === 'unknown' && (
                      <span className="sg-chip-time" aria-hidden="true">not known</span>
                    )}
                    {density === 'full' && chip.pr !== null && <span className="sg-chip-pr" aria-hidden="true">#{chip.pr}</span>}
                    {chip.asks > 0 && <span className="sg-ask" aria-hidden="true">?</span>}
                  </button>
                ),
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
