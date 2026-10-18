import { useEffect, useState } from 'react'

import { loadStartup, loadTaskEventsPage, type StartupRead } from './api'
import { CONCURRENCY_STATES, type TaskEvent, type TaskState } from './types'
import './styles/live.css'

/**
 * A BROWSER AGENT'S START-UP, AS A BAR WITH NAMED STEPS (owner request
 * 2026-10-13: "a progress bar when the browser agent is starting up the
 * browser so you get a sense of progress as it comes up"; detail "Stages +
 * real pod steps"; drawn on the Browser tab, the workflow graph's live
 * thumbnail and the theater).
 *
 * WHAT IT READS. `GET /v1/tasks/{id}/startup` (lane STARTUP-API): the current
 * attempt's `lease_acquired`, `dispatched`, `starting`, `running` and every
 * `startup_progress` step the pod reported, and the profile's median time
 * from `dispatched` to each step. Every 2 s, only while the start-up is not
 * done and the tab is visible; a done answer stops the polling for good.
 *
 * A 404 IS THE ROUTE NOT BEING DEPLOYED YET, and the bar still moves: it is
 * drawn from the task's own events, which carry the four lifecycle steps and
 * none of the pod's, so it shows only the stages those four can fill. Any
 * other failure falls back the same way rather than freezing the bar.
 *
 * A WARM START SKIPS STEPS, AND THE BAR SAYS SO (LIVE-UNUSED, #1030).
 * MEASURED 2026-10-13 13:3x UTC on task_84cb2038aa704c829d48: the node was
 * warm and the image cached, and no `pod_scheduled`, `image_pulling`,
 * `image_pulled` or `container_started` reached the task. A stage none of
 * whose steps was ever seen, with a later stage reached, is `skipped`
 * ("already ready"), drawn with a check and counted as done in the fill, so
 * the bar fills by the latest step reached and nothing waits forever on a
 * step that is not coming.
 *
 * THE ESTIMATE NEVER COUNTS BELOW ZERO. Past the median it says "taking longer
 * than usual"; with no history (`sample_size` 0) it says nothing at all,
 * because a guess drawn as a number is read as a promise.
 */

/** Poll cadence while starting: the pod's steps are seconds apart. */
export const STARTUP_POLL_MS = 2000

/** The stages the owner named, each the raw steps that fill it, in order. */
export const STARTUP_STAGES: readonly { key: string; label: string; steps: readonly string[] }[] = [
  { key: 'capacity', label: 'Capacity reserved', steps: ['lease_acquired', 'dispatched'] },
  { key: 'machine', label: 'Machine found', steps: ['pod_scheduled'] },
  { key: 'image', label: 'Downloading browser image', steps: ['image_pulling', 'image_pulled'] },
  { key: 'agent', label: 'Starting the agent', steps: ['container_started', 'starting', 'running'] },
  { key: 'chromium', label: 'Starting Chromium', steps: ['browser_launching', 'browser_ready'] },
  { key: 'live', label: 'Live', steps: ['first_frame'] },
]

/** The steps the task's events carry without the startup route: what the fallback can fill. */
const LIFECYCLE_STEPS: ReadonlySet<string> = new Set(['lease_acquired', 'dispatched', 'starting', 'running'])

/** What the bar draws, from either source. `fallback` is true when it came from the events. */
export interface StartupData {
  steps: { step: string; at: string }[]
  estimate: Partial<Record<string, number>>
  sampleSize: number
  done: boolean
  fallback: boolean
}

export interface StageView {
  key: string
  label: string
  /** `skipped`: none of its steps was reported, and a later stage was reached -- it was already ready. */
  status: 'done' | 'skipped' | 'current' | 'todo'
  /** Seconds the stage took (done) or has taken so far (current); null when no step of it was seen. */
  seconds: number | null
}

export interface StartupView {
  stages: StageView[]
  /** 0-100, by stage: the share of stages done. */
  percent: number
  /** Seconds since the first step, for the thumbnail's "starting 0:41". */
  elapsed: number
  /** "~N s left", "taking longer than usual", or null when there is no history. */
  remaining: string | null
  done: boolean
}

