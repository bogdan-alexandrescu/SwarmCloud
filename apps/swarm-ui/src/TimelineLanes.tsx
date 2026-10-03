/**
 * WORK › TIMELINE › LANES (timeline.html pick A, owner 2026-10-02).
 *
 * One lane per agent over wall-clock time, grouped under its workflow,
 * standalone agents last. The marks are `lanes.ts`'s; this file reads and
 * draws them. Its reads, each with its own honest state:
 *
 *   GET /v1/attempts             the bars. Newest first, paged; "Read the next
 *                                page" follows the token. How many pages are
 *                                left is a dash: the route returns a token,
 *                                never a total.
 *   GET /v1/tasks                names, profiles, states and workflows for the
 *                                lanes; a task not on that page is read on its
 *                                own (`loadTask`), up to MISSING_TASK_READS.
 *   GET /v1/workflows/{id}       each workflow drawn: its state, and the steps
 *                                that never had an attempt (drawn, no bar).
 *   GET /v1/tasks/{id}/events    ONE PAGE PER LANE IN VIEW, newest first
 *                                (`order=desc`, redesign-v2 S1's UI half): the
 *                                park, fence and cancel marks. The open lane's
 *                                list follows the page token for older events.
 *   GET /v1/outcomes             the one-line strip above the lanes, on its
 *                                own, so its failure leaves the lanes drawn.
 *
 * State, profile, workflow and kind filters run HERE, over the rows read,
 * because `/v1/attempts` takes none of them -- and the coverage line says so.
 *
 * Shared components (U0's, components.html A) are not on main yet, so the few
 * this page needs are local and prefixed `Tl` for a later pass to swap.
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'

import {
  loadAttemptsPage,
  loadOutcomes,
  loadTask,
  loadTaskEventsPage,
  loadTasks,
  loadWorkflow,
  type WorkflowRead,
} from './api'
import { agentName } from './agentlist'
import { eventKind } from './events'
import type { ApiError, Result } from './fetch'
import {
  ALL_STATES,
  LANE_GROUPS,
  LANE_KINDS,
  LANE_SPANS,
  axisTicks,
  buildLane,
  filterLanes,
  groupLanes,
  lanesWindow,
  madeOnOutcomes,
  outcomesQueryFor,
  parseLanesView,
  serializeLanesView,
  spanDays,
  zoomedTo,
  type Lane,
  type LaneBlock,
  type LaneMark,
  type LaneSeg,
  type LanesView,
} from './lanes'
import { STATE_MARK, type MarkName } from './marks'
import { Button, ButtonLink, MarkIcon, Segmented, StateMark } from './components'
import { DEFAULT_VIEW, outcomesQuery, viewerZone, type Outcomes } from './outcomes'
import { PageHead, timeAgo } from './Shell'
import type { AttemptRow, Task, TaskEvent, TaskState } from './types'
import { formatDuration, TERMINAL_STATES } from './types'
import { useInView } from './useInView'
import { AGE_TICK_MS, useNow } from './useNow'
import './styles/timeline.css'

/**
 * HOW MANY TASKS NOT ON THE TASK PAGE ARE READ ONE BY ONE. A 90-day span can
 * name hundreds; past this the lane is labelled by its id and says why, rather
 * than the page sending a read per lane before it draws anything.
 */
export const MISSING_TASK_READS = 40

/** Past this many days the bars are slivers, and the page suggests Outcomes. */
const SUGGEST_OUTCOMES_DAYS = 7

/** A drag shorter than this on the axis is a click, not a zoom. */
const DRAG_MIN_PX = 4

// ---------------------------------------------------------------------------
// The two pages
// ---------------------------------------------------------------------------

/** Lanes | Outcomes: the Timeline's two pages, one strip on each. */
export function TimelinePages({ at }: { at: 'lanes' | 'outcomes' }) {
  return (
    <nav className="tl-pages" aria-label="Timeline pages">
      <a href="/timeline" aria-current={at === 'lanes' ? 'page' : undefined}>
        Lanes
      </a>
      <a href="/timeline/outcomes" aria-current={at === 'outcomes' ? 'page' : undefined}>
        Outcomes
      </a>
    </nav>
  )
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

const CLOCK = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })
const CLOCK_S = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
const DAY = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' })

function clock(ms: number, wide = false): string {
  return wide ? `${DAY.format(ms)} ${CLOCK.format(ms)}` : CLOCK.format(ms)
}

function rangeWords(since: number, until: number): string {
  const wide = until - since > 36 * 3_600_000 || DAY.format(since) !== DAY.format(until)
  return `${clock(since, wide)}–${clock(until, wide)}`
}

function failureWords(e: ApiError): string {
  return e.httpStatus === null ? `did not answer (${e.message})` : `answered ${e.httpStatus}`
}

const MARK_OF: Record<LaneMark['kind'], MarkName | 'warn'> = {
  cancel_requested: 'warn',
  succeeded: 'succeeded',
  failed: 'failed',
  dead_lettered: 'dead',
  cancelled: 'cancelled',
}
const MARK_HUE: Record<LaneMark['kind'], string> = {
  cancel_requested: 'warn',
  succeeded: 'neu',
  failed: 'bad',
  dead_lettered: 'bad',
  cancelled: 'neu',
}
const MARK_WORD: Record<LaneMark['kind'], string> = {
  cancel_requested: 'cancel asked',
  succeeded: 'succeeded',
  failed: 'failed',
  dead_lettered: 'dead-lettered',
  cancelled: 'cancelled',
}

// ---------------------------------------------------------------------------
// Read state
// ---------------------------------------------------------------------------

type AttemptsRead =
  | { key: string; status: 'loading' }
  | { key: string; status: 'error'; error: ApiError; at: number }
  | { key: string; status: 'done'; rows: AttemptRow[]; next: string | null; pages: number; fetchedAt: number; more: 'idle' | 'reading' | ApiError }

type EventsRead =
  | { status: 'reading' }
  | { status: 'error'; httpStatus: number | null; message: string }
  | { status: 'ok'; events: TaskEvent[]; next: string | null; older: 'idle' | 'reading' | string }

type WorkflowState = { status: 'ok'; read: WorkflowRead } | { status: 'error'; why: string }

/**
 * How long the outcome strip waits for /v1/outcomes before it says so. The
 * ledger over a day is the heaviest read on the page; past this the strip
 * prints "no answer" rather than "Reading…" beside lanes already drawn, and
 * a late answer still replaces it.
 */
