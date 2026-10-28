import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'

import { DRAWER_SETTLE_MS, Mark } from './AgentDetail'
import { loadAttempts, loadTaskLogs, loadTranscript } from './api'
import {
  ARTIFACTS_POLL_MS,
  LOG_VIEWS,
  LogBody,
  POLLED_STREAMS,
  logsSettled,
  transcriptSettled,
  type LogFeed,
  type LogView,
} from './Artifacts'
import type { Result } from './fetch'
import {
  ERROR_PATTERN_SAYS,
  arrivedLines,
  isProgressStep,
  linesOf,
  tailGap,
  windowAt,
  type Arrived,
  type Gap,
  type GapUnknown,
  type WindowAt,
} from './logLines'
import { MarkdownInline } from './ArtifactViewer'
import { phoneWidth } from './HelpCard'
import { GapNotice, LogMarksContext, type LogMarks } from './logMarks'
import { attemptLine, useRead } from './RunFiles'
import { TERMINAL_STATES, clockTime, type AttemptRow, type LogStream, type Task, type TaskTranscript } from './types'
import { useNow } from './useNow'
import { Button, Segmented, ToneMark } from './components'

/**
 * THE LOGS TAB (owner decision 2026-10-04; replaces viewers.html pick A's
 * bottom dock).
 *
 * The log was a dock along the bottom of the agent's column, open across
 * every tab, with a strip, a resize handle and an `Open full` overlay. The
 * owner's QA found it over the agent's details at every split width and
 * moving the page when it was focused (D7), and its four streams scrolled
 * out of sight inside a pill row (D13). The owner does not want the log
 * overlapping the agent: it is a tab now, beside Details, Attempts,
 * Artifacts, Checkpoints and Children, at `/agents/<tab>/<id>/logs`, and it
 * fills the whole detail area under the header with nothing beneath it. An
 * agent opens on Details, not here (#1646, 2026-10-19; a running agent
 * opened here from 2026-10-04); Details keeps a one-line last log line that
 * switches here (`LogLastLine`).
 *
 * WHAT IT READS. `GET /v1/tasks/{id}/transcript` and `/logs` -- the agent's
 * stderr and the runner's two streams in one read -- for the attempt the
 * reader picked, by `attempt_id`. The picker is labelled by GENERATION,
 * because generation is what fencing reasons about.
 *
 * EVERY RULE IN ITS HONEST FORM (redesign-v2.md, "Honesty rules"):
 *
 *  - search covers THIS WINDOW, and says so -- not the whole stream;
 *  - "jump to error" is a console-side pattern plus a transcript tool result's
 *    `is_error`, because the API marks no line as an error; the button says
 *    what it matches, and that it can miss;
 *  - the masked count is the server's, per window, and a masked value is the
 *    server's `********`, never anything this console worked out;
 *  - a gap between two reads of a live tail is drawn as a gap, with both
 *    bytes, and a tail with no position header is "cannot tell", never
 *    "nothing missing";
 *  - while paused, what is on screen is the read the reader paused on, and
 *    the count of what arrived since is exact only while the tail has not
 *    slid -- otherwise it says "about".
 */

/** A runner with an agent CLI writes a transcript; the others open on the runner's own log. */
export function defaultView(profile: string): LogView {
  return /^(claude|codex)/.test(profile) ? 'transcript' : 'runner'
}

/** The streams a view draws from the polled read, for its masked count, its gaps and its new lines. */
function streamsOf(view: LogView): readonly string[] {
  return view === 'stderr' ? ['agent_stderr'] : view === 'runner' ? ['stdout', 'stderr'] : []
}

function okFeed<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

/** Where each stream of a read sits, by name; the transcript's under `transcript`. */
function positions(feed: LogFeed): Record<string, { at: WindowAt; content: string }> {
  const out: Record<string, { at: WindowAt; content: string }> = {}
  const logs = okFeed(feed.logs)
  for (const s of logs?.streams ?? []) out[s.stream] = { at: windowAt(s), content: s.content ?? '' }
  const t = okFeed(feed.transcript)
  if (t !== null) {
    const steps = t.steps ?? []
    // A transcript's "content" for the arrival count is one line per step.
    out.transcript = { at: windowAt(t.stream), content: steps.map((s) => `${s.id}\n`).join('') }
  }
  return out
}

/** Whether the reads can still change: a live attempt, or a finish whose last writes are not in yet. */
function stillMoving(task: Task, feed: LogFeed | null, now: number): boolean {
  if (!TERMINAL_STATES.has(task.state)) return true
  if (feed === null) return true
  if (transcriptSettled(feed) && logsSettled(feed)) return false
  const done = task.completed_at === null ? Number.NaN : Date.parse(task.completed_at)
  return Number.isFinite(done) && Math.abs(now - done) <= DRAWER_SETTLE_MS
}

/**
 * A FINISHED AGENT'S TRANSCRIPT OPENS ON ITS NEWEST WINDOW (BT-018, #2423).
 * The route reads from byte 0 unless told otherwise, so a finished agent's
 * Logs tab opened on its first window and ended in `later steps are in the
 * next window`: how the agent finished was pages away. The first read says
 * how long the object is and how much one window holds; when that is not the
 * whole of it, the window that ends at the last byte is read instead (the
 * route drops the line it begins inside). A live tail is the newest part
 * already, and a second read that fails leaves the first window, which says
 * what it is. `fromStart` is the reader's choice of the first window.
 */
export async function readTranscriptWindow(
  task: Task,
  attemptId: string | null,
  fromStart: boolean,
): Promise<Result<TaskTranscript>> {
  const attempt = attemptId === null ? {} : { attemptId }
  const first = await loadTranscript(task.id, { ...attempt, source: 'auto' })
  if (fromStart || !TERMINAL_STATES.has(task.state) || first.status !== 'ok') return first
  const s = first.data.stream
  if (s.status !== 'ok' || s.source === 'live' || s.next_offset === null || s.total_bytes === null || s.returned_bytes <= 0) {
    return first
  }
  const offset = Math.max(0, s.total_bytes - s.returned_bytes)
  if (offset <= s.offset) return first
  const last = await loadTranscript(task.id, { ...attempt, source: s.source === 'artifact' ? 'auto' : 'final', offset })
  return last.status === 'ok' ? last : first
}

