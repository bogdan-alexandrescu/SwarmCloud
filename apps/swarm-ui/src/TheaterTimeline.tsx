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
// WHAT IT SHOWS BY DEFAULT (lane LB-R2). What happened, not every protocol
// call. MEASURED 2026-10-12 08:05-08:20 PDT (re-QA after release f1c242c8):
// one line per method put about 40 lines of browser attach chatter --
// `Browser.getVersion`, `Target.setAutoAttach`, `Runtime.enable`,
// `Runtime.runIfWaitingForDebugger` xn -- above "You took control" in the
// first seconds. So `foldActions` keeps a line for what a person reading
// wants (a navigation, a new page, a reload, everything a person or the gate
// did, every refusal), counts input (`Input.*`) into one line per burst, and
// folds every other method into one collapsed "N protocol calls" group per
// burst, which "Show protocol" opens. The classification is here, not in
// swarm-api, because the route serves the log as written and a second reader
// (the recording's own log) wants every line.
//
// WHERE IT READS FROM. `GET /v1/tasks/{id}/live/actions`, the task route, which
// checks the tenant the way every task route does and answers a foreign task
// with a 404 (§5.2). Never a storage path and never the relay: the console
// talks to swarm-api only (§3.3). The route is lane L9's (`routes/live.py`);
// this lane was built before it, against the shape stated on `LiveActions`.

import { Fragment, useEffect, useState, type ReactNode } from 'react'

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
  'phone-390': 'Phone 390',
  'phone-380': 'Phone 380',
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
  preempted: 'the agent’s submitter took control',
  membership_lost: 'the person is no longer a member of the tenant',
  cancelled: 'the agent was cancelled',
  // LB-RETURN: a hold that ran out PARKS the hand-off; I'm ready resumes it.
  hold_expired: 'the hand-off parked (press I’m ready to resume)',
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

/** The agent methods the timeline keeps a line for, in words; the URL, when
 *  there is one, is the line under them. */
const SHOWN_METHOD: Readonly<Record<string, string>> = {
  'Page.navigate': 'The agent went to',
  'Target.createTarget': 'The agent opened a new page',
  'Page.reload': 'The agent reloaded the page',
}

/** The input methods, counted into one line per burst rather than drawn one
 *  by one. A mouse line carries no event type (cdpproxy.py `action_line`
 *  keeps the method's name and nothing else), so a click -- a move, a press
 *  and a release -- cannot be told from a move here: the line counts mouse
 *  EVENTS, and says so. */
const INPUT_METHOD: Readonly<Record<string, 'keys' | 'mouse' | 'touches' | 'drags'>> = {
  'Input.dispatchKeyEvent': 'keys',
  'Input.imeSetComposition': 'keys',
  'Input.dispatchMouseEvent': 'mouse',
  'Input.dispatchTouchEvent': 'touches',
  'Input.dispatchDragEvent': 'drags',
}

/** A quiet gap this long ends a burst: the next method starts a new group. */
export const BURST_GAP_MS = 10_000

/** One line the timeline draws (`foldActions`). */
export type TimelineEntry =
  | { type: 'action'; key: string; action: LiveAction }
  /** The agent's input in one burst, as counts. `typed` is characters. */
  | { type: 'input'; key: string; at: string; typed: number; keys: number; mouse: number; touches: number; drags: number }
  /** Every other method in one burst, oldest first. */
  | { type: 'protocol'; key: string; at: string; calls: LiveAction[] }

function shown(a: LiveAction): boolean {
  return a.kind !== 'cdp' || a.outcome === 'refused' || (a.method !== undefined && a.method in SHOWN_METHOD)
}

function instant(iso: string): number {
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? 0 : t
}

/**
 * The timeline as drawn: every action a person reading wants as its own line,
 * and each burst of the rest -- the methods between two such lines, with no
 * gap of `BURST_GAP_MS` inside -- as at most one input line and one protocol
 * group, in the order each first appeared. Keys are the source index of the
 * entry's first action, so a group a person opened stays open across a poll.
 */