export const STRIP_DEADLINE_MS = 20_000

type StripRead = { status: 'loading' } | { status: 'ok'; data: Outcomes } | { status: 'error'; error: ApiError }

const isData = <T,>(r: Result<T>): r is Extract<Result<T>, { data: T }> => r.status === 'ok' || r.status === 'stale'

// ---------------------------------------------------------------------------
// The screen
// ---------------------------------------------------------------------------

export function TimelineLanesScreen({
  view: hashView = null,
  onView,
}: {
  /** The address's query, when it carries one. */
  view?: string | null
  /** Write a new view to the address. */
  onView?: (query: string) => void
} = {}) {
  const [own, setOwn] = useState<string>(hashView ?? '')
  const query = onView === undefined ? own : (hashView ?? '')
  const view = useMemo(() => parseLanesView(query), [query])
  const setView = useCallback(
    (next: LanesView) => {
      const q = serializeLanesView(next)
      setOwn(q)
      onView?.(q)
    },
    [onView],
  )
  const now = useNow(AGE_TICK_MS)

  // THE WINDOW IS ANCHORED when it is read: a span ends at the moment of the
  // read (or the refresh), not at every tick, so the bars and the axis agree.
  const [nonce, setNonce] = useState(0)
  const spanKey = view.since !== null ? `${view.since}|${view.until}` : `span:${view.span}`
  const [anchor, setAnchor] = useState(() => Date.now())
  const lastSpan = useRef(spanKey)
  if (lastSpan.current !== spanKey) {
    lastSpan.current = spanKey
    setAnchor(Date.now())
  }
  const win = useMemo(() => lanesWindow(view, anchor), [view, anchor])
  const sinceIso = new Date(win.since).toISOString()
  const untilIso = new Date(win.until).toISOString()
  const readKey = `${sinceIso}|${untilIso}|${nonce}`
  const refresh = () => {
    setAnchor(Date.now())
    setNonce((n) => n + 1)
  }

  // ---- the attempts ------------------------------------------------------
  const [att, setAtt] = useState<AttemptsRead>({ key: readKey, status: 'loading' })
  /** The last drawing, kept dimmed while a new span is read. */
  const [drawn, setDrawn] = useState<Extract<AttemptsRead, { status: 'done' }> | null>(null)
  useEffect(() => {
    let live = true
    setAtt({ key: readKey, status: 'loading' })
    loadAttemptsPage({ since: sinceIso, until: untilIso }).then((r) => {
      if (!live) return
      if (r.status === 'loading') return
      if (r.status === 'error') {
        setAtt({ key: readKey, status: 'error', error: r.error, at: Date.now() })
        return
      }
      const done = {
        key: readKey,
        status: 'done' as const,
        rows: r.status === 'empty' ? [] : r.data.attempts,
        next: r.status === 'empty' ? null : r.data.next_page_token,
        pages: 1,
        fetchedAt: r.fetchedAt,
        more: 'idle' as const,
      }
      setAtt(done)
      setDrawn(done)
    })
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readKey])

  const readNextPage = () => {
    if (att.status !== 'done' || att.next === null || att.more === 'reading') return
    const base = att
    setAtt({ ...base, more: 'reading' })
    loadAttemptsPage({ since: sinceIso, until: untilIso, pageToken: base.next }).then((r) => {
      setAtt((cur) => {
        if (cur.key !== base.key || cur.status !== 'done') return cur
        if (r.status === 'error') return { ...cur, more: r.error }
        if (r.status === 'loading') return cur
        const rows = r.status === 'empty' ? [] : r.data.attempts
        const next = { ...cur, rows: [...cur.rows, ...rows], next: r.status === 'empty' ? null : r.data.next_page_token, pages: cur.pages + 1, more: 'idle' as const }
        setDrawn(next)
        return next
      })
    })
  }

  const shown = att.status === 'done' ? att : drawn
  const rereading = att.status === 'loading' && drawn !== null
  const rows = useMemo(() => shown?.rows ?? [], [shown])

  // ---- the tasks behind the lanes ----------------------------------------
  const [taskPage, setTaskPage] = useState<Map<string, Task> | 'error' | null>(null)
  useEffect(() => {
    let live = true
    setTaskPage(null)
    loadTasks().then((r) => {
      if (!live) return
      setTaskPage(isData(r) ? new Map(r.data.tasks.map((t) => [t.id, t])) : r.status === 'empty' ? new Map() : 'error')
    })
    return () => {
      live = false
    }
  }, [nonce])

  const [single, setSingle] = useState<Map<string, Task | string>>(new Map())
  const asked = useRef(new Set<string>())
  useEffect(() => {
    asked.current = new Set()
    setSingle(new Map())
  }, [nonce])

  const [workflows, setWorkflows] = useState<Map<string, WorkflowState>>(new Map())
  const askedWf = useRef(new Set<string>())
  useEffect(() => {
    askedWf.current = new Set()
    setWorkflows(new Map())
  }, [nonce])

  /** Every task document read, by any of the three reads. */
  const tasks = useMemo(() => {
    const m = new Map<string, Task>()
    if (taskPage instanceof Map) for (const [k, t] of taskPage) m.set(k, t)
    for (const [k, t] of single) if (typeof t !== 'string') m.set(k, t)
    for (const w of workflows.values()) if (w.status === 'ok') for (const t of w.read.tasks) m.set(t.id, t)
    return m
  }, [taskPage, single, workflows])

  const laneIds = useMemo(() => Array.from(new Set(rows.map((a) => a.task_id))), [rows])

  // Tasks not on the task page, one by one, up to the cap.
  useEffect(() => {
    if (taskPage === null) return
    const missing = laneIds.filter((id) => !tasks.has(id) && !asked.current.has(id))
    const room = MISSING_TASK_READS - asked.current.size
    for (const id of missing.slice(0, Math.max(0, room))) {
      asked.current.add(id)
      loadTask(id).then((r) => {
        setSingle((m) => new Map(m).set(id, isData(r) ? r.data : r.status === 'error' ? `${r.error.httpStatus ?? 'no answer'}` : 'not read'))
      })
    }
  }, [taskPage, laneIds, tasks])

  // Each workflow the lanes name, once.
  useEffect(() => {
    for (const id of laneIds) {
      const wf = tasks.get(id)?.workflow_id ?? null
      if (wf === null || askedWf.current.has(wf)) continue
      askedWf.current.add(wf)
      loadWorkflow(wf).then((r) => {
        setWorkflows((m) =>
          new Map(m).set(
            wf,
            isData(r)
              ? { status: 'ok', read: r.data }
              : { status: 'error', why: r.status === 'error' ? `/v1/workflows/${wf} ${failureWords(r.error)}` : 'not read' },
          ),
        )
      })
    }
  }, [laneIds, tasks])

  // ---- one events page per lane in view -----------------------------------
  const [events, setEvents] = useState<Map<string, EventsRead>>(new Map())
  // THE ASKED SET BELONGS TO ONE READ. A refresh keeps the drawing mounted,
  // so no row leaves the view and none would ask again on its own: each row
  // re-asks on the new `nonce`, and the first ask of a read starts its set
  // and map afresh. React runs the rows' effects before this screen's, so the
  // reset lives in ensureEvents, with the screen's effect covering a read in
  // which no row asked. A page landing after a newer read began is dropped.
  const nonceNow = useRef(nonce)
  nonceNow.current = nonce
  const askedEv = useRef({ gen: nonce, ids: new Set<string>() })
  const startRead = () => {
    if (askedEv.current.gen === nonceNow.current) return
    askedEv.current = { gen: nonceNow.current, ids: new Set() }
    setEvents(new Map())
  }
  useEffect(startRead, [nonce])
  const ensureEvents = useCallback((id: string) => {
    startRead()
    if (askedEv.current.ids.has(id)) return
    askedEv.current.ids.add(id)
    const gen = askedEv.current.gen
    setEvents((m) => new Map(m).set(id, { status: 'reading' }))
    loadTaskEventsPage(id, { order: 'desc' }).then((r) => {
      if (askedEv.current.gen !== gen) return
      setEvents((m) =>
        new Map(m).set(
          id,
          isData(r)
            ? { status: 'ok', events: r.data.events, next: r.data.next_page_token ?? null, older: 'idle' }
            : r.status === 'empty'
              ? { status: 'ok', events: [], next: null, older: 'idle' }
              : r.status === 'error'
                ? { status: 'error', httpStatus: r.error.httpStatus, message: r.error.message }
                : { status: 'error', httpStatus: null, message: 'the read did not complete' },
        ),
      )
    })
  }, [])
  const retryEvents = (id: string) => {
    askedEv.current.ids.delete(id)
    ensureEvents(id)
  }
  const readOlder = (id: string) => {
    const cur = events.get(id)
    if (cur?.status !== 'ok' || cur.next === null || cur.older === 'reading') return
    const token = cur.next
    const gen = askedEv.current.gen
    setEvents((m) => new Map(m).set(id, { ...cur, older: 'reading' }))
    loadTaskEventsPage(id, { order: 'desc', pageToken: token }).then((r) => {
      if (askedEv.current.gen !== gen) return
      setEvents((m) => {
        const c = m.get(id)
        if (c?.status !== 'ok') return m
        if (isData(r)) return new Map(m).set(id, { status: 'ok', events: [...c.events, ...r.data.events], next: r.data.next_page_token ?? null, older: 'idle' })
        if (r.status === 'empty') return new Map(m).set(id, { ...c, next: null, older: 'idle' })
        return new Map(m).set(id, { ...c, older: r.status === 'error' ? `the older page ${failureWords(r.error)}` : 'the older page was not read' })
      })
    })
  }

  // ---- the lanes -----------------------------------------------------------
  const allLanes = useMemo(() => {
    const byTask = new Map<string, AttemptRow[]>()
    for (const a of rows) byTask.set(a.task_id, [...(byTask.get(a.task_id) ?? []), a])
    const out: Lane[] = []
    for (const [id, as] of byTask) {
      const e = events.get(id)
      out.push(buildLane(id, tasks.get(id) ?? null, as, e?.status === 'ok' ? e.events : null, anchor))
    }
    // Workflow steps that never had an attempt, from the workflow read.
    for (const w of workflows.values()) {
      if (w.status !== 'ok') continue
      for (const t of w.read.tasks) {
        if (!byTask.has(t.id) && t.attempt_count === 0) out.push(buildLane(t.id, t, [], [], anchor))
      }
    }
    return out
  }, [rows, tasks, events, workflows, anchor])
  const lanes = useMemo(() => filterLanes(allLanes, view), [allLanes, view])
  const blocks = useMemo(() => groupLanes(lanes, view.by), [lanes, view.by])
  const neverRan = allLanes.filter((l) => l.neverRan).length

  // ---- the outcome strip -----------------------------------------------------
  const tz = useMemo(viewerZone, [])
  const stripQuery = useMemo(
    () =>
      outcomesQuery(
        {
          ...DEFAULT_VIEW,
          span: view.since !== null ? null : (view.span ?? '24h'),
          since: view.since,
          until: view.until,
          profile: view.profile,
          kind: view.kind,
        },
        tz,
      ).toString(),
    [view.since, view.until, view.span, view.profile, view.kind, tz],
  )
  const [strip, setStrip] = useState<StripRead>({ status: 'loading' })
  useEffect(() => {
    let live = true
    let landed = false
    setStrip({ status: 'loading' })
    // EVERY WAY THE READ ENDS IS SAID (visual QA Q6, 2026-10-02): the strip
    // read "Reading /v1/outcomes…" beside drawn lanes and never moved, because
    // an `empty` answer was mapped back to loading and a read that never
    // answered had no end. An empty answer, a thrown read and no answer by
    // the deadline are each an honest error; a late answer still replaces it.
    const fail = (message: string, httpStatus: number | null = null) =>
      setStrip({ status: 'error', error: { kind: 'unreachable', httpStatus, code: null, message } })
    const deadline = setTimeout(() => {
      if (live && !landed) fail(`no answer after ${Math.round(STRIP_DEADLINE_MS / 1000)} s`)
    }, STRIP_DEADLINE_MS)
    ;(loadOutcomes(new URLSearchParams(stripQuery), ['totals']) as Promise<Result<Outcomes>>).then(
      (r) => {
        if (!live) return
        landed = true
        clearTimeout(deadline)
        if (isData(r)) setStrip({ status: 'ok', data: r.data })
        else if (r.status === 'error') setStrip({ status: 'error', error: r.error })
        else if (r.status === 'empty') fail('the route answered with nothing')
        else fail('the read did not finish')
      },
      (e: unknown) => {
        if (!live) return
        landed = true
        clearTimeout(deadline)
        fail(e instanceof Error ? e.message : String(e))
      },
    )
    return () => {
      live = false
      clearTimeout(deadline)
    }
  }, [stripQuery, nonce])

  // ---- what the page draws ----------------------------------------------------
  const selected = view.lane === null ? null : (allLanes.find((l) => l.taskId === view.lane) ?? null)
  const days = spanDays(view, anchor)
  const back = view.back === null ? null : parseLanesView(view.back)
  const zoomed = view.since !== null
  const select = (id: string | null) => setView({ ...view, lane: id })
  const workflowCount = blocks.filter((b) => b.kind === 'workflow').length
  const standaloneCount = blocks.find((b) => b.kind === 'standalone')?.lanes.length ?? 0

  const profiles = useMemo(() => uniqSorted(allLanes.map((l) => l.task?.runner_profile ?? null)), [allLanes])
  const workflowIds = useMemo(() => uniqSorted(allLanes.map((l) => l.task?.workflow_id ?? null)), [allLanes])

  const zoomBy = (factor: number) => {
    const mid = (win.since + win.until) / 2
    const half = ((win.until - win.since) * factor) / 2
    const until = Math.min(mid + half, Date.now())
    setView(zoomedTo(view, Math.max(0, until - 2 * half), until))
  }

  return (
    <div className="tl-page">
      <PageHead title="Timeline">
        {rangeWords(win.since, win.until)} · {shown === null ? 'reading…' : `${allLanes.length} ${allLanes.length === 1 ? 'lane' : 'lanes'} read ${timeAgo(new Date(shown.fetchedAt).toISOString(), now)}`}{' '}
        <button type="button" onClick={refresh} disabled={att.status === 'loading'}>
          {att.status === 'loading' ? 'reading…' : 'refresh'}
        </button>
      </PageHead>

      <TimelinePages at="lanes" />

      {madeOnOutcomes(query) && (
        <p className="tl-note">
          This link was made on the Outcomes page, and Lanes reads only its span.{' '}
          <a href={`/timeline/outcomes?${query}`}>Open it on Outcomes ›</a>
        </p>
      )}

      <div className="tl-ctl" role="group" aria-label="Timeline span and filters">
        <Segmented
          label="Span"
          value={zoomed ? null : view.span}
          options={LANE_SPANS.map((s) => ({ key: s, label: s }))}
          onChange={(s) => setView({ ...view, span: s, since: null, until: null, back: null })}
        />
        {back !== null && zoomed && (
          <button type="button" className="tl-backchip" onClick={() => setView(back)}>
            ← {back.since !== null && back.until !== null ? rangeWords(Date.parse(back.since), Date.parse(back.until)) : back.span} · zoomed to {rangeWords(win.since, win.until)}
          </button>
        )}
        <span className="tl-zoom">
          <Button size="sm" aria-label="Zoom out" onClick={() => zoomBy(2)}>
            −
          </Button>
          <Button size="sm" aria-label="Zoom in" onClick={() => zoomBy(0.5)}>
            +
          </Button>
        </span>
        <TlPicker
          label="State"
          options={ALL_STATES}
          selected={view.state}
          word={(s) => s.toLowerCase().replace('_', '-')}
          onChange={(next) => setView({ ...view, state: next as TaskState[] })}
        />
        <TlPicker label="Profile" options={profiles} selected={view.profile} onChange={(next) => setView({ ...view, profile: next })} />
        <label className={view.wf !== null ? 'tl-flt is-on' : 'tl-flt'}>
          Workflow{' '}
          <select value={view.wf ?? ''} onChange={(e) => setView({ ...view, wf: e.target.value === '' ? null : e.target.value })}>
            <option value="">any</option>
            {[...workflowIds, ...(view.wf !== null && !workflowIds.includes(view.wf) ? [view.wf] : [])].map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </label>
        <Segmented
          label="Kind"
          value={view.kind}
          options={LANE_KINDS.map((k) => ({ key: k, label: k === 'steps' ? 'workflow steps' : k }))}
          onChange={(k) => setView({ ...view, kind: k })}
        />
        <label className="tl-flt">
          Group by{' '}
          <select value={view.by} onChange={(e) => setView({ ...view, by: e.target.value as LanesView['by'] })}>
            {LANE_GROUPS.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        </label>
      </div>

      <OutcomeStrip strip={strip} lanes={allLanes} view={view} since={win.since} until={win.until} />

      {days > SUGGEST_OUTCOMES_DAYS && (
        <p className="tl-note tl-suggest">
          <span>{`Past ${SUGGEST_OUTCOMES_DAYS} days the bars are slivers: counts over the span read better as a ledger.`}</span>{' '}
          <a href={`/timeline/outcomes?${outcomesQueryFor(view)}`}>Open Outcomes ›</a>
        </p>
      )}

      <Legend />

      {att.status === 'error' ? (
        <div className="tl-state is-bad" role="alert">
          <p>
            The attempts could not be read. /v1/attempts {failureWords(att.error)} at {CLOCK_S.format(att.at)}. Nothing is drawn, because nothing was read.
          </p>
          <Button size="sm" onClick={refresh}>
            Try again
          </Button>
        </div>
      ) : shown === null ? (
        <div className="tl-state" aria-busy="true">
          <p>{`Reading /v1/attempts for ${rangeWords(win.since, win.until)}…`}</p>
          <div className="tl-skel" aria-hidden="true" />
          <div className="tl-skel is-short" aria-hidden="true" />
        </div>
      ) : allLanes.length === 0 ? (
        <div className="tl-state">
          <p>{`No attempt held capacity between ${rangeWords(win.since, win.until).replace('–', ' and ')}.`}</p>
          <p>
            Waiting work costs nothing and draws no bar, so this is not &ldquo;nothing exists&rdquo;. <a href="/agents/waiting">Agents › Waiting ›</a>
          </p>
        </div>
      ) : lanes.length === 0 ? (
        <div className="tl-state">
          <p>{`No lane matches ${filterWords(view)} among the ${rows.length} attempts read.`}</p>
          <Button size="sm" onClick={() => setView({ ...view, state: [], profile: [], wf: null, kind: 'all', lane: null })}>
            Clear filters
          </Button>
        </div>
      ) : (
        <div className={rereading ? 'tl-chart is-stale' : 'tl-chart'} aria-busy={rereading}>
          <div className="tl" role="group" aria-label="Lanes">
            <Axis
              since={win.since}
              until={win.until}
              nowAt={anchor}
              summary={`${workflowCount} ${workflowCount === 1 ? 'workflow' : 'workflows'} · ${standaloneCount} standalone`}
              onZoom={(a, b) => setView(zoomedTo(view, a, b))}
            />
            {blocks.map((b) => (
              <Fragment key={b.key}>
                <GroupHead block={b} workflow={b.kind === 'workflow' ? (workflows.get(b.key) ?? null) : null} />
                {b.lanes.map((l) => (
                  <LaneRow
                    key={l.taskId}
                    lane={l}
                    since={win.since}
                    until={win.until}
                    events={events.get(l.taskId) ?? null}
                    single={single.get(l.taskId) ?? null}
                    taskPageFailed={taskPage === 'error'}
                    selected={view.lane === l.taskId}
                    onSelect={() => select(view.lane === l.taskId ? null : l.taskId)}
                    onSeen={ensureEvents}
                    readNonce={nonce}
                    onRetry={retryEvents}
                  />
                ))}
              </Fragment>
            ))}
          </div>
          {rereading && <p className="tl-q">Reading the new span… the lanes above are the previous read.</p>}
        </div>
      )}

      {shown !== null && att.status !== 'error' && shown.next !== null && (
        <div className="tl-state is-partial" role="status" aria-label="More attempts in this span">
          <p>
            {shown.rows.length} attempts drawn, newest first. Older attempts in this span: <b className="tl-dash">—</b> the route returns a page token, not a total.
          </p>
          {typeof shown.more === 'object' && <p>{`The next page ${failureWords(shown.more)}.`}</p>}
          <Button size="sm" onClick={readNextPage} disabled={shown.more === 'reading'}>
            {shown.more === 'reading' ? 'Reading…' : 'Read the next page ›'}
          </Button>
        </div>
      )}

      {shown !== null && att.status !== 'error' && (
        <p className="tl-cov">
          <span>
            <b>{shown.rows.length}</b> attempts from {shown.pages === 1 ? 'page 1' : `pages 1–${shown.pages}`} of /v1/attempts (since {clock(win.since, days > 1)}, until {clock(win.until, days > 1)}) · {shown.next === null ? 'no next page' : 'a next page exists'}
          </span>
          <span>an attempt created before the span is not on this read</span>
          {neverRan > 0 && <span>{neverRan} workflow {neverRan === 1 ? 'step' : 'steps'} with no attempt, from the workflow reads</span>}
          <span>events: one page per lane in view, newest first (order=desc)</span>
          <span>State, profile and workflow filters run in this browser over the rows read</span>
          <span>times {tz}</span>
        </p>
      )}

      {selected !== null && (
        <LaneDetail
          lane={selected}
          events={events.get(selected.taskId) ?? null}
          onClose={() => select(null)}
          onOlder={() => readOlder(selected.taskId)}
          onRead={() => ensureEvents(selected.taskId)}
        />
      )}
      {view.lane !== null && selected === null && shown !== null && (
        <p className="tl-note">{`The lane ${view.lane} is not among the lanes read for this span.`}</p>
      )}
    </div>
  )
}

function uniqSorted(xs: ReadonlyArray<string | null>): string[] {
  return Array.from(new Set(xs.filter((x): x is string => x !== null))).sort()
}

function filterWords(v: LanesView): string {
  const parts: string[] = []
  if (v.state.length > 0) parts.push(`state ${v.state.map((s) => s.toLowerCase()).join(' or ')}`)
  if (v.profile.length > 0) parts.push(`profile ${v.profile.join(' or ')}`)
  if (v.wf !== null) parts.push(`workflow ${v.wf}`)
  if (v.kind !== 'all') parts.push(v.kind === 'steps' ? 'workflow steps' : 'standalone')
  return parts.length === 0 ? 'these filters' : parts.join(' and ')
}

// ---------------------------------------------------------------------------
// Local components (prefixed for U0's shared set to replace)
// ---------------------------------------------------------------------------

function TlPicker({
  label,
  options,
  selected,
  word = (s) => s,
  onChange,
}: {
  label: string
  options: readonly string[]
  selected: readonly string[]
  word?: (s: string) => string
  onChange: (next: string[]) => void
}) {
  const summary = selected.length === 0 ? `all ${options.length}` : selected.map(word).join(', ')
  return (
    <details className={selected.length > 0 ? 'tl-flt tl-pick is-on' : 'tl-flt tl-pick'}>
      <summary>
        {label} <i>{summary}</i>
      </summary>
      <div className="tl-pop" role="group" aria-label={label}>
        {options.length === 0 && <span className="tl-q">none in the rows read</span>}
        {options.map((o) => (
          <label key={o}>
            <input
              type="checkbox"
              checked={selected.includes(o)}
              onChange={(e) => onChange(e.target.checked ? [...selected, o].sort() : selected.filter((s) => s !== o))}
            />{' '}
            {word(o)}
          </label>
        ))}
      </div>
    </details>
  )
}

function Legend() {
  return (
    <p className="tl-legend">
      <span>
        <i className="tl-lg is-hold" /> held capacity (leased → running)
      </span>
      <span>
        <i className="tl-lg is-wait" /> waiting, free (queued, ready)
      </span>
      <span>
        <i className="tl-lg is-park" /> parked, free
      </span>
      <span>
        <i className="tl-lg is-cut" /> fenced generation
      </span>
      <span className="tl-mk is-warn">
        <MarkIcon mark="warn" /> cancel asked
      </span>
      <span className="tl-mk is-neu">
        <MarkIcon mark="succeeded" /> ended
      </span>
      <span className="tl-mk is-bad">
        <MarkIcon mark="failed" /> failed · dead-lettered
      </span>
    </p>
  )
}


function pct(t: number, since: number, until: number): number {
  return ((Math.min(Math.max(t, since), until) - since) / (until - since)) * 100
}

function Axis({
  since,
  until,
  nowAt,
  summary,
  onZoom,
}: {
  since: number
  until: number
  nowAt: number
  summary: string
  onZoom: (since: number, until: number) => void
}) {
  const ticks = axisTicks(since, until)
  const wide = until - since > 36 * 3_600_000
  const [drag, setDrag] = useState<{ x0: number; x1: number; w: number; left: number } | null>(null)
  const at = (x: number, d: { w: number; left: number }) => since + (Math.min(Math.max(x - d.left, 0), d.w) / d.w) * (until - since)
  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    const r = e.currentTarget.getBoundingClientRect()
    if (!(r.width > 0)) return
    // Captured, so a drag by touch or pen keeps reaching the axis.
    e.currentTarget.setPointerCapture?.(e.pointerId)
    setDrag({ x0: e.clientX, x1: e.clientX, w: r.width, left: r.left })
  }
  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (drag !== null) setDrag({ ...drag, x1: e.clientX })
  }
  const up = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (drag === null) return
    const d = { ...drag, x1: e.clientX }
    setDrag(null)
    if (Math.abs(d.x1 - d.x0) < DRAG_MIN_PX) return
    onZoom(at(Math.min(d.x0, d.x1), d), at(Math.max(d.x0, d.x1), d))
  }
  const brush =
    drag !== null && Math.abs(drag.x1 - drag.x0) >= DRAG_MIN_PX
      ? {
          left: ((Math.min(drag.x0, drag.x1) - drag.left) / drag.w) * 100,
          width: (Math.abs(drag.x1 - drag.x0) / drag.w) * 100,
          words: rangeWords(at(Math.min(drag.x0, drag.x1), drag), at(Math.max(drag.x0, drag.x1), drag)),
        }
      : null
  return (
    <div className="tl-row tl-axis">
      <div className="tl-lab">
        <small>{summary}</small>
      </div>
      <div
        className="tl-trk"
        title="Drag across the axis to zoom"
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={() => setDrag(null)}
      >
        {ticks.map((t) => (
          <span key={t} className="tl-tk" style={{ left: `${pct(t, since, until)}%` }}>
            {clock(t, wide && new Date(t).getHours() === 0 ? false : wide).replace(/ 00:00$/, '')}
          </span>
        ))}
        {until >= nowAt - 60_000 && <span className="tl-tk is-now">now</span>}
        {brush !== null && (
          <span className="tl-brush" style={{ left: `${brush.left}%`, width: `${brush.width}%` }}>
            <b>drag to zoom: {brush.words}</b>
          </span>
        )}
      </div>
    </div>
  )
}

