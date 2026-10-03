import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react'

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
import { nudgePane } from './focus'
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
import { GapNotice, LogMarksContext, type LogMarks } from './logMarks'
import { clampPane, readPane, writePane, type PaneSpec } from './panes'
import { attemptLine, useRead } from './RunFiles'
import { TERMINAL_STATES, type AttemptRow, type LogStream, type Task } from './types'
import { useNow } from './useNow'
import { Button } from './components'

/**
 * THE LOG DOCK (viewers.html, pick A, 2026-10-02).
 *
 * The log docks along the bottom of the agent's detail column and stays open
 * while the reader moves between Details, Attempts, Artifacts and Checkpoints:
 * a log is what people watch while they read something else, so it gets the
 * one place in the split that can stay put. It spans the detail column only;
 * the list keeps its full height. It sits above the app's own API-reads strip
 * (Dock.tsx), which is unchanged.
 *
 * WHAT IT READS. `GET /v1/tasks/{id}/transcript` and `/logs` -- the agent's
 * stderr and the runner's two streams in one read -- for the attempt the
 * reader picked, by `attempt_id`. The Artifacts pane used to read both for
 * the latest attempt only, always; the picker is labelled by GENERATION,
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

/** The dock's height, remembered per viewer: 120px to 70% of the window, as the frames say. */
const LOG_DOCK: PaneSpec = { key: 'swarm.agents.logdock.h', min: 120, max: 720, initial: 300 }
/**
 * OPEN WHILE THE AGENT RUNS, FOLDED TO ITS ONE LINE ONCE IT HAS FINISHED
 * (walkthrough B, owner 2026-10-03). A live log is what people open an agent
 * to watch; a finished agent is opened to read what it did, and an open log
 * took the column's lower third from the Details that say so. This was one
 * open-or-folded preference for every agent, which opened every finished
 * agent with its log up. The HEIGHT is still remembered per device
 * (`LOG_DOCK`); whether it is open is the agent's state, and a reader's click
 * holds for the agent on screen.
 */
export function logOpenByDefault(task: Pick<Task, 'state'>): boolean {
  return !TERMINAL_STATES.has(task.state)
}

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

function attemptEnd(a: AttemptRow): string {
  if (a.completed_at === null) return 'running'
  return a.exit_code === null ? 'ended' : `exit ${a.exit_code}`
}

