import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ClipboardEvent,
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
  type SyntheticEvent,
} from 'react'

import { AgentLogs } from './AgentLogs'
import {
  closeLiveToMembers,
  loadLiveSession,
  mintLiveTicket,
  openLiveSocket,
  renewLiveControl,
  returnLiveControl,
  openLiveToMembers,
  setLiveDevice,
  takeLiveControl,
  type LiveChannel,
  type LiveOrigin,
  type LiveRead,
  type LiveSession,
  type LiveStatus,
} from './api'
import { SaveAsLoginOffer } from './BrowserLogins'
import { BrowserBadges, browserStatusOf } from './BrowserBadges'
import { qaIdentityOf } from './QaIdentityGrants'
import { usePhoneTables } from './capacityPoll'
import { Button, EmptyState, buttonClass } from './components'
import { HandoffStrip } from './Handoff'
import { LiveToolMenu } from './LiveToolMenu'
import { agentPath } from './OverviewRegions'
import { RecordingsPanel } from './Recordings'
import { StartupProgress } from './StartupProgress'
import { TheaterButton, TheaterHost, useTheater, type TheaterHandoff, type TheaterSession } from './Theater'
import { TERMINAL_STATES, type Task, type TaskState } from './types'
import './styles/live.css'

/**
 * THE BROWSER TAB (docs/design/live-browser.md §3.4, §6, §6.1; lane L11).
 *
 * A running browser agent's split opens on this tab: noVNC drawing the
 * agent's browser as PIXELS into a canvas (§3.4 -- a DOM-streaming viewer
 * would run every page the agent visits in the console's origin), and above
 * it, outside the canvas, a strip the page cannot draw into: the top frame's
 * origin as the gate read it from Chromium, the hand-off's reason, and who is
 * in control. Its toolbar is the mockup's (owner decisions 2026-10-12, §6):
 * device chips, `⟲ rotate`, `touch: on|off`, `◫ logs`, and `⤢ Theater` at the
 * right, then "Let any member take control" as built. The chips, rotate and
 * touch work for the person in control, and only for them
 * (`you.can_switch_device`, LB-DEV-B). Under the device, a
 * caption: `<device> · <W>×<H> · <path>`. Take control sits in the agent
 * header beside Stop
 * (`LiveControl`), and the view is read-only until it is pressed.
 *
 * WHAT THE CONSOLE DECIDES, AND WHAT IT ONLY DRAWS. Who may watch, who may
 * control, whether control may be taken over plain HTTP: swarm-api and the
 * gate decide all of it, on every connection (§5.1, §5.2, §5.7). The console
 * hides what it knows will be refused, so a viewer is not offered a button
 * that cannot work -- and it never relies on that hiding for anything.
 *
 * THE TICKET GOES IN THE FIRST MESSAGE, NEVER THE URL (§5.2). Load balancer,
 * IAP and Cloud Run logs record URLs; `openLiveSocket` opens
 * `/v1/live/<session>/stream` and sends the one-use ticket as its first frame.
 *
 * KEYS ONLY WITH CANVAS FOCUS (§5.6). noVNC listens for keys on its own
 * canvas, so a key pressed anywhere else in the console never reaches it, and
 * a paste goes only from the focused canvas. The connection is view-only
 * (`viewOnly`, which sends no key, pointer or paste at all) except while this
 * person holds control. While the canvas has focus in control, a key stops at
 * the canvas, so the console's own shortcuts (Escape closes the split, `[`
 * folds the list) do not fire under someone typing into the agent's browser.
 *
 * THE KEYS FOLLOW THE PERSON IN CONTROL BACK TO THE CANVAS (KEYS-FOCUS; part
 * of #1030). MEASURED 2026-10-14 on task_84cb2038aa704c829d48 generation 5:
 * the owner switched iphone-15 -> desktop twice and typed into the form; the
 * gate counted keys=0 pointer=87 for that connection. Pressing a device chip
 * leaves focus on the chip, so every key went to the console; a reconnect
 * replaces noVNC's canvas, and the theater moves it, without a blur, so the
 * line under the canvas went on saying the keys went to the agent's browser.
 * So: a device switch in control hands focus back to the canvas, a canvas
 * that replaces a focused one takes its focus, a press anywhere on the
 * screen focuses the canvas, whether the canvas has focus is read from the
 * document rather than remembered, and "Click to type" sits on the screen
 * -- in the tab and in the theater -- whenever it does not.
 *
 * THE POINTER MOVES WITHOUT A CLICK FIRST (LB-CURSOR2; part of #1030). Input
 * used to open only once the canvas also had focus, so every move before the
 * first click was dropped by noVNC itself: MEASURED 2026-10-13 on
 * task_1d1785b21d5b4e1f894e, 20 moves, a click and two keys on the canvas
 * reached the gate as two pointer events -- the press and the release -- and
 * the agent's page never saw the mouse arrive. Moving is not typing: the
 * pointer is the controller's from the moment they hold control.
 *
 * TOUCH ONLY IN CONTROL, WITH TOUCH ON (§6.1). A touch on the canvas reaches
 * noVNC, which turns it into the pointer events the gate makes touch events
 * of, only while this person holds control and has touch on. Otherwise it is
 * stopped before noVNC sees it, and the page scrolls as it would anywhere.
 *
 * THEATER IS THIS TAB, FULL SCREEN (§6, lane L11b). The tab's body sits in
 * a <TheaterHost>, and ⤢ in the toolbar (or `T`) opens it. The theater opens
 * no connection of its own: it borrows the node LiveScreen gave noVNC and
 * moves it into its stage, and the strip, the logs, the device bar and the
 * control button it shows are this file's own components, so who may do what
 * is decided in one place.
 *
 * SAVE AS LOGIN FOLLOWS RETURN CONTROL (§5.5.1, lane L12c). The pane notes
 * when this person's control ends with nobody in control -- they returned it,
 * or let it lapse -- and draws `SaveAsLoginOffer`, which itself draws nothing
 * outside a personal (`u-`) workspace. A takeover by someone else is not a
 * return: the session in the browser is no longer only theirs to keep.
 *
 * NOTHING TYPED IS LOGGED OR KEPT (§5.3.3). This file has no console call and
 * stores no key, paste or pointer position; a paste in control goes straight
 * to noVNC, which sends it as ClientCutText for the gate to type as keys.
 */

/**
 * How often an open session is read again: the controller, the origin, the
 * device and the hand-off move under the viewer. ~3 s (#1343): often enough
 * that a reported device or address shows before the person looks for it,
 * and over the fetch layer's 2 s reuse window (`REUSE_SETTLED_MS`), so the
 * task re-read on the same clock is never handed its own last answer.
 */
export const LIVE_POLL_MS = 3000

/**
 * The controller's console renews this often. Control ends 60 seconds after
 * the last renewal (`renew_by`, §4.2), so three renewals fit in the window and
 * one lost request does not take control away from someone mid-sign-in.
 */
export const LIVE_RENEW_MS = 20_000

/** The longest wait between reconnects; noVNC's socket ends with Cloud Run's request timeout (§3.3, R2). */
const RECONNECT_MAX_MS = 30_000

/**
 * RFC 6455's 1012, "Service Restart": the relay is restarting, reconnect now
 * (RELAY-ROLL). The relay closes with it on a roll, and swarm-api closes a
 * viewer with it whenever the viewer's relay link ends. MEASURED 2026-10-12/14:
 * 40 relay restarts in ~48 h, one per release, and each one cost every viewer
 * the 2 s, 4 s, 8 s ... backoff of an ordinary drop before it rejoined.
 */
export const LIVE_RESTART_CODE = 1012
/** After a restart: the first reconnect ~250 ms away, doubling, jittered, never past 4 s. */
const RESTART_RECONNECT_MS = 250
const RESTART_RECONNECT_MAX_MS = 4_000

/**
 * How long reconnect number `tries` (1 first) waits. After a restart close,
 * ~250 ms doubling and jittered to 75-125 %, so the viewers a relay drops at
 * once do not all knock in the same instant; otherwise the ordinary backoff.
 */
export function liveReconnectDelay(tries: number, restart: boolean, rand: () => number = Math.random): number {
  const step = Math.max(0, Math.min(tries - 1, 10))
  if (restart) return Math.min(RESTART_RECONNECT_MAX_MS, RESTART_RECONNECT_MS * 2 ** step * (0.75 + 0.5 * rand()))
  return Math.min(RECONNECT_MAX_MS, 1000 * 2 ** Math.min(tries, 5))
}

/** The presets' display names (§6.1, LB-6). The gate holds the list and its sizes; a name not here is shown as served. */
const PRESET_LABEL: Readonly<Record<string, string>> = {
  desktop: 'Desktop',
  'iphone-15': 'iPhone 15',
  // QA-KIT (owner decision 2026-10-19): the 390x844 phone a visual QA sweep names.
  'phone-390': 'Phone 390',
  // IMPLEMENT-RENDER (owner decision 2026-10-26): Phone 390, ten pixels narrower.
  'phone-380': 'Phone 380',
  'pixel-8': 'Pixel 8',
  'iphone-se': 'iPhone SE',
  // `iPad`, as the mockup's chip reads (owner decision 2026-10-12); the
  // preset is the 7th generation's frame, which its size in the caption says.
  'ipad-7': 'iPad',
}

/**
 * What the Browser tab says over a closed live session (LB-RETURN, part of #1030).
 *
 * A hand-off whose hold ran out closes its session with `hold_expired`, and this
 * said "The live browser has closed (hold_expired)" over a task that one press of
 * I'm ready resumes -- MEASURED by the live-browser acceptance group, 2026-10-14;
 * the person read it as over. A park is not an end: it says so, and what to press.
 */
export function closedSessionNote(
  cause: string | null,
  task: { state: TaskState; park_reason?: string | null },
): string {
  if (cause === 'hold_expired') {
    return task.state === 'PARKED' && task.park_reason === 'HUMAN_REQUIRED'
      ? 'The hand-off is parked: nobody took control in time, so its browser closed and its capacity went back. Press I’m ready to resume — the agent starts again and asks for you.'
      : 'The hand-off is parking: nobody took control in time. Once it has parked, press I’m ready to resume.'
  }
  return `The live browser has closed${cause ? ` (${cause})` : ''}. The recording is kept for seven days.`
}

export function presetLabel(name: string): string {
  return PRESET_LABEL[name] ?? name
}

// ---------------------------------------------------------------------------
// The session read
// ---------------------------------------------------------------------------

export type LiveState =
  | { status: 'idle' }
  | { status: 'loading' }
  | {
      status: 'ok'
      liveBrowser: boolean
      session: LiveSession | null
      /**
       * The read's `browser` object (BSTATUS-STRIP), unparsed: the tool,
       * egress and Jev badges read it with `browserStatusOf`, here or on
       * `session.browser`, and draw nothing when an older API serves none.
       */
      browser?: unknown
    }
  | { status: 'error'; message: string }

/**
 * A control route's answer, when it is the session read (`LiveRead`): take,
 * return, renew, the device and member control all answer with it.
 */
export function liveReadOf(answer: unknown): LiveRead | null {
  if (typeof answer !== 'object' || answer === null) return null
  const a = answer as Partial<LiveRead>
  return typeof a.live_browser === 'boolean' && 'session' in a ? (a as LiveRead) : null
}

/** The read's top-level `browser` object, if it serves one (BSTATUS-STRIP). */
function browserOfRead(read: LiveRead): unknown {
  return (read as LiveRead & { browser?: unknown }).browser
}

/** Read the session again; given a control route's answer, draw it at once first. */
export type LiveReload = (answer?: unknown) => void

/**
 * The Browser tab (or its theater) on screen: the session and the task are
 * read again every `LIVE_POLL_MS` until the task is finished (LIVE-POLL, part
 * of #1343). `onTask` reads the task again; the agent view owns that read.
 */
export interface LiveWatch {
  on: boolean
  finished: boolean
  onTask?: () => void
}