/** One read of an attempt's logs: the transcript and the polled streams, each with its own answer. */
async function readFeed(task: Task, attemptId: string | null, fromStart: boolean): Promise<Result<LogFeed>> {
  const attempt = attemptId === null ? {} : { attemptId }
  const [transcript, logs] = await Promise.all([
    readTranscriptWindow(task, attemptId, fromStart),
    loadTaskLogs(task.id, { ...attempt, stream: POLLED_STREAMS, source: 'auto' }),
  ])
  return { status: 'ok', data: { task, attemptId, transcript, logs }, fetchedAt: Date.now() }
}

/**
 * The newest line a view holds in a read, or null when the window has none.
 * A BLANK LINE IS NO LINE (owner QA R9, 2026-10-04): a window ending in an
 * empty line drew the Details strip with nothing in it, which reads as a
 * line nobody can see rather than as no line at all.
 *
 * NOR IS A PROGRESS NOTICE (QA G2-26, 2026-10-07): the newest transcript
 * step of a running agent was usually a `tool_progress`, so the strip read
 * `other`. The line is the newest step that is not one (`isProgressStep`).
 */
export function lastLineOf(feed: LogFeed | null, view: LogView): string | null {
  if (feed === null) return null
  if (view === 'transcript') {
    const steps = okFeed(feed.transcript)?.steps ?? []
    const s = [...steps].reverse().find((x) => !isProgressStep(x))
    if (s === undefined) return null
    const first = (s.text ?? s.tool?.name ?? '').split('\n').find((l) => l.trim() !== '') ?? ''
    return `${s.kind} ${first}`.trim()
  }
  const names = view === 'stdout' ? ['agent_stdout'] : streamsOf(view)
  const rows = (okFeed(feed.logs)?.streams ?? []).filter((s) => names.includes(s.stream))
  const lines = linesOf(rows.map((s) => s.content ?? '').join('\n')).filter((l) => l.trim() !== '')
  return lines.length === 0 ? null : lines[lines.length - 1]!
}

/**
 * WHETHER THE TRANSCRIPT ENDS ON THE CLI'S OWN SUCCESS (QA-115, visual QA run
 * 2). The newest step that is not a progress notice is a `result` the CLI did
 * not mark `is_error`. On a FAILED task that is the case a reader most
 * misreads: the CLI finished and said so, and the task still failed after it
 * -- the worker's exit, its publish, a refused diff -- so `result · success`
 * at the end of the log is the CLI's verdict, never why the task failed.
 */
export function endsInReportedSuccess(feed: LogFeed | null): boolean {
  if (feed === null) return false
  const steps = okFeed(feed.transcript)?.steps ?? []
  const s = [...steps].reverse().find((x) => !isProgressStep(x))
  return s !== undefined && s.kind === 'result' && s.meta?.['is_error'] !== true
}

/**
 * BRING `target` TO THE MIDDLE OF THE LOG'S OWN BODY, AND MOVE NOTHING ELSE
 * (owner QA R3, 2026-10-04: the no-jump rule). `scrollIntoView` scrolls every
 * scrolling ancestor, so Enter in the search moved the PAGE 0 -> 148.5 and
 * slid the agent list and the header up. Only the body's `scrollTop` is set.
 */
export function scrollWithin(body: HTMLElement, target: HTMLElement): void {
  const b = body.getBoundingClientRect()
  const t = target.getBoundingClientRect()
  const top = body.scrollTop + (t.top - b.top) - (body.clientHeight - t.height) / 2
  body.scrollTop = Math.max(0, top)
}

/**
 * The log's own body to its newest line, and nothing else. Marked as the
 * log's own scroll only when it moves, so a body already at its end does not
 * swallow the reader's next scroll as one of its own.
 */
function toEnd(body: HTMLElement, own: { current: boolean }): void {
  if (body.scrollTop + body.clientHeight >= body.scrollHeight - 1) return
  own.current = true
  body.scrollTop = body.scrollHeight
}

/**
 * HOW FAR THE BAR HAS SPILLED INTO MORE (U10a D13 review; N11 at the 380px
 * split). The row never wraps by design: the four streams, the search, Follow,
 * Error, the attempt picker and Wrap are one row. The labels shorten first, by
 * a container query on the bar (styles/agents.css); then, while the row still
 * overflows, these move into More one at a time, least used first: the
 * attempts-unread note, Wrap, the attempt picker, Error. Past the last step
 * the row wraps rather than hide a control. The four streams never spill:
 * which log is on screen is the one thing the bar must always say.
 *
 * NOR DOES FOLLOW (BT-001, #2423). In the column beside the live browser and
 * in the theater's Logs panel the bar is ~210px, and Follow was the last
 * control to spill: it was always under More there, so the embedded log had
 * no Follow anyone could see while the Logs tab had one reading `Following`.
 * Whether the log is following is said wherever the log is.
 */
export const BAR_SPILL = { unread: 1, wrap: 2, attempt: 3, error: 4, row: 5 } as const

/** A label with a short form for a narrow bar. The accessible name is always the long one. */
function LogWords({ long, short }: { long: string; short: string }) {
  if (long === short) return <>{long}</>
  return (
    <>
      <span className="ag-logs-long">{long}</span>
      <span className="ag-logs-short" aria-hidden="true">
        {short}
      </span>
    </>
  )
}

/** Each stream's short label, for a bar under 720px; the long one stays its name and its title. */
const SHORT_VIEW: Readonly<Record<LogView, string>> = {
  transcript: 'Transcript',
  stdout: 'stdout',
  stderr: 'stderr',
  runner: 'Runner',
}

/**
 * HOW AN ATTEMPT ENDED, for the picker. An attempt with no `completed_at` is
 * `running` only while it can be: its task has not ended and no later
 * generation replaced it (visual QA run 5b, ff-desktop-08: a task that failed
 * after three attempts listed `gen 1 · … · running`, its first attempt
 * refused at dispatch). Otherwise its end was not recorded, and an attempt
 * that never started says so.
 */