export function foldActions(actions: readonly LiveAction[]): TimelineEntry[] {
  const out: TimelineEntry[] = []
  let input: Extract<TimelineEntry, { type: 'input' }> | null = null
  let protocol: Extract<TimelineEntry, { type: 'protocol' }> | null = null
  let last = Number.NEGATIVE_INFINITY
  const endBurst = () => {
    input = null
    protocol = null
  }
  actions.forEach((a, i) => {
    if (shown(a)) {
      endBurst()
      out.push({ type: 'action', key: `a${i}`, action: a })
      return
    }
    const t = instant(a.at)
    if (t - last > BURST_GAP_MS) endBurst()
    last = t
    const method = a.method ?? ''
    const counted = INPUT_METHOD[method]
    if (method === 'Input.insertText' || counted) {
      if (!input) {
        input = { type: 'input', key: `i${i}`, at: a.at, typed: 0, keys: 0, mouse: 0, touches: 0, drags: 0 }
        out.push(input)
      }
      if (counted) input[counted] += 1
      else input.typed += a.length ?? 0
      return
    }
    if (!protocol) {
      protocol = { type: 'protocol', key: `p${i}`, at: a.at, calls: [] }
      out.push(protocol)
    }
    protocol.calls.push(a)
  })
  return out
}

function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** An input burst in words: "Agent input: 12 characters typed, 3 mouse events". */
export function inputWords(e: Extract<TimelineEntry, { type: 'input' }>): string {
  const parts = [
    e.typed > 0 ? `${count(e.typed, 'character')} typed` : '',
    e.keys > 0 ? count(e.keys, 'key event') : '',
    e.mouse > 0 ? count(e.mouse, 'mouse event') : '',
    e.touches > 0 ? count(e.touches, 'touch event') : '',
    e.drags > 0 ? count(e.drags, 'drag event') : '',
  ].filter(Boolean)
  return `Agent input: ${parts.length > 0 ? parts.join(', ') : 'text typed'}`
}

/** A URL with no query string or fragment. The proxy already cuts both
 *  (cdpproxy.py `action_url`); this keeps one that arrived some other way
 *  from putting a token-shaped parameter on screen. */
