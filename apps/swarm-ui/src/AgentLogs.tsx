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
  linesOf,
  tailGap,
  windowAt,
  type Arrived,
  type Gap,
  type GapUnknown,
  type WindowAt,
} from './logLines'
import { MarkdownInline } from './ArtifactViewer'
import { GapNotice, LogMarksContext, type LogMarks } from './logMarks'
import { attemptLine, useRead } from './RunFiles'
import { TERMINAL_STATES, type AttemptRow, type LogStream, type Task } from './types'
import { useNow } from './useNow'
import { Button, Segmented } from './components'

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
 * fills the whole detail area under the header with nothing beneath it. A
 * running agent opens on it (AgentSplit); Details keeps a one-line last log
 * line that switches here (`LogLastLine`).
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

/** One read of an attempt's logs: the transcript and the polled streams, each with its own answer. */
async function readFeed(task: Task, attemptId: string | null): Promise<Result<LogFeed>> {
  const attempt = attemptId === null ? {} : { attemptId }
  const [transcript, logs] = await Promise.all([
    loadTranscript(task.id, { ...attempt, source: 'auto' }),
    loadTaskLogs(task.id, { ...attempt, stream: POLLED_STREAMS, source: 'auto' }),
  ])
  return { status: 'ok', data: { task, attemptId, transcript, logs }, fetchedAt: Date.now() }
}

/**
 * The newest line a view holds in a read, or null when the window has none.
 * A BLANK LINE IS NO LINE (owner QA R9, 2026-10-04): a window ending in an
 * empty line drew the Details strip with nothing in it, which reads as a
 * line nobody can see rather than as no line at all.
 */