/** `m:ss`, never negative. */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/** The view at `now` (ms): pure, so the stage rules are tested without a timer. */
export function startupView(data: StartupData, now: number): StartupView {
  const at = new Map<string, number>()
  for (const s of data.steps) {
    const t = Date.parse(s.at)
    if (Number.isFinite(t) && !at.has(s.step)) at.set(s.step, t)
  }
  const stages = data.fallback ? STARTUP_STAGES.filter((g) => g.steps.some((s) => LIFECYCLE_STEPS.has(s))) : STARTUP_STAGES
  const seen = stages.map((g) => g.steps.filter((s) => at.has(s)).map((s) => at.get(s)!))
  const lastSeen = seen.reduce((n, xs, i) => (xs.length > 0 ? i : n), -1)
  const first = Math.min(...[...at.values()])

  const out: StageView[] = []
  let prevEnd: number | null = null
  let current = false
  for (let i = 0; i < stages.length; i += 1) {
    const g = stages[i]!
    const times = seen[i]!
    // Done: its last step was seen, a later stage has begun, or the whole start-up is.
    const done = data.done || i < lastSeen || at.has(g.steps[g.steps.length - 1]!)
    const start = prevEnd ?? (times.length > 0 ? Math.min(...times) : null)
    if (times.length === 0 && i < lastSeen) {
      out.push({ key: g.key, label: g.label, status: 'skipped', seconds: null })
    } else if (done) {
      const end = times.length > 0 ? Math.max(...times) : null
      out.push({ key: g.key, label: g.label, status: 'done', seconds: start !== null && end !== null ? (end - start) / 1000 : null })
      if (end !== null) prevEnd = end
    } else if (!current) {
      current = true
      out.push({ key: g.key, label: g.label, status: 'current', seconds: start !== null ? (now - start) / 1000 : null })
    } else {
      out.push({ key: g.key, label: g.label, status: 'todo', seconds: null })
    }
  }

  const doneCount = out.filter((s) => s.status === 'done' || s.status === 'skipped').length
  let remaining: string | null = null
  const target = data.estimate.first_frame
  if (!data.done && data.sampleSize > 0 && typeof target === 'number') {
    const from = at.get('dispatched')
    const left = Math.round(target - (from === undefined ? 0 : (now - from) / 1000))
    remaining = left > 0 ? `~${left} s left` : 'taking longer than usual'
  }
  return {
    stages: out,
    percent: Math.round((doneCount / out.length) * 100),
    elapsed: Number.isFinite(first) ? Math.max(0, (now - first) / 1000) : 0,
    remaining,
    done: data.done,
  }
}

/**
 * The fallback: the CURRENT attempt's lifecycle events -- those of the newest
 * `lease_acquired`'s attempt -- read newest first, so a task with a long
 * history still reaches its last attempt in one page.
 */
export function fromEvents(events: readonly TaskEvent[], state: TaskState | null): StartupData {
  const asc = [...events].sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
  const lease = [...asc].reverse().find((e) => e.type === 'lease_acquired')
  const attempt = lease?.attempt_id ?? null
  const steps = asc
    .filter((e) => LIFECYCLE_STEPS.has(e.type) && (attempt === null || e.attempt_id === attempt))
    .map((e) => ({ step: e.type, at: e.at }))
  const running = steps.some((s) => s.step === 'running')
  return {
    steps,
    estimate: {},
    sampleSize: 0,
    done: running || (state !== null && !CONCURRENCY_STATES.has(state)),
    fallback: true,
  }
}

function fromRead(r: StartupRead): StartupData {
  return {
    steps: Array.isArray(r.steps) ? r.steps : [],
    estimate: r.estimate ?? {},
    sampleSize: typeof r.sample_size === 'number' ? r.sample_size : 0,
    done: r.done === true,
    fallback: false,
  }
}

async function readStartup(taskId: string, state: TaskState | null): Promise<StartupData | null> {
  try {
    const r = await loadStartup(taskId)
    if (r?.status === 'ok' || r?.status === 'stale') return fromRead(r.data)
  } catch {
    // Falls through to the events, as a 404 does.
  }
  try {
    const ev = await loadTaskEventsPage(taskId, { order: 'desc' })
    if (ev?.status === 'ok' || ev?.status === 'stale') return fromEvents(ev.data.events, state)
  } catch {
    // Neither source answered: the bar keeps what it last drew.
  }
  return null
}

function visible(): boolean {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden'
}

/**
 * The start-up of one task, polled every `STARTUP_POLL_MS` while not done and
 * while the tab is visible; null until the first answer. `state` is the
 * task's, when the caller has it, so the fallback knows a task that left the
 * starting states is done.
 */
export function useStartup(taskId: string, state: TaskState | null = null): StartupData | null {
  const [data, setData] = useState<StartupData | null>(null)
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let inFlight = false
    const poll = async () => {
      timer = null
      if (stopped || inFlight) return
      if (!visible()) return // resumed by the visibility listener
      inFlight = true
      const got = await readStartup(taskId, state)
      inFlight = false
      if (stopped) return
      if (got !== null) setData(got)
      if (got?.done === true) {
        document.removeEventListener('visibilitychange', onVisible)
        return
      }
      timer = setTimeout(() => void poll(), STARTUP_POLL_MS)
    }
    const onVisible = () => {
      if (visible() && timer === null && !inFlight) void poll()
    }
    document.addEventListener('visibilitychange', onVisible)
    void poll()
    return () => {
      stopped = true
      if (timer !== null) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [taskId, state])
  return data
}

/** A one-second clock for the live counters, running only while `on`. */
function useNow(on: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!on) return
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [on])
  return now
}