function GroupHead({ block, workflow }: { block: LaneBlock; workflow: WorkflowState | null }) {
  let title: string
  let note: string
  if (block.kind === 'workflow') {
    title = block.key
    note =
      workflow === null
        ? 'workflow: reading…'
        : workflow.status === 'error'
          ? `steps — (${workflow.why})`
          : `${workflow.read.workflow.steps.length} steps · ${workflow.read.workflow.state.toLowerCase().replace('_', '-')}`
  } else if (block.kind === 'standalone') {
    title = 'Standalone'
    note = `${block.lanes.length} ${block.lanes.length === 1 ? 'task' : 'tasks'} in this span`
  } else if (block.kind === 'unread') {
    title = 'Task not read'
    note = `${block.lanes.length} ${block.lanes.length === 1 ? 'lane' : 'lanes'}: workflow and profile unknown`
  } else if (block.kind === 'profile') {
    title = block.key
    note = `${block.lanes.length} ${block.lanes.length === 1 ? 'lane' : 'lanes'}`
  } else {
    title = 'All lanes'
    note = `${block.lanes.length}`
  }
  const state = workflow?.status === 'ok' ? workflow.read.workflow.state : null
  return (
    <div className="tl-row is-group" data-group={block.key}>
      <div className="tl-lab">
        <b>
          {state !== null && state in STATE_MARK && <StateMark state={state as TaskState} bare />}
          {block.kind === 'workflow' && workflow?.status === 'ok' ? (
            <a href={`/workflows/${encodeURIComponent(block.key)}`}>{title}</a>
          ) : (
            title
          )}
        </b>
        <small>{note}</small>
      </div>
      <div className="tl-trk" />
    </div>
  )
}

