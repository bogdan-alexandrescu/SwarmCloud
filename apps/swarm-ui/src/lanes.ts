/**
 * THE TIMELINE'S LANES, AS DATA (timeline.html pick A, owner 2026-10-02).
 *
 * One lane per agent over wall-clock time. What each mark means is the
 * point of the page, and it is CONTRACT invariant 1 drawn:
 *
 *   hold   an attempt, from its `created_at` (the lease was taken) to its
 *          `completed_at`. The only thing on the page that held capacity.
 *   cut    an attempt whose generation was FENCED (invariant 5): exit code 70
 *          (`agent_worker.errors.ExitCode.GENERATION_FENCED`), or a
 *          `generation_fenced` event naming its generation as the expected
 *          one. Drawn apart from a hold and labelled "gen N fenced".
 *   park   from a `parked` event to the next attempt (or to now, while the
 *          task is still PARKED). Holds nothing.
 *   wait   the task's whole life, as a thin line under everything else.
 *          Queued and ready hold nothing either.
 *
 * Marks: a cancel REQUEST is a mark, never an end (`cancel_requested`, and
 * the legacy flag-only `cancelled` read through `eventKind`); a terminal task
 * ends in its state's mark.
 *
 * HONESTY. Bars come from `/v1/attempts`; parks, fences named only by an
 * event, and cancel marks need the lane's events page. `events === null`
 * means that page was NOT read, and the lane says so rather than drawing a
 * lane that looks as if nothing happened. An attempt with no `completed_at`
 * whose task has moved on is drawn to where the next thing began, with
 * `endKnown: false`: its end was never written (a killed worker), and the
 * page must not imply it was.
 *
 * Pure: no React, no reads, so every rule above is tested without a DOM.
 */
import { eventKind } from './events'
import type { AttemptRow, Task, TaskEvent, TaskState } from './types'
import { CONCURRENCY_STATES, TERMINAL_STATES } from './types'

// ---------------------------------------------------------------------------
// The view, which is the address
// ---------------------------------------------------------------------------

export const LANE_SPANS = ['24h', '7d', '14d', '30d', '90d'] as const
export type LaneSpan = (typeof LANE_SPANS)[number]
export const LANE_KINDS = ['all', 'standalone', 'steps'] as const
export type LaneKind = (typeof LANE_KINDS)[number]
export const LANE_GROUPS = ['workflow', 'profile', 'none'] as const
export type LaneGroup = (typeof LANE_GROUPS)[number]

export const ALL_STATES: readonly TaskState[] = [
  'SUBMITTED', 'QUEUED', 'PARKED', 'READY', 'LEASED', 'DISPATCHED',
  'STARTING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED',
]

export interface LanesView {
  /** null while a since/until range (a zoom) is set. */
  span: LaneSpan | null
  /** ISO instants; `until` exclusive, as `/v1/attempts` reads it. */
  since: string | null
  until: string | null
  /** The view a zoom came from, as its own query, for the back chip. */
  back: string | null
  state: TaskState[]
  profile: string[]
  /** One workflow id, or null for any. */
  wf: string | null
  kind: LaneKind
  by: LaneGroup
  /** The open lane's task id. */
  lane: string | null
}

/**
 * 24h BY DEFAULT, not the ledger's 14d: a lane is readable over a day, and
 * past 7 days the bars are slivers -- which is why the page suggests
 * Outcomes there instead.
 */
export const DEFAULT_LANES_VIEW: LanesView = {
  span: '24h',
  since: null,
  until: null,
  back: null,
  state: [],
  profile: [],
  wf: null,
  kind: 'all',
  by: 'workflow',
  lane: null,
}

const isSpan = (s: string | null): s is LaneSpan => (LANE_SPANS as readonly string[]).includes(s ?? '')
const isKind = (s: string | null): s is LaneKind => (LANE_KINDS as readonly string[]).includes(s ?? '')
const isGroup = (s: string | null): s is LaneGroup => (LANE_GROUPS as readonly string[]).includes(s ?? '')
const isState = (s: string): s is TaskState => (ALL_STATES as readonly string[]).includes(s)
const uniq = (xs: readonly string[]): string[] => Array.from(new Set(xs)).sort()