const NOT_WATCHING: LiveWatch = { on: false, finished: false }

/** The longest wait between re-reads after failures. */
const LIVE_POLL_MAX_MS = 30_000

/**
 * The wait before the next re-read after `failures` failed reads in a row:
 * a poll, doubling per failure, never past `LIVE_POLL_MAX_MS`. A failure
 * slows the reads; it never ends them.
 */
export function liveRereadDelay(failures: number): number {
  return Math.min(LIVE_POLL_MAX_MS, LIVE_POLL_MS * 2 ** Math.min(Math.max(failures, 0), 4))
}

function pageHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

/**
 * The live session of one agent, read while its attempt is in flight and
 * again every `LIVE_POLL_MS` while its profile is a live-browser one, or
 * while `watch.on`. An agent whose profile is not, and nobody is watching,
 * is read once: a catalogue property does not change under a running task.
 *
 * WHILE WATCHED (LIVE-POLL, part of #1343), the reads do not depend on what
 * the last one said. MEASURED 2026-10-15 (release b93ad6cb): the tab read the
 * session at load and never again; the header said "dispatched", the strip
 * said no address had been reported and the device bar stayed disabled while
 * the API already served all three. The poll ran only while the last read
 * had said `live_browser`, so a read that failed at load ended the reads for
 * good, and the task was read on the drawer's 10 s clock. Now a failure backs
 * off (`liveRereadDelay`), the task is read with the session, and the reads
 * stop once the task is finished -- the tab then says so.
 *
 * NOTHING IS READ WHILE THE PAGE IS HIDDEN; showing it reads at once, rather
 * than drawing a minutes-old session for one more poll.
 *
 * `reload(answer)` DRAWS A CONTROL ROUTE'S ANSWER BEFORE THE NEXT READ
 * (QA12-22): after Return control the banner kept offering Take control
 * until a poll caught up. The answer is the session as the change left it,
 * so the tab draws that, and reads again behind it.
 */
export function useLiveSession(
  taskId: string,
  inFlight: boolean,
  watch: LiveWatch = NOT_WATCHING,
): { live: LiveState; reload: LiveReload } {
  const [got, setGot] = useState<{ taskId: string; state: LiveState } | null>(null)
  const [tick, setTick] = useState(0)
  // Failed reads in a row, for the backoff.
  const failures = useRef(0)
  const onTask = useRef(watch.onTask)
  onTask.current = watch.onTask
  const reload = useCallback(
    (answer?: unknown) => {
      const read = liveReadOf(answer)
      if (read !== null) {
        setGot({ taskId, state: { status: 'ok', liveBrowser: read.live_browser === true, session: read.session ?? null, browser: browserOfRead(read) } })
      }
      setTick((n) => n + 1)
    },
    [taskId],
  )
  const draw = useCallback(
    (r: Awaited<ReturnType<typeof loadLiveSession>>) => {
      if (r.status === 'ok' || r.status === 'stale') {
        failures.current = 0
        setGot({
          taskId,
          state: { status: 'ok', liveBrowser: r.data.live_browser === true, session: r.data.session ?? null, browser: browserOfRead(r.data) },
        })
      } else if (r.status === 'error') {
        failures.current += 1
        // A failed re-read keeps the session it had: a blip must not tear the
        // canvas down under someone typing into it.
        setGot((was) =>
          was !== null && was.taskId === taskId && was.state.status === 'ok'
            ? was
            : { taskId, state: { status: 'error', message: r.error.message } },
        )
      }
    },
    [taskId],
  )
  const liveBrowser = got !== null && got.taskId === taskId && got.state.status === 'ok' && got.state.liveBrowser

  // The read at load, and after every `reload`.
  useEffect(() => {
    if (!inFlight) return
    let gone = false
    void loadLiveSession(taskId).then((r) => {
      if (!gone) draw(r)
    })
    return () => {
      gone = true
    }
  }, [taskId, inFlight, tick, draw])

  // THE RE-READS: one at a time, each scheduled when the last has landed, so
  // a slow API is never asked twice at once and an answer is never dropped
  // for the next tick's.
  const watching = watch.on && !watch.finished
  const looping = watching || (inFlight && liveBrowser)
  useEffect(() => {
    if (!looping) return
    let stopped = false
    let busy = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const run = async (): Promise<void> => {
      timer = null
      if (stopped || busy || pageHidden()) return // a hidden page is resumed by `onVisible`
      busy = true
      if (watching) onTask.current?.()
      if (inFlight) {
        const r = await loadLiveSession(taskId)
        if (!stopped) draw(r)
      }
      busy = false
      if (!stopped) timer = setTimeout(() => void run(), liveRereadDelay(failures.current))
    }
    const onVisible = () => {
      if (pageHidden() || busy) return
      if (timer !== null) clearTimeout(timer)
      void run()
    }
    timer = setTimeout(() => void run(), liveRereadDelay(failures.current))
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      stopped = true
      if (timer !== null) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [looping, watching, inFlight, taskId, draw])

  const live: LiveState = !inFlight
    ? { status: 'idle' }
    : got === null || got.taskId !== taskId
      ? { status: 'loading' }
      : got.state
  return { live, reload }
}

// ---------------------------------------------------------------------------
// The origin strip's verdict (§5.7)
// ---------------------------------------------------------------------------

function bareHost(h: string): string {
  return h.trim().toLowerCase().replace(/\.$/, '')
}

/**
 * Whether the page is on the site the hand-off named (§5.7 item 2): the same
 * host, a subdomain of it, or the same registrable domain -- the gate serves
 * the registrable domain public-suffix aware, so `github.io` pages of two
 * different owners never match each other.
 */
export function expectMatches(origin: LiveOrigin, expect: string): boolean {
  const e = bareHost(expect)
  const host = bareHost(origin.host)
  const reg = bareHost(origin.registrable_domain)
  if (e === '' || reg === '') return false
  return host === e || host.endsWith(`.${e}`) || reg === e || e.endsWith(`.${reg}`)
}

/** Why the gate will refuse control on this page (§5.7 item 4, §5.4.4), or null. */
export function controlRefusal(origin: LiveOrigin | null): string | null {
  if (origin === null) return null
  if (origin.scheme === 'http:') return 'The page is plain HTTP, so the gate will not hand over control on it.'
  if (origin.ip_literal === true) return 'The page is an IP address, not a site name, so the gate will not hand over control on it.'
  if (origin.cert_error === true) return 'The page reports a certificate error, so the gate will not hand over control on it.'
  return null
}

export type StripTone = 'unknown' | 'ok' | 'warn' | 'mismatch' | 'refused'

/** The strip's colour and the one sentence that says why. */
export function stripVerdict(s: LiveSession): { tone: StripTone; say: string } {
  const o = s.origin
  // A BLANK PAGE HAS NO ADDRESS TO REPORT (BT-014, part of #2423). MEASURED
  // 2026-10-26: "has not reported the page's address yet" over a frame whose
  // own address bar read about:blank -- the likelier of the two causes.
  if (o === null) return { tone: 'unknown', say: 'The page is blank (about:blank), or the gate has not reported the page’s address yet.' }
  const refused = controlRefusal(o)
  if (refused !== null) return { tone: 'refused', say: refused }
  const wait = s.human_wait
  if (wait === null) return { tone: 'ok', say: 'The address Chromium reports for the page the agent is on.' }
  if (wait.expect === null || wait.expect.trim() === '') {
    return { tone: 'warn', say: 'The agent did not say which site it wants you to sign in to. Check the address before you type anything.' }
  }
  if (!expectMatches(o, wait.expect)) {
    return {
      tone: 'mismatch',
      say: `This page is ${o.registrable_domain}, but the agent said it would ask you to sign in to ${bareHost(wait.expect)}.`,
    }
  }
  return { tone: 'ok', say: `The page is on ${bareHost(wait.expect)}, the site the agent named.` }
}

// ---------------------------------------------------------------------------
// Whether the agent is using this browser (LIVE-UNUSED, #1030)
// ---------------------------------------------------------------------------

/**
 * How long after the first frame a RUNNING agent may go without connecting
 * before the strip says so. MEASURED 2026-10-13 (task_7cda0d5335f049b4b442):
 * the agent worked in a browser of its own while this one stayed blank, and
 * nothing on the tab said so. A minute is well past the seconds Playwright
 * takes to connect once the agent decides to, and short enough that a person
 * watching a blank page is told before they give up on it.
 */
export const AGENT_UNUSED_AFTER_MS = 60_000

/**
 * When the "isn't using this browser yet" note is due (ms since epoch), or
 * null when it never is for this reading: the task is not RUNNING, the agent
 * has connected (now or ever), the gate has not reported its connections
 * (an older gate: unknown is never shown as "not using"), or no first frame
 * has been reported to time it from.
 */
export function agentUnusedDue(
  session: Pick<LiveSession, 'state' | 'agent_connected_at' | 'agent_connections' | 'first_frame_at'>,
  taskState: TaskState,
): number | null {
  if (taskState !== 'RUNNING' || session.state !== 'open') return null
  if (typeof session.agent_connections !== 'number' || session.agent_connections > 0) return null
  if (session.agent_connected_at != null) return null
  const frame = session.first_frame_at != null ? Date.parse(session.first_frame_at) : NaN
  return Number.isFinite(frame) ? frame + AGENT_UNUSED_AFTER_MS : null
}

/** True once the note is due; re-rendered by a timer at the due time, and false the moment the agent connects. */
export function useAgentUnused(session: LiveSession | null, taskState: TaskState): boolean {
  const due = session === null ? null : agentUnusedDue(session, taskState)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (due === null) return
    const wait = due - Date.now()
    if (wait <= 0) {
      setNow(Date.now())
      return
    }
    const t = setTimeout(() => setNow(Date.now()), wait)
    return () => clearTimeout(t)
  }, [due])
  return due !== null && Math.max(now, Date.now()) >= due
}

function AgentUnused({ task }: { task: Task }) {
  return (
    <span className="live-strip-unused" data-testid="agent-unused">
      <strong>The agent isn’t using this browser yet.</strong>{' '}
      It may be working in another tool; its log shows what it is doing.{' '}
      <a href={`${agentPath(task)}/logs`}>Open the logs</a>
    </span>
  )
}

function isController(s: LiveSession): boolean {
  return s.controller !== null && s.controller.email === s.you.email
}

// ---------------------------------------------------------------------------
// Take control / Return control, in the agent header
// ---------------------------------------------------------------------------

/**
 * AN ASK IS A FOCUSED DIALOG (QA12-09). MEASURED in QA p12: the take-control
 * ask did not take focus, carried no aria-modal, and Esc closed it only
 * after tabbing into it. Open, it focuses its first button; Tab stays inside
 * it; Esc closes it from anywhere on the page (and not the theater behind
 * it); closed, focus goes back to what opened it.
 */
function useAsk(open: boolean, close: () => void): { ref: RefObject<HTMLDivElement> } {
  const ref = useRef<HTMLDivElement>(null)
  const closeRef = useRef(close)
  closeRef.current = close
  useEffect(() => {
    if (!open) return
    const back = typeof document !== 'undefined' ? (document.activeElement as HTMLElement | null) : null
    ref.current?.querySelector<HTMLElement>('button:not([disabled])')?.focus()
    const onKey = (e: globalThis.KeyboardEvent) => {
      const node = ref.current
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        closeRef.current()
        return
      }
      if (e.key !== 'Tab' || node === null) return
      const buttons = Array.from(node.querySelectorAll<HTMLElement>('button:not([disabled])'))
      if (buttons.length === 0) return
      const [first, last] = [buttons[0]!, buttons[buttons.length - 1]!]
      const at = document.activeElement
      if (!node.contains(at)) {
        e.preventDefault()
        first.focus()
      } else if (e.shiftKey && at === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && at === last) {
        e.preventDefault()
        first.focus()
      }
    }
    // Capture, on the window: before the theater's own Esc, and wherever focus is.
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.removeEventListener('keydown', onKey, true)
      if (back !== null && back.isConnected) back.focus()
    }
  }, [open])
  return { ref }
}

