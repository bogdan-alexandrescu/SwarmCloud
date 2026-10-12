// THE THEATER'S LEFT COLUMN: the agent's actions, and the hand-off checklist
// (docs/design/live-browser.md §6, LB-5; lane LB-L11b).
//
// WHAT THE TIMELINE IS. One line per CDP **method** the gate's proxy forwarded
// (or refused) for the agent, as the relay writes it to
// `actions-<seq>.live.jsonl` (§7), and one per thing a person did, from the
// task's live audit, merged by swarm-api (LB-V3): who took control, the
// device they chose, a rotation, the touch toggle, the return, the hand-off.
// A method name, a redacted URL with no query, a typed text's LENGTH, a
// preset by name -- never the text, never a key, never a parameter that
// could hold one. So this view can render every field it is given; it is the
// gate and the API, not this file, that keep typed text out.
//
// WHERE IT READS FROM. `GET /v1/tasks/{id}/live/actions`, the task route, which
// checks the tenant the way every task route does and answers a foreign task
// with a 404 (§5.2). Never a storage path and never the relay: the console
// talks to swarm-api only (§3.3). The route is lane L9's (`routes/live.py`);
// this lane was built before it, against the shape stated on `LiveActions`.

import { useEffect, useState, type ReactNode } from 'react'

import { errorHeading, read, route, type Result } from './fetch'

/** One line of the timeline, as the API returns it: a line of
 *  `actions-<seq>.live.jsonl`, or an entry of the task's live audit (LB-V3). */