function attemptEnd(a: AttemptRow, open: boolean): string {
  if (a.completed_at === null) {
    if (open) return 'running'
    return a.started_at === null ? 'never started' : 'end not recorded'
  }
  return a.exit_code === null ? 'ended' : `exit ${a.exit_code}`
}

/** An attempt's place among the task's attempts, oldest first: 1 for the first. */
function attemptOrdinal(rows: readonly AttemptRow[], row: AttemptRow): number {
  const ordered = [...rows].sort((a, b) => a.created_at.localeCompare(b.created_at))
  return ordered.indexOf(row) + 1
}

/** How many leading characters of an attempt id the More menu prints; the whole id is its title and its copy. */
const ATT_ID_HEAD = 12

/** `1 · gen 1 · att_8f8ecaa6e5…` and a copy of the whole id (G2-16). */
function AttemptFact({ row, ordinal }: { row: AttemptRow; ordinal: number }) {
  const [said, setSaid] = useState('')
  const id = row.attempt_id
  const copy = () => {
    const clip = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clip === undefined || typeof clip.writeText !== 'function') {
      setSaid('copy refused')
      return
    }
    clip.writeText(id).then(
      () => setSaid('copied'),
      () => setSaid('copy refused'),
    )
  }
  return (
    <span className="ag-logs-attfact">
      {ordinal > 0 ? `${ordinal} · ` : ''}gen {row.generation} ·{' '}
      <span className="mono" title={id}>
        {id.length > ATT_ID_HEAD ? `${id.slice(0, ATT_ID_HEAD)}…` : id}
      </span>{' '}
      <Button size="sm" className="copy" onClick={copy} aria-label={`Copy attempt id ${id}`}>
        copy
      </Button>
      {said !== '' && <span role="status"> {said}</span>}
    </span>
  )
}