/**
 * TAKE CONTROL, IN THE HEADER BESIDE STOP (§6). Drawn only for a person the
 * server says may take control now (§5.1): the submitter, a member once the
 * submitter opened control, and the submitter over someone else's control.
 * Anyone else is told who holds it, and gets no button.
 *
 * IT ASKS FIRST, AND TWICE ON THE WRONG SITE (§5.7). The first ask says what
 * taking control means and recommends a purpose-made account. When the page
 * is not on the site the hand-off named, a second ask names both domains in
 * red, and only a yes to it takes control; the request says what was
 * confirmed past, so the audit has it.
 *
 * THE AGENT'S REASON STAYS IN VIEW (QA12-09): the ask dropped over the
 * hand-off banner and hid the reason the person was deciding on, so the ask
 * quotes it as its first line.
 */
export function LiveControl({
  taskId,
  session,
  reload,
  takeOnly = false,
  refusal = 'line',
}: {
  taskId: string
  session: LiveSession | null
  reload: LiveReload
  /**
   * The hand-off banner's copy of the button (LB-UX1): Take control, the same
   * action as the header's, and nothing at all for anyone who may not take
   * it now -- no "View only", no Return control, which stay the header's --
   * nor once the session has no hand-off: after Return control the banner
   * offered Take control until the task was read again (QA12-22).
   */
  takeOnly?: boolean
  /**
   * How a page the gate refuses control on (`controlRefusal`) is said: as a
   * line beside the disabled button, or as its title only, where the origin
   * strip already says it as a line (the Browser tab, BT-010: a plain-HTTP
   * page's refusal was said twice and wrapped the header).
   */
  refusal?: 'line' | 'title'
}) {
  const [ask, setAsk] = useState<'none' | 'first' | 'mismatch'>('none')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mine = session !== null && isController(session)
  const close = useCallback(() => setAsk('none'), [])
  const first = useAsk(ask === 'first', close)
  const mismatch = useAsk(ask === 'mismatch', close)

  // The controller's renewal: while this console holds control it says so,
  // and a closed tab stops saying it, which ends control within 60 s (§4.2).
  useEffect(() => {
    if (!mine) return
    const id = setInterval(() => {
      void renewLiveControl(taskId)
    }, LIVE_RENEW_MS)
    return () => clearInterval(id)
  }, [mine, taskId])

  if (session === null || session.state !== 'open') return null
  if (takeOnly && session.human_wait === null) return null
  const verdict = stripVerdict(session)
  const holder = session.controller

  if (mine) {
    if (takeOnly) return null
    return (
      <span className="live-control">
        <Button
          kind="primary"
          busy={busy ? 'Returning…' : false}
          onClick={() => {
            setBusy(true)
            void returnLiveControl(taskId).then((r) => {
              setBusy(false)
              setError(r.status === 'error' ? r.error.message : null)
              // The answer is the session with control returned: drawn now,
              // so no copy of Take control outlives the hand-off (QA12-22).
              reload(r.status === 'ok' ? r.data : undefined)
            })
          }}
        >
          Return control
        </Button>
        {error !== null && <span className="live-control-error" role="alert">{error}</span>}
      </span>
    )
  }

  const eligible = session.you.can_control && (holder === null || session.you.can_preempt)
  if (!eligible) {
    if (takeOnly) return null
    return (
      <span className="live-control live-control-view" title="Only the submitter, or a member they opened control to, can take control.">
        {holder === null ? 'View only' : `${holder.email} is in control`}
      </span>
    )
  }

  const refused = controlRefusal(session.origin)
  const take = () => {
    const o = session.origin
    const expect = session.human_wait?.expect ?? null
    setBusy(true)
    setAsk('none')
    void takeLiveControl(taskId, {
      ...(holder !== null ? { preempt: true } : {}),
      ...(verdict.tone === 'mismatch' && o !== null && expect !== null
        ? { confirmed_mismatch: { expected: bareHost(expect), actual: o.registrable_domain } }
        : {}),
    }).then((r) => {
      setBusy(false)
      setError(r.status === 'error' ? r.error.message : null)
      reload(r.status === 'ok' ? r.data : undefined)
    })
  }
  const label = holder === null ? 'Take control' : `Take control from ${holder.email}`
  const reason = session.human_wait?.reason ?? null

  return (
    <span className="live-control">
      <Button
        kind="primary"
        busy={busy ? 'Taking control…' : false}
        disabledReason={refused === null || refusal === 'title' ? undefined : refused}
        disabled={refused !== null}
        title={refusal === 'title' && refused !== null ? refused : undefined}
        onClick={() => setAsk('first')}
      >
        {label}
      </Button>
      {error !== null && <span className="live-control-error" role="alert">{error}</span>}
      {ask === 'first' && (
        <div ref={first.ref} className="live-ask" role="dialog" aria-modal="true" aria-label="Take control of the agent’s browser">
          {reason !== null && reason.trim() !== '' && (
            <p className="live-ask-reason">
              The agent needs your help: <b>{reason}</b>
            </p>
          )}
          <h4>Take control of the agent’s browser?</h4>
          <p>
            The agent waits while you are in control, and every member watching sees the page; password fields are
            painted over. Where the page wants an account, use one made for this agent where you can, not your own.
          </p>
          {verdict.tone === 'warn' && <p className="live-ask-warn">{verdict.say}</p>}
          <span className="live-ask-actions">
            <Button kind="primary" onClick={() => (verdict.tone === 'mismatch' ? setAsk('mismatch') : take())}>
              {label}
            </Button>
            <Button onClick={close}>Cancel</Button>
          </span>
        </div>
      )}
      {ask === 'mismatch' && session.origin !== null && (
        <div
          ref={mismatch.ref}
          className="live-ask is-mismatch"
          role="alertdialog"
          aria-modal="true"
          aria-label="This is not the site the agent named"
        >
          <h4>This is not the site the agent named</h4>
          <p>
            The page is <strong>{session.origin.registrable_domain}</strong>. The agent asked for help on{' '}
            <strong>{bareHost(session.human_wait?.expect ?? '')}</strong>. A page that only looks like a sign-in page
            is how a password is stolen.
          </p>
          <span className="live-ask-actions">
            <Button kind="danger" onClick={take}>
              Take control on {session.origin.registrable_domain}
            </Button>
            <Button onClick={close}>Cancel</Button>
          </span>
        </div>
      )}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The Browser tab
// ---------------------------------------------------------------------------

/** The Browser tab: hand-off, origin strip, toolbar, and the canvas (with the logs beside it when asked). */
export function LiveBrowserPane({
  task,
  live,
  reload,
  theater: openTheater = false,
  onHandoffReady,
  onPicture,
}: {
  task: Task
  live: LiveState
  reload: LiveReload
  /**
   * Told whether the screen has a picture: true once noVNC reports a frame,
   * false while it has none (ovag2-12). The tab strip says live on this, not
   * on the session read alone, which an empty frame also has.
   */
  onPicture?: (shown: boolean) => void
  /** Open in the theater (`/browser?theater=1`): the graph card, the banner and a shared link land here. */
  theater?: boolean
  /** "I'm ready to sign in" was accepted: the agent's view reads the task again. */
  onHandoffReady?: () => void
}) {
  const [logs, setLogs] = useState(false)
  // The element noVNC draws into, for the theater to borrow; null until LiveScreen has mounted it.
  const [screenNode, setScreenNode] = useState<HTMLElement | null>(null)
  // The gate's word that the browser closed, while its desktop name carries it (`gateReason`).
  const [cover, setCover] = useState<string | null>(null)
  // The frame noVNC draws (`LiveScreen`'s `onFrame`): what the caption checks the gate's report against.
  const [frame, setFrame] = useState<FrameSize | null>(null)
  // The device the person in control last asked for, on the session they asked on (`LiveDevice`'s `onAsked`).
  const [asked, setAsked] = useState<DeviceAsk | null>(null)
  // The canvas as it last drew, kept when the screen goes (`LiveScreen`'s
  // `onLastFrame`): a live agent that finishes while watched leaves it here.
  const [lastFrame, setLastFrame] = useState<string | null>(null)
  // The logs column's share of the body, in percent (`LogSplit`).
  const [split, setSplit] = useState(readSplit)
  const pictureRef = useRef(onPicture)
  pictureRef.current = onPicture
  const shown = frame !== null
  useEffect(() => {
    pictureRef.current?.(shown)
  }, [shown])
  useEffect(() => () => pictureRef.current?.(false), [])
  const session = live.status === 'ok' ? live.session : null
  const open = session !== null && session.state === 'open' ? session : null
  // A CHANGE IS WAITED ON UNTIL THE GATE REPORTS IT (LIVE-POLL, #1343): read
  // again every `DEVICE_CONFIRM_MS` meanwhile, and rotate and touch held, so
  // neither acts on the device from before the change.
  const unapplied = asked !== null && open !== null && asked.sessionId === open.session_id && !deviceApplied(open.device, asked.body)
  const waiting = useDeviceReport(unapplied ? asked : null, reload)
  const returned = useControlReturned(open?.session_id ?? null, open !== null && isController(open), open?.controller ?? null)
  const unused = useAgentUnused(open, task.state)

  // THE THEATER ASKED FOR IS HELD THROUGH START-UP (part of #1346, comment
  // 3). The router reads `?theater=1` on arrival only and writes the bar back
  // without it (App.tsx `Route.theater`), and <TheaterHost> is drawn only
  // once a session exists: a tab opened while the browser was starting read
  // `openTheater` after the route had dropped it, and opened without the
  // theater. The request is kept here from arrival (or a later ask) and
  // spent on the first host drawn, so a browser that restarts does not
  // reopen a theater the person closed.
  const theaterAsked = useRef(openTheater)
  const theaterWas = useRef(openTheater)
  if (openTheater && !theaterWas.current) theaterAsked.current = true
  theaterWas.current = openTheater
  const hosted = session !== null && session.state !== 'closed'
  useEffect(() => {
    if (hosted) theaterAsked.current = false
  }, [hosted])

  // One object per hand-off, not per poll: the theater turns its left column
  // to the checklist whenever this changes, and a fresh object every
  // `LIVE_POLL_MS` would take the column back from someone reading the logs.
  const wait = open?.human_wait ?? null
  const [reason, expect, deadline] = [wait?.reason ?? null, wait?.expect ?? null, wait?.deadline ?? null]
  const handoff = useMemo<TheaterHandoff | null>(
    () =>
      reason === null || deadline === null
        ? null
        : { reason, expect: expect !== null && expect.trim() !== '' ? bareHost(expect) : 'no site named', deadline },
    [reason, expect, deadline],
  )

  // THE ONE HAND-OFF BANNER ON THIS TAB (LB-UX1): the agent view's strip
  // steps aside here, and this one is drawn whatever the session is -- a
  // parked hand-off has no session at all, and says "I'm ready to sign in".
  const banner = (actions?: ReactNode) => (
    <HandoffStrip task={task} onReady={onHandoffReady}>
      {actions}
    </HandoffStrip>
  )
  const note = (say: ReactNode) => (
    <div className="live-tab">
      {banner()}
      <p className="live-note">{say}</p>
    </div>
  )

  if (live.status === 'idle') {
    // No attempt in flight: between attempts (a hand-off parked, or READY
    // after "I'm ready to sign in"), or finished.
    if (TERMINAL_STATES.has(task.state)) {
      return (
        <FinishedBrowser task={task} lastFrame={lastFrame} logs={logs} split={split} onSplit={setSplit}>
          {banner()}
        </FinishedBrowser>
      )
    }
    return note('The agent’s browser is not running. It starts when the agent is admitted again, and appears here.')
  }
  if (live.status === 'loading') {
    return note('Reading the agent’s live browser.')
  }
  if (live.status === 'error') {
    return note(`The live browser could not be read: ${live.message}`)
  }
  if (session === null) {
    // THE START-UP, STEP BY STEP (owner request 2026-10-13): what the pod has
    // done so far and what is left, where this said only "starting".
    return (
      <div className="live-tab">
        {banner()}
        <StartupProgress taskId={task.id} state={task.state} />
      </div>
    )
  }
  if (session.state === 'closed') {
    return note(closedSessionNote(session.closed_reason ?? null, task))
  }

  const mine = isController(session)
  const verdict = stripVerdict(session)
  // A PRESET ASKED FOR IS SAID UNTIL THE GATE REPORTS IT (STRIP-LABEL, part of
  // #1341). `session.device` is what the gate last REPORTED (swarm-api
  // live.py `set_device`): the device route answers with the session as it
  // was before the switch, and the device changes only when the gate's report
  // reaches a later read. Until then the caption said the old preset ("iPad")
  // with nothing of the Desktop the person had just chosen. It goes when the
  // gate reports the preset asked for, or the session is another.
  const pending =
    asked !== null && asked.sessionId === session.session_id && session.device?.preset !== asked.body.preset ? asked.body.preset : null
  const onAsked = (body: DeviceBody) => setAsked({ sessionId: session.session_id, body, at: Date.now() })
  const o = session.origin
  // The tool, egress and Jev badges (BSTATUS-STRIP): the API may serve the
  // object on the session or beside it; either, and none from an older one.
  const tools = browserStatusOf(
    (session as LiveSession & { browser?: unknown }).browser ?? (live.status === 'ok' ? live.browser : undefined),
  )
  const theater: TheaterSession = {
    taskId: task.id,
    tenantId: task.tenant_id,
    // The session read serves no attempt id, and the theater reads none from
    // here: each recording segment it replays names its own attempt.
    attemptId: '',
    sessionId: session.session_id,
    screen: screenNode,
    live: true,
    browser: session, reload, // the device, the controller and the return, for the theater (LB-UX2a)
  }
  const who =
    session.controller === null
      ? 'Nobody is in control: the agent is driving.'
      : mine
        ? 'You are in control.'
        : `${session.controller.email} is in control.`
  // ONE LINE: THE ADDRESS, WHO DRIVES, AND THE BADGES (BT-003, part of
  // #2423). MEASURED 2026-10-26 at 1440: the strip was three lines, 103 px
  // of a 407 px pane, mostly the sentence explaining a green address. That
  // sentence is folded under ⓘ; a warning (a hand-off naming no site, the
  // wrong site, a page the gate refuses control on) is the point of the
  // strip, and stays a line of its own -- the one place the tab says it
  // (BT-010).
  const warns = verdict.tone === 'warn' || verdict.tone === 'mismatch' || verdict.tone === 'refused'
  const strip = (
    <div className={`live-strip is-${verdict.tone}`} data-tone={verdict.tone} role="status">
      <div className="live-strip-line">
        <span className="live-origin" title={o === null ? undefined : o.origin}>
          {o === null ? (
            'no web address'
          ) : (
            <>
              <span className="live-origin-scheme">{o.scheme}//</span>
              <OriginHost origin={o} />
            </>
          )}
        </span>
        <span className="live-strip-who" title={who}>
          {who}
        </span>
        {!warns && (
          <details className="live-strip-why">
            <summary title="About this address" aria-label="About this address">
              ⓘ
            </summary>
            <span className="live-strip-say">{verdict.say}</span>
          </details>
        )}
        <BrowserBadges status={tools} qaIdentity={qaIdentityOf(task.input)} />
      </div>
      {warns && <span className="live-strip-say">{verdict.say}</span>}
      {/* THE GATE'S WORD IS IN THE STRIP (STRIP-LABEL, part of #1341).
          MEASURED 2026-10-15: under the screen, it sat below the fold
          behind the theater. The strip is drawn in both. */}
      {cover !== null && (
        <span className="live-strip-cover" data-testid="live-cover">
          {cover}.
        </span>
      )}
      {unused && <AgentUnused task={task} />}
    </div>
  )
  return (
    <TheaterHost
      session={theater}
      initiallyOpen={theaterAsked.current}
      strip={strip}
      handoff={handoff}
      logs={<AgentLogs task={task} />}
      device={<LiveDevice taskId={task.id} session={session} reload={reload} onAsked={onAsked} waiting={waiting} />}
      control={<LiveControl taskId={task.id} session={session} reload={reload} />}
      take={<LiveControl taskId={task.id} session={session} reload={reload} takeOnly />}
    >
      <div className={`live-tab${logs ? ' with-logs' : ''}`}>
        {banner(
          <>
            {/* The refusal is the strip's line (BT-010); here it is the disabled button's title. */}
            <LiveControl taskId={task.id} session={session} reload={reload} takeOnly refusal="title" />
            <OpenInTheater />
          </>,
        )}
        {strip}
        <LiveToolbar
          taskId={task.id}
          session={session}
          logs={logs}
          onLogs={() => setLogs((v) => !v)}
          reload={reload}
          onAsked={onAsked}
          waiting={waiting}
        />
        <SaveAsLoginOffer taskId={task.id} tenantId={task.tenant_id} site={o?.origin ?? null} controlReturned={returned} />
        {/* LOGS ON THE LEFT, THE DEVICE ON THE RIGHT (the mockup's ◫ logs),
            split where the person drags it; stacked in a narrow tab (live.css). */}
        <div className="live-body" style={splitStyle(split)}>
          {logs && (
            <div className="live-logs">
              <AgentLogs task={task} />
            </div>
          )}
          {logs && <LogSplit value={split} onChange={setSplit} />}
          <LiveScreen
            key={session.session_id}
            taskId={task.id}
            sessionId={session.session_id}
            control={mine}
            touch={mine && session.device !== null && session.device.touch}
            onScreen={setScreenNode}
            onGateSays={setCover}
            onFrame={setFrame}
            onLastFrame={setLastFrame}
            device={deviceSize(session)}
            fitTo={fitShape(deviceSize(session), frame)}
            caption={
              <span className="live-caption">
                {deviceCaption(session, frame)}
                {pending !== null && (
                  <span className="live-caption-asked" data-testid="live-device-asked">
                    {` · ${presetLabel(pending)} asked for; the browser has not applied it yet`}
                  </span>
                )}
              </span>
            }
            onControlChange={reload}
            onSessionMoved={reload}
          />
        </div>
      </div>
    </TheaterHost>
  )
}

/**
 * True once this person's control of the session ends with nobody in control
 * (Return control, or a lapsed renewal), until they take it again. Read from
 * the polled session: the console has no other word of a return, and a
 * takeover by someone else, or a new session, is not one.
 */
function useControlReturned(sessionId: string | null, mine: boolean, controller: LiveSession['controller']): boolean {
  const was = useRef<{ sessionId: string | null; mine: boolean }>({ sessionId, mine })
  const [returned, setReturned] = useState(false)
  const nobody = controller === null
  useEffect(() => {
    const before = was.current
    was.current = { sessionId, mine }
    if (before.sessionId !== sessionId || mine || !nobody) setReturned(false)
    else if (before.mine) setReturned(true)
  }, [sessionId, mine, nobody])
  return returned
}

/** The host with its registrable domain emphasised, and a punycode host shown decoded and flagged (§5.7 item 1). */
function OriginHost({ origin }: { origin: LiveOrigin }) {
  const host = origin.display_host ?? origin.host
  const reg = origin.registrable_domain
  const at = reg !== '' && host.endsWith(reg) ? host.length - reg.length : -1
  const puny = origin.host.split('.').some((label) => label.startsWith('xn--'))
  return (
    <span className="live-origin-host">
      {at > 0 && <span className="live-origin-sub">{host.slice(0, at)}</span>}
      <strong>{at >= 0 ? reg : host}</strong>
      {puny && (
        <span className="live-origin-puny" title={`Chromium reports this host as ${origin.host}`}>
          punycode: {origin.host}
        </span>
      )}
    </span>
  )
}

/** The banner's Open in theater: the theater of this tab, which the banner sits inside. */
function OpenInTheater() {
  const theater = useTheater()
  if (theater === null) return null
  return (
    <Button size="sm" onClick={theater.open}>
      Open in theater
    </Button>
  )
}

/**
 * THE DEVICE AS DRAWN, IN CSS PIXELS (LB-V1): the preset's size with its
 * sides in the orientation's order -- the long side across in landscape, down
 * in portrait. Ordered rather than swapped, because the gate's presets are not
 * all written portrait: desktop's natural orientation is landscape and its
 * preset is already 1440×900, so a swap printed "Desktop · 900×1440" for the
 * desktop the person was looking at (visual QA 5, B2). Null while the gate
 * has not reported a device, or reports one not in the list.
 */
export function deviceSize(
  session: Pick<LiveSession, 'device' | 'presets'>,
): { width: number; height: number; scale: number; mobile: boolean } | null {
  const d = session.device
  const p = d === null ? undefined : session.presets.find((x) => x.name === d.preset)
  if (d === null || p === undefined) return null
  const [long, short] = [Math.max(p.width, p.height), Math.min(p.width, p.height)]
  return d.orientation === 'landscape'
    ? { width: long, height: short, scale: p.scale, mobile: p.mobile }
    : { width: short, height: long, scale: p.scale, mobile: p.mobile }
}

/**
 * UNDER THE DEVICE, WHAT IT IS (the mockup's caption): `iPhone 15 · 393×852
 * · <path>`, width × height as the device is turned (`deviceSize`). The path
 * is the one the gate read from the top frame and reported beside the origin
 * (`origin.path`, redacted, no query: LB-R6 -- until then nothing carried it,
 * and every live session read "path not reported", re-QA pass 8). With no
 * path reported it still says so rather than print one nobody measured, and
 * a device the gate has not reported is said the same way (LB-SEAM).
 */
export function deviceCaption(
  session: Pick<LiveSession, 'device' | 'presets' | 'origin'>,
  frame: FrameSize | null = null,
): string {
  const d = session.device
  const size = deviceSize(session)
  const seen = size === null ? null : frameContradicts(size, frame)
  const device =
    d === null
      ? 'Device not reported'
      : seen !== null
        ? `Device not confirmed: the gate reported ${presetLabel(d.preset)} (${d.orientation}), but the screen it sends is ${seen}`
        : size === null
          ? presetLabel(d.preset)
          : `${presetLabel(d.preset)} · ${size.width}×${size.height}`
  // No origin at all is the strip's "no web address" (BT-014): a blank page
  // has no path to report, and "path not reported" read as a fault under a
  // frame showing about:blank.
  if (session.origin == null) return `${device} · no web address`
  const path = session.origin.path
  return `${device} · ${typeof path === 'string' && path !== '' ? path : 'path not reported'}`
}

/** The framebuffer noVNC draws, in the gate's screen pixels (`LiveScreen`'s `onFrame`). */
export interface FrameSize {
  width: number
  height: number
}

/**
 * A REPORT THE SCREEN CONTRADICTS IS NOT THE DEVICE (LB-GREY; part of #1341).
 * MEASURED 2026-10-15 on task_2ac5d7e22a51460995a2: the caption read "iPad ·
 * 810×1080" under a session whose person in control had Desktop. Every device
 * label here is `session.device`, which swarm-api writes from the gate's
 * report alone (live.py `record_report`): this file does not derive a preset,
 * it prints the one reported. The session's own device is the frame the gate
 * sends, cropped to the device's display (livegate/device.py `captured`), so
 * the two are compared by the one thing that survives the browser's band and
 * the screen scale: which way round they are. The frame's orientation
 * ("landscape" or "portrait") when it is the other way from the reported
 * device's, else null -- also with no frame yet, or a square one.
 */
export function frameContradicts(device: { width: number; height: number }, frame: FrameSize | null): string | null {
  if (frame === null || frame.width <= 0 || frame.height <= 0 || frame.width === frame.height) return null
  if (device.width === device.height) return null
  const wide = frame.width > frame.height
  return wide === device.width > device.height ? null : wide ? 'landscape' : 'portrait'
}

/**
 * THE DEVICE, FITTED INTO THE STAGE (LB-V1, visual QA 5 B3): the largest box
 * of the device's proportions that fits `room` both ways -- CSS `contain`,
 * whole pixels, never larger than the room. An iPhone 15 frame drawn to the
 * stage's width alone was 692×1552 in a 900 px window, and only its top 40%
 * could be seen. Null when there is no room to fit into (not laid out yet).
 */
export function fitDevice(
  room: { width: number; height: number },
  device: { width: number; height: number },
): { width: number; height: number } | null {
  if (room.width <= 0 || room.height <= 0 || device.width <= 0 || device.height <= 0) return null
  // The bounding side is the room's exactly; the other is cut to whole pixels,
  // computed in one division so float error never takes a pixel off the first.
  if (room.width * device.height <= room.height * device.width) {
    return { width: Math.floor(room.width), height: Math.max(1, Math.floor((device.height * room.width) / device.width)) }
  }
  return { width: Math.max(1, Math.floor((device.width * room.height) / device.height)), height: Math.floor(room.height) }
}

/**
 * THE PROPORTIONS THE FRAME IS FITTED TO (BT-014, part of #2423): the frame
 * the gate sends, once noVNC reports it and it is the way round the device
 * is, else the device's. MEASURED 2026-10-26 at 1440: the desktop preset is
 * 1440×900 but the gate's screen is 1440×987 (the browser's own bar is in
 * it), so a box of the preset's proportions drew the canvas 554 px wide in
 * 609 px, with black bars each side. A frame that contradicts the device is
 * not trusted for the fit (`frameContradicts`), and with no device there is
 * no fit at all, as before.
 */
export function fitShape(
  device: { width: number; height: number } | null,
  frame: FrameSize | null,
): { width: number; height: number } | null {
  if (device === null) return null
  if (frame === null || frame.width <= 0 || frame.height <= 0 || frameContradicts(device, frame) !== null) {
    return { width: device.width, height: device.height }
  }
  return { width: frame.width, height: frame.height }
}

// ---------------------------------------------------------------------------
// The logs beside the frame: a split the person drags (BT-002)
// ---------------------------------------------------------------------------

/** The logs column's share of the body, in percent: its default and its bounds. */
const SPLIT_DEFAULT = 45
const SPLIT_MIN = 20
const SPLIT_MAX = 75
const SPLIT_STEP = 5
/** Where the share is kept, so the next agent's tab opens split the same way. */
const SPLIT_KEY = 'swarm.live.logsSplit'

function clampSplit(v: number): number {
  return Math.round(Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, v)))
}