export interface LiveAction {
  /** ISO instant the gate forwarded the method, or the audit wrote the event. */
  at: string
  /** `cdp`: an agent's method through the proxy. `control`: a person's act, or the gate's. */
  kind: 'cdp' | 'control'
  /** The CDP method, for `cdp` (`Page.navigate`, `Input.insertText`). */
  method?: string
  /** The redacted URL, no query, for a navigation. */
  url?: string
  /** A typed text's length -- the text itself never leaves the gate. */
  length?: number
  /** The event, for `control` (`control_taken`, `rotated`, `refused`, ...). */
  event?: string
  /** Why: a refusal's or an ending's cause, a fixed name. */
  cause?: string
  /** `refused`, for an agent method the gate refused; a hand-off's outcome. */
  outcome?: string
  /** The person who did it, from the audit. */
  by?: string
  /** `by` is the person reading this timeline. */
  you?: boolean
  /** A device the person in control chose: a preset by name, its orientation, touch. */
  preset?: string
  orientation?: string
  touch?: boolean
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

/** The presets' display names. LiveBrowser.tsx's `presetLabel` holds the
 *  same list, but LiveBrowser imports Theater, which imports this file, so it
 *  cannot be imported back; a name not here is shown as served. */
const PRESET_LABEL: Readonly<Record<string, string>> = {
  desktop: 'Desktop',
  'iphone-15': 'iPhone 15',
  'pixel-8': 'Pixel 8',
  'iphone-se': 'iPhone SE',
  'ipad-7': 'iPad',
}

/** Why, in plain words, for every cause the gate, the proxy and swarm-api
 *  write (cdpproxy.py `take_control` / `REFUSAL_CAUSES`, live.py `END_CAUSES`,
 *  __main__.py `apply_device`). A cause this build does not know is shown by
 *  its name, spaced. */
const CAUSE_WORDS: Readonly<Record<string, string>> = {
  // Taking control (the gate's `refused`).
  not_paused: 'the agent was still driving the browser',
  security_state_unknown: 'the page’s connection could not be checked',
  insecure_connection: 'the page is not on a secure https connection',
  browser_closed: 'the browser had closed',
  no_page: 'no page was open to control',
  // The agent's methods (the proxy's refusals).
  method_policy: 'the live gate does not let the agent do that',
  not_its_session: 'it named a browser session the agent does not own',
  control_window_request: 'it asked for what was sent while a person was in control',
  // Control ending.
  idle: 'nobody used the browser for too long',
  renew_lapsed: 'the console stopped holding it',
  preempted: 'the task’s submitter took control',
  membership_lost: 'the person is no longer a member of the tenant',
  cancelled: 'the task was cancelled',
  hold_expired: 'the hand-off’s time ran out',
  attempt_ended: 'the attempt ended',
  page_closed: 'the page closed',
  // A device the gate did not apply.
  not_a_preset_name: 'it was not a device preset',
  unknown_preset: 'the browser does not know that device',
  no_touch: 'that device has no touch screen',
  not_applied: 'the browser could not apply it',
}

/** What the agent's refused method was, in words. */
const METHOD_NOUN: Readonly<Record<string, string>> = {
  'Page.navigate': 'A navigation',
  'Target.createTarget': 'Opening a new page',
  'Input.insertText': 'Typing',
}

/** How a hand-off ended (`handoff.py` `ResolveHandoff.outcome`). */
const HANDOFF_OUTCOME: Readonly<Record<string, string>> = {
  returned: 'Hand-off resolved: control is back with the agent',
  expired: 'Hand-off ended: nobody finished in time',
  cancelled: 'Hand-off ended: the task was cancelled',
  unavailable: 'Hand-off ended: the live browser was unavailable',
}

function because(cause: string | undefined): string {
  if (!cause) return 'no reason was recorded'
  return CAUSE_WORDS[cause] ?? cause.replace(/_/g, ' ')
}

function presetName(preset: string | undefined): string {
  return preset ? PRESET_LABEL[preset] ?? preset : 'another device'
}

/**
 * One timeline line in plain words (LB-V3). Who did it -- "You" when the
 * audit names the reader -- what changed, and for a refusal what was refused
 * and why. MEASURED 2026-10-12: a take, a device switch, a rotation and a
 * return read "A person took control" and "refused"; every one of those now
 * has its own line.
 */
export function actionWords(a: LiveAction): string {
  const who = a.you ? 'You' : a.by ? a.by : 'A person'
  if (a.kind === 'cdp') {
    const method = a.method ?? 'CDP method'
    if (a.outcome !== 'refused') return method
    return `${METHOD_NOUN[method] ?? `The agent’s ${method}`} was refused: ${because(a.cause)}`
  }
  switch (a.event) {
    case 'control_taken':
      return `${who} took control`
    case 'control_returned':
      return `${who} returned control to the agent`
    case 'control_ended':
      return `${a.you ? 'Your' : a.by ? `${a.by}’s` : 'The person’s'} control ended: ${because(a.cause)}`
    case 'control_opened_to_members':
      return `${who} let any member take control`
    case 'handoff_requested':
      return 'The agent asked for help'
    case 'handoff_resolved':
      return HANDOFF_OUTCOME[a.outcome ?? ''] ?? 'Hand-off resolved'
    case 'device_changed': {
      const shape = a.orientation ? `, ${a.orientation}` : ''
      const touch = a.touch === true ? ', touch on' : a.touch === false && a.preset !== 'desktop' ? ', touch off' : ''
      return `${who} switched the device to ${presetName(a.preset)}${shape}${touch}`
    }
    case 'rotated':
      return `${who} rotated the ${presetName(a.preset)} to ${a.orientation ?? 'its other orientation'}`
    case 'touch_toggled':
      return `${who} turned touch ${a.touch ? 'on' : 'off'}`
    case 'device_refused':
      return `A device change was refused: ${because(a.cause)}`
    case 'refused':
      return `Taking control was refused: ${because(a.cause)}`
    case 'granted':
      return 'The browser handed control to the person'
    case 'ended':
      return `The browser ended control: ${because(a.cause)}`
    default:
      return a.event ? a.event.replace(/_/g, ' ') : 'Control event'
  }
}

function ActionLine({ a }: { a: LiveAction }) {
  const refused = a.outcome === 'refused' || a.event === 'refused' || a.event === 'device_refused'
  return (
    <li className={`th-act is-${a.kind}${refused ? ' is-refused' : ''}`}>
      <time className="th-act-at" dateTime={a.at}>
        {clock(a.at)}
      </time>
      <span className="th-act-what">{actionWords(a)}</span>
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
 *
 * THE TICKS ARE WHAT IS KNOWN, NOT WHAT IS HOPED (lane LB-UX2a, the chosen
 * mockup). "The agent opened the sign-in page" is done the moment a hand-off
 * exists: the agent asks from the page it could not get past (§4.1), and the
 * request is what put this checklist on screen. "You took control" is done
 * while the session names THIS person as its controller -- the server's
 * answer, polled by the tab, never a click remembered here. Checking the
 * strip and signing in are the person's own acts, which nothing reports, so
 * they are never ticked.
 *
 * "I'M DONE, RETURN CONTROL" IS THE BOTTOM BAR'S RETURN, not a second one:
 * `onReturn` is the theater's call of `returnLiveControl` followed by the
 * tab's reload, the action the tab's Return control button performs. It waits
 * until the person is in control, since there is nothing to return before.
 */
export function HandoffChecklist({
  handoff,
  youControl = false,
  onReturn,
}: {
  handoff: TheaterHandoff
  /** The session names this person as its controller. */
  youControl?: boolean
  /** Return control; resolves to an error message, or null when it was returned. */
  onReturn?: () => Promise<string | null>
}) {
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(t)
  }, [])
  const step = (done: boolean, body: ReactNode) => (
    <li className={`th-step${done ? ' is-done' : ''}`} data-done={done ? 'true' : 'false'}>
      <span className="th-step-mark" aria-hidden="true">
        {done ? '✓' : ''}
      </span>
      <span className="th-step-body">
        {body}
        {done ? <span className="sk-vh"> (done)</span> : null}
      </span>
    </li>
  )
  const returnWhy = !youControl ? 'Take control first: there is nothing to return yet.' : !onReturn ? 'The tab has no session to return control on.' : undefined
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
        {step(true, <>The agent opened the sign-in page and asked for you.</>)}
        {step(
          false,
          <>
            Check the strip above the browser reads <span className="mono">{handoff.expect}</span>. If it does not, do
            not type anything.
          </>,
        )}
        {step(youControl, <>You took control.</>)}
        {step(false, <>Sign in. Passwords are masked before they leave the agent’s pod.</>)}
      </ol>
      <button
        type="button"
        className="th-done"
        disabled={returnWhy !== undefined || busy}
        title={returnWhy}
        onClick={() => {
          if (!onReturn) return
          setBusy(true)
          void onReturn().then((err) => {
            setBusy(false)
            setError(err)
          })
        }}
      >
        {busy ? 'Returning…' : "I'm done, return control"}
      </button>
      <p className="th-note">The agent carries on from the signed-in page.</p>
      {error !== null ? (
        <p className="th-note is-bad" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  )
}