export function parseLanesView(query: string | null | undefined): LanesView {
  const q = new URLSearchParams((query ?? '').replace(/^\?/, ''))
  const since = q.get('since')
  const until = q.get('until')
  const range =
    since !== null && until !== null && Number.isFinite(Date.parse(since)) && Number.isFinite(Date.parse(until)) && Date.parse(since) < Date.parse(until)
  const span = q.get('span')
  const kind = q.get('kind')
  const by = q.get('by')
  const wf = q.get('wf')
  const lane = q.get('lane')
  return {
    span: range ? null : isSpan(span) ? span : DEFAULT_LANES_VIEW.span,
    since: range ? since : null,
    until: range ? until : null,
    back: q.get('back'),
    state: uniq(q.getAll('state').filter(isState)) as TaskState[],
    profile: uniq(q.getAll('profile').filter((p) => p !== '')),
    wf: wf === null || wf === '' ? null : wf,
    kind: isKind(kind) ? kind : 'all',
    by: isGroup(by) ? by : 'workflow',
    lane: lane === null || lane === '' ? null : lane,
  }
}

/** Canonical order, defaults left out: the default page is plain `/timeline`. */
export function serializeLanesView(v: LanesView): string {
  const q = new URLSearchParams()
  if (v.since !== null && v.until !== null) {
    q.set('since', v.since)
    q.set('until', v.until)
  } else if (v.span !== null && v.span !== DEFAULT_LANES_VIEW.span) {
    q.set('span', v.span)
  }
  if (v.back !== null) q.set('back', v.back)
  for (const s of v.state) q.append('state', s)
  for (const p of v.profile) q.append('profile', p)
  if (v.wf !== null) q.set('wf', v.wf)
  if (v.kind !== 'all') q.set('kind', v.kind)
  if (v.by !== 'workflow') q.set('by', v.by)
  if (v.lane !== null) q.set('lane', v.lane)
  return q.toString()
}

/**
 * The keys only the Outcomes page writes. A link copied from the old
 * Timeline (`/timeline?span=7d&table=1`) carries them; Lanes points it at
 * the page it was made on rather than silently dropping half of it.
 */
const LEDGER_ONLY_KEYS = ['bucket', 'scope', 'tenant', 'exclude_tenant', 'submitted_by', 'group', 'table'] as const

export function madeOnOutcomes(query: string | null | undefined): boolean {
  const q = new URLSearchParams((query ?? '').replace(/^\?/, ''))
  return LEDGER_ONLY_KEYS.some((k) => q.has(k))
}

const DAY_MS = 86_400_000
const spanMs = (s: LaneSpan): number => (s === '24h' ? DAY_MS : Number(s.replace('d', '')) * DAY_MS)

/** The window the view draws, in epoch ms: `until` exclusive. */
export function lanesWindow(v: LanesView, now: number): { since: number; until: number } {
  if (v.since !== null && v.until !== null) return { since: Date.parse(v.since), until: Date.parse(v.until) }
  return { since: now - spanMs(v.span ?? '24h'), until: now }
}

export function spanDays(v: LanesView, now: number): number {
  const w = lanesWindow(v, now)
  return (w.until - w.since) / DAY_MS
}

/** A zoom to [since, until), keeping every filter, with the chip back to `v`. */
export function zoomedTo(v: LanesView, since: number, until: number): LanesView {
  return {
    ...v,
    span: null,
    since: new Date(since).toISOString(),
    until: new Date(until).toISOString(),
    back: serializeLanesView({ ...v, back: null }),
  }
}