function readSplit(): number {
  try {
    const v = Number(window.localStorage.getItem(SPLIT_KEY))
    return Number.isFinite(v) && v > 0 ? clampSplit(v) : SPLIT_DEFAULT
  } catch {
    return SPLIT_DEFAULT
  }
}

function keepSplit(v: number): void {
  try {
    window.localStorage.setItem(SPLIT_KEY, String(v))
  } catch {
    /* storage refused: the split holds for this tab only */
  }
}

/** The body's `--live-split`, which live.css reads for the logs column's width. */
function splitStyle(split: number): CSSProperties {
  return { '--live-split': `${split}%` } as CSSProperties
}

/**
 * THE SPLIT BETWEEN THE LOGS AND THE FRAME (BT-002, part of #2423). MEASURED
 * 2026-10-26: a fixed 2:3 split gave the log 241 px at 1440 and 208 at 1024,
 * and the frame a quarter of its page. The person drags it (or moves it with
 * the arrow keys, Home and End), between 20% and 75% of the body; in a
 * narrow tab the two stack and this is not drawn (live.css `@container`).
 */
function LogSplit({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const set = (v: number) => {
    const next = clampSplit(v)
    onChange(next)
    keepSplit(next)
  }
  const drag = (e: ReactPointerEvent<HTMLDivElement>) => {
    const body = e.currentTarget.parentElement
    if (body === null) return
    e.preventDefault()
    const box = body.getBoundingClientRect()
    if (box.width <= 0) return
    const move = (ev: globalThis.MouseEvent) => set(((ev.clientX - box.left) / box.width) * 100)
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
  }
  return (
    <div
      className="live-split"
      role="separator"
      aria-orientation="vertical"
      aria-label="Logs width"
      aria-valuemin={SPLIT_MIN}
      aria-valuemax={SPLIT_MAX}
      aria-valuenow={value}
      tabIndex={0}
      title="Drag to share the width between the logs and the browser"
      onPointerDown={drag}
      onKeyDown={(e) => {
        const to =
          e.key === 'ArrowLeft'
            ? value - SPLIT_STEP
            : e.key === 'ArrowRight'
              ? value + SPLIT_STEP
              : e.key === 'Home'
                ? SPLIT_MIN
                : e.key === 'End'
                  ? SPLIT_MAX
                  : null
        if (to === null) return
        e.preventDefault()
        set(to)
      }}
    />
  )
}

// ---------------------------------------------------------------------------
// A finished agent's Browser tab (BT-006)
// ---------------------------------------------------------------------------

/**
 * WHAT IS LEFT OF THE BROWSER ONCE THE AGENT HAS FINISHED (BT-006, part of
 * #2423). MEASURED 2026-10-26: one sentence in an empty pane, "Its
 * recordings are on Attempts" with Attempts plain text, the recordings
 * 2,450 px down that tab under its history, no end time, and the frame and
 * the logs vanishing the instant a watched agent finished. Now: when it
 * ended, links to the recordings' heading on Attempts and to the screenshots
 * on Artifacts, the last frame the screen drew (when this tab was watching
 * it), the logs if they were open, and the recordings with their player,
 * here. The recordings are RecordingsPanel's, the one Attempts draws.
 */
function FinishedBrowser({
  task,
  lastFrame,
  logs,
  split,
  onSplit,
  children,
}: {
  task: Task
  lastFrame: string | null
  logs: boolean
  split: number
  onSplit: (v: number) => void
  children?: ReactNode
}) {
  const base = agentPath(task)
  const ended = task.completed_at == null ? null : new Date(task.completed_at)
  const at = ended === null || Number.isNaN(ended.getTime()) ? null : ended.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return (
    <div className={`live-tab is-finished${logs ? ' with-logs' : ''}`}>
      {children}
      <p className="live-note live-ended">
        <span className="live-ended-when">{at === null ? 'ended' : `ended ${at}`}</span>
        {' · '}This agent has finished, and its live browser closed with it. Its recordings are below and on{' '}
        <a href={`${base}/attempts#rec-h-${task.id}`}>Attempts</a>; the screenshots it saved are on <a href={`${base}/artifacts`}>Artifacts</a>.
      </p>
      {(lastFrame !== null || logs) && (
        <div className={`live-body${lastFrame === null ? ' is-logs-only' : ''}`} style={splitStyle(split)}>
          {logs && (
            <div className="live-logs">
              <AgentLogs task={task} />
            </div>
          )}
          {logs && lastFrame !== null && <LogSplit value={split} onChange={onSplit} />}
          {lastFrame !== null && (
            <figure className="live-last">
              <img src={lastFrame} alt="The agent’s browser as it last drew, before it closed." />
              <figcaption className="live-caption">The last frame this tab saw, before the browser closed.</figcaption>
            </figure>
          )}
        </div>
      )}
      <RecordingsPanel task={task} />
    </div>
  )
}

/**
 * THE TOOLBAR IS TWO GROUPS (BT-011, part of #2423): at the left what
 * changes THE AGENT'S BROWSER -- the device menu and "Let any member take
 * control" -- and at the right what changes THIS VIEW, `◫ logs` and
 * `⤢ Theater`. MEASURED 2026-10-26: the member checkbox sat right beside
 * Theater (one thumb-width from it at 390) and the device chips looked like
 * view toggles. This replaces the 2026-10-12 order (checkbox after
 * Theater), which BGD's 2026-10-26 review asked to change.
 *
 * THE CHIPS ARE ONE MENU (BT-003): `Device: <preset> ▾`. Eight chips were
 * 408 px of a 611 px pane and took the toolbar to two rows above the frame.
 * At phone width LiveToolMenu's `⋯` holds them instead, as before.
 *
 * THE DEVICE CONTROLS ARE SWITCHED BY THE PERSON IN CONTROL (§6.1, Q13;
 * LB-DEV-B): enabled when `you.can_switch_device`, disabled with why for
 * everyone else. The why is the disabled controls' tooltip and their
 * accessible description, NOT a line in the toolbar: as a line it pushed
 * ⤢ Theater to a second row at 1440 with the agent list open, and the
 * toolbar to three rows at 1024 (visual QA 5, U3; LB-V1).
 */
function LiveToolbar({
  taskId,
  session,
  logs,
  onLogs,
  reload,
  onAsked,
  waiting = false,
}: {
  taskId: string
  session: LiveSession
  logs: boolean
  onLogs: () => void
  reload: LiveReload
  onAsked?: (body: DeviceBody) => void
  waiting?: boolean
}) {
  return (
    <div className="live-toolbar" role="toolbar" aria-label="Browser view">
      <span className="live-toolbar-control" role="group" aria-label="The agent’s browser">
        <LiveToolMenu>
          <DeviceMenu session={session}>
            <LiveDevice taskId={taskId} session={session} reload={reload} onAsked={onAsked} waiting={waiting} />
          </DeviceMenu>
        </LiveToolMenu>
        {session.you.can_open && <MemberControl taskId={taskId} session={session} reload={reload} />}
      </span>
      <span className="live-toolbar-end" role="group" aria-label="This view">
        <Button size="sm" aria-pressed={logs} onClick={onLogs} title={logs ? 'Hide the logs' : 'Show the logs beside the browser'}>
          ◫ logs
        </Button>
        <TheaterButton />
      </span>
    </div>
  )
}

/**
 * `Device: <preset> ▾` and its menu of chips, rotate and touch (BT-003). It
 * names the device the gate reported, so a viewer who may not switch it
 * still reads it without opening anything; the chips inside say why they
 * are disabled (`LiveDevice`). At phone width it is not drawn: LiveToolMenu's
 * `⋯` is already the menu, and a menu in a menu is one press too many.
 */
function DeviceMenu({ session, children }: { session: LiveSession; children: ReactNode }) {
  const phone = usePhoneTables()
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLSpanElement>(null)
  const button = useRef<HTMLButtonElement>(null)

  // Closed by a press outside it, as any menu is; Escape is handled on the menu.
  useEffect(() => {
    if (!open) return
    const away = (e: PointerEvent) => {
      if (box.current !== null && !box.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', away)
    return () => document.removeEventListener('pointerdown', away)
  }, [open])

  if (phone) return <>{children}</>
  const d = session.device
  const name = d === null ? 'not reported' : presetLabel(d.preset)
  return (
    <span className="live-device-menu" ref={box}>
      <button
        ref={button}
        type="button"
        className={buttonClass({ size: 'sm' }, false, 'live-device-btn')}
        aria-haspopup="true"
        aria-expanded={open}
        title={session.you.can_switch_device ? 'Change the device the agent’s browser emulates' : DEVICE_WHY}
        onClick={() => setOpen((v) => !v)}
      >
        {`Device: ${name}`} ▾
      </button>
      {open && (
        <div
          className="live-device-pop"
          onKeyDown={(e) => {
            if (e.key !== 'Escape') return
            e.stopPropagation()
            setOpen(false)
            button.current?.focus()
          }}
        >
          {children}
        </div>
      )}
    </span>
  )
}

/**
 * "LET ANY MEMBER TAKE CONTROL" IS THE TASK'S, AND IT GOES BOTH WAYS (owner
 * decision 2026-10-13, "Carry over, with a confirm"; QA12-11). MEASURED in
 * QA p12: one way, with no word of what it does, and back off on the
 * resumed attempt. swarm-api now keeps it on the task and starts every later
 * attempt's session with it; turning it on asks once, saying so; turning it
 * off is one click, since it only takes a right away -- whoever holds
 * control keeps it until they return it, and the submitter can take it over.
 */
function MemberControl({ taskId, session, reload }: { taskId: string; session: LiveSession; reload: LiveReload }) {
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const close = useCallback(() => setAsking(false), [])
  const dialog = useAsk(asking, close)
  const send = (open: boolean) => {
    setAsking(false)
    setBusy(true)
    void (open ? openLiveToMembers(taskId) : closeLiveToMembers(taskId)).then((r) => {
      setBusy(false)
      setError(r.status === 'error' ? r.error.message : null)
      reload(r.status === 'ok' ? r.data : undefined)
    })
  }
  return (
    <span className="live-open-wrap">
      <label className="live-open">
        <input
          type="checkbox"
          checked={session.open_to_members}
          disabled={busy}
          onChange={(e) => (e.target.checked ? setAsking(true) : send(false))}
        />
        Let any member take control
      </label>
      {asking && (
        <div ref={dialog.ref} className="live-ask is-members" role="dialog" aria-modal="true" aria-label="Let any member take control">
          <h4>Let any member take control?</h4>
          <p>
            While the agent asks for help, any member of this workspace can take control of its browser, not only you.
            This stays on for this agent&apos;s later attempts too, until you turn it off here. Every take is in the
            audit, and you can still take control from them.
          </p>
          <span className="live-ask-actions">
            <Button kind="primary" onClick={() => send(true)}>
              Let any member take control
            </Button>
            <Button onClick={close}>Cancel</Button>
          </span>
        </div>
      )}
      {error !== null && (
        <span className="live-control-error" role="alert">
          {error}
        </span>
      )}
    </span>
  )
}

const DEVICE_WHY = 'Only the person in control can change the device.'
const DEVICE_WAIT_WHY = 'Waiting for the browser to report the device just chosen.'

/** What the device route is asked for: a preset by name, and an orientation or touch when turned. */
type DeviceBody = Parameters<typeof setLiveDevice>[1]

/** A device change the route accepted, on the session it was asked on, and when. */
interface DeviceAsk {
  sessionId: string
  body: DeviceBody
  at: number
}

/** True when the reported device is what was asked for: the preset, and the orientation and touch when asked. */
export function deviceApplied(device: LiveSession['device'], body: DeviceBody): boolean {
  return (
    device !== null &&
    device.preset === body.preset &&
    (body.orientation === undefined || device.orientation === body.orientation) &&
    (body.touch === undefined || device.touch === body.touch)
  )
}

/** How often a device change is read again until the gate reports it. */
export const DEVICE_CONFIRM_MS = 1000
/**
 * How long a change is waited on. A gate that never reports it must not hold
 * rotate and touch for good: after this they act on the device reported.
 */
export const DEVICE_CONFIRM_FOR_MS = 15_000

/**
 * While `ask` is set (a change not yet reported), the session is read every
 * `DEVICE_CONFIRM_MS` for up to `DEVICE_CONFIRM_FOR_MS`; true while it is.
 */
function useDeviceReport(ask: DeviceAsk | null, reload: LiveReload): boolean {
  const [lapsed, setLapsed] = useState<number | null>(null)
  const reread = useRef(reload)
  reread.current = reload
  const at = ask?.at ?? null
  useEffect(() => {
    if (at === null) return
    const left = at + DEVICE_CONFIRM_FOR_MS - Date.now()
    if (left <= 0) {
      setLapsed(at)
      return
    }
    const id = setInterval(() => {
      if (!pageHidden()) reread.current()
    }, DEVICE_CONFIRM_MS)
    const end = setTimeout(() => setLapsed(at), left)
    return () => {
      clearInterval(id)
      clearTimeout(end)
    }
  }, [at])
  return at !== null && lapsed !== at
}

/** The orientation a rotate asks for. */
type Orientation = NonNullable<LiveSession['device']>['orientation']

function rotated(orientation: Orientation): Orientation {
  return orientation === 'portrait' ? 'landscape' : 'portrait'
}

/**
 * The device chips, rotate and touch: the toolbar's, and the same component in the theater's device slot.
 * Each is marked `data-live-device`: a switch it asked for hands the keys back to the canvas (KEYS-FOCUS).
 */
function LiveDevice({
  taskId,
  session,
  reload,
  onAsked,
  waiting = false,
}: {
  taskId: string
  session: LiveSession
  reload: LiveReload
  /** Told what a switch the route accepted asked for: the caption says it, and the pane waits, until the gate reports it. */
  onAsked?: (body: DeviceBody) => void
  /** A change is not reported yet: rotate and touch would act on the device from before it, so they wait. */
  waiting?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const whyId = useId()
  const device = session.device
  // NOT REPORTED (LB-SEAM): the gate reports the device it applied over the
  // viewer link; until a report has reached swarm-api the session serves
  // null. No chip is pressed then, rather than the first drawn as if it were
  // the one applied -- and nothing is offered to switch from a device nobody
  // knows.
  const names =
    device === null || session.presets.some((p) => p.name === device.preset)
      ? session.presets.map((p) => p.name)
      : [device.preset, ...session.presets.map((p) => p.name)]
  const can = session.state === 'open' && session.you.can_switch_device && device !== null
  const why = device === null ? 'The gate has not reported which device it applied.' : can ? undefined : DEVICE_WHY
  // Rotate and touch turn the REPORTED device (#1343): before the gate has
  // reported a change, that is the device from before it, so they wait.
  const turnWhy = why ?? (waiting ? DEVICE_WAIT_WHY : undefined)
  const mobile = device === null ? false : (session.presets.find((p) => p.name === device.preset)?.mobile ?? device.touch)
  // The why, said to a screen reader as each disabled control's description.
  const describedBy = why === undefined ? undefined : whyId
  const turnDescribedBy = turnWhy === undefined ? undefined : whyId
  const send = (body: DeviceBody) => {
    setBusy(true)
    void setLiveDevice(taskId, body).then((r) => {
      setBusy(false)
      setError(r.status === 'error' ? r.error.message : null)
      // The answer is drawn as every other control route's is (QA12-22), but
      // its `device` is still the one the gate last reported, from before
      // this switch (swarm-api live.py `set_device`): what was asked for is
      // told to the pane instead, which says it until the gate reports it.
      if (r.status === 'ok') onAsked?.(body)
      reload(r.status === 'ok' ? r.data : undefined)
    })
  }
  return (
    <>
      <span className="live-chips" role="group" aria-label="Device">
        {names.map((n) => (
          <Button
            key={n}
            size="sm"
            data-live-device=""
            className="live-chip"
            aria-pressed={device !== null && device.preset === n}
            disabled={!can || busy}
            title={why}
            aria-describedby={describedBy}
            onClick={() => {
              if (device !== null && n !== device.preset) send({ preset: n })
            }}
          >
            {presetLabel(n)}
          </Button>
        ))}
      </span>
      <Button
        size="sm"
        data-live-device=""
        disabled={!can || busy || waiting}
        title={turnWhy}
        aria-describedby={turnDescribedBy}
        onClick={() => {
          if (device !== null && !waiting) send({ preset: device.preset, orientation: rotated(device.orientation) })
        }}
      >
        ⟲ rotate
      </Button>
      <Button
        size="sm"
        data-live-device=""
        aria-pressed={device?.touch ?? false}
        disabled={!can || busy || !mobile || waiting}
        title={can && !mobile ? 'This preset has no touch.' : turnWhy}
        aria-describedby={turnDescribedBy}
        onClick={() => {
          if (device !== null && !waiting) send({ preset: device.preset, orientation: device.orientation, touch: !device.touch })
        }}
      >
        {device?.touch === true ? 'touch: on' : 'touch: off'}
      </Button>
      {turnWhy !== undefined && (
        <span id={whyId} className="sk-vh">
          {turnWhy}
        </span>
      )}
      {error !== null && (
        <span className="live-control-error" role="alert">
          {error}
        </span>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// The canvas: noVNC over the ticketed socket
// ---------------------------------------------------------------------------

/** The part of noVNC's `RFB` this file uses (noVNC 1.4.0, docs/API.md). */
export interface Rfb {
  viewOnly: boolean
  scaleViewport: boolean
  resizeSession: boolean
  focusOnClick: boolean
  showDotCursor: boolean
  qualityLevel: number
  compressionLevel: number
  disconnect(): void
  clipboardPasteFrom(text: string): void
  addEventListener(type: 'connect' | 'disconnect' | 'securityfailure' | 'desktopname', listener: (e: Event) => void): void
  /**
   * The framebuffer's size, as ServerInit and every resize set it (noVNC
   * 1.4.0 `_resize`). Not public API: noVNC exposes no getter and fires no
   * resize event, and the version is pinned in package.json. Absent (an
   * older or a test RFB), the caption is the gate's report unchecked.
   */
  readonly _fbWidth?: number
  readonly _fbHeight?: number
}

/** The gate's words once the attempt ended and Chromium closed: held equal to mask.py `REASON_BROWSER_CLOSED`. */
const BROWSER_CLOSED = 'The agent has finished; its browser is closed.'

/**
 * WHAT THE GATE SAYS IN ITS DESKTOP NAME (livegate/rfb.py `desktop_name`),
 * which noVNC hands the page as a `desktopname` event. After the attempt ends
 * the gate says, with no lead, that the agent finished (mask.py
 * `REASON_BROWSER_CLOSED`; GREY-TAIL, part of #1344), and the strip draws it.
 *
 * Only that name; null for the gate's ordinary name (the page is shown) or
 * anything else. The gate redacts no part of the screen (BGD, 2026-10-25;
 * LIVE-NOMASK-COPY, part of #2298), so a name that says the page cannot be
 * shown safely or is partly covered is not a reason here.
 */
export function gateReason(name: unknown): string | null {
  return name === BROWSER_CLOSED ? name : null
}

export type RfbConstructor = new (target: HTMLElement, channel: LiveChannel, options?: { shared?: boolean }) => Rfb

/**
 * CRISP AT A WHOLE SCALE (STREAM-SMOOTH; part of #1030). noVNC scales its canvas
 * with CSS, and the browser smooths every scaled image -- 1:1 on a 2x screen too,
 * where each frame pixel is 2x2 device pixels and text comes out soft. At a whole
 * number of device pixels per frame pixel, nearest-neighbour is exact, so the canvas
 * is drawn `pixelated` there; at a fractional fit smoothing stays, since
 * nearest-neighbour would drop whole rows of text.
 */
export function crispAt(cssWidth: number, frameWidth: number, dpr: number): boolean {
  if (cssWidth <= 0 || frameWidth <= 0) return false
  const scale = (cssWidth * dpr) / frameWidth
  return scale >= 1 && Math.abs(scale - Math.round(scale)) < 0.01
}

/** noVNC, loaded with the first Browser tab rather than with the console. */
async function loadRfb(): Promise<RfbConstructor> {
  // @ts-expect-error -- noVNC 1.4.0 ships no type declarations; `Rfb` above is the surface this file uses.
  const mod = (await import('@novnc/novnc/core/rfb.js')) as { default: RfbConstructor }
  return mod.default
}

type Conn = 'connecting' | 'connected' | 'reconnecting' | 'failed'

// The frame's border, in CSS pixels, as live.css draws it (`.live-frame`,
// content-box, so the fit is the screen's size and the outline is outside
// it): the fit leaves room for it on every side. A phone or tablet is drawn
// in an outline; a desktop is not, and keeps only the hairline the screen
// itself always had, which sits inside the fit.
const MOBILE_BEZEL = 10
const DESKTOP_BEZEL = 0
/** `.live-screen`'s own border, inside the frame: the fit is the box inside it, where noVNC draws. */
const HAIRLINE = 1
/** The gap between the device and its caption (`.live-stage`'s `gap`). */
const STAGE_GAP = 6
/**
 * Under this many pixels of tab the tab stacks and reads top to bottom
 * (live.css's `@container live-tab (max-width: 479px)`): the pane is sized to
 * its content there, so the stage has no height to take and is given one.
 */
const NARROW_TAB = 480
/** A mobile preset this wide on its short side is a tablet (iPad: 810), and gets the tablet's outline. */
const TABLET_MIN = 600
/** Focus is on no control at all: the page's body, as after the focused element left the page. */
function nowhere(): boolean {
  return document.activeElement === null || document.activeElement === document.body
}

function LiveScreen({
  taskId,
  sessionId,
  control,
  touch,
  onScreen,
  onGateSays,
  device = null,
  fitTo = null,
  caption,
  onFrame,
  onLastFrame,
  onControlChange,
  onSessionMoved,
}: {
  taskId: string
  /**
   * The session this canvas draws: the CURRENT attempt's, as the pane read
   * it. A ticket minted for any other is not connected (LB-R6).
   */
  sessionId: string
  control: boolean
  touch: boolean
  /** Told the element noVNC draws into, and null when it goes: the theater borrows it. */
  onScreen?: (node: HTMLElement | null) => void
  /** Told the gate's word that the browser closed (`gateReason`), and null when it has none: the strip draws it. */
  onGateSays?: (reason: string | null) => void
  /** The device as turned (`deviceSize`): the frame is fitted to its proportions, in an outline when it is mobile. */
  device?: { width: number; height: number; mobile: boolean } | null
  /** The proportions the frame is fitted to (`fitShape`): the gate's frame once reported, else the device's. */
  fitTo?: { width: number; height: number } | null
  /** Under the device frame: what it is, its size and the page's path. */
  caption?: ReactNode
  /** Told the framebuffer's size when it connects and whenever noVNC rescales it, and null when it goes (`frameContradicts`). */
  onFrame?: (frame: FrameSize | null) => void
  /** Told the canvas as it last drew (a data URL) when this screen goes: a finished agent's tab keeps it (BT-006). */
  onLastFrame?: (dataUrl: string) => void
  /** Called when swarm-api's status frames say control or the session changed: the pane re-reads the session. */
  onControlChange?: () => void
  /**
   * Called when a ticket names another session than `sessionId`, or a re-read
   * before a reconnect finds it closed or moved: the pane re-reads which one
   * is current, drawing the re-read when it is given one.
   */
  onSessionMoved?: LiveReload
}) {
  const host = useRef<HTMLDivElement>(null)
  const rfb = useRef<Rfb | null>(null)
  /**
   * The person's keys were last in this screen: it took focus, and nothing
   * outside it has taken focus since. A canvas noVNC removes takes no blur
   * with it, so this outlives a reconnect, for the new canvas to take over.
   */
  const keysHere = useRef(false)
  const controlRef = useRef(control)
  controlRef.current = control
  const changedRef = useRef(onControlChange)
  changedRef.current = onControlChange
  const movedRef = useRef(onSessionMoved)
  movedRef.current = onSessionMoved
  const saysRef = useRef(onGateSays)
  saysRef.current = onGateSays
  const frameRef = useRef(onFrame)
  frameRef.current = onFrame
  const [conn, setConn] = useState<Conn>('connecting')
  const [why, setWhy] = useState<string | null>(null)
  const [refused, setRefused] = useState<string | null>(null)
  const [hasFocus, setHasFocus] = useState(false)
  const stage = useRef<HTMLDivElement>(null)
  const below = useRef<HTMLDivElement>(null)
  const [room, setRoom] = useState<{ width: number; height: number; caption: number; narrow: boolean } | null>(null)
  const lastRef = useRef(onLastFrame)
  lastRef.current = onLastFrame
  // noVNC has reported a frame of some size: the canvas holds a picture worth keeping.
  const drew = useRef(false)
  // The same, as state: until it is true a failed connection is said IN the frame.
  const [framed, setFramed] = useState(false)

  // THE LAST FRAME OUTLIVES THE SCREEN (BT-006). Declared before the
  // connection's effect, so this cleanup reads the canvas before noVNC's
  // disconnect can take it away. A canvas that never drew a frame, or that
  // cannot be read, leaves nothing.
  useEffect(() => {
    const node = host.current
    return () => {
      if (!drew.current) return
      const canvas = node?.querySelector('canvas') ?? null
      if (canvas === null || canvas.width === 0 || canvas.height === 0) return
      try {
        const url = canvas.toDataURL('image/png')
        if (typeof url === 'string' && url.startsWith('data:image/')) lastRef.current?.(url)
      } catch {
        /* unreadable: nothing is kept */
      }
    }
  }, [])

  // THE ROOM THE DEVICE HAS: the stage's box, less the caption under the
  // device and the outline around it. Measured before paint and again
  // whenever the stage changes size (the logs opening, the agent list
  // folding, the window), so the device is never drawn bigger than it.
  useLayoutEffect(() => {
    const node = stage.current
    if (node === null) return
    const measure = () => {
      const caption = below.current?.offsetHeight ?? 0
      // A tab of no measured width (no layout) is not taken for a narrow one.
      const tab = node.closest<HTMLElement>('.live-tab')?.clientWidth ?? 0
      const next = {
        width: node.clientWidth,
        height: node.clientHeight - caption - STAGE_GAP,
        caption,
        narrow: tab > 0 && tab < NARROW_TAB,
      }
      setRoom((was) =>
        was !== null &&
        was.width === next.width &&
        was.height === next.height &&
        was.caption === next.caption &&
        was.narrow === next.narrow
          ? was
          : next,
      )
    }
    measure()
    window.addEventListener('resize', measure)
    if (typeof ResizeObserver === 'undefined') return () => window.removeEventListener('resize', measure)
    const watch = new ResizeObserver(measure)
    watch.observe(node)
    return () => {
      window.removeEventListener('resize', measure)
      watch.disconnect()
    }
  }, [])

  const bezel = device !== null && device.mobile ? MOBILE_BEZEL : DESKTOP_BEZEL
  // Fitted is the box noVNC scales its canvas into -- inside the outline AND
  // the screen's hairline -- so the canvas fills it, edge to edge.
  const edge = bezel + HAIRLINE
  const proportions = fitTo ?? device
  const fit =
    proportions === null || room === null
      ? null
      : fitDevice({ width: room.width - 2 * edge, height: room.height - 2 * edge }, proportions)
  // A NARROW TAB'S STAGE IS AS TALL AS THE DEVICE AT ITS WIDTH (review of
  // BT-003, 2026-10-26). There the pane is sized to its content and the
  // drawer scrolls, so `flex: 1 1 0` gave the stage nothing and it fell to
  // live.css's 160 px floor: rendered at 390, a desktop 182x115 in a 358 px
  // column and a phone preset ~74x160. QA12-19's width-led floor is kept
  // there, and only there, held to the window's height so a phone preset is
  // never taller than the screen; a wider tab is bounded by its pane and
  // takes the room left (BT-003).
  const floorFit =
    proportions === null || room === null || !room.narrow
      ? null
      : fitDevice({ width: room.width - 2 * edge, height: Number.POSITIVE_INFINITY }, proportions)
  const floor =
    floorFit === null || room === null
      ? null
      : Math.min(floorFit.height + 2 * edge + room.caption + STAGE_GAP, window.innerHeight)
  const kind = device === null || !device.mobile ? 'desktop' : Math.min(device.width, device.height) >= TABLET_MIN ? 'tablet' : 'phone'

  useEffect(() => {
    onScreen?.(host.current)
    return () => onScreen?.(null)
  }, [onScreen])

  // WHETHER THE CANVAS HAS FOCUS IS READ, NOT REMEMBERED (KEYS-FOCUS). A
  // canvas that is removed or moved takes focus with it and fires no blur.
  const typing = useCallback(() => {
    const at = document.activeElement
    return at !== null && host.current !== null && host.current.contains(at)
  }, [])
  /** Give noVNC's canvas the keys, for the person in control only. */
  const focusCanvas = useCallback(() => {
    const canvas = host.current?.querySelector('canvas')
    if (!controlRef.current || canvas === null || canvas === undefined) return
    canvas.focus({ preventScroll: true })
    setHasFocus(typing())
  }, [typing])
  useEffect(() => {
    const moved = (e: FocusEvent) => {
      const inside = e.target instanceof Node && host.current !== null && host.current.contains(e.target)
      keysHere.current = inside
      setHasFocus(inside)
    }
    document.addEventListener('focusin', moved)
    return () => document.removeEventListener('focusin', moved)
  }, [])

  // A DEVICE SWITCH IN CONTROL HANDS THE KEYS BACK. The chip that asked for it
  // (or the page, once a busy chip let go of focus) holds them otherwise, and
  // the person types on into the console. Once the switched device is drawn.
  const shape = device === null ? null : `${device.width}x${device.height}:${device.mobile}:${touch}`
  const drawn = useRef(shape)
  useEffect(() => {
    const was = drawn.current
    drawn.current = shape
    if (was === null || was === shape) return
    const at = document.activeElement
    if (nowhere() || (at instanceof Element && at.closest('[data-live-device]') !== null)) focusCanvas()
  }, [shape, focusCanvas])

  // VIEW-ONLY UNLESS IN CONTROL. Set on the RFB object directly, not through
  // a render, so the connection's first moment in control already sends. Not
  // gated on focus: keys need it anyway (noVNC hears them on its canvas) and
  // the pointer must not (the header comment's measurement).
  const applyInput = useCallback(() => {
    const r = rfb.current
    if (r !== null) r.viewOnly = !controlRef.current
  }, [])
  useEffect(applyInput, [control, applyInput])

  useEffect(() => {
    let stopped = false
    let tries = 0
    let timer: ReturnType<typeof setTimeout> | null = null
    // swarm-api's status frames (LB-STREAM): never noVNC's, which reads only
    // the binary RFB stream. A change of controller, of whether this viewer
    // holds control, or of the session closing re-reads the session.
    //
    // THE FIRST FRAME IS CHECKED, NOT ASSUMED (STRIP-LABEL, part of #1341).
    // It was taken as the state the session read already drew, so a read
    // older than the connection -- drawn before a take that the relay
    // already knew of -- was never read again by a frame: the screen said
    // "View only." under a header that said "You are in control". The
    // first frame now re-reads when it disagrees with the control drawn.
    let seen: LiveStatus | null = null
    const onStatus = (s: LiveStatus) => {
      const changed =
        seen === null
          ? s.in_control !== controlRef.current
          : s.in_control !== seen.in_control || s.controller !== seen.controller || s.open !== seen.open
      seen = s
      if (changed && !stopped) changedRef.current?.()
    }
    // The relay turned this connection's control away (`liveRefusal`): said
    // under the canvas, so "you are in control" is never drawn over a browser
    // that will ignore every move. A later connection starts clear.
    const onRefused = (cause: string) => {
      if (stopped) return
      setRefused(cause)
      // Most often a connection left on an earlier attempt's session: the pane
      // reads again which session is current, as for a ticket that moved.
      movedRef.current?.()
    }
    // Set when the relay closed this viewer with LIVE_RESTART_CODE, and kept
    // through the retries that meet it still coming back, until one connects.
    let restarting = false
    const retry = (message: string, restart = false) => {
      if (stopped) return
      tries += 1
      restarting = restarting || restart
      setWhy(message)
      // A relay coming back is not a failed viewer, however many quick knocks it takes.
      setConn(tries > 5 && !restarting ? 'failed' : 'reconnecting')
      timer = setTimeout(() => void connect(), liveReconnectDelay(tries, restarting))
    }
    const connect = async () => {
      // noVNC is loaded BEFORE a ticket is minted or a socket opened, so the
      // socket's 'open' handler can construct it at once (`openLiveSocket`):
      // a frame the relay sends while noVNC's chunk is still importing would
      // otherwise be lost, and the tab would wait on a handshake forever.
      let Ctor: RfbConstructor
      try {
        Ctor = await loadRfb()
      } catch (err) {
        retry(err instanceof Error ? err.message : 'The live viewer did not load.')
        return
      }
      if (stopped) return
      // A RECONNECT ASKS FIRST WHETHER THE SESSION IS STILL OPEN (QA12-25).
      // MEASURED in QA p12: as an attempt ended and the next one was
      // STARTING, the reconnects of the old canvas minted twice and were
      // answered 409 twice, each a console error. A ticket is asked for only
      // while the session this canvas draws is the open one; otherwise the
      // pane is handed the read, and draws the start-up or the new session.
      if (tries > 0) {
        const read = await loadLiveSession(taskId)
        if (stopped) return
        if (read.status !== 'ok' && read.status !== 'stale') {
          retry(read.status === 'error' ? read.error.message : 'The live session could not be read.')
          return
        }
        const now = read.data.session
        if (now === null || now.state !== 'open' || now.session_id !== sessionId) {
          setWhy('The agent’s browser closed. Waiting for it to open again.')
          movedRef.current?.(read.data)
          return
        }
      }
      const ticket = await mintLiveTicket(taskId)
      if (stopped) return
      if (ticket.status !== 'ok') {
        retry(ticket.status === 'error' ? ticket.error.message : 'No viewer ticket was issued.')
        return
      }
      if (ticket.data.session_id !== sessionId) {
        // THE CURRENT ATTEMPT'S SESSION, NEVER ANOTHER (LB-R6): swarm-api
        // mints for the task's current attempt, and this canvas is the one
        // the pane drew for `sessionId`. They differ only across an attempt
        // change; the pane reads the session again, and the canvas for the
        // new one (keyed by its id) connects to it.
        movedRef.current?.()
        retry('The agent’s browser moved to a new attempt. Reconnecting.')
        return
      }
      try {
        await openLiveSocket(ticket.data.session_id, ticket.data.ticket, (ws) => {
          if (stopped || host.current === null) {
            ws.close()
            return
          }
          const r = new Ctor(host.current, ws, { shared: true })
          r.scaleViewport = true
          // The gate sizes the display from the device preset (§6.1); the viewer never asks for a size.
          r.resizeSession = false
          r.focusOnClick = true
          // noVNC hides the local pointer over its canvas and draws the server's cursor shape
          // instead; the gate sends one (-239, LB-CURSOR), and against a gate that sends none
          // the dot keeps a pointer visible rather than leaving the person clicking blind.
          r.showDotCursor = true
          // The gate sends only lossless Tight (never JPEG, livegate/rfb.py); quality 9 says
          // the same to noVNC. Compression 1 is the gate's ZLIB_LEVEL, from livegate.bench.
          r.qualityLevel = 9
          r.compressionLevel = 1
          const canvas = host.current.querySelector('canvas')
          // The frame the gate sends: noVNC rescales its canvas when it
          // changes size, so the observer below hears every resize too.
          const sized = () => {
            const [width, height] = [r._fbWidth, r._fbHeight]
            if (!stopped && typeof width === 'number' && typeof height === 'number') {
              if (width > 0 && height > 0) {
                drew.current = true
                setFramed(true)
              }
              frameRef.current?.(width > 0 && height > 0 ? { width, height } : null)
            }
          }
          r.addEventListener('connect', sized)
          if (canvas !== null && typeof ResizeObserver !== 'undefined') {
            const sharpen = () => {
              const crisp = crispAt(canvas.getBoundingClientRect().width, canvas.width, window.devicePixelRatio || 1)
              canvas.style.imageRendering = crisp ? 'pixelated' : ''
              sized()
            }
            const sizes = new ResizeObserver(sharpen)
            sizes.observe(canvas)
            r.addEventListener('connect', sharpen)
            r.addEventListener('disconnect', () => sizes.disconnect())
          }
          rfb.current = r
          applyInput()
          // noVNC set the channel's close handler as it attached; the close
          // code is read on the way through, for the 'disconnect' below.
          let closeCode: number | null = null
          const noVncClose = ws.onclose
          ws.onclose = (ev) => {
            closeCode = ev.code
            noVncClose?.call(ws, ev)
          }
          r.addEventListener('connect', () => {
            tries = 0
            restarting = false
            setWhy(null)
            setRefused(null)
            setConn('connected')
            // A canvas that replaces the one the keys were in takes them (KEYS-FOCUS).
            if (keysHere.current && nowhere()) focusCanvas()
            else setHasFocus(typing())
          })
          r.addEventListener('desktopname', (e) => {
            if (!stopped) saysRef.current?.(gateReason((e as CustomEvent<{ name?: unknown }>).detail?.name))
          })
          r.addEventListener('disconnect', () => {
            setHasFocus(typing())
            if (rfb.current === r) rfb.current = null
            if (!stopped) saysRef.current?.(null)
            if (!stopped) frameRef.current?.(null)
            if (closeCode === LIVE_RESTART_CODE) retry('The live relay restarted. Reconnecting.', true)
            else retry('The live connection closed. Reconnecting with a new ticket.')
          })
        }, onStatus, onRefused)
      } catch (err) {
        retry(err instanceof Error ? err.message : 'The live connection did not open.')
      }
    }
    void connect()
    return () => {
      stopped = true
      if (timer !== null) clearTimeout(timer)
      const r = rfb.current
      rfb.current = null
      r?.disconnect()
      // A reason belongs to this connection: the next one starts with none.
      saysRef.current?.(null)
      // So does its frame.
      frameRef.current?.(null)
    }
  }, [taskId, sessionId, applyInput, focusCanvas, typing])

  // NO PICTURE YET AND NONE COMING NOW: SAID IN THE FRAME (visual QA
  // ovag2-12, 2026-10-29). A ticket the API would not mint -- the fixture
  // build serves none -- left a large empty frame, the reason in small text
  // in the caption under it. Until a first frame, a connection that is
  // retrying or has given up puts its reason in the frame's middle; once a
  // picture has drawn, a reconnect keeps it and says so in the caption.
  const blank = !framed && (conn === 'reconnecting' || conn === 'failed') && why !== null
  const onFocus = () => {
    keysHere.current = true
    setHasFocus(true)
  }
  const onBlur = (e: { currentTarget: HTMLElement; relatedTarget: EventTarget | null }) => {
    if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return
    setHasFocus(false)
  }
  // A PRESS ANYWHERE ON THE SCREEN TAKES THE KEYS. noVNC focuses its canvas
  // only for a press on the canvas itself, and stops that press there, so
  // this listens in the capture phase; a press beside a canvas scaled to fit
  // would otherwise leave focus on the page.
  const pressToType = (e: MouseEvent<HTMLDivElement>) => {
    if (!controlRef.current) return
    if (!(e.target instanceof HTMLCanvasElement)) e.preventDefault()
    focusCanvas()
  }
  // A key typed into the agent's browser is the agent's browser's, not the console's.
  const keepKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (controlRef.current && typing()) e.stopPropagation()
  }
  const gateTouch = (e: SyntheticEvent) => {
    if (!(controlRef.current && touch)) e.stopPropagation()
  }
  const onPaste = (e: ClipboardEvent<HTMLDivElement>) => {
    const r = rfb.current
    if (r === null || !controlRef.current || !typing()) return
    const text = e.clipboardData.getData('text/plain')
    e.preventDefault()
    if (text !== '') r.clipboardPasteFrom(text)
  }

  return (
    <div className="live-screen-wrap">
      {/* THE STAGE: the device fitted into it both ways and centred, in a
          phone or tablet outline for a mobile preset, the caption under it.
          The fit sizes the FRAME, and noVNC (scaleViewport) scales its canvas
          to the screen element that fills it -- never a CSS transform, which
          would move the pixels away from where noVNC maps a click. The screen
          element carries no size of its own, so the theater, which borrows
          it, sizes it to its stage instead. */}
      {/* THE STAGE IS THE ROOM THE PANE HAS LEFT, AND NO MORE (BT-003, part
          of #2423). MEASURED 2026-10-26 at 1440x757: the stage asked for the
          device at the pane's full width (QA12-19's floor), 380 px of frame
          in a pane with ~280 left, so the pane scrolled and 42% of the frame,
          its caption and 'View only.' sat below its edge. The device is
          fitted into the stage both ways; the pane scrolls only under
          live.css's small floor. */}
      <div ref={stage} className="live-stage" style={floor === null ? undefined : { minHeight: `${floor}px` }}>
        <div
          // ITS OWN BOX IN EVERY STATE (visual QA run 6, task-006): a
          // desktop frame drew no edge of its own, only the screen's hairline
          // inside it, so wherever the screen is not drawn whole -- borrowed
          // by the theater, or not yet connected -- the frame lost its box.
          // `is-bordered` draws it on the frame (styles/live.css).
          className={`live-frame is-${kind} is-bordered${blank ? ' has-empty' : ''}`}
          data-device={kind}
          style={fit === null ? undefined : { width: `${fit.width + 2 * HAIRLINE}px`, height: `${fit.height + 2 * HAIRLINE}px` }}
        >
          <div
            ref={host}
            className={`live-screen${control ? ' is-control' : ''}`}
            data-conn={conn}
            // Keys here are the remote browser's: the theater's `T` and Esc leave them alone.
            data-live-screen=""
            // A NAME NEEDS A ROLE (task-001, visual QA run 3, #1680): on a
            // plain div the label is prohibited (axe aria-prohibited-attr,
            // serious) and announced by nothing. In control the screen takes
            // the keys -- an application; watching, it is a named group. Not
            // `img`: its children are presentational, and noVNC's canvas
            // inside is focusable (tabIndex -1), which axe's
            // nested-interactive check flags.
            role={control ? 'application' : 'group'}
            aria-label={control ? 'The agent’s browser. Click it to type into it.' : 'The agent’s browser, view only.'}
            onFocus={onFocus}
            onBlur={onBlur}
            onMouseDownCapture={pressToType}
            onKeyDown={keepKey}
            onKeyUp={keepKey}
            onPaste={onPaste}
            onTouchStartCapture={gateTouch}
            onTouchMoveCapture={gateTouch}
            onTouchEndCapture={gateTouch}
            onTouchCancelCapture={gateTouch}
          >
            {/* Inside the screen, so it goes with it into the theater. Clicks go through it. */}
            {control && conn === 'connected' && !hasFocus && (
              <span className="live-type-hint" aria-hidden="true">
                Click to type
              </span>
            )}
          </div>
          {blank && (
            <div className="live-frame-empty" data-testid="live-frame-empty">
              <EmptyState kind={conn === 'failed' ? 'failed' : 'empty'} heading="No picture from the agent’s browser">
                {conn === 'failed' ? `The live connection keeps failing: ${why}` : `${why} Trying again.`}
              </EmptyState>
            </div>
          )}
        </div>
        {/* The caption and the connection's state share one line under the
            frame (BT-003): a line of its own below the stage was one more
            row the frame did not get. */}
        <div ref={below} className="live-stage-caption">
          {caption}
          <span className="live-screen-say" role="status">
            {/* Said once: in the frame while it has no picture (`blank`). */}
            {blank
              ? null
              : conn === 'connecting'
              ? 'Connecting to the agent’s browser.'
              : conn === 'connected'
                ? control && refused !== null
                  ? `The relay refused this connection’s control (${refused}). Your input does not reach the agent’s browser.`
                  : control
                  ? hasFocus
                    ? 'Your keys go to the agent’s browser. Click outside it to use the console.'
                    : 'Click the browser to type into it.'
                  : 'View only.'
                : conn === 'failed'
                  ? `The live connection keeps failing: ${why ?? ''}`
                  : why}
          </span>
        </div>
        {/* Until the first frame, the start-up over the empty screen: it
            steps aside when noVNC connects or the pod reports the frame. */}
        {conn === 'connecting' && <StartupProgress taskId={taskId} overlay hideWhenDone />}
      </div>
    </div>
  )
}