/**
 * Whether the tab's noVNC screen has connected: `LiveScreen` writes its
 * connection to the element's `data-conn`, and the theater, which only
 * borrows that element, reads it here. A start-up whose pod never reports
 * `first_frame` (an older worker) must still step aside for the browser.
 */
function useConnected(screen: HTMLElement | null | undefined): boolean {
  const read = () => screen?.dataset.conn === 'connected'
  const [connected, setConnected] = useState(read)
  useEffect(() => {
    setConnected(screen?.dataset.conn === 'connected')
    if (!screen || typeof MutationObserver === 'undefined') return
    const watch = new MutationObserver(() => setConnected(screen.dataset.conn === 'connected'))
    watch.observe(screen, { attributes: true, attributeFilter: ['data-conn'] })
    return () => watch.disconnect()
  }, [screen])
  return connected
}

const MARK: Record<StageView['status'], string> = { done: '✓', skipped: '✓', current: '●', todo: '○' }
const SAY: Record<StageView['status'], string> = {
  done: 'done',
  skipped: 'done',
  current: 'in progress',
  todo: 'to come',
}

/**
 * The full bar and step list: the Browser tab while the agent's browser is
 * starting, and the theater over its screen. `hideWhenDone` draws nothing
 * once the start-up is done -- for an overlay that must step aside for the
 * browser itself.
 */
export function StartupProgress({
  taskId,
  state = null,
  overlay = false,
  hideWhenDone = false,
  screen,
}: {
  taskId: string
  state?: TaskState | null
  /** Centred over a screen area (the tab's stage, the theater's). */
  overlay?: boolean
  hideWhenDone?: boolean
  /** The borrowed noVNC screen, in the theater: once it has connected, nothing is drawn over it. */
  screen?: HTMLElement | null
}) {
  const data = useStartup(taskId, state)
  const now = useNow(data !== null && !data.done)
  const connected = useConnected(screen)
  if (connected) return null
  if (data === null) {
    if (hideWhenDone) return null
    return (
      <div className={`startup${overlay ? ' is-overlay' : ''}`} data-testid="startup-progress">
        <p className="startup-title">The agent’s browser is starting.</p>
        <p className="startup-say">Reading its start-up.</p>
      </div>
    )
  }
  if (hideWhenDone && data.done) return null
  const v = startupView(data, now)
  const current = v.stages.find((s) => s.status === 'current')
  return (
    <div className={`startup${overlay ? ' is-overlay' : ''}`} data-testid="startup-progress" data-fallback={data.fallback ? '' : undefined}>
      <div className="startup-card">
        <p className="startup-title">
          {v.done ? 'The agent’s browser has started.' : 'The agent’s browser is starting.'}
          {v.remaining !== null && <span className="startup-left"> {v.remaining}</span>}
        </p>
        <div
          className="startup-bar"
          role="progressbar"
          aria-label="Browser start-up"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={v.percent}
          aria-valuetext={current ? `${current.label}, ${v.percent}%` : `${v.percent}%`}
        >
          <span className="startup-fill" style={{ width: `${v.percent}%` }} />
        </div>
        <ol className="startup-steps">
          {v.stages.map((s) => (
            <li key={s.key} className={`startup-step is-${s.status}`} data-status={s.status} aria-current={s.status === 'current' ? 'step' : undefined}>
              <span className="startup-mark" aria-hidden="true">
                {MARK[s.status]}
              </span>
              <span className="startup-label">{s.label}</span>
              {s.status === 'skipped' && <span className="startup-skipped"> skipped (already ready)</span>}
              <span className="startup-sr"> ({SAY[s.status]})</span>
              {s.seconds !== null && s.status !== 'todo' && <span className="startup-time">{clock(s.seconds)}</span>}
            </li>
          ))}
        </ol>
        {data.fallback && (
          <p className="startup-say">Start-up steps from the agent’s events; the pod’s own steps are not reported here yet.</p>
        )}
      </div>
    </div>
  )
}

/**
 * The thumbnail's thin bar and "starting 0:41": the graph's live card while
 * its browser is starting. Draws nothing once the start-up is done.
 */
export function StartupBar({ taskId }: { taskId: string }) {
  const data = useStartup(taskId)
  const now = useNow(data !== null && !data.done)
  if (data === null || data.done) return null
  const v = startupView(data, now)
  const current = v.stages.find((s) => s.status === 'current')
  return (
    <div className="startup-thin" data-testid="startup-bar">
      <div
        className="startup-bar is-thin"
        role="progressbar"
        aria-label="Browser start-up"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={v.percent}
        aria-valuetext={current ? `${current.label}, ${v.percent}%` : `${v.percent}%`}
      >
        <span className="startup-fill" style={{ width: `${v.percent}%` }} />
      </div>
      <span className="startup-thin-say" title={current?.label}>
        starting {clock(v.elapsed)}
      </span>
    </div>
  )
}
