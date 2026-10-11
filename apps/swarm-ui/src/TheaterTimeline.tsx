// THE THEATER'S LEFT COLUMN: the agent's actions, and the hand-off checklist
// (docs/design/live-browser.md §6, LB-5; lane LB-L11b).
//
// WHAT THE TIMELINE IS. One line per CDP **method** the gate's proxy forwarded
// for the agent, and one per control event from the audit: exactly what the
// relay writes to `actions-<seq>.live.jsonl` (§7). A method name, a redacted
// URL with no query, a typed text's LENGTH -- never the text, never a key,
// never a parameter that could hold one. So this view can render every field
// it is given; it is the relay, not this file, that keeps typed text out.
//
// WHERE IT READS FROM. `GET /v1/tasks/{id}/live/actions`, the task route, which
// checks the tenant the way every task route does and answers a foreign task
// with a 404 (§5.2). Never a storage path and never the relay: the console
// talks to swarm-api only (§3.3). The route is lane L9's (`routes/live.py`);
// this lane was built before it, against the shape stated on `LiveActions`.

import { useEffect, useState } from 'react'

import { errorHeading, read, route, type Result } from './fetch'

/** One line of `actions-<seq>.live.jsonl`, as the API returns it. */
export interface LiveAction {
  /** ISO instant the gate forwarded the method, or the audit wrote the event. */
  at: string
  /** `cdp`: an agent's method through the proxy. `control`: an audit event. */
  kind: 'cdp' | 'control'
  /** The CDP method, for `cdp` (`Page.navigate`, `Input.insertText`). */
  method?: string
  /** The redacted URL, no query, for a navigation. */
  url?: string
  /** A typed text's length -- the text itself never leaves the gate. */
  length?: number
  /** The audit entry, for `control` (`control_taken`, `control_returned`). */
  event?: string
}

/** `GET /v1/tasks/{id}/live/actions`: the current attempt's actions, oldest first. */
export interface LiveActions {
  attempt_id: string
  actions: LiveAction[]
}

/** How often a live session's timeline is read again. The relay closes a
 *  segment every minute (§7), and a person watching wants to see the click
 *  they just watched happen, so the read is far inside that. */
export const TIMELINE_POLL_MS = 5_000

export function loadLiveActions(taskId: string, signal?: AbortSignal): Promise<Result<LiveActions>> {
  return read<LiveActions>(
    route('/v1/tasks/{id}/live/actions', { id: taskId }),
    (d) => !Array.isArray(d.actions) || d.actions.length === 0,
    { signal },
  )
}

function clock(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** The words for a control event; an event this build does not know is shown by its name. */
const CONTROL_WORDS: Readonly<Record<string, string>> = {
  handoff_requested: 'Agent asked for help',
  control_taken: 'A person took control',
  control_returned: 'Control returned to the agent',
  control_ended: 'Control ended',
  control_opened_to_members: 'Control opened to the tenant',
  handoff_resolved: 'Hand-off resolved',
}

function ActionLine({ a }: { a: LiveAction }) {
  const what = a.kind === 'control' ? CONTROL_WORDS[a.event ?? ''] ?? a.event ?? 'Control event' : a.method ?? 'CDP method'
  return (
    <li className={`th-act is-${a.kind}`}>
      <time className="th-act-at" dateTime={a.at}>
        {clock(a.at)}
      </time>
      <span className="th-act-what">{what}</span>
      {a.url ? <span className="th-act-url" title={a.url}>{a.url}</span> : null}
      {typeof a.length === 'number' ? <span className="th-act-len">{`${a.length} characters typed`}</span> : null}
    </li>
  )
}

/**
 * The agent's actions for this task's live session. Read again every
 * `TIMELINE_POLL_MS` while the session is live; read once for a finished one.
 */
export function TheaterTimeline({ taskId, live }: { taskId: string; live: boolean }) {
  const [res, setRes] = useState<Result<LiveActions>>({ status: 'loading', since: Date.now() })

  useEffect(() => {
    const ctl = new AbortController()
    let timer: ReturnType<typeof setTimeout> | null = null
    let previous: { data: LiveActions; fetchedAt: number } | null = null
    const tick = async (): Promise<void> => {
      const next = await loadLiveActions(taskId, ctl.signal)
      if (ctl.signal.aborted) return
      // A failed re-read keeps the lines already shown, marked stale, rather
      // than blanking a timeline the person was reading.
      if (next.status === 'error' && previous) {
        setRes({ status: 'stale', data: previous.data, fetchedAt: previous.fetchedAt, error: next.error })
      } else {
        if (next.status === 'ok') previous = { data: next.data, fetchedAt: next.fetchedAt }
        setRes(next)
      }
      if (live) timer = setTimeout(() => void tick(), TIMELINE_POLL_MS)
    }
    void tick()
    return () => {
      ctl.abort()
      if (timer !== null) clearTimeout(timer)
    }
  }, [taskId, live])

  if (res.status === 'loading') return <p className="th-note">Reading the agent’s actions…</p>
  if (res.status === 'error') {
    return (
      <p className="th-note is-bad" role="status">
        {`${errorHeading(res.error)}: the agent’s actions could not be read.`}
      </p>
    )
  }
  if (res.status === 'empty') return <p className="th-note">No action recorded yet.</p>
  return (
    <>
      {res.status === 'stale' ? (
        <p className="th-note is-bad" role="status">
          {`${errorHeading(res.error)}: showing the actions read at ${clock(new Date(res.fetchedAt).toISOString())}.`}
        </p>
      ) : null}
      <ol className="th-acts" aria-label="Agent actions">
        {res.data.actions.map((a, i) => (
          <ActionLine key={`${a.at}-${i}`} a={a} />
        ))}
      </ol>
    </>
  )
}

/** `Task.human_wait`, as much of it as the checklist shows (§4.1). */
export interface TheaterHandoff {
  /** Redacted by the worker before it was stored (§4.1). */
  reason: string
  /** The host the agent expects the sign-in on. */
  expect: string
  /** ISO instant the hold ends and the worker parks (§4.3). */
  deadline: string
}

function timeLeft(deadline: string, now: number): string {
  const end = new Date(deadline).getTime()
  if (Number.isNaN(end)) return 'unknown'
  const s = Math.max(0, Math.round((end - now) / 1000))
  if (s === 0) return 'the hold has ended'
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')} left`
}

/**
 * The hand-off checklist the left column becomes while the agent waits for a
 * person (§6). The steps are the protocol's (§4.2), in its order, with the one
 * check a phishing page cannot fake first: the origin strip, drawn by the
 * console from what Chromium reports, against the site the agent named (§5.7).
 */
export function HandoffChecklist({ handoff }: { handoff: TheaterHandoff }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(t)
  }, [])
  return (
    <section className="th-handoff" aria-label="Hand-off checklist">
      <p className="th-handoff-reason">{handoff.reason}</p>
      <p className="th-handoff-meta">
        <span>
          Expected site <span className="mono">{handoff.expect}</span>
        </span>
        <time dateTime={handoff.deadline}>{timeLeft(handoff.deadline, now)}</time>
      </p>
      <ol className="th-steps">
        <li>
          Check the strip above the browser reads <span className="mono">{handoff.expect}</span>. If it does not, do
          not type anything.
        </li>
        <li>Take control.</li>
        <li>Sign in. Passwords are masked before they leave the agent’s pod.</li>
        <li>Return control. The agent carries on from the signed-in page.</li>
      </ol>
    </section>
  )
}