export function LogDock({ task, phone = false }: { task: Task; phone?: boolean }) {
  const [open, setOpenState] = useState(() => logOpenByDefault(task))
  const [full, setFull] = useState(false)
  const [height, setHeight] = useState(() => readPane(LOG_DOCK))
  const [view, setView] = useState<LogView>(() => defaultView(task.runner_profile))
  const [attemptId, setAttemptId] = useState<string | null>(null)
  const [needle, setNeedle] = useState('')
  const [wrap, setWrap] = useState(false)
  const [current, setCurrent] = useState<string | null>(null)
  const [targets, setTargets] = useState<{ hits: string[]; errors: string[] }>({ hits: [], errors: [] })
  const [tick, setTick] = useState(0)
  const body = useRef<HTMLDivElement | null>(null)
  const search = useRef<HTMLInputElement | null>(null)
  const dragging = useRef(false)
  const autoScroll = useRef(false)
  const root = useRef<HTMLElement | null>(null)

  const setOpen = useCallback((next: boolean) => setOpenState(next), [])

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
  const picked = attemptRows?.find((a) => a.attempt_id === attemptId) ?? null
  const latestRunning = attemptId === null && !TERMINAL_STATES.has(task.state)

  // THE READ. One transcript read and one polled-streams read per tick, for
  // the picked attempt. Each keeps its own answer.
  const readKey = `${attemptId ?? 'latest'}:${tick}`
  const held = useRead<LogFeed>(
    async () => {
      const attempt = attemptId === null ? {} : { attemptId }
      const [transcript, logs] = await Promise.all([
        loadTranscript(taskId, { ...attempt, source: 'auto' }),
        loadTaskLogs(taskId, { ...attempt, stream: POLLED_STREAMS, source: 'auto' }),
      ])
      return { status: 'ok', data: { task, attemptId, transcript, logs }, fetchedAt: Date.now() }
    },
    taskId,
    readKey,
    null,
  )
  // ONLY A READ OF THE PICKED ATTEMPT IS ITS LOG. `useRead` keeps the last
  // answer on screen until the next lands, which for a new pick is the
  // previous attempt's -- so that one is "reading", never shown as this one.
  const landed = okFeed(held.state)
  const latest = landed !== null && landed.attemptId === attemptId ? landed : null
  const latestAt = latest !== null && held.state.status === 'ok' ? held.state.fetchedAt : null
  // THE TASK ON THE FEED IS ALWAYS THE ONE ON SCREEN: the views read its
  // state to tell "not yet" from "never", and the split re-reads it.
  const feedNow = latest === null ? null : { ...latest, task }

  // THE CADENCE: every 5 s while anything can still change, as the Artifacts
  // pane re-read, and not while the browser tab is hidden.
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
  }, [follow, latest === null, attemptId])
  const shown =
    follow || frozen === null || frozen.feed.attemptId !== attemptId ? feedNow : { ...frozen.feed, task }
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
    const key = okFeed(latest.logs)?.attempt_id ?? attemptId ?? 'latest'
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
  }, [latest, attemptId])

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

  // FOLLOWING: keep the newest line in view after each read.
  useLayoutEffect(() => {
    const el = body.current
    if (!follow || el === null) return
    autoScroll.current = true
    el.scrollTop = el.scrollHeight
  }, [follow, shown, view, open, full])

  const jump = useCallback(
    (list: string[]) => {
      if (list.length === 0) return
      const i = current === null ? -1 : list.indexOf(current)
      const next = list[(i + 1) % list.length]!
      setCurrent(next)
      setFollow(false)
      const target = [...(body.current?.querySelectorAll<HTMLElement>('[data-log-key]') ?? [])].find((n) => n.dataset.logKey === next)
      target?.scrollIntoView?.({ block: 'center' })
    },
    [current],
  )

  const toggleFollow = useCallback(() => setFollow((f) => !f), [])

  // THE KEYS: F follow, W wrap, E next error, / search. `[` stays the list
  // toggle (listSnap.ts). Never inside a field, never with a modifier, and
  // never while another control outside the dock (a tab, a button, a link)
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
        if (!open) setOpen(true)
        search.current?.focus()
      } else return
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggleFollow, jump, targets.errors, open, setOpen])

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

  const ceiling = () => Math.max(LOG_DOCK.min, Math.round((globalThis.innerHeight || LOG_DOCK.max) * 0.7))
  const onGripMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return
    const top = e.currentTarget.parentElement?.getBoundingClientRect().bottom ?? globalThis.innerHeight
    setHeight(clampPane(top - e.clientY, LOG_DOCK.min, ceiling()))
  }
  const endGrip = () => {
    if (!dragging.current) return
    dragging.current = false
    writePane(LOG_DOCK, height)
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
  const lastLine = useMemo(() => {
    if (shown === null) return null
    if (view === 'transcript') {
      const steps = transcriptRead?.steps ?? []
      const s = steps[steps.length - 1]
      return s === undefined ? null : `${s.kind} ${(s.text ?? s.tool?.name ?? '').split('\n')[0]}`
    }
    const rows = (logs?.streams ?? []).filter((s) => streamsOf(view).includes(s.stream))
    const text = rows.map((s) => s.content ?? '').join('\n')
    const lines = linesOf(text)
    return lines.length === 0 ? null : lines[lines.length - 1]!
  }, [shown, view, transcriptRead, logs])

  const viewLabel = LOG_VIEWS.find((v) => v.id === view)?.label ?? view
  // ON A PHONE THE DOCK IS A SHEET: its one line, or the whole screen.
  const expanded = phone ? full : open || full
  const cls = ['ag-logdock', expanded ? 'is-open' : 'is-strip', full ? 'is-full' : '', phone ? 'is-phone' : '']
    .filter(Boolean)
    .join(' ')

  return (
    <LogMarksContext.Provider value={marks}>
      <section
        ref={root}
        className={cls}
        aria-label="Log"
        style={expanded && !full ? { height } : undefined}
        data-follow={follow ? 'on' : 'off'}
      >
        {expanded && !full && (
          <div
            className="ag-logdock-grip"
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize the log"
            tabIndex={0}
            aria-valuenow={height}
            aria-valuemin={LOG_DOCK.min}
            aria-valuemax={ceiling()}
            onKeyDown={(e) => {
              const next = nudgePane(height, e.key, { min: LOG_DOCK.min, max: ceiling() }, 'ArrowUp')
              if (next === null) return
              e.preventDefault()
              setHeight(next)
              writePane(LOG_DOCK, next)
            }}
            onPointerDown={(e) => {
              dragging.current = true
              e.currentTarget.setPointerCapture?.(e.pointerId)
            }}
            onPointerMove={onGripMove}
            onPointerUp={endGrip}
            onPointerCancel={endGrip}
          />
        )}

        {!expanded ? (
          // THE ONE-LINE STRIP, and on a phone the bottom sheet: the newest
          // line, and a tap opens the log.
          <button type="button" className="ag-logdock-line" aria-expanded={false} onClick={() => (phone ? setFull(true) : setOpen(true))}>
            <span className="ag-logdock-src">{source === 'live' ? 'live' : (source ?? 'log')}</span>
            <b>Log</b>
            <span className="ag-logdock-last mono">{lastLine ?? (held.state.status === 'loading' ? 'reading…' : 'no line in this window')}</span>
            <span className="ag-logdock-facts">
              {follow ? 'Following' : 'Paused'} · errors {targets.errors.length} · masked {masked ?? '—'}
            </span>
          </button>
        ) : (
          <>
            <div className="ag-logdock-head">
              {full && (
                <button type="button" className="ag-logdock-back" onClick={() => setFull(false)}>
                  ‹ Agent
                </button>
              )}
              <h2>Log</h2>
              <div className="ag-logdock-streams" role="group" aria-label="Which log">
                {LOG_VIEWS.map((o) => (
                  <button key={o.id} type="button" aria-pressed={view === o.id} onClick={() => setView(o.id)}>
                    {o.label}
                  </button>
                ))}
              </div>
              <label className="ag-logdock-attempt">
                Attempt{' '}
                <select
                  value={attemptId ?? ''}
                  onChange={(e) => {
                    setAttemptId(e.target.value === '' ? null : e.target.value)
                    setCurrent(null)
                  }}
                  aria-label="Attempt, by generation"
                >
                  <option value="">latest</option>
                  {(attemptRows ?? [])
                    .slice()
                    .sort((a, b) => b.generation - a.generation)
                    .map((a) => (
                      <option key={a.attempt_id} value={a.attempt_id}>
                        gen {a.generation} · {a.attempt_id} · {attemptEnd(a)}
                      </option>
                    ))}
                </select>
                {attempts.state.status === 'error' && (
                  <span className="ctl-sub">
                    {' '}
                    <Mark kind="unread" say="The attempt list did not load, so only the latest attempt can be picked." /> attempts
                    not read
                  </span>
                )}
              </label>
              {!full && (
                <Button size="sm" onClick={() => setFull(true)}>
                  Open full
                </Button>
              )}
              {!full && (
                <Button size="sm" aria-expanded={true} onClick={() => setOpen(false)}>
                  Fold
                </Button>
              )}
            </div>

            <div className="ag-logdock-tools">
              <label className="ag-logdock-search">
                <input
                  ref={search}
                  type="search"
                  value={needle}
                  placeholder="Search this window"
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
              {needle.trim() !== '' && (
                <span className="ag-logdock-count" aria-live="polite">
                  {targets.hits.length === 0
                    ? 'none in this window'
                    : `${hitAt < 0 ? '–' : hitAt + 1} of ${targets.hits.length} · this window`}
                </span>
              )}
              <Button size="sm" aria-pressed={follow} aria-keyshortcuts="F" onClick={toggleFollow}>
                {follow ? 'Following' : 'Paused · Follow'} <kbd>F</kbd>
              </Button>
              <Button size="sm" aria-pressed={wrap} aria-keyshortcuts="W" onClick={() => setWrap((w) => !w)}>
                Wrap <kbd>W</kbd>
              </Button>
              <Button
                size="sm"
                disabled={targets.errors.length === 0}
                aria-keyshortcuts="E"
                onClick={() => jump(targets.errors)}
                title={`Matches a transcript tool result marked is_error, and lines with ${ERROR_PATTERN_SAYS}. The API marks no line as an error, so this is the console's own pattern: it can miss an error that says none of these, and match a line that only quotes one.`}
              >
                {targets.errors.length === 0
                  ? 'No error matched'
                  : `Error ${errAt < 0 ? '–' : errAt + 1} of ${targets.errors.length}`}{' '}
                <kbd>E</kbd>
              </Button>
            </div>

            <ul className="ctl-facts ag-logdock-facts">
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

            <div className={`ag-logdock-body${wrap ? ' is-wrap' : ''}`} ref={body} onScroll={onScroll} aria-label={`${viewLabel} log`}>
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

            {!follow && arrived !== null && arrived.lines > 0 && (
              <button type="button" className="ag-logdock-arrived" onClick={() => setFollow(true)}>
                ↓ {arrived.exact ? '' : 'about '}
                {arrived.lines} new {view === 'transcript' ? 'step' : 'line'}
                {arrived.lines === 1 ? '' : 's'} since you paused · Follow
              </button>
            )}
          </>
        )}
      </section>
    </LogMarksContext.Provider>
  )
}
