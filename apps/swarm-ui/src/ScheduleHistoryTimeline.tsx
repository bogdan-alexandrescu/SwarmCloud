/**
 * A SCHEDULE'S HISTORY AS A TIMELINE (#1109; schedules.html detail A's History
 * pane, BGD 2026-10-25 "build it now"). One lane per firing, a bar per task
 * each of its work items ran, over the time since that firing: solid while an
 * attempt held capacity, hatched while the work waited holding nothing, a
 * fenced generation drawn cut. The table it replaced (S7) stays behind the
 * Timeline / Table toggle, and the choice is remembered on this device.
 *
 * THE BARS ARE THE TIMELINE PAGE'S (TimelineLanes.tsx): `lanes.buildLane`
 * turns a task and its attempts into the same segments, `Seg` draws them with
 * the same `.tl-seg` looks, so a parked, held or fenced bar reads the same on
 * both pages. What differs is the clock: each lane runs from its own firing,
 * so firings days apart line up and their durations compare.
 *
 * ONE READ PER PAGE OF FIRINGS (the mock-up's risk note): the firings and
 * their `work[]` arrive with the schedule; `GET /v1/schedules/{id}/spans`
 * reads the bars of the page on screen, `SPAN_PAGE` firings, never every
 * firing ever. A firing with no work asks for nothing.
 *
 * HONEST GAPS (redesign-v2.md): an item or task whose attempts were not read
 * draws a dash with the reason the route gave, and a failed read draws a dash
 * on every lane of the page with the failure -- never an empty lane, which
 * would read as "ran nothing".
 *
 * THE LIST UNDER THE LANES (SCHED-HISTORY-FOLLOWUP, part of #1109): a row per
 * work item of the page, in words, as the mock-up's History frame lists them
 * -- an issue run by its issue's number and title, the link to the run, its
 * state, and its cost as `/spans` summed it from the attempts' recorded cost.
 * A cost nobody recorded is a dash with the route's reason, never $0.
 */
import { useEffect, useId, useState, type ReactNode } from 'react'

import type { FiringWork, ScheduleFiring } from './api'
import { Button, Segmented } from './components'
import { read, route, type ApiError, type Result } from './fetch'
import { buildLane, type LaneSeg } from './lanes'
import { usd } from './measure'
import { Mark } from './primitives'
import { InApp, SpendFigure, localTime } from './Schedules'
import { timeAgo } from './Shell'
import { Seg, gridlineImage } from './TimelineLanes'
import type { AttemptRow, Task } from './types'
import { useNow } from './useNow'
import './styles/timeline.css'

// ---------------------------------------------------------------------------
// The read
// ---------------------------------------------------------------------------

/** Firings per page and per read: `routes/schedules.py::SPAN_FIRINGS`. */
export const SPAN_PAGE = 10

/** One attempt as `/spans` serves it: its span, generation and exit. */
export interface SpanAttempt {
  attempt_id: string
  task_id: string
  generation: number
  created_at: string
  completed_at: string | null
  exit_code: number | null
}

/** One task a work item ran. `attempts` null: not read, `attempts_unread` says why. */
export interface SpanTask {
  task_id: string
  step_id?: string | null
  label: string | null
  state?: Task['state']
  created_at?: string
  updated_at?: string
  completed_at?: string | null
  current_generation?: number
  park_reason?: string | null
  next_eligible_at?: string | null
  attempts: SpanAttempt[] | null
  attempts_truncated?: boolean
  attempts_unread: string | null
}

/** An issue run's issue, from its run record. `title` null: the run's read of the issue failed. */
export interface SpanIssue {
  number: number
  ref: string
  url: string
  title: string | null
}

/**
 * An item's cost, summed from its attempts' recorded `cost_usd` the way the
 * tick sums a firing's. `reported_usd` null: nothing was recorded, and
 * `unread` says why. Beside a figure, `unread` says what it does not cover.
 */
export interface SpanCost {
  reported_usd: number | null
  unreported_attempts: number
  unread: string | null
}