export function AgentLogs({ task }: { task: Task }) {
  const [view, setView] = useState<LogView>(() => defaultView(task.runner_profile))
  const [attemptId, setAttemptId] = useState<string | null>(null)
  const [needle, setNeedle] = useState('')
  // WRAPPED BY DEFAULT ON A PHONE (QA1 overview-agents-38, 2026-10-28): at
  // 390 every raw stream-json line on stdout needed a sideways scroll inside
  // the log box. The phone opens wrapped; Wrap and W still turn it off.
  const [wrap, setWrap] = useState(phoneWidth)
  const [current, setCurrent] = useState<string | null>(null)
  const [targets, setTargets] = useState<{ hits: string[]; errors: string[] }>({ hits: [], errors: [] })
  const drawnTargets = useRef(targets)
  const [tick, setTick] = useState(0)
  const body = useRef<HTMLDivElement | null>(null)
  const search = useRef<HTMLInputElement | null>(null)
  const more = useRef<HTMLDetailsElement | null>(null)
  const autoScroll = useRef(false)
  const root = useRef<HTMLElement | null>(null)
  const finished = TERMINAL_STATES.has(task.state)
  // A finished transcript is read from its newest window unless the reader
  // asks for its first (BT-018, `readTranscriptWindow`).
  const [fromStart, setFromStart] = useState(false)

  // A different agent opened in the same split starts over: its own default
  // stream, its latest attempt, nothing searched.
  const taskId = task.id
  const seenTask = useRef(taskId)
  useEffect(() => {
    if (seenTask.current === taskId) return
    seenTask.current = taskId
    setView(defaultView(task.runner_profile))
    setAttemptId(null)
    setNeedle('')
    setCurrent(null)
    setFromStart(false)
  }, [taskId, task.runner_profile])

  // THE ATTEMPTS, FOR THE PICKER. Re-read when the task's own count moves, so
  // a new attempt appears without reopening the agent.
  const attempts = useRead<{ attempts: AttemptRow[] }>(
    () => loadAttempts(taskId),
    taskId,
    `${task.attempt_count}`,
    null,
  )
  const attemptRows = okFeed(attempts.state)?.attempts ?? null
  // THE DEFAULT IS THE CURRENT GENERATION (owner QA R9, 2026-10-04): the
  // picker said `gen 1` under a header that said `gen 2` (59d1cff5), because
  // "latest" was whatever attempt the log route found newest. With the
  // attempts read, the default reads the attempt minted at the task's
  // `current_generation`, by id, and says its generation; before they are
  // read, or when no attempt carries that generation yet, it is "latest".
  const currentRow = attemptRows?.find((a) => a.generation === task.current_generation) ?? null
  const readId = attemptId ?? currentRow?.attempt_id ?? null
  const picked = attemptRows?.find((a) => a.attempt_id === readId) ?? null
  const latestRunning = attemptId === null && !finished
  // Whether an attempt with no end can still be running: not once its task
  // has ended, nor once a later generation replaced it (`attemptEnd`).
  const newestGeneration = Math.max(task.current_generation, ...(attemptRows ?? []).map((a) => a.generation))
  const attemptOpen = (a: AttemptRow) => !finished && a.generation >= newestGeneration

  // THE READ. One transcript read and one polled-streams read per tick, for
  // the picked attempt. Each keeps its own answer.
  const readKey = `${readId ?? 'latest'}:${fromStart ? 'start' : 'end'}:${tick}`
  const held = useRead<LogFeed, boolean>(() => readFeed(task, readId, fromStart), taskId, readKey, fromStart)
  // ONLY A READ OF THE PICKED ATTEMPT IS ITS LOG. `useRead` keeps the last
  // answer on screen until the next lands, which for a new pick is the
  // previous attempt's -- so that one is "reading", never shown as this one.
  // So is a read of the other end of a finished transcript (`held.asked`).
  const landed = okFeed(held.state)
  const latest = landed !== null && landed.attemptId === readId && held.asked === fromStart ? landed : null
  const latestAt = latest !== null && held.state.status === 'ok' ? held.state.fetchedAt : null
  // THE TASK ON THE FEED IS ALWAYS THE ONE ON SCREEN: the views read its
  // state to tell "not yet" from "never", and the split re-reads it.
  const feedNow = latest === null ? null : { ...latest, task }

  // THE CADENCE: every 5 s while anything can still change, and not while the
  // browser tab is hidden.
  const clock = useNow(ARTIFACTS_POLL_MS)
  const moving = stillMoving(task, feedNow, clock)
  useEffect(() => {
    if (!moving) return
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      setTick((n) => n + 1)
    }, ARTIFACTS_POLL_MS)
    return () => clearInterval(id)
  }, [moving])

  // FOLLOW IS ON FOR A RUNNING ATTEMPT, OFF FOR A FINAL RECORD. Picking an
  // attempt is a choice of record, so it re-decides; scrolling up pauses.
  const [follow, setFollow] = useState(latestRunning)
  useEffect(() => setFollow(latestRunning), [latestRunning, attemptId])

  // PAUSED, THE SCREEN KEEPS THE READ IT WAS PAUSED ON.
  // KEPT PER WINDOW (`fromStart`) as well as per attempt: the first and the
  // newest window of one finished transcript are two different records.
  const [frozen, setFrozen] = useState<{ feed: LogFeed; at: number; fromStart: boolean } | null>(null)
  useEffect(() => {
    if (follow) setFrozen(null)
    else if (feedNow !== null) {
      setFrozen((f) =>
        f !== null && f.feed.attemptId === feedNow.attemptId && f.fromStart === fromStart
          ? f
          : { feed: feedNow, at: latestAt ?? Date.now(), fromStart },
      )
    }
    // On the switch, and on the first read of a newly picked attempt; `feedNow`
    // is rebuilt each render, so its identity is not the dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [follow, latest === null, readId, fromStart])
  const shown =
    follow || frozen === null || frozen.feed.attemptId !== readId || frozen.fromStart !== fromStart
      ? feedNow
      : { ...frozen.feed, task }
  const shownAt = follow || frozen === null ? latestAt : frozen.at

  // GAPS, COMPARED READ BY READ, per stream, for this attempt. A gap is kept
  // until the reader picks another attempt: it is a fact about the stream.
  const last = useRef<{ attempt: string; at: Record<string, WindowAt> } | null>(null)
  const [gaps, setGaps] = useState<Record<string, Gap | GapUnknown | undefined>>({})
  useEffect(() => {
    if (latest === null) return
    // KEYED ON THE ATTEMPT THE READ RETURNED, not on the pick: with the picker
    // on "latest", a new attempt's first tail compared with the previous
    // attempt's last window would be drawn as output missing between them.
    const key = okFeed(latest.logs)?.attempt_id ?? readId ?? 'latest'
    const now = positions(latest)
    const prev = last.current !== null && last.current.attempt === key ? last.current.at : null
    if (prev === null) setGaps({})
    else {
      const found: Record<string, Gap | GapUnknown> = {}
      for (const [name, p] of Object.entries(now)) {
        const g = tailGap(prev[name] ?? null, p.at)
        if (g !== null) found[name] = g
      }
      if (Object.keys(found).length > 0) setGaps((cur) => ({ ...cur, ...found }))
    }
    last.current = { attempt: key, at: Object.fromEntries(Object.entries(now).map(([k, v]) => [k, v.at])) }
  }, [latest, readId])

  // WHAT ARRIVED SINCE THE PAUSE, for the view on screen.
  const arrived: Arrived | null = useMemo(() => {
    if (follow || frozen === null || latest === null) return null
    const names = view === 'transcript' ? ['transcript'] : streamsOf(view)
    if (names.length === 0) return null
    const then = positions(frozen.feed)
    const now = positions(latest)
    let lines = 0
    let exact = true
    for (const n of names) {
      const a = then[n]
      const b = now[n]
      if (a === undefined || b === undefined) return null
      const got = arrivedLines(a, b)
      if (got === null) return null
      lines += got.lines
      exact = exact && got.exact
    }
    return { lines, exact }
  }, [follow, frozen, latest, view])

  // THE MASKED COUNT, the server's, for the window on screen.
  const masked: number | null = useMemo(() => {
    if (shown === null) return null
    if (view === 'transcript') {
      const t = okFeed(shown.transcript)
      return t === null || t.stream.status !== 'ok' ? null : t.redaction_count
    }
    const logs = okFeed(shown.logs)
    if (logs === null) return null
    const names = streamsOf(view)
    const rows = names.map((n) => logs.streams.find((s) => s.stream === n)).filter((s): s is LogStream => s !== undefined && s.status === 'ok')
    return rows.length === 0 ? null : rows.reduce((n, s) => n + s.redaction_count, 0)
  }, [shown, view])

  // THE TARGETS ARE READ OFF THE DOM (logMarks.tsx): what is drawn as a hit
  // or an error is exactly what is counted and walked.
  const recount = useCallback(() => {
    const el = body.current
    if (el === null) return
    const keys = (sel: string) => [...el.querySelectorAll<HTMLElement>(sel)].map((n) => n.dataset.logKey ?? '').filter(Boolean)
    const hits = keys('[data-log-key].is-hit')
    const errors = keys('[data-log-key].is-err')
    // COMPARED AGAINST A REF, NOT IN A STATE UPDATER: an updater that hands
    // back `cur` still queues a render, and this runs after every render and
    // on every class change under the body, so a recount that changed nothing
    // re-rendered forever (`Maximum update depth exceeded`, PR #2010 CI).
    const cur = drawnTargets.current
    if (cur.hits.join('|') === hits.join('|') && cur.errors.join('|') === errors.join('|')) return
    drawnTargets.current = { hits, errors }
    setTargets(drawnTargets.current)
  }, [])
  useLayoutEffect(recount)
  // AND WHENEVER THE LOG REDRAWS ITSELF (visual QA run 5b, ff-desktop-07): a
  // view draws lines from its own state too -- the transcript's next window,
  // its records -- and that redraws the view alone, not this bar. The count
  // stayed the last one the bar saw: Error drew `Error –/1` in one frame and
  // was named `No error matched` and disabled in another. Any change under
  // the log's body counts again, so what the control shows, its name and
  // whether it can be pressed all come from the lines on the glass.
  useEffect(() => {
    const el = body.current
    if (el === null || typeof MutationObserver === 'undefined') return
    const seen = new MutationObserver(recount)
    seen.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'data-log-key'] })
    return () => seen.disconnect()
  }, [recount])

  // FOLLOWING: keep the newest line in view after each read. Only the log's
  // own body scrolls: the page and the column never move.
  useLayoutEffect(() => {
    const el = body.current
    if (!follow || el === null) return
    // `toEnd`, NOT AN UNCONDITIONAL MARK (QA1 overview-agents-34, 2026-10-28).
    // This runs after every render -- `shown` is rebuilt on each -- and it
    // marked the next scroll as the log's own even when the body was already
    // at its end and no scroll event was coming. So the reader's own scroll
    // up was swallowed as the log's, Follow stayed on, and the next 5 s read
    // pinned the view back to the bottom: the start of a live transcript
    // could not be read. Only a move that happens is marked.
    toEnd(el, autoScroll)
  }, [follow, shown, view])

  // AND WHENEVER THE BODY ITSELF CHANGES SIZE (BT-001, #2423). The log beside
  // the live browser and in the theater is drawn into a column that is hidden,
  // toggled or resized after its read lands; the scroll above ran against a
  // body with no height and the log opened at its top. While following, a
  // body that is resized is brought back to its newest line.
  useEffect(() => {
    const el = body.current
    if (!follow || el === null || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => toEnd(el, autoScroll))
    ro.observe(el)
    return () => ro.disconnect()
  }, [follow])

  // A FINISHED AGENT OPENS AT ITS LAST LINE (BT-001/BT-018): how it finished
  // is what a reader opens a final record for. Once per agent, attempt, view
  // and window; after that the reader's scroll is theirs.
  const openedAt = useRef<string | null>(null)
  useLayoutEffect(() => {
    const el = body.current
    if (!finished || fromStart || shown === null || el === null) return
    const at = `${taskId}:${readId ?? 'latest'}:${view}`
    if (openedAt.current === at) return
    openedAt.current = at
    toEnd(el, autoScroll)
  }, [finished, fromStart, shown, taskId, readId, view])

  const jump = useCallback(
    (list: string[]) => {
      if (list.length === 0) return
      const i = current === null ? -1 : list.indexOf(current)
      const next = list[(i + 1) % list.length]!
      setCurrent(next)
      setFollow(false)
      const el = body.current
      const target = [...(el?.querySelectorAll<HTMLElement>('[data-log-key]') ?? [])].find((n) => n.dataset.logKey === next)
      if (el !== null && target !== undefined) scrollWithin(el, target)
    },
    [current],
  )

  // NOTHING TO FOLLOW ON A FINISHED AGENT: Follow is drawn for a live agent
  // only, and F does nothing on a final record.
  const toggleFollow = useCallback(() => {
    if (!finished) setFollow((f) => !f)
  }, [finished])

  // THE KEYS: F follow, W wrap, E next error, / search. `[` stays the list
  // toggle (listSnap.ts). Never inside a field, never with a modifier, and
  // never while another control outside the log (a tab, a button, a link)
  // holds focus: a letter typed there is not addressed to the log.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target as HTMLElement | null
      if (t !== null && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      if (t instanceof Element && t.closest('button, a[href], [role="tab"], [role="separator"], [role="slider"]') !== null && !root.current?.contains(t)) return
      const k = e.key.toLowerCase()
      if (k === 'f') toggleFollow()
      else if (k === 'w') setWrap((w) => !w)
      else if (k === 'e') jump(targets.errors)
      else if (e.key === '/') {
        e.preventDefault()
        search.current?.focus()
      } else return
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggleFollow, jump, targets.errors])

  const onScroll = () => {
    const el = body.current
    if (el === null) return
    if (autoScroll.current) {
      autoScroll.current = false
      return
    }
    const atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 8
    if (follow && !atEnd) setFollow(false)
  }

  /*
   * ESCAPE IN MORE CLOSES MORE, AND STOPS THERE (N11, owner QA 2026-10-04).
   * An open `<details>` has no Escape of its own, so the key went on to the
   * split, whose Escape closes the whole agent and navigates to the list. The
   * innermost open thing closes: the menu folds, focus goes back to its
   * summary, and the key goes no further. A closed menu leaves the key alone.
   */
  const onMoreKey = (e: ReactKeyboardEvent<HTMLDetailsElement>) => {
    const d = more.current
    if (e.key !== 'Escape' || d === null || !d.open) return
    e.preventDefault()
    e.stopPropagation()
    d.open = false
    d.querySelector<HTMLElement>(':scope > summary')?.focus()
  }

  const marks: LogMarks = { needle, current, wrap, gaps }
  const hitAt = current !== null ? targets.hits.indexOf(current) : -1
  const errAt = current !== null ? targets.errors.indexOf(current) : -1
  const logs = shown === null ? null : okFeed(shown.logs)
  const transcriptRead = shown === null ? null : okFeed(shown.transcript)
  const source =
    view === 'transcript'
      ? (transcriptRead?.stream.source ?? null)
      : (logs?.streams.find((s) => streamsOf(view).includes(s.stream))?.source ?? null)

  const viewLabel = LOG_VIEWS.find((v) => v.id === view)?.label ?? view

  // THE SPILL (BAR_SPILL): measured after every render, reset whenever what
  // the row holds or the row's width changes, so it never stays spilled after
  // the room came back.
  const bar = useRef<HTMLDivElement | null>(null)
  const [spill, setSpill] = useState(0)
  const searching = needle.trim() !== ''
  const unmasked = logs !== null && !logs.redaction.applied_at_read_time
  const attemptsUnread = attempts.state.status === 'error'
  const maskWarn = masked !== null && masked > 0
  useLayoutEffect(() => {
    setSpill(0)
  }, [searching, maskWarn, unmasked, attemptsUnread, finished, view])
  useLayoutEffect(() => {
    const el = bar.current
    if (el === null || spill >= BAR_SPILL.row) return
    if (el.scrollWidth > el.clientWidth + 1) setSpill((n) => Math.min(n + 1, BAR_SPILL.row))
  })
  useEffect(() => {
    const el = bar.current
    if (el === null || typeof ResizeObserver === 'undefined') return
    let width = el.clientWidth
    const ro = new ResizeObserver(() => {
      // Only a change of WIDTH: wrapping changes the height, and resetting on
      // that would undo the wrap that caused it.
      if (el.clientWidth === width) return
      width = el.clientWidth
      setSpill(0)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const pausedAt = frozen === null ? null : clockTime(new Date(frozen.at).toISOString(), clock)
  const unreadNote = attemptsUnread && (
    <span className="ctl-sub ag-logs-unread">
      <Mark kind="unread" say="The attempt list did not load, so only the latest attempt can be picked." />{' '}
      <LogWords long="attempts not read" short="" />
    </span>
  )
  // THE CHIP SAYS IT WITHOUT A HOVER (N13, visual QA run 4): `Final record`
  // alone left a reader on a failed agent asking what it meant. The short
  // label holds to eight characters, as the bar's others do, so the chip
  // never pushes Error under More at the 380px split (qa.u11a.logs).
  // `Log done`, NOT `Complete` (visual QA run 5b, r4 D6 still seen): alone on
  // the narrow bar `Complete` did not say what was complete. It is the log.
  // A STATUS, NOT A BUTTON'S SHAPE (visual QA run 6, flow-find-failure-desktop-04):
  // the bordered pill sat in a row of buttons and read as one more. It is
  // drawn as the console's status mark -- the ended glyph and its words, no
  // outline (`is-mark`, styles/live.css) -- and its words stay the bar's
  // direct grandchildren, so the narrow bar still swaps them.
  const followControl = finished ? (
    <span
      className="ag-logs-final is-mark"
      role="note"
      aria-label="Agent ended: this is its complete log, so there is nothing to follow"
      title="This agent has finished: its log is the final record, and nothing more will arrive to follow."
    >
      <ToneMark tone="ended" /> <LogWords long="Ended · complete log" short="Log done" />
    </span>
  ) : (
    <Button size="sm" aria-pressed={follow} aria-keyshortcuts="F" onClick={toggleFollow}>
      <LogWords long={follow ? 'Following' : 'Paused · Follow'} short={follow ? 'Follow' : 'Paused'} /> <kbd>F</kbd>
    </Button>
  )
  const errN = targets.errors.length
  const errK = errAt >= 0 ? errAt + 1 : errN === 1 ? 1 : null
  // NO COUNT BEFORE THE READ (QA1 overview-agents-32, 2026-10-28): while the
  // log was still being read the bar said `0 err ln`, then `Error 1/1` on a
  // FAILED agent once it landed: a count nobody had taken, drawn as zero.
  // Until the window on screen has been read there is no figure at all.
  const errCounted = shown !== null
  const errWords = !errCounted
    ? { long: 'Errors · reading', short: 'Errors' }
    : errN === 0
      ? { long: 'No error matched', short: '0 err ln' }
      : errK === null
        ? { long: `${errN} error lines`, short: `${errN} err ln` }
        : { long: `Error ${errK} of ${errN}`, short: `Error ${errK}/${errN}` }
  const errorButton = (
    <Button
      size="sm"
      disabled={targets.errors.length === 0}
      aria-keyshortcuts="E"
      onClick={() => jump(targets.errors)}
      title={`${errCounted ? '' : 'Not counted yet: the log is still being read. '}Matches a transcript tool result or final result marked is_error, and lines with ${ERROR_PATTERN_SAYS}. The API marks no line as an error, so this is the console's own pattern: it can miss an error that says none of these, and match a line that only quotes one.`}
    >
      <LogWords
        // LINES, NOT ERRORS (QA-115, run 2). The narrow `Errors 0` on a
        // task drawn FAILED read as "nothing went wrong"; what it counts is
        // log lines the console's pattern matched, and the failure is often
        // in none. The wide label keeps its length: one more character
        // spills the button under More at the 380px split (qa.u11a.logs).
        //
        // NOT `No match` EITHER (ff-010, task-022, visual QA run 3): beside
        // the search box it read as a search that found nothing, before
        // anything was searched. The narrow label says what it counts --
        // error lines -- and that the count is zero, in the eight characters
        // `No match` had: a ninth spills Error under More at the 380px split.
        //
        // NO DASH (visual QA run 6, flow-find-failure-desktop-04, box
        // 6343764730): `Error –/1` read as a figure missing. With one line
        // there is only one to be at, so it reads `Error 1 of 1`; with several
        // and none jumped to, it says how many, in the zero count's shape
        // (`N err ln`, N13); after a jump it says which.
        long={errWords.long}
        short={errWords.short}
      />{' '}
      <kbd>E</kbd>
    </Button>
  )
  const attemptPicker = (
    <label className="ag-logs-attempt">
      <span className="ag-logs-attempt-k">Attempt</span>
      <select
        value={attemptId ?? ''}
        onChange={(e) => {
          setAttemptId(e.target.value === '' ? null : e.target.value)
          setCurrent(null)
        }}
        aria-label="Attempt, by generation"
      >
        <option value="">{currentRow === null ? 'latest' : `current · gen ${currentRow.generation} · ${currentRow.attempt_id} · ${attemptEnd(currentRow, attemptOpen(currentRow))}`}</option>
        {(attemptRows ?? [])
          .slice()
          .sort((a, b) => b.generation - a.generation)
          .filter((a) => a !== currentRow)
          .map((a) => (
            <option key={a.attempt_id} value={a.attempt_id}>
              gen {a.generation} · {a.attempt_id} · {attemptEnd(a, attemptOpen(a))}
            </option>
          ))}
      </select>
    </label>
  )
  // THE OTHER END OF A FINISHED TRANSCRIPT (BT-018): it opens on its newest
  // window, whose `earlier steps are not in this window` is answered here.
  const windowPick = finished && view === 'transcript' && (fromStart || transcriptRead?.window_starts_mid_stream === true) && (
    <Button size="sm" onClick={() => setFromStart((f) => !f)}>
      {fromStart ? 'Read the newest window' : 'Read from the first window'}
    </Button>
  )
  const wrapButton = (
    <Button size="sm" aria-pressed={wrap} aria-keyshortcuts="W" onClick={() => setWrap((w) => !w)}>
      Wrap <kbd>W</kbd>
    </Button>
  )

  return (
    <LogMarksContext.Provider value={marks}>
      <section ref={root} className="ag-logs" aria-label="Logs" data-follow={follow ? 'on' : 'off'}>
        {/* ONE ROW OF CONTROLS. The four streams are a segmented control that
            is never scrolled or spilled (D13's hidden streams); the search
            gives way; then Follow, Error, the attempt picker and Wrap, which
            move into More while the row overflows. */}
        <div ref={bar} className={`ag-logs-bar${spill >= BAR_SPILL.row ? ' is-wrap' : ''}`} data-spill={spill}>
          <Segmented
            label="Which log"
            className="ag-logs-streams"
            small
            value={view}
            options={LOG_VIEWS.map((o) => ({ key: o.id, title: o.label, label: <LogWords long={o.label} short={SHORT_VIEW[o.id]} /> }))}
            onChange={(k) => setView(k)}
          />
          <label className="ag-logs-search">
            <input
              ref={search}
              type="search"
              value={needle}
              // `Search…`, NOT THE WHOLE PHRASE (G2-15, QA 2026-10-07): at the
              // 1440 split the box cut `Search this window` to `Search this
              // wind`. The phrase stays the title and the accessible name; the
              // box's floor is unchanged, so the row spills nothing new.
              placeholder="Search…"
              title="Search this window: the lines this read holds, not the whole stream"
              aria-label="Search this window"
              onChange={(e) => {
                setNeedle(e.target.value)
                setCurrent(null)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  jump(targets.hits)
                }
              }}
            />
            <kbd>/</kbd>
          </label>
          {searching && (
            <span className="ag-logs-count" aria-live="polite">
              <LogWords
                long={
                  targets.hits.length === 0
                    ? 'none in this window'
                    : `${hitAt < 0 ? '–' : hitAt + 1} of ${targets.hits.length} · this window`
                }
                short={targets.hits.length === 0 ? '0 found' : `${hitAt < 0 ? '–' : hitAt + 1}/${targets.hits.length}`}
              />
            </span>
          )}
          {followControl}
          {spill < BAR_SPILL.error && errorButton}
          {spill < BAR_SPILL.attempt && attemptPicker}
          {spill < BAR_SPILL.wrap && wrapButton}
          {/* A MASK IS A WARNING, so it is never only under More; nor is a
              read the server did not mask (U10a review). */}
          {maskWarn && <span className="art-masked is-warn ag-logs-maskchip">masked {masked}</span>}
          {unmasked && (
            <span className="art-masked is-warn ag-logs-maskchip" title="The server did not apply its masking rules to this read.">
              not masked
            </span>
          )}
          {spill < BAR_SPILL.unread && unreadNote}
          <details ref={more} className="ag-logs-more" onKeyDown={onMoreKey}>
            <summary>More</summary>
            <div className="ag-logs-menu">
              {spill >= BAR_SPILL.error && errorButton}
              {spill >= BAR_SPILL.attempt && attemptPicker}
              {spill >= BAR_SPILL.wrap && wrapButton}
              {spill >= BAR_SPILL.unread && unreadNote}
              {windowPick}
              <ul className="ctl-facts ag-logs-facts">
                {logs !== null && (logs.attempt.status === 'latest' || logs.attempt.status === 'requested') && (
                  <li className="ctl-fact">
                    <b>attempt</b>
                    {/* THE ID ONCE (G2-16, QA 2026-10-07): it read `attempt
                        attempt att_8f8e… · att_8f8e…`, the line already
                        naming the id and a span adding it again, and the
                        second ran past the popover. With the attempts read
                        it is `attempt 1 · gen 1 · att_8f8e…` with a copy. */}
                    {picked !== null ? (
                      <AttemptFact row={picked} ordinal={attemptOrdinal(attemptRows ?? [], picked)} />
                    ) : (
                      attemptLine(logs).toLowerCase()
                    )}
                  </li>
                )}
                {/* FACTS ONLY FOR A WINDOW THAT WAS READ (#222): no source and no
                    masked count over an object that does not exist -- the view
                    below says absent, not applicable or not read in words. */}
                {source !== null && (
                  <li className="ctl-fact">
                    <b>source</b>
                    {source === 'live' ? 'live tail' : source === 'final' ? 'final record' : 'the artifact'}
                    {/* THE PAGE'S ONE CLOCK (N14, visual QA run 4): 24-hour, the
                        ISO time and its age as the title, as the head's `ended`
                        is -- `toLocaleTimeString()` drew `9:25:53 PM` beside it.
                        And what paused means: following stopped then, and what
                        arrived since is the count under the log. */}
                    {!follow && frozen !== null && pausedAt !== null && (
                      <span className="ctl-sub ag-logs-paused">
                        {' '}
                        · following paused at{' '}
                        <time dateTime={new Date(frozen.at).toISOString()} title={pausedAt.title}>
                          {pausedAt.text}
                        </time>
                        ; anything newer is counted below
                      </span>
                    )}
                  </li>
                )}
                {masked !== null && (
                  <li className="ctl-fact">
                    <b>masked</b>
                    <span className={`art-masked${masked > 0 ? ' is-warn' : ''}`}>{masked} in this window</span>
                  </li>
                )}
                {logs !== null && (
                  <li className="ctl-fact">
                    <b>masking</b>
                    <span className={`art-masked${logs.redaction.applied_at_read_time ? '' : ' is-warn'}`}>
                      {logs.redaction.applied_at_read_time ? 'at read time' : 'not applied at read time'}
                    </span>
                  </li>
                )}
              </ul>
            </div>
          </details>
        </div>

        {/* THE CLI'S SUCCESS IS NOT THE TASK'S (QA-115, run 2). A failed
            task's transcript can end `result · success · 7 turns`, and with
            `No error line` beside it the log read as a run that went well.
            Above the body, not in it, so a log scrolled to its end still
            has it in view; it names the error the task did write. */}
        {task.state === 'FAILED' && view === 'transcript' && endsInReportedSuccess(shown) && (
          <p className="ag-gap ag-failnote" role="note">
            This agent failed after its CLI reported success: the <b>result</b> at the end of this transcript is the
            CLI&rsquo;s own verdict, not why the agent failed.{' '}
            {task.last_error ? (
              <>
                The error it wrote: <code className="mono">{firstLine(task.last_error)}</code>
              </>
            ) : (
              'It wrote no error text.'
            )}
          </p>
        )}
        <div className={`ag-logs-body${wrap ? ' is-wrap' : ''}`} ref={body} onScroll={onScroll} aria-label={`${viewLabel} log`}>
          {view === 'transcript' && <GapNotice name="transcript" />}
          {shown === null ? (
            <p className="art-loading">
              <Mark kind="pending" say={`Reading ${viewLabel.toLowerCase()} for ${picked ? `gen ${picked.generation}` : 'the latest attempt'}. The read is in flight.`} />
              <span className="ctl-pending art-loading-bar" />
            </p>
          ) : (
            <LogBody key={fromStart ? 'start' : 'end'} v={shown} view={view} reading={{ fetchedAt: shownAt ?? 0, pollMs: moving ? ARTIFACTS_POLL_MS : null }} now={clock} />
          )}
        </div>

        {!follow && !finished && arrived !== null && arrived.lines > 0 && (
          <button type="button" className="ag-logs-arrived" onClick={() => setFollow(true)}>
            ↓ {arrived.exact ? '' : 'about '}
            {arrived.lines} new {view === 'transcript' ? 'step' : 'line'}
            {arrived.lines === 1 ? '' : 's'} since you paused · Follow
          </button>
        )}
      </section>
    </LogMarksContext.Provider>
  )
}

/**
 * THE DETAILS TAB'S ONE LOG LINE (owner decision 2026-10-04): the newest line
 * of the agent's default stream and `Open logs`, which switches to the Logs
 * tab -- no overlay, nothing drawn over the details. It reads the latest
 * attempt as the tab does, at the same cadence while anything can change.
 */
export function LogLastLine({ task, onOpen }: { task: Task; onOpen: () => void }) {
  const [tick, setTick] = useState(0)
  const held = useRead<LogFeed>(() => readFeed(task, null, false), task.id, `${tick}`, null)
  const feed = okFeed(held.state)
  const clock = useNow(ARTIFACTS_POLL_MS)
  const moving = stillMoving(task, feed === null ? null : { ...feed, task }, clock)
  useEffect(() => {
    if (!moving) return
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      setTick((n) => n + 1)
    }, ARTIFACTS_POLL_MS)
    return () => clearInterval(id)
  }, [moving])
  const view = defaultView(task.runner_profile)
  const line = lastLineOf(feed, view)
  // AN HONEST LINE WHEN THERE IS NONE (owner QA R9): the strip was empty.
  // A live agent has written nothing YET; a finished one wrote nothing this
  // read holds.
  const said =
    line ??
    (held.state.status === 'loading'
      ? 'reading…'
      : held.state.status === 'error'
        ? 'the log was not read'
        : TERMINAL_STATES.has(task.state)
          ? 'no log lines in this read'
          : 'no log lines yet')
  // NOT THE CAUSE (QA-115 follow-up, the owner's choice of 2026-10-20). On a
  // failed task this line sits under the Failed card's error, and `Last log
  // line: result ## Summary` read as why it failed. It is only the newest
  // line, so there it says so; on any other task the label is unchanged.
  //
  // UNLESS IT IS THE ERROR (QA1 overview-agents-40, 2026-10-28): a failed
  // agent whose last step is its CLI's error result read `Last log line (not
  // the error)` over `result exit status 2: pytest was interrupted…`, the
  // error word for word. When the line carries the error's first line, the
  // label says it is the error, as logged.
  const isTheError = task.state === 'FAILED' && line !== null && sameAsError(line, task.last_error)
  const label =
    task.state !== 'FAILED' ? 'Last log line' : isTheError ? 'Last log line (the error, as logged)' : 'Last log line (not the error)'
  // AND NOT A SUCCESS LINE UNDER A FAILURE (QA-115, visual QA run 2). The
  // label alone did not hold: `result ## Summary` under the error was still
  // read as its cause. When the line is the CLI's own success result, the
  // strip says what that line means instead of quoting it; the line itself
  // stays in the title and is one click away in Logs.
  const succeededThenFailed = task.state === 'FAILED' && view === 'transcript' && line !== null && endsInReportedSuccess(feed)
  return (
    <button type="button" className="ag-loglast" onClick={onOpen}>
      <b>{label}</b>
      <span className={`ag-loglast-line${line === null || succeededThenFailed ? ' is-absent' : ' mono'}`} title={line ?? undefined}>
        {/* A transcript line is the agent's markdown: its code spans are
            drawn as code, never as raw backticks (owner QA R9). */}
        {succeededThenFailed
          ? 'its CLI reported success · it failed after that, with the error above'
          : line !== null && view === 'transcript'
            ? <MarkdownInline text={line} />
            : said}
      </span>
      <span className="ag-loglast-open">Open logs ›</span>
    </button>
  )
}

/** The first non-blank line of an error, for a one-line note; the whole error is on Details. */
function firstLine(text: string): string {
  return text.split('\n').find((l) => l.trim() !== '') ?? text
}

/**
 * Whether a log line carries the task's error: the error's first non-blank
 * line, whitespace folded, inside the line (a transcript line is `<kind>
 * <text>`). No error text is never a match.
 */
export function sameAsError(line: string, lastError: string | null | undefined): boolean {
  if (typeof lastError !== 'string') return false
  const fold = (t: string) => t.replace(/\s+/g, ' ').trim()
  const err = fold(firstLine(lastError))
  return err !== '' && fold(line).includes(err)
}