function laneNote(lane: Lane, events: EventsRead | null, single: Task | string | null, taskPageFailed: boolean): string {
  const t = lane.task
  if (t === null) {
    const why =
      typeof single === 'string'
        ? `its read answered ${single}`
        : single === null
          ? taskPageFailed
            ? 'the task list read failed'
            : `not on the task page, and over the ${MISSING_TASK_READS}-read cap or still reading`
          : 'not read'
    return `${lane.taskId} · profile —, task not read (${why})`
  }
  const parts = [t.step_id !== null ? t.id : null, t.runner_profile].filter((x): x is string => x !== null)
  if (lane.neverRan) parts.push(TERMINAL_STATES.has(t.state) ? 'never ran · no attempt' : 'never ran yet · no attempt')
  else if (lane.attempts.length > 1) parts.push(`${lane.attempts.length} attempts`)
  if (!lane.neverRan && events?.status === 'error') {
    parts.push(`fence and cancel marks: — this lane's events read failed (${events.httpStatus ?? 'no answer'})`)
  }
  return parts.join(' · ')
}

function LaneRow({
  lane,
  since,
  until,
  events,
  single,
  taskPageFailed,
  selected,
  onSelect,
  onSeen,
  readNonce,
  onRetry,
}: {
  lane: Lane
  since: number
  until: number
  events: EventsRead | null
  single: Task | string | null
  taskPageFailed: boolean
  selected: boolean
  onSelect: () => void
  onSeen: (id: string) => void
  /** The screen's read generation: a refresh asks a row still in view again. */
  readNonce: number
  onRetry: (id: string) => void
}) {
  const [ref, inView] = useInView<HTMLDivElement>()
  useEffect(() => {
    if (inView && !lane.neverRan) onSeen(lane.taskId)
  }, [inView, lane.neverRan, lane.taskId, onSeen, readNonce])
  const t = lane.task
  const name = t !== null ? agentName(t) : lane.taskId
  const wide = until - since > 36 * 3_600_000
  const segs = lane.segs.filter((s) => s.to > since && s.from < until)
  const marks = lane.marks.filter((m) => m.at >= since && m.at <= until)
  return (
    <div ref={ref} className={selected ? 'tl-row is-sel' : 'tl-row'} data-lane={lane.taskId}>
      <div className="tl-lab">
        <b>
          {t !== null && <StateMark state={t.state} bare />}
          <button type="button" className="tl-name" aria-pressed={selected} onClick={onSelect} title={lane.taskId}>
            {name}
          </button>
        </b>
        <small>{laneNote(lane, events, single, taskPageFailed)}</small>
        {events?.status === 'error' && !lane.neverRan && (
          <button type="button" className="c-link tl-retry" onClick={() => onRetry(lane.taskId)}>
            read its events again
          </button>
        )}
      </div>
      <div className="tl-trk">
        {segs.map((s, i) => (
          <Seg key={i} s={s} since={since} until={until} wide={wide} multi={lane.attempts.length > 1} />
        ))}
        {marks.map((m, i) => (
          <span
            key={`m${i}`}
            className={`tl-mkr is-${MARK_HUE[m.kind]}`}
            data-mark={m.kind}
            style={{ left: `${pct(m.at, since, until)}%` }}
            title={`${MARK_WORD[m.kind]} ${clock(m.at, wide)}`}
          >
            <MarkIcon mark={MARK_OF[m.kind]} />
            {m.kind === 'cancel_requested' && <span className="tl-tag">{`cancel asked ${clock(m.at, wide)}`}</span>}
          </span>
        ))}
      </div>
    </div>
  )
}