/** The ledger view for the same window: the strip's "Outcomes for this span" link. */
export function outcomesQueryFor(v: LanesView): string {
  const q = new URLSearchParams()
  if (v.since !== null && v.until !== null) {
    q.set('since', v.since)
    q.set('until', v.until)
  } else {
    q.set('span', v.span ?? '24h')
  }
  for (const p of v.profile) q.append('profile', p)
  if (v.kind !== 'all') q.set('kind', v.kind)
  return q.toString()
}

// ---------------------------------------------------------------------------
// One lane
// ---------------------------------------------------------------------------

/** `agent_worker.errors.ExitCode.GENERATION_FENCED`: the worker found its generation stale. */
export const FENCED_EXIT_CODE = 70

export type SegKind = 'hold' | 'cut' | 'park' | 'wait'

export interface LaneSeg {
  kind: SegKind
  from: number
  to: number
  gen: number | null
  /** Runs on past `to` (an attempt still holding, drawn to now). */
  open: boolean
  /** False when the end was never written and `to` is where the next thing began. */
  endKnown: boolean
  /** A park's expected resume, past now: dotted, not yet happened. */
  future: boolean
  label: string | null
  attemptId: string | null
  /** A park's reason, as the event or the task wrote it. */
  reason: string | null
}

export type LaneMarkKind = 'cancel_requested' | 'succeeded' | 'failed' | 'dead_lettered' | 'cancelled'

export interface LaneMark {
  kind: LaneMarkKind
  at: number
  gen: number | null
}

export interface Lane {
  taskId: string
  /** null when the task document was not read: the lane is labelled by its id. */
  task: Task | null
  /** Oldest first. */
  attempts: AttemptRow[]
  segs: LaneSeg[]
  marks: LaneMark[]
  /** Whether this lane's events page was read. */
  eventsRead: boolean
  /** A workflow step with no attempt, filled from the workflow read. */
  neverRan: boolean
  /** The earliest moment the lane draws, for ordering. */
  start: number
}

const TERMINAL_MARK: Partial<Record<TaskState, LaneMarkKind>> = {
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
  DEAD_LETTERED: 'dead_lettered',
  CANCELLED: 'cancelled',
}

/** The events after a park that say it was over. */
const PARK_ENDS: ReadonlySet<string> = new Set([
  'ready', 'queued', 'lease_acquired', 'dispatched', 'starting', 'running',
  'succeeded', 'failed', 'cancelled', 'dead_lettered',
])

const ms = (s: string | null | undefined): number | null => {
  if (s === null || s === undefined) return null
  const t = Date.parse(s)
  return Number.isFinite(t) ? t : null
}

function seg(kind: SegKind, from: number, to: number, extra: Partial<LaneSeg> = {}): LaneSeg {
  return { kind, from, to, gen: null, open: false, endKnown: true, future: false, label: null, attemptId: null, reason: null, ...extra }
}

/** The generations a `generation_fenced` event names as stale. */
function fencedByEvent(events: readonly TaskEvent[]): Set<number> {
  const out = new Set<number>()
  for (const e of events) {
    if (eventKind(e) !== 'generation_fenced') continue
    const g = e.detail?.['expected_generation']
    if (typeof g === 'number') out.add(g)
  }
  return out
}