export function lastLineOf(feed: LogFeed | null, view: LogView): string | null {
  if (feed === null) return null
  if (view === 'transcript') {
    const steps = okFeed(feed.transcript)?.steps ?? []
    const s = steps[steps.length - 1]
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
 * HOW FAR THE BAR HAS SPILLED INTO MORE (U10a D13 review; N11 at the 380px
 * split). The row never wraps by design: the four streams, the search, Follow,
 * Error, the attempt picker and Wrap are one row. The labels shorten first, by
 * a container query on the bar (styles/agents.css); then, while the row still
 * overflows, these move into More one at a time, least used first: the
 * attempts-unread note, Wrap, the attempt picker, Error, Follow. Past the last
 * step the row wraps rather than hide a control. The four streams never
 * spill: which log is on screen is the one thing the bar must always say.
 */
export const BAR_SPILL = { unread: 1, wrap: 2, attempt: 3, error: 4, follow: 5, row: 6 } as const

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

function attemptEnd(a: AttemptRow): string {
  if (a.completed_at === null) return 'running'
  return a.exit_code === null ? 'ended' : `exit ${a.exit_code}`
}

export function AgentLogs({ task }: { task: Task }) {
  const [view, setView] = useState<LogView>(() => defaultView(task.runner_profile))
  const [attemptId, setAttemptId] = useState<string | null>(null)
  const [needle, setNeedle] = useState('')
  const [wrap, setWrap] = useState(false)
  const [current, setCurrent] = useState<string | null>(null)
  const [targets, setTargets] = useState<{ hits: string[]; errors: string[] }>({ hits: [], errors: [] })
  const [tick, setTick] = useState(0)
  const body = useRef<HTMLDivElement | null>(null)
  const search = useRef<HTMLInputElement | null>(null)
  const more = useRef<HTMLDetailsElement | null>(null)
  const autoScroll = useRef(false)
  const root = useRef<HTMLElement | null>(null)
  const finished = TERMINAL_STATES.has(task.state)

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

  // THE READ. One transcript read and one polled-streams read per tick, for
  // the picked attempt. Each keeps its own answer.
  const readKey = `${readId ?? 'latest'}:${tick}`
  const held = useRead<LogFeed>(() => readFeed(task, readId), taskId, readKey, null)
  // ONLY A READ OF THE PICKED ATTEMPT IS ITS LOG. `useRead` keeps the last
  // answer on screen until the next lands, which for a new pick is the
  // previous attempt's -- so that one is "reading", never shown as this one.
  const landed = okFeed(held.state)
  const latest = landed !== null && landed.attemptId === readId ? landed : null
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
  const [frozen, setFrozen] = useState<{ feed: LogFeed; at: number } | null>(null)
  useEffect(() => {
    if (follow) setFrozen(null)
    else if (feedNow !== null) {
      setFrozen((f) => (f !== null && f.feed.attemptId === feedNow.attemptId ? f : { feed: feedNow, at: latestAt ?? Date.now() }))
    }
    // On the switch, and on the first read of a newly picked attempt; `feedNow`
    // is rebuilt each render, so its identity is not the dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [follow, latest === null, readId])
  const shown =
    follow || frozen === null || frozen.feed.attemptId !== readId ? feedNow : { ...frozen.feed, task }
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
  useLayoutEffect(() => {
    const el = body.current
    if (el === null) return
    const keys = (sel: string) => [...el.querySelectorAll<HTMLElement>(sel)].map((n) => n.dataset.logKey ?? '').filter(Boolean)
    const hits = keys('[data-log-key].is-hit')
    const errors = keys('[data-log-key].is-err')
    setTargets((cur) =>
      cur.hits.join('|') === hits.join('|') && cur.errors.join('|') === errors.join('|') ? cur : { hits, errors },
    )
  })

  // FOLLOWING: keep the newest line in view after each read. Only the log's
  // own body scrolls: the page and the column never move.
  useLayoutEffect(() => {
    const el = body.current
    if (!follow || el === null) return
    autoScroll.current = true
    el.scrollTop = el.scrollHeight
  }, [follow, shown, view])

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

  const unreadNote = attemptsUnread && (
    <span className="ctl-sub ag-logs-unread">
      <Mark kind="unread" say="The attempt list did not load, so only the latest attempt can be picked." />{' '}
      <LogWords long="attempts not read" short="" />
    </span>
  )
  const followControl = finished ? (
    <span
      className="ag-logs-final"
      title="This agent has finished: its log is the final record, and nothing more will arrive to follow."
    >
      Final record
    </span>
  ) : (
    <Button size="sm" aria-pressed={follow} aria-keyshortcuts="F" onClick={toggleFollow}>
      <LogWords long={follow ? 'Following' : 'Paused · Follow'} short={follow ? 'Follow' : 'Paused'} /> <kbd>F</kbd>
    </Button>
  )
  const errorButton = (
    <Button
      size="sm"
      disabled={targets.errors.length === 0}
      aria-keyshortcuts="E"
      onClick={() => jump(targets.errors)}
      title={`Matches a transcript tool result or final result marked is_error, and lines with ${ERROR_PATTERN_SAYS}. The API marks no line as an error, so this is the console's own pattern: it can miss an error that says none of these, and match a line that only quotes one.`}
    >
      <LogWords
        long={targets.errors.length === 0 ? 'No error matched' : `Error ${errAt < 0 ? '–' : errAt + 1} of ${targets.errors.length}`}
        short={targets.errors.length === 0 ? 'Errors 0' : `Error ${errAt < 0 ? '–' : errAt + 1}/${targets.errors.length}`}
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
        <option value="">{currentRow === null ? 'latest' : `current · gen ${currentRow.generation} · ${currentRow.attempt_id} · ${attemptEnd(currentRow)}`}</option>
        {(attemptRows ?? [])
          .slice()
          .sort((a, b) => b.generation - a.generation)
          .filter((a) => a !== currentRow)
          .map((a) => (
            <option key={a.attempt_id} value={a.attempt_id}>
              gen {a.generation} · {a.attempt_id} · {attemptEnd(a)}
            </option>
          ))}
      </select>
    </label>
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
              placeholder="Search this window"
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
          {spill < BAR_SPILL.follow && followControl}
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
              {spill >= BAR_SPILL.follow && followControl}
              {spill >= BAR_SPILL.error && errorButton}
              {spill >= BAR_SPILL.attempt && attemptPicker}
              {spill >= BAR_SPILL.wrap && wrapButton}
              {spill >= BAR_SPILL.unread && unreadNote}
              <ul className="ctl-facts ag-logs-facts">
                {logs !== null && (logs.attempt.status === 'latest' || logs.attempt.status === 'requested') && (
                  <li className="ctl-fact">
                    <b>attempt</b>
                    {attemptLine(logs).toLowerCase()}
                    {picked !== null && <span className="mono"> · {picked.attempt_id}</span>}
                  </li>
                )}
                {/* FACTS ONLY FOR A WINDOW THAT WAS READ (#222): no source and no
                    masked count over an object that does not exist -- the view
                    below says absent, not applicable or not read in words. */}
                {source !== null && (
                  <li className="ctl-fact">
                    <b>source</b>
                    {source === 'live' ? 'live tail' : source === 'final' ? 'final record' : 'the artifact'}
                    {!follow && frozen !== null && (
                      <span className="ctl-sub"> · paused {new Date(frozen.at).toLocaleTimeString()}</span>
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

        <div className={`ag-logs-body${wrap ? ' is-wrap' : ''}`} ref={body} onScroll={onScroll} aria-label={`${viewLabel} log`}>
          {view === 'transcript' && <GapNotice name="transcript" />}
          {shown === null ? (
            <p className="art-loading">
              <Mark kind="pending" say={`Reading ${viewLabel.toLowerCase()} for ${picked ? `gen ${picked.generation}` : 'the latest attempt'}. The read is in flight.`} />
              <span className="ctl-pending art-loading-bar" />
            </p>
          ) : (
            <LogBody v={shown} view={view} reading={{ fetchedAt: shownAt ?? 0, pollMs: moving ? ARTIFACTS_POLL_MS : null }} now={clock} />
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
  const held = useRead<LogFeed>(() => readFeed(task, null), task.id, `${tick}`, null)
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
  return (
    <button type="button" className="ag-loglast" onClick={onOpen}>
      <b>Last log line</b>
      <span className={`ag-loglast-line${line === null ? ' is-absent' : ' mono'}`} title={line ?? undefined}>
        {/* A transcript line is the agent's markdown: its code spans are
            drawn as code, never as raw backticks (owner QA R9). */}
        {line !== null && view === 'transcript' ? <MarkdownInline text={line} /> : said}
      </span>
      <span className="ag-loglast-open">Open logs ›</span>
    </button>
  )
}