/** One work item of a firing: its tasks, or why none could be drawn. */
export interface SpanItem {
  kind: FiringWork['kind'] | string
  id: string
  /** The issue run's or workflow's own state, when it has one. */
  state: string | null
  updated_at: string | null
  /** An issue run's issue; null for every other kind, or a run not found. */
  issue?: SpanIssue | null
  tasks: SpanTask[]
  unread: string | null
  cost?: SpanCost | null
}

export interface FiringSpans {
  firings: { firing_id: string; items: SpanItem[] }[]
  not_found: string[]
}

/** `GET /v1/schedules/{id}/spans?firing_id=…`: one page of firings' bars. */
export async function loadFiringSpans(scheduleId: string, firingIds: readonly string[]): Promise<Result<FiringSpans>> {
  const q = new URLSearchParams(firingIds.map((id) => ['firing_id', id]))
  return read(route('/v1/schedules/{id}/spans', { id: scheduleId }, q), () => false)
}

// ---------------------------------------------------------------------------
// The remembered view
// ---------------------------------------------------------------------------

export type HistoryView = 'timeline' | 'table'

/**
 * THE CHOICE IS THIS DEVICE'S, AND THE PAGE IS RIGHT WITHOUT IT: storage
 * throws in a private window and in previews, and the tab then opens on the
 * timeline, the decided default.
 */
export const HISTORY_VIEW_KEY = 'swarm.schedule.history.view'

export function rememberedHistoryView(): HistoryView {
  try {
    return window.localStorage.getItem(HISTORY_VIEW_KEY) === 'table' ? 'table' : 'timeline'
  } catch {
    return 'timeline'
  }
}

function rememberHistoryView(v: HistoryView): void {
  try {
    window.localStorage.setItem(HISTORY_VIEW_KEY, v)
  } catch {
    // Nothing to do: this visit still shows the choice.
  }
}