export function withoutQuery(url: string): string {
  return url.replace(/[?#].*$/s, '')
}

/** A method name that may wrap after its dot and nowhere else. */
function MethodName({ name }: { name: string }) {
  const dot = name.indexOf('.')
  if (dot < 0) return <>{name}</>
  return (
    <>
      {name.slice(0, dot + 1)}
      <wbr />
      {name.slice(dot + 1)}
    </>
  )
}

/** A burst's methods, each named once with how often it was sent, in the
 *  order each was first sent. A browser attaching sends the same few methods
 *  interleaved, once per target, so consecutive runs would not fold them. */
function tally(calls: readonly LiveAction[]): { method: string; n: number }[] {
  const out: { method: string; n: number }[] = []
  for (const c of calls) {
    const method = c.method ?? 'unknown'
    const seen = out.find((r) => r.method === method)
    if (seen) seen.n += 1
    else out.push({ method, n: 1 })
  }
  return out
}

/** How a hand-off ended (`handoff.py` `ResolveHandoff.outcome`). */
const HANDOFF_OUTCOME: Readonly<Record<string, string>> = {
  returned: 'Hand-off resolved: control is back with the agent',
  // LB-RETURN: not "ended" -- a park that one press of I'm ready resumes.
  expired: 'Hand-off parked: nobody took control in time · press I’m ready to resume',
  cancelled: 'Hand-off ended: the agent was cancelled',
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
    if (a.outcome !== 'refused') return SHOWN_METHOD[method] ?? method
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
      {a.url ? <span className="th-act-url">{withoutQuery(a.url)}</span> : null}
      {typeof a.length === 'number' ? <span className="th-act-len">{`${a.length} characters typed`}</span> : null}
    </li>
  )
}

function InputLine({ e }: { e: Extract<TimelineEntry, { type: 'input' }> }) {
  return (
    <li className="th-act is-input">
      <time className="th-act-at" dateTime={e.at}>
        {clock(e.at)}
      </time>
      <span className="th-act-what">{inputWords(e)}</span>
    </li>
  )
}

/** One burst's protocol calls, collapsed until a person opens it. The names
 *  are drawn only while it is open, so a closed group is one line to a
 *  screen reader and to a search of the page, as it is to the eye. */
function ProtocolLine({ e, initiallyOpen }: { e: Extract<TimelineEntry, { type: 'protocol' }>; initiallyOpen: boolean }) {
  const [open, setOpen] = useState(initiallyOpen)
  return (
    <li className="th-act is-protocol">
      <time className="th-act-at" dateTime={e.at}>
        {clock(e.at)}
      </time>
      <details className="th-proto" open={open} onToggle={(ev) => setOpen(ev.currentTarget.open)}>
        <summary className="th-act-what">{count(e.calls.length, 'protocol call')}</summary>
        {open ? (
          <ul className="th-proto-calls">
            {tally(e.calls).map((r) => (
              <li key={r.method}>
                <MethodName name={r.method} />
                {r.n > 1 ? ` ×${r.n}` : ''}
              </li>
            ))}
          </ul>
        ) : null}
      </details>
    </li>
  )
}

/**
 * The agent's actions for this task's live session. Read again every
 * `TIMELINE_POLL_MS` while the session is live; read once for a finished one.
 */
export function TheaterTimeline({ taskId, live }: { taskId: string; live: boolean }) {
  const [res, setRes] = useState<Result<LiveActions>>({ status: 'loading', since: Date.now() })
  const [showProtocol, setShowProtocol] = useState(false)

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
  const entries = foldActions(res.data.actions)
  const anyProtocol = entries.some((e) => e.type === 'protocol')
  return (
    <>
      {res.status === 'stale' ? (
        <p className="th-note is-bad" role="status">
          {`${errorHeading(res.error)}: showing the actions read at ${clock(new Date(res.fetchedAt).toISOString())}.`}
        </p>
      ) : null}
      {anyProtocol ? (
        <button type="button" className="th-proto-toggle" aria-pressed={showProtocol} onClick={() => setShowProtocol((v) => !v)}>
          {showProtocol ? 'Hide protocol' : 'Show protocol'}
        </button>
      ) : null}
      <ol className="th-acts" aria-label="Agent actions">
        {entries.map((e) => (
          <Fragment key={e.key}>
            {e.type === 'action' ? <ActionLine a={e.action} /> : null}
            {e.type === 'input' ? <InputLine e={e} /> : null}
            {/* Keyed on the toggle too: flipping it re-opens or re-closes every
                group, including one a person opened or closed by hand. */}
            {e.type === 'protocol' ? <ProtocolLine key={String(showProtocol)} e={e} initiallyOpen={showProtocol} /> : null}
          </Fragment>
        ))}
      </ol>
    </>
  )
}

/** `Task.human_wait`, as much of it as the checklist shows (§4.1). */
export interface TheaterHandoff {
  /** Redacted by the worker before it was stored (§4.1). */
  reason: string
  /** The host the agent named for the hand-off; empty when it named none. */
  expect: string
  /** ISO instant the hold ends and the worker parks (§4.3). */
  deadline: string
}

/**
 * The hold's countdown, ONE FORMAT, m:ss (QA12-21): `4:32 left`. IN CONTROL
 * IT IS THE IDLE TIMER: taking control moves the deadline to
 * `now + control_idle_seconds` on every input (§4.3), so what is counting
 * down is how long the browser may sit unused before it goes back to the
 * agent, and it says that: `returns to the agent after 4:32 idle`.
 */
function timeLeft(deadline: string, now: number, inControl = false): string {
  const end = new Date(deadline).getTime()
  if (Number.isNaN(end)) return 'unknown'
  const s = Math.max(0, Math.round((end - now) / 1000))
  if (s === 0) return inControl ? 'returning to the agent' : 'the hold has ended'
  const mss = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
  return inControl ? `returns to the agent after ${mss} idle` : `${mss} left`
}

/**
 * The hand-off checklist the left column becomes while the agent waits for a
 * person (§6). The steps are the protocol's (§4.2), in its order, with the one
 * check a phishing page cannot fake first: the origin strip, drawn by the
 * console from what Chromium reports, against the site the agent named (§5.7).
 *
 * NEUTRAL WORDING (owner decision 2026-10-13, "Neutral wording"): a hand-off
 * is not always a sign-in -- a captcha, an approval, a second factor -- so
 * the agent's own reason is the headline and the steps fit any of them.
 * Nothing under the steps says a password is masked: the live view shows the
 * page as Chromium draws it (BGD, 2026-10-25; LIVE-NOMASK-COPY, part of #2298).
 *
 * THE TICKS ARE WHAT IS KNOWN, NOT WHAT IS HOPED (lane LB-UX2a, the chosen
 * mockup). "The agent asked for your help" is done the moment a hand-off
 * exists: the request is what put this checklist on screen. "Check the
 * strip" is done while the strip's own verdict is the site the agent named
 * (`stripOk`, QA12-23: it was never ticked, even with the strip green).
 * "You took control" is done while the session names THIS person as its
 * controller -- the server's answer, polled by the tab, never a click
 * remembered here. Doing what the agent asked is the person's own act, which
 * nothing reports, so it is never ticked.
 *
 * "I'M DONE, RETURN CONTROL" IS THE BOTTOM BAR'S RETURN, not a second one:
 * `onReturn` is the theater's call of `returnLiveControl` followed by the
 * tab's reload, the action the tab's Return control button performs.
 *
 * BEFORE THAT, THE BUTTON IS TAKE CONTROL (lane THEATER-TAKE, owner report
 * 2026-10-13: the checklist said "You took control." and offered nothing that
 * took it). `take` is the tab's own `<LiveControl takeOnly />`, so the route,
 * the first ask and the wrong-site second ask are the tab's, not rebuilt
 * here. It is drawn only for a person the server says may take control now
 * (`canTake`: the eligibility LiveControl itself applies); anyone else is
 * told who holds control, and gets no button. With no `take` or no session
 * read, the waiting return button stays, with its reason.
 */
export function HandoffChecklist({
  handoff,
  youControl = false,
  onReturn,
  take,
  canTake,
  holder = null,
  stripOk = false,
}: {
  handoff: TheaterHandoff
  /** The strip's verdict is green for the site the hand-off named (`stripVerdict`). */
  stripOk?: boolean
  /** The session names this person as its controller. */
  youControl?: boolean
  /** The tab's Take control, with its asks (`<LiveControl takeOnly />`). */
  take?: ReactNode
  /**
   * The server says this person may take control now (`you.can_control`, and
   * nobody holds it or `you.can_preempt`); undefined while there is no session read.
   */
  canTake?: boolean
  /** Who holds control when it is not this person, for the not-eligible line. */
  holder?: string | null
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
  const named = handoff.expect.trim() !== ''
  const returnWhy = !youControl ? 'Take control first: there is nothing to return yet.' : !onReturn ? 'The tab has no session to return control on.' : undefined
  return (
    <section className="th-handoff" aria-label="Hand-off checklist">
      <p className="th-handoff-reason">{handoff.reason}</p>
      <p className="th-handoff-meta">
        {named ? (
          <span>
            Expected site <span className="mono">{handoff.expect}</span>
          </span>
        ) : null}
        <time dateTime={handoff.deadline}>{timeLeft(handoff.deadline, now, youControl)}</time>
      </p>
      <ol className="th-steps">
        {step(true, <>The agent stopped and asked for your help.</>)}
        {step(
          stripOk,
          named ? (
            <>
              Check the strip above the browser reads <span className="mono">{handoff.expect}</span>. If it does not, do
              not type anything.
            </>
          ) : (
            <>Check the address in the strip above the browser before you type anything: the agent named no site.</>
          ),
        )}
        {step(youControl, <>You took control.</>)}
        {step(false, <>Do what the agent asked, in the browser.</>)}
      </ol>
      {!youControl && canTake && take ? (
        <div className="th-take">{take}</div>
      ) : !youControl && canTake === false ? (
        <p className="th-note th-view-only">{holder === null ? 'View only' : `${holder} is in control`}</p>
      ) : (
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
      )}
      <p className="th-note">The agent carries on from the page you leave it on.</p>
      {error !== null ? (
        <p className="th-note is-bad" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  )
}