export function buildLane(
  taskId: string,
  task: Task | null,
  attempts: readonly AttemptRow[],
  events: readonly TaskEvent[] | null,
  now: number,
): Lane {
  const sorted = [...attempts]
    .filter((a) => ms(a.created_at) !== null)
    .sort((a, b) => ms(a.created_at)! - ms(b.created_at)!)
  const evs = [...(events ?? [])]
    .filter((e) => ms(e.at) !== null)
    .sort((a, b) => ms(a.at)! - ms(b.at)!)
  const fenced = fencedByEvent(evs)
  for (const a of sorted) if (a.exit_code === FENCED_EXIT_CODE) fenced.add(a.generation)
  const live = task !== null && CONCURRENCY_STATES.has(task.state)
  const terminal = task !== null && TERMINAL_STATES.has(task.state)

  const bars: LaneSeg[] = sorted.map((a, i) => {
    const from = ms(a.created_at)!
    const next = sorted[i + 1]
    const done = ms(a.completed_at)
    const current = live && next === undefined && task.current_generation === a.generation
    let to: number
    let open = false
    let endKnown = true
    if (done !== null) {
      to = done
    } else if (current) {
      to = now
      open = true
    } else {
      // NO END WAS WRITTEN and the task moved on: drawn to where the next
      // thing began, flagged, never closed with a confident edge.
      endKnown = false
      to = (next ? ms(next.created_at) : null) ?? ms(task?.completed_at) ?? ms(task?.updated_at) ?? from
    }
    const g = a.generation
    const isFenced = fenced.has(g)
    return seg(isFenced ? 'cut' : 'hold', from, Math.max(from, to), {
      gen: g,
      open,
      endKnown,
      label: isFenced ? `gen ${g} fenced` : `gen ${g}`,
      attemptId: a.attempt_id,
    })
  })

  const parks: LaneSeg[] = []
  for (const p of evs.filter((e) => eventKind(e) === 'parked')) {
    const from = ms(p.at)!
    const reasonRaw = p.detail?.['reason'] ?? p.detail?.['park_reason'] ?? task?.park_reason ?? null
    const reason = typeof reasonRaw === 'string' ? reasonRaw : null
    const nextAttempt = sorted.find((a) => ms(a.created_at)! > from)
    const nextEnd = evs.find((e) => ms(e.at)! > from && PARK_ENDS.has(eventKind(e)))
    const ends = [nextAttempt ? ms(nextAttempt.created_at)! : null, nextEnd ? ms(nextEnd.at)! : null].filter(
      (x): x is number => x !== null,
    )
    if (ends.length > 0) {
      parks.push(seg('park', from, Math.min(...ends), { reason, gen: p.generation }))
    } else if (task?.state === 'PARKED') {
      parks.push(seg('park', from, now, { reason, gen: p.generation, open: true }))
      const resume = ms(task.next_eligible_at)
      if (resume !== null && resume > now) parks.push(seg('park', now, resume, { reason, future: true }))
    } else {
      // Over, by the task's state, but nothing read says when.
      parks.push(seg('park', from, ms(task?.completed_at) ?? ms(task?.updated_at) ?? now, { reason, gen: p.generation, endKnown: false }))
    }
  }

  const firstDrawn = sorted[0] ? ms(sorted[0].created_at)! : null
  const born = ms(task?.created_at) ?? firstDrawn
  const lastBar = bars.length > 0 ? Math.max(...bars.map((b) => b.to)) : null
  const ended = terminal ? ms(task.completed_at) ?? ms(task.updated_at) ?? lastBar : null
  const waits: LaneSeg[] = []
  if (born !== null) {
    const to = task === null ? lastBar ?? born : terminal ? ended ?? born : now
    if (to > born) waits.push(seg('wait', born, to))
  }

  const marks: LaneMark[] = []
  for (const e of evs) {
    if (eventKind(e) === 'cancel_requested') marks.push({ kind: 'cancel_requested', at: ms(e.at)!, gen: e.generation })
  }
  const endMark = task !== null ? TERMINAL_MARK[task.state] : undefined
  if (endMark !== undefined && ended !== null) {
    marks.push({ kind: endMark, at: ended, gen: task?.current_generation ?? null })
  } else if (task === null) {
    // Task not read: its end is still known from a terminal event, if one was read.
    const last = [...evs].reverse().find((e) => ['succeeded', 'failed', 'dead_lettered', 'cancelled'].includes(eventKind(e)))
    if (last !== undefined) marks.push({ kind: eventKind(last) as LaneMarkKind, at: ms(last.at)!, gen: last.generation })
  }

  return {
    taskId,
    task,
    attempts: sorted,
    segs: [...waits, ...parks, ...bars],
    marks,
    eventsRead: events !== null,
    neverRan: sorted.length === 0,
    start: firstDrawn ?? born ?? now,
  }
}