function Seg({ s, since, until, wide, multi }: { s: LaneSeg; since: number; until: number; wide: boolean; multi: boolean }) {
  const left = pct(s.from, since, until)
  const width = Math.max(pct(s.to, since, until) - left, s.kind === 'wait' ? 0 : 0.4)
  const cls = [
    'tl-seg',
    `is-${s.kind}`,
    s.open ? 'is-open' : '',
    s.future ? 'is-future' : '',
    s.endKnown ? '' : 'is-unknown-end',
  ]
    .filter((c) => c !== '')
    .join(' ')
  const span = `${clock(s.from, wide)} → ${s.open ? 'now' : clock(s.to, wide)}`
  const title =
    s.kind === 'hold'
      ? `${s.label ?? 'attempt'}: held capacity ${span}${s.endKnown ? '' : ' (no completed_at: its end was never written)'}`
      : s.kind === 'cut'
        ? `${s.label}: ${span}, then fenced`
        : s.kind === 'park'
          ? s.future
            ? `expected to resume by ${clock(s.to, wide)}; nothing held`
            : `parked ${span}${s.reason ? ` · ${s.reason}` : ''}; nothing held`
          : `waiting ${span}; nothing held`
  return (
    <span className={cls} data-seg={s.kind} style={{ left: `${left}%`, width: `${width}%` }} title={title}>
      {s.kind === 'cut' && <span className="tl-seg-t">{s.label}</span>}
      {s.kind === 'hold' && multi && <span className="tl-seg-t">{s.label}</span>}
      {s.kind === 'park' && !s.future && <span className="tl-seg-t">parked</span>}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The outcome strip
// ---------------------------------------------------------------------------

function OutcomeStrip({ strip, lanes, view, since, until }: { strip: StripRead; lanes: readonly Lane[]; view: LanesView; since: number; until: number }) {
  const fenced = lanes.reduce((n, l) => n + l.segs.filter((s) => s.kind === 'cut').length, 0)
  const drawn = lanes.filter((l) => !l.neverRan)
  const unread = drawn.filter((l) => !l.eventsRead).length
  const parks = drawn.reduce((n, l) => n + l.segs.filter((s) => s.kind === 'park' && !s.future).length, 0)
  const still = drawn.filter((l) => l.task?.state === 'PARKED').length
  const link = <a href={`/timeline/outcomes?${outcomesQueryFor(view)}`}>Outcomes for this span ›</a>
  return (
    <div className="tl-strip">
      {strip.status === 'loading' ? (
        <p className="tl-q">Reading /v1/outcomes…</p>
      ) : strip.status === 'error' ? (
        <p>
          <span>{`Outcomes not read (/v1/outcomes${strip.error.httpStatus === null ? '' : ` ${strip.error.httpStatus}`}: ${strip.error.message}).`}</span> The lanes below are unaffected.
        </p>
      ) : (
        <p className="tl-strip1">
          {strip.data.totals.rate === null ? (
            <span className="tl-x">
              <b className="tl-dash">—</b> success: no task was decided in this span
            </span>
          ) : (
            <span className="tl-x">
              <b>{Math.round(strip.data.totals.rate.p * 100)} %</b> success, {strip.data.totals.rate.k} of {strip.data.totals.rate.n} decided
            </span>
          )}
          <span className="tl-x">
            <b>{strip.data.totals.failed + strip.data.totals.dead_lettered}</b> failed or dead-lettered
          </span>
          <span className="tl-x">
            <b>{strip.data.totals.cancelled.total}</b> cancelled
          </span>
          {unread > 0 && fenced === 0 ? (
            <span className="tl-x">
              fenced <b className="tl-dash">—</b> events not read on {unread} {unread === 1 ? 'lane' : 'lanes'}
            </span>
          ) : (
            <span className="tl-x">
              <b>{unread > 0 ? `${fenced}+` : fenced}</b> {fenced === 1 && unread === 0 ? 'generation' : 'generations'} fenced
              {unread > 0 && `, events not read on ${unread} ${unread === 1 ? 'lane' : 'lanes'}`}
            </span>
          )}
          {unread > 0 ? (
            <span className="tl-x">
              parks <b className="tl-dash">—</b> events not read on {unread} {unread === 1 ? 'lane' : 'lanes'}
            </span>
          ) : (
            <span className="tl-x">
              <b>{parks}</b> {parks === 1 ? 'park' : 'parks'}, {still} still parked
            </span>
          )}
          {link}
        </p>
      )}
      {strip.status !== 'ok' && strip.status !== 'loading' && link}
      <p className="tl-q">
        Decided counts from GET /v1/outcomes over {rangeWords(since, until)}; parks and fences counted from the lanes drawn, not the whole tenant.
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The open lane: summary and newest events, a sheet on a phone
// ---------------------------------------------------------------------------

function LaneDetail({
  lane,
  events,
  onClose,
  onOlder,
  onRead,
}: {
  lane: Lane
  events: EventsRead | null
  onClose: () => void
  onOlder: () => void
  onRead: () => void
}) {
  useEffect(() => {
    if (!lane.neverRan) onRead()
  }, [lane.taskId, lane.neverRan, onRead])
  const t = lane.task
  const name = t !== null ? agentName(t) : lane.taskId
  const href = `/agents/recent/${encodeURIComponent(lane.taskId)}/attempts`
  const costs = lane.attempts.map((a) => a.cost_usd)
  const reported = costs.filter((c): c is number => c !== null)
  const created = t !== null ? Date.parse(t.created_at) : null
  const ended = t?.completed_at ? Date.parse(t.completed_at) : null
  return (
    <section className="tl-detail" aria-label={`${name} (${lane.taskId})`}>
      <div className="tl-card">
        <div className="tl-card-head">
          <h2>{name}</h2>
          {t !== null ? <StateMark state={t.state} /> : <span className="tl-q">state — task not read</span>}
          <button type="button" className="tl-close" aria-label="Close" onClick={onClose}>
            ×
          </button>
        </div>
        <p className="tl-q">
          {lane.taskId}
          {t !== null && ` · ${t.runner_profile} · ${t.resource_class} · current generation ${t.current_generation}`}
          {t?.workflow_id ? ` · ${t.workflow_id}` : ''}
        </p>
        <dl className="tl-kv">
          <dt>Attempts</dt>
          <dd>
            {lane.attempts.length === 0
              ? 'none: this step never ran'
              : lane.segs
                  .filter((s) => s.kind === 'hold' || s.kind === 'cut')
                  .map((s) => `${s.label}: ${clock(s.from, true)} → ${s.open ? 'running' : s.endKnown ? `${clock(s.to, true)} · ${formatDuration(s.to - s.from)}` : '— no completed_at: worker killed'}`)
                  .join('; ')}
          </dd>
          {lane.segs.some((s) => s.kind === 'cut') && (
            <>
              <dt>Fenced</dt>
              <dd>A stale generation exits without running the agent and without touching the lease (invariant 5).</dd>
            </>
          )}
          <dt>Parked</dt>
          <dd>
            {!lane.eventsRead && !lane.neverRan
              ? '— needs the events page'
              : (() => {
                  const ps = lane.segs.filter((s) => s.kind === 'park' && !s.future)
                  return ps.length === 0
                    ? 'never, in the events read'
                    : ps.map((p) => `${clock(p.from, true)} → ${p.open ? 'now' : clock(p.to, true)}${p.reason ? ` · ${p.reason}` : ''}`).join('; ') + ' · free, no capacity held'
                })()}
          </dd>
          <dt>Wall time</dt>
          <dd>
            {created === null
              ? '— task not read'
              : ended === null
                ? `${clock(created, true)} → not finished`
                : `${clock(created, true)} → ${clock(ended, true)} · ${formatDuration(ended - created)}`}
          </dd>
          <dt>Cost</dt>
          <dd>
            {lane.attempts.length === 0
              ? '— no attempt'
              : reported.length === 0
                ? '— not reported by any attempt read'
                : `$${reported.reduce((a, b) => a + b, 0).toFixed(2)} · reported by ${reported.length} of ${costs.length} attempts`}
          </dd>
        </dl>
        <p className="tl-acts">
          <ButtonLink kind="primary" href={href}>
            Open in Agents ›
          </ButtonLink>
        </p>
      </div>
      <div className="tl-card">
        <div className="tl-card-head">
          <h2>Events</h2>
          <span className="tl-q">
            {events?.status === 'ok'
              ? `${events.events.length} read · newest first · ${events.next === null ? 'all read' : 'older events exist: — the route returns a page token, not a total'}`
              : ''}
          </span>
        </div>
        {lane.neverRan ? (
          <p className="tl-q">No attempt, so no run events to read.</p>
        ) : events === null || events.status === 'reading' ? (
          <p className="tl-q">Reading this lane&rsquo;s newest events…</p>
        ) : events.status === 'error' ? (
          <p className="tl-q">{`Events not read: the read answered ${events.httpStatus ?? 'nothing'} (${events.message}).`}</p>
        ) : events.events.length === 0 ? (
          <p className="tl-q">This task has no events.</p>
        ) : (
          <ol className="tl-evl">
            {events.events.map((e) => (
              <EventRow key={e.event_id} e={e} />
            ))}
          </ol>
        )}
        {events?.status === 'ok' && events.next !== null && (
          <Button onClick={onOlder} disabled={events.older === 'reading'}>
            {events.older === 'reading' ? 'Reading…' : 'Older events ›'}
          </Button>
        )}
        {events?.status === 'ok' && typeof events.older === 'string' && events.older !== 'idle' && events.older !== 'reading' && (
          <p className="tl-q">{events.older}</p>
        )}
      </div>
    </section>
  )
}

const EVENT_MARK: Record<string, { mark: MarkName | 'warn'; hue: string }> = {
  succeeded: { mark: 'succeeded', hue: 'neu' },
  failed: { mark: 'failed', hue: 'bad' },
  dead_lettered: { mark: 'dead', hue: 'bad' },
  cancelled: { mark: 'cancelled', hue: 'neu' },
  cancel_requested: { mark: 'warn', hue: 'warn' },
  generation_fenced: { mark: 'warn', hue: 'warn' },
  parked: { mark: 'parked', hue: 'park' },
  running: { mark: 'running', hue: 'live' },
  lease_acquired: { mark: 'starting', hue: 'live' },
  dispatched: { mark: 'starting', hue: 'live' },
  starting: { mark: 'starting', hue: 'live' },
  ready: { mark: 'ready', hue: 'neu' },
  queued: { mark: 'queued', hue: 'neu' },
  submitted: { mark: 'queued', hue: 'neu' },
}

function EventRow({ e }: { e: TaskEvent }) {
  const kind = eventKind(e)
  const m = EVENT_MARK[kind] ?? { mark: 'queued' as const, hue: 'neu' }
  const t = Date.parse(e.at)
  const fence =
    kind === 'generation_fenced' && typeof e.detail?.['expected_generation'] === 'number'
      ? ` · gen ${String(e.detail['expected_generation'])} → ${String(e.detail['observed_generation'] ?? '—')}`
      : ''
  const reason = kind === 'parked' && typeof e.detail?.['reason'] === 'string' ? ` · ${e.detail['reason']}` : ''
  return (
    <li className={kind === 'generation_fenced' ? 'tl-evr is-fence' : 'tl-evr'}>
      <span className="tl-evt">{Number.isFinite(t) ? clock(t, true) : '—'}</span>
      <span className={`tl-mk is-${m.hue}`}>
        <MarkIcon mark={m.mark} />
      </span>
      <span className="tl-evk">
        {kind}
        {fence}
        {reason}
      </span>
      <span className="tl-evg">{e.generation === null ? 'gen —' : `gen ${e.generation}`}</span>
    </li>
  )
}