/** The History tab: the toggle, then the timeline or the table. */
export function HistoryPane({ timeline, table }: { timeline: ReactNode; table: ReactNode }) {
  const [view, setView] = useState<HistoryView>(rememberedHistoryView)
  return (
    <>
      <div className="sht-bar">
        {/* THE PANE IS HEADED (schedules.html detail A, "Firings, last 7 days";
            VQ-028). Not "last 7 days": the pane pages through every firing. */}
        <h3 className="sc-h3 sht-h">Firings, newest first</h3>
        <Segmented<HistoryView>
          small
          label="Draw the firings as"
          value={view}
          onChange={(v) => {
            setView(v)
            rememberHistoryView(v)
          }}
          options={[
            { key: 'timeline', label: 'Timeline' },
            { key: 'table', label: 'Table' },
          ]}
        />
      </div>
      {view === 'timeline' ? timeline : table}
    </>
  )
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

const ms = (s: string | null | undefined): number | null => {
  if (s === null || s === undefined) return null
  const t = Date.parse(s)
  return Number.isFinite(t) ? t : null
}

/** An issue run waiting on a person or a check, holding nothing: drawn hatched. */
const RUN_WAITING: Record<string, string> = {
  PLANNED: 'plan waiting for approval',
  CHECKING: 'checking CI on its pull request',
}

/** The steps the axis may take, in ms: the first that gives at most five ticks. */
const STEPS = [5, 10, 15, 30, 60, 120, 240, 360, 720, 1440, 2880, 10080].map((m) => m * 60_000)

/** How long after its firing, as the axis says it: "+0", "+45m", "+2h", "+1d". */
export function offsetWords(d: number): string {
  if (d <= 0) return '+0'
  const m = Math.round(d / 60_000)
  if (m < 60) return `+${m}m`
  if (m < 1440) return m % 60 === 0 ? `+${m / 60}h` : `+${Math.floor(m / 60)}h${m % 60}m`
  const days = m / 1440
  return Number.isInteger(days) ? `+${days}d` : `+${days.toFixed(1)}d`
}

/** The page's scale: the longest firing on it, at least a quarter hour, rounded to a step. */
export function pageScale(longest: number): { span: number; step: number } {
  const want = Math.max(longest, 15 * 60_000)
  const step = STEPS.find((s) => want / s <= 5) ?? STEPS[STEPS.length - 1]!
  return { span: Math.ceil(want / step) * step, step }
}

/** One drawn row: a task's bars, or a dash and why. */
interface Row {
  key: string
  label: string
  /** Where its label links: the task's page, or the item's. */
  to: string | null
  sub: string
  segs: LaneSeg[]
  multi: boolean
  /** A dash with this reason instead of bars. */
  gap: string | null
  /** A measured nothing ("made no work"), not a gap. */
  none: string | null
  /** False for the item's own wait: `Seg` would print "parked" in it. */
  words?: boolean
}

/** Where one piece of a firing's work is read: Work's own page for it. */
export function workAddress(w: { kind: string; id: string }): string | null {
  if (w.kind === 'issue_run') return `work/runs?${new URLSearchParams({ run: w.id }).toString()}`
  if (w.kind === 'task') return `work/task/${encodeURIComponent(w.id)}`
  if (w.kind === 'workflow') return `work/workflows?${new URLSearchParams({ wf: w.id }).toString()}`
  // An api_action (a label, a comment) has no page of its own here.
  return null
}

const KIND_WORD: Record<string, string> = { issue_run: 'issue run', task: 'task', workflow: 'workflow', api_action: 'API action' }

/**
 * The runner profile `/spans` labels a task by when it has no step
 * (`_span_task`: `step_label or step_id or runner_profile`), or null for a
 * step. A profile says where the task ran, never what it was (VQ-028).
 */
function profileOf(t: SpanTask): string | null {
  return t.step_id === null || t.step_id === undefined ? t.label : null
}

/**
 * WHAT A WORK ITEM IS, IN WORDS (schedules.html detail A; VQ-028): an issue
 * run by its issue, anything else by what the firing's type made -- "docs-drift
 * task", "issue-sweep workflow". Every row was its runner profile
 * ("claude-code"), which named no firing apart from another.
 */
export function itemName(it: Pick<SpanItem, 'kind' | 'id' | 'issue'>, type: string): string {
  if (it.kind === 'issue_run' && it.issue) return it.issue.title === null ? `#${it.issue.number}` : `#${it.issue.number} ${it.issue.title}`
  if (it.kind === 'task' || it.kind === 'workflow') return `${type} ${it.kind}`
  return KIND_WORD[it.kind] ?? it.kind
}

function attemptsWord(n: number, truncated: boolean): string {
  if (n === 0) return 'no attempt yet'
  return `${truncated ? 'last ' : ''}${n} ${n === 1 ? 'attempt' : 'attempts'}`
}

/** A task's lane, as the Timeline page builds it, from what `/spans` served. */
function taskSegs(t: SpanTask, now: number): LaneSeg[] {
  // `buildLane` reads only the task fields `/spans` serves (its state, its
  // generation, its times and its park) and the attempts' spans: the rest of
  // a `Task` and an `AttemptRow` is never touched, so the narrower shapes
  // stand in for them.
  const lane = buildLane(t.task_id, t as unknown as Task, (t.attempts ?? []) as unknown as AttemptRow[], null, now)
  return lane.segs
}

function rowsOf(f: ScheduleFiring, items: SpanItem[] | null, gap: string | null, now: number): Row[] {
  if (f.work.length === 0) {
    return [{ key: 'none', label: '', to: null, sub: '', segs: [], multi: false, gap: null, none: 'this firing made no work' }]
  }
  if (items === null) {
    return f.work.map((w) => ({
      key: `${w.kind}:${w.id}`, label: w.id, to: workAddress(w), sub: KIND_WORD[w.kind] ?? w.kind,
      segs: [], multi: false, gap, none: null,
    }))
  }
  const rows: Row[] = []
  for (const it of items) {
    const kind = KIND_WORD[it.kind] ?? it.kind
    const itemAt = workAddress(it)
    const name = itemName(it, f.type)
    // An issue run's lane rows say its number; the whole title is the list's.
    const short = it.kind === 'issue_run' && it.issue ? `#${it.issue.number}` : it.id
    if (it.tasks.length === 0) {
      rows.push({ key: `${it.kind}:${it.id}`, label: it.kind === 'issue_run' && it.issue ? name : it.id, to: itemAt, sub: kind, segs: [], multi: false, gap: it.unread ?? 'nothing was served for it', none: null })
      continue
    }
    for (const t of it.tasks) {
      // A task item is named by what it is, its profile said beside it; a
      // step of a run or a workflow by its step, its item beside it.
      const profile = profileOf(t)
      const label = it.kind === 'task' ? name : t.label ?? t.task_id
      const beside = it.kind === 'task' ? (profile === null ? t.label : `on ${profile}`) : short
      const sub = [beside, t.state?.toLowerCase().replace(/_/g, ' '), t.attempts === null ? null : attemptsWord(t.attempts.length, t.attempts_truncated === true)]
        .filter((x): x is string => x !== null && x !== undefined)
        .join(' · ')
      rows.push({
        key: `${it.kind}:${it.id}:${t.task_id}`,
        label,
        to: `work/task/${encodeURIComponent(t.task_id)}`,
        sub,
        segs: t.attempts === null ? [] : taskSegs(t, now),
        multi: (t.attempts?.length ?? 0) > 1,
        gap: t.attempts === null ? t.attempts_unread ?? 'its attempts were not read' : null,
        none: null,
      })
    }
    // AN ISSUE RUN WAITING ON A PERSON OR A CHECK holds nothing, and its
    // tasks have ended: the wait is the item's, drawn hatched from when the
    // run last moved to now (the mock-up's "merge waiting for approval").
    const waiting = it.kind === 'issue_run' && it.state !== null ? RUN_WAITING[it.state] : undefined
    const since = ms(it.updated_at)
    if (waiting !== undefined && since !== null && since < now) {
      rows.push({
        key: `${it.kind}:${it.id}:waiting`,
        label: waiting,
        to: itemAt,
        sub: `${short} · holds nothing`,
        segs: [{ kind: 'park', from: since, to: now, gen: null, open: true, endKnown: true, future: false, label: null, attemptId: null, reason: waiting, current: false }],
        multi: false,
        gap: null,
        none: null,
        words: false,
      })
    }
    if (it.unread !== null) {
      rows.push({ key: `${it.kind}:${it.id}:rest`, label: it.id, to: itemAt, sub: kind, segs: [], multi: false, gap: it.unread, none: null })
    }
  }
  return rows
}

// ---------------------------------------------------------------------------
// The list under the lanes
// ---------------------------------------------------------------------------

/** An issue run's state as the list says it (`issueruns.RunState`). */
const RUN_STATE_WORDS: Record<string, string> = {
  PLANNING: 'planning',
  PLANNED: 'plan waiting for approval',
  APPROVED: 'plan approved',
  RUNNING: 'workflow running',
  CHECKING: 'checking CI on its pull request',
  FIXING: 'fixing CI',
  DONE: 'done',
  FAILED: 'failed',
  REJECTED: 'plan rejected',
  CANCELLED: 'cancelled',
  NOT_READY: 'not ready',
}

/** What the row's link opens, as the mock-up says it: "→ run run_8f2a". */
const LINK_WORD: Record<string, string> = { issue_run: 'run', task: 'task', workflow: 'workflow' }

const plainState = (s: string): string => s.toLowerCase().replace(/_/g, ' ')

/** The item's state in words: the run's or workflow's own, a task's, or an action's outcome. */
function itemState(it: SpanItem, w: FiringWork | undefined): string | null {
  if (it.kind === 'issue_run' && it.state !== null) return RUN_STATE_WORDS[it.state] ?? plainState(it.state)
  if (it.state !== null) return plainState(it.state)
  if (it.kind === 'task' && it.tasks[0]?.state) return plainState(it.tasks[0].state)
  if (it.kind === 'api_action') return w?.outcome ? `ended ${plainState(w.outcome)}` : 'ended when it was recorded'
  return null
}

/** What the item is, in words: its issue, or what the firing's type made, and where it ran. */
function itemWhat(it: SpanItem, type: string): ReactNode {
  if (it.kind === 'issue_run' && it.issue) {
    return it.issue.title === null ? (
      <>
        <b>#{it.issue.number}</b> <i className="ctl-em">title not read</i>
      </>
    ) : (
      <b>{`#${it.issue.number} ${it.issue.title}`}</b>
    )
  }
  if (it.kind === 'task') {
    const t = it.tasks[0]
    const profile = t === undefined ? null : profileOf(t)
    const step = t === undefined || profile !== null ? null : t.label
    return (
      <>
        <b>{itemName(it, type)}</b>
        {step !== null && ` ${step}`}
        {profile !== null && ` on ${profile}`}
      </>
    )
  }
  if (it.kind === 'workflow') {
    return (
      <>
        <b>{itemName(it, type)}</b>
        {` of ${it.tasks.length} ${it.tasks.length === 1 ? 'step' : 'steps'}`}
      </>
    )
  }
  return (
    <>
      <b>{KIND_WORD[it.kind] ?? it.kind}</b> <span className="mono">{it.id}</span>
    </>
  )
}

/** A cost never recorded: a dash whose reason is its title (schedules.html's `.dash`). The words stay in
 *  the text, read aloud, and schedules.css shows them under the item on a phone, where there is no hover. */
function CostDash({ why }: { why: string }) {
  return (
    <i className="ctl-em sht-dash" title={why}>
      &mdash;<span className="sht-why"> {why}</span>
    </i>
  )
}

/** The item's cost: the figure, a floor marked partial, or a dash with its reason -- never $0 for nothing recorded. */
function ItemCost({ cost }: { cost: SpanCost | null | undefined }) {
  if (cost === null || cost === undefined) return <CostDash why="its cost was not served" />
  if (cost.reported_usd === null) return <CostDash why={cost.unread ?? 'no attempt recorded a cost'} />
  if (cost.unread !== null) {
    return (
      <span className="sc-spend">
        <Mark kind="partial" say={`${usd(cost.reported_usd)} is a floor: ${cost.unread}`} /> <span className="sc-usd">{usd(cost.reported_usd)}</span> · {cost.unread}
      </span>
    )
  }
  return <SpendFigure usdReported={cost.reported_usd} unreported={cost.unreported_attempts} what="this item" />
}

/** The mock-up's list: a row per work item of the page's firings, newest firing first. */
function ItemList({
  lanes,
  now,
  go,
}: {
  lanes: readonly { f: ScheduleFiring; items: SpanItem[] | null }[]
  now: number
  go: (to: string) => void
}) {
  const headId = useId()
  const rows = lanes.flatMap(({ f, items }) => (items ?? []).map((it) => ({ f, it })))
  if (rows.length === 0) return null
  return (
    <>
      {/* THE LIST IS HEADED AND SET APART (VQ-028): it began 10px under the
          last lane, unnamed, and read as one more lane. */}
      <h3 className="sc-h3 sht-list-h" id={headId}>
        What these firings made
      </h3>
      <ul className="sc-audit sht-list" data-testid="sc-history-items" aria-labelledby={headId}>
      {rows.map(({ f, it }) => {
        const when = f.fired_at ?? f.slot
        const to = workAddress(it)
        const state = itemState(it, f.work.find((w) => w.kind === it.kind && w.id === it.id))
        return (
          <li key={`${f.firing_id}:${it.kind}:${it.id}`} data-work={`${it.kind}:${it.id}`}>
            <span className="sht-words">
              <span className="sc-who">
                {when === null ? 'time not recorded' : <span title={localTime(when)}>{timeAgo(when, now)}</span>}
              </span>
              {' · '}
              {itemWhat(it, f.type)}
              {to !== null && (
                <>
                  {' → '}
                  {LINK_WORD[it.kind] ?? it.kind}{' '}
                  <InApp go={go} to={to} className="mono">
                    {it.id}
                  </InApp>
                </>
              )}
              {state !== null && ` · ${state}`}
            </span>
            <span className="sc-sub sht-cost" data-cost="">
              <ItemCost cost={it.cost} />
            </span>
          </li>
        )
      })}
      </ul>
    </>
  )
}

function firingStart(f: ScheduleFiring): number | null {
  return ms(f.fired_at) ?? ms(f.slot)
}

type SpansRead = { status: 'loading' } | { status: 'ok'; data: FiringSpans } | { status: 'error'; error: ApiError }

function failureWords(e: ApiError): string {
  return `its work was not read: ${e.message || e.kind.replace(/_/g, ' ')}`
}

/**
 * The timeline. `outcome` draws a firing's outcome words exactly as the
 * table's Outcome cell does (ScheduleDetail.tsx), so the two views never
 * disagree about what a firing came to.
 */
export function HistoryTimeline({
  scheduleId,
  firings,
  go,
  outcome,
}: {
  scheduleId: string
  firings: readonly ScheduleFiring[]
  go: (to: string) => void
  outcome: (f: ScheduleFiring) => ReactNode
}) {
  const now = useNow()
  const [page, setPage] = useState(0)
  const pages = Math.max(1, Math.ceil(firings.length / SPAN_PAGE))
  const at = Math.min(page, pages - 1)
  const shown = firings.slice(at * SPAN_PAGE, at * SPAN_PAGE + SPAN_PAGE)
  const asked = shown.filter((f) => f.work.length > 0).map((f) => f.firing_id)
  const askedKey = asked.join('|')
  const [spans, setSpans] = useState<SpansRead>({ status: 'loading' })
  const [round, setRound] = useState(0)

  useEffect(() => {
    if (asked.length === 0) {
      setSpans({ status: 'ok', data: { firings: [], not_found: [] } })
      return
    }
    let live = true
    setSpans({ status: 'loading' })
    void loadFiringSpans(scheduleId, asked).then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') setSpans({ status: 'ok', data: r.data })
      else if (r.status === 'error') setSpans({ status: 'error', error: r.error })
      else setSpans({ status: 'ok', data: { firings: [], not_found: asked } })
    })
    return () => {
      live = false
    }
    // `askedKey` is `asked`, as a value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scheduleId, askedKey, round])

  const byId = new Map(spans.status === 'ok' ? spans.data.firings.map((x) => [x.firing_id, x.items]) : [])
  const lanes = shown.map((f) => {
    const items = byId.get(f.firing_id) ?? null
    const gap =
      spans.status === 'error'
        ? failureWords(spans.error)
        : spans.status === 'ok' && items === null
          ? 'its work was not served: the firing was not found'
          : null
    return { f, items, start: firingStart(f), rows: spans.status === 'loading' && f.work.length > 0 ? null : rowsOf(f, items, gap, now) }
  })
  // THE PAGE'S SCALE is its longest firing, by what has ENDED: the firing's
  // own end, and the end of every bar that closed. Something still open --
  // a step parked for days, a run waiting for approval -- runs on to now
  // and off the right edge, open-ended (the dashed `is-open` end), rather
  // than stretching the scale until every other bar is a sliver (rendered
  // review, 2026-10-25: one 4-day park made the page +5d wide).
  let longest = 0
  for (const l of lanes) {
    if (l.start === null) continue
    const ends = [ms(l.f.ended_at)]
    // The wait line is left out: on a task not yet ended `buildLane` runs it
    // from submission to now, and an ended task's line ends where its bars do.
    for (const r of l.rows ?? []) for (const s of r.segs) if (s.kind !== 'wait' && !s.open && !s.future && !s.current) ends.push(s.to)
    for (const r of l.rows ?? []) for (const s of r.segs) if (s.open || s.current) ends.push(s.from)
    for (const e of ends) if (e !== null) longest = Math.max(longest, e - l.start)
  }
  const { span, step } = pageScale(longest)
  const ticks = Array.from({ length: Math.floor(span / step) + 1 }, (_, i) => ({ at: i * step, pct: ((i * step) / span) * 100 }))
  const grid = gridlineImage(ticks.slice(1, -1))

  return (
    <div className="sht" data-testid="sc-history-timeline">
      <p className="tl-legend sht-legend">
        <span>
          <i className="tl-lg is-hold" /> an attempt held capacity
        </span>
        <span>
          <i className="tl-lg is-park" /> waiting (parked, or for approval): holds nothing
        </span>
        <span>
          <i className="tl-lg is-wait" /> queued or ready: holds nothing
        </span>
        <span>
          <i className="tl-lg is-cut" /> fenced generation
        </span>
      </p>
      <div className="tl sht-axis" aria-hidden="true">
        <div className="tl-row">
          <div className="tl-lab">
            <small>time since each firing</small>
          </div>
          <div className="tl-trk">
            {ticks.map((t, i) => (
              <span key={t.at} className={i === ticks.length - 1 ? 'tl-tk sht-tk-end' : 'tl-tk'} style={i === ticks.length - 1 ? undefined : { left: `${t.pct}%` }}>
                {offsetWords(t.at)}
              </span>
            ))}
          </div>
        </div>
      </div>
      <ol className="sht-lanes">
        {lanes.map(({ f, start, rows }) => {
          const when = f.fired_at ?? f.slot
          return (
            <li key={f.firing_id} className="sht-firing" data-firing-lane={f.firing_id}>
              <div className="sht-head">
                <b className="sht-when">
                  {when === null ? <i className="ctl-em">&mdash; time not recorded</i> : <span title={localTime(when)}>{timeAgo(when, now)}</span>}
                </b>
                <span className="sht-trigger">{f.trigger.replace(/_/g, ' ')}</span>
                <span className="sht-out">{outcome(f)}</span>
                {f.work.length > 0 && (
                  <span className="sht-count">{`${f.work.length} work ${f.work.length === 1 ? 'item' : 'items'}`}</span>
                )}
              </div>
              <div className="tl sht-tl" style={{ ['--tl-grid' as string]: grid }}>
                {rows === null ? (
                  <div className="tl-row">
                    <div className="tl-lab">
                      <small>reading its work</small>
                    </div>
                    <div className="tl-trk">
                      <div className="tl-skel is-short sht-skel" />
                    </div>
                  </div>
                ) : (
                  rows.map((r) =>
                    r.none !== null ? (
                      <p key={r.key} className="sht-none" data-item={r.key}>
                        {r.none}
                      </p>
                    ) : (
                    <div key={r.key} className="tl-row" data-item={r.key} data-gap={r.gap !== null ? '' : undefined}>
                      <div className="tl-lab">
                        {r.label !== '' && (
                          <b>
                            {r.to === null ? <span className="tl-name mono">{r.label}</span> : <InApp go={go} to={r.to} className="tl-name">{r.label}</InApp>}
                          </b>
                        )}
                        {r.sub !== '' && <small title={r.sub}>{r.sub}</small>}
                      </div>
                      <div className="tl-trk">
                        {r.gap !== null ? (
                          <span className="sht-gap" data-unread={r.gap}>
                            <i className="ctl-em">&mdash; {r.gap}</i>
                          </span>
                        ) : start === null ? (
                          <span className="sht-gap" data-unread="no time">
                            <i className="ctl-em">&mdash; the firing&apos;s time was not recorded, so its bars have no origin</i>
                          </span>
                        ) : (
                          r.segs.map((s, i) => (
                            <Seg key={`${s.kind}:${s.attemptId ?? i}`} s={s} since={start} until={start + span} wide={false} multi={r.multi} words={r.words !== false} />
                          ))
                        )}
                      </div>
                    </div>
                    ),
                  )
                )}
              </div>
            </li>
          )
        })}
      </ol>
      <ItemList lanes={lanes} now={now} go={go} />
      {spans.status === 'error' && (
        <p className="sht-retry">
          <Button size="sm" onClick={() => setRound((n) => n + 1)}>
            Read the bars again
          </Button>
        </p>
      )}
      {pages > 1 && (
        <nav className="sht-pager" aria-label="Pages of firings">
          <Button size="sm" disabled={at === 0} onClick={() => setPage(at - 1)}>
            ‹ Newer
          </Button>
          <span>
            firings {at * SPAN_PAGE + 1}&ndash;{at * SPAN_PAGE + shown.length} of {firings.length}
          </span>
          <Button size="sm" disabled={at >= pages - 1} onClick={() => setPage(at + 1)}>
            Older ›
          </Button>
        </nav>
      )}
    </div>
  )
}