// ---------------------------------------------------------------------------
// Filters and grouping, in the browser, over the rows read
// ---------------------------------------------------------------------------

/**
 * `/v1/attempts` cannot filter by state, profile or workflow, so these run
 * here over the pages read -- and the page says so. A lane whose task was
 * not read cannot be placed under any of them, so a set filter leaves it out.
 */
export function filterLanes(lanes: readonly Lane[], v: LanesView): Lane[] {
  return lanes.filter((l) => {
    const t = l.task
    if (v.state.length > 0 && (t === null || !v.state.includes(t.state))) return false
    if (v.profile.length > 0 && (t === null || !v.profile.includes(t.runner_profile))) return false
    if (v.wf !== null && (t === null || t.workflow_id !== v.wf)) return false
    if (v.kind === 'standalone' && (t === null || t.workflow_id !== null)) return false
    if (v.kind === 'steps' && (t === null || t.workflow_id === null)) return false
    return true
  })
}

export function filtersSet(v: LanesView): number {
  return (v.state.length > 0 ? 1 : 0) + (v.profile.length > 0 ? 1 : 0) + (v.wf !== null ? 1 : 0) + (v.kind !== 'all' ? 1 : 0)
}

export interface LaneBlock {
  key: string
  kind: 'workflow' | 'standalone' | 'profile' | 'unread' | 'all'
  lanes: Lane[]
}

const byStart = (a: Lane, b: Lane): number =>
  a.neverRan === b.neverRan ? a.start - b.start || a.taskId.localeCompare(b.taskId) : a.neverRan ? 1 : -1

/** Workflows first (by when they began), standalone agents last; or by profile; or one block. */
export function groupLanes(lanes: readonly Lane[], by: LaneGroup): LaneBlock[] {
  if (by === 'none') return lanes.length === 0 ? [] : [{ key: 'all', kind: 'all', lanes: [...lanes].sort(byStart) }]
  const blocks = new Map<string, LaneBlock>()
  const unread: Lane[] = []
  const standalone: Lane[] = []
  for (const l of lanes) {
    if (l.task === null) {
      unread.push(l)
      continue
    }
    const key = by === 'workflow' ? l.task.workflow_id : l.task.runner_profile
    if (key === null) {
      standalone.push(l)
      continue
    }
    const b = blocks.get(key) ?? { key, kind: by === 'workflow' ? 'workflow' : 'profile', lanes: [] }
    b.lanes.push(l)
    blocks.set(key, b)
  }
  const out = [...blocks.values()].map((b) => ({ ...b, lanes: b.lanes.sort(byStart) }))
  if (by === 'workflow') {
    const first = (b: LaneBlock) => Math.min(...b.lanes.map((l) => l.start))
    out.sort((a, b) => first(a) - first(b) || a.key.localeCompare(b.key))
  } else {
    out.sort((a, b) => a.key.localeCompare(b.key))
  }
  if (standalone.length > 0) out.push({ key: 'standalone', kind: 'standalone', lanes: standalone.sort(byStart) })
  if (unread.length > 0) out.push({ key: 'unread', kind: 'unread', lanes: unread.sort(byStart) })
  return out
}

// ---------------------------------------------------------------------------
// The axis
// ---------------------------------------------------------------------------

const MIN = 60_000
const HOUR = 3_600_000
const STEPS = [15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY_MS, 2 * DAY_MS, 7 * DAY_MS, 14 * DAY_MS, 30 * DAY_MS]

/** Ticks on the viewer's clock, at most about seven across the window. */
export function axisTicks(since: number, until: number): number[] {
  const width = until - since
  if (!(width > 0)) return []
  const step = STEPS.find((s) => width / s <= 7) ?? STEPS[STEPS.length - 1]!
  const off = new Date(since).getTimezoneOffset() * MIN
  const first = Math.ceil((since - off) / step) * step + off
  const out: number[] = []
  for (let t = first; t < until; t += step) out.push(t)
  return out
}
