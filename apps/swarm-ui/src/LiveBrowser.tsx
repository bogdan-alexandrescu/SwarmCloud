import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type KeyboardEvent,
  type ReactNode,
  type SyntheticEvent,
} from 'react'

import { AgentLogs } from './AgentLogs'
import {
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
  type LiveSession,
  type LiveStatus,
} from './api'
import { SaveAsLoginOffer } from './BrowserLogins'
import { Button } from './components'
import { HandoffStrip } from './Handoff'
import { LiveToolMenu } from './LiveToolMenu'
import { TheaterButton, TheaterHost, useTheater, type TheaterHandoff, type TheaterSession } from './Theater'
import { TERMINAL_STATES, type Task } from './types'
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
 * canvas, so a key pressed anywhere else in the console never reaches it;
 * and the connection is view-only (`viewOnly`, which sends no key, pointer or
 * paste at all) except while this person holds control AND the canvas has
 * focus. While it has focus in control, a key stops at the canvas, so the
 * console's own shortcuts (Escape closes the split, `[` folds the list) do
 * not fire under someone typing into the agent's browser.
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

/** How often an open session is read again: the controller, the origin and the hand-off move under the viewer. */
export const LIVE_POLL_MS = 2000

/**
 * The controller's console renews this often. Control ends 60 seconds after
 * the last renewal (`renew_by`, §4.2), so three renewals fit in the window and
 * one lost request does not take control away from someone mid-sign-in.
 */
export const LIVE_RENEW_MS = 20_000

/** The longest wait between reconnects; noVNC's socket ends with Cloud Run's request timeout (§3.3, R2). */
const RECONNECT_MAX_MS = 30_000

/** The presets' display names (§6.1, LB-6). The gate holds the list and its sizes; a name not here is shown as served. */
const PRESET_LABEL: Readonly<Record<string, string>> = {
  desktop: 'Desktop',
  'iphone-15': 'iPhone 15',
  'pixel-8': 'Pixel 8',
  'iphone-se': 'iPhone SE',
  // `iPad`, as the mockup's chip reads (owner decision 2026-10-12); the
  // preset is the 7th generation's frame, which its size in the caption says.
  'ipad-7': 'iPad',
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
  | { status: 'ok'; liveBrowser: boolean; session: LiveSession | null }
  | { status: 'error'; message: string }

/**
 * The live session of one agent, read while its attempt is in flight and
 * again every `LIVE_POLL_MS` while its profile is a live-browser one. An
 * agent whose profile is not is read once: a catalogue property does not
 * change under a running task.
 */
export function useLiveSession(taskId: string, inFlight: boolean): { live: LiveState; reload: () => void } {
  const [got, setGot] = useState<{ taskId: string; state: LiveState } | null>(null)
  const [tick, setTick] = useState(0)
  const reload = useCallback(() => setTick((n) => n + 1), [])
  const liveBrowser = got !== null && got.taskId === taskId && got.state.status === 'ok' && got.state.liveBrowser

  useEffect(() => {
    if (!inFlight) return
    let gone = false
    void loadLiveSession(taskId).then((r) => {
      if (gone) return
      if (r.status === 'ok' || r.status === 'stale') {
        setGot({ taskId, state: { status: 'ok', liveBrowser: r.data.live_browser === true, session: r.data.session ?? null } })
      } else if (r.status === 'error') {
        // A failed re-read keeps the session it had: a blip must not tear the
        // canvas down under someone typing into it.
        setGot((was) =>
          was !== null && was.taskId === taskId && was.state.status === 'ok'
            ? was
            : { taskId, state: { status: 'error', message: r.error.message } },
        )
      }
    })
    return () => {
      gone = true
    }
  }, [taskId, inFlight, tick])

  useEffect(() => {
    if (!inFlight || !liveBrowser) return
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      setTick((n) => n + 1)
    }, LIVE_POLL_MS)
    return () => clearInterval(id)
  }, [inFlight, liveBrowser])

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
  if (o === null) return { tone: 'unknown', say: 'The gate has not reported the page’s address yet.' }
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

function isController(s: LiveSession): boolean {
  return s.controller !== null && s.controller.email === s.you.email
}

// ---------------------------------------------------------------------------
// Take control / Return control, in the agent header
// ---------------------------------------------------------------------------

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
 */
export function LiveControl({
  taskId,
  session,
  reload,
  takeOnly = false,
}: {
  taskId: string
  session: LiveSession | null
  reload: () => void
  /**
   * The hand-off banner's copy of the button (LB-UX1): Take control, the same
   * action as the header's, and nothing at all for anyone who may not take
   * it now -- no "View only", no Return control, which stay the header's.
   */
  takeOnly?: boolean
}) {
  const [ask, setAsk] = useState<'none' | 'first' | 'mismatch'>('none')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mine = session !== null && isController(session)

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
              reload()
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
      reload()
    })
  }
  const label = holder === null ? 'Take control' : `Take control from ${holder.email}`

  return (
    <span className="live-control">
      <Button
        kind="primary"
        busy={busy ? 'Taking control…' : false}
        disabledReason={refused ?? undefined}
        onClick={() => setAsk('first')}
      >
        {label}
      </Button>
      {error !== null && <span className="live-control-error" role="alert">{error}</span>}
      {ask === 'first' && (
        <div className="live-ask" role="dialog" aria-label="Take control of the agent’s browser" onKeyDown={escapeTo(() => setAsk('none'))}>
          <h4>Take control of the agent’s browser?</h4>
          <p>
            The agent waits while you are in control, and every member watching sees the page, with password fields
            painted over. Sign in with an account made for this agent where you can, not your own.
          </p>
          {verdict.tone === 'warn' && <p className="live-ask-warn">{verdict.say}</p>}
          <span className="live-ask-actions">
            <Button kind="primary" onClick={() => (verdict.tone === 'mismatch' ? setAsk('mismatch') : take())}>
              {label}
            </Button>
            <Button onClick={() => setAsk('none')}>Cancel</Button>
          </span>
        </div>
      )}
      {ask === 'mismatch' && session.origin !== null && (
        <div className="live-ask is-mismatch" role="alertdialog" aria-label="This is not the site the agent named" onKeyDown={escapeTo(() => setAsk('none'))}>
          <h4>This is not the site the agent named</h4>
          <p>
            The page is <strong>{session.origin.registrable_domain}</strong>. The agent asked for help signing in to{' '}
            <strong>{bareHost(session.human_wait?.expect ?? '')}</strong>. A page that only looks like a sign-in page
            is how a password is stolen.
          </p>
          <span className="live-ask-actions">
            <Button kind="danger" onClick={take}>
              Take control on {session.origin.registrable_domain}
            </Button>
            <Button onClick={() => setAsk('none')}>Cancel</Button>
          </span>
        </div>
      )}
    </span>
  )
}

function escapeTo(close: () => void) {
  return (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== 'Escape') return
    e.stopPropagation()
    close()
  }
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
}: {
  task: Task
  live: LiveState
  reload: () => void
  /** Open in the theater (`/browser?theater=1`): the graph card, the banner and a shared link land here. */
  theater?: boolean
  /** "I'm ready to sign in" was accepted: the agent's view reads the task again. */
  onHandoffReady?: () => void
}) {
  const [logs, setLogs] = useState(false)
  // The element noVNC draws into, for the theater to borrow; null until LiveScreen has mounted it.
  const [screenNode, setScreenNode] = useState<HTMLElement | null>(null)
  const session = live.status === 'ok' ? live.session : null
  const open = session !== null && session.state === 'open' ? session : null
  const returned = useControlReturned(open?.session_id ?? null, open !== null && isController(open), open?.controller ?? null)

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
    return note(
      TERMINAL_STATES.has(task.state)
        ? 'This agent has finished, and its live browser closed with it. Its recordings are on Attempts.'
        : 'The agent’s browser is not running. It starts when the agent is admitted again, and appears here.',
    )
  }
  if (live.status === 'loading') {
    return note('Reading the agent’s live browser.')
  }
  if (live.status === 'error') {
    return note(`The live browser could not be read: ${live.message}`)
  }
  if (session === null) {
    return note('The agent’s browser is starting. It appears here once its pod is running.')
  }
  if (session.state === 'closed') {
    return note(
      `The live browser has closed${session.closed_reason ? ` (${session.closed_reason})` : ''}. The recording is kept for seven days.`,
    )
  }

  const mine = isController(session)
  const verdict = stripVerdict(session)
  const o = session.origin
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
  const strip = (
    <div className={`live-strip is-${verdict.tone}`} data-tone={verdict.tone} role="status">
        <span className="live-origin">
          {o === null ? (
            '—'
          ) : (
            <>
              <span className="live-origin-scheme">{o.scheme}//</span>
              <OriginHost origin={o} />
            </>
          )}
        </span>
        <span className="live-strip-say">{verdict.say}</span>
        <span className="live-strip-who">
          {session.controller === null
            ? 'Nobody is in control: the agent is driving.'
            : mine
              ? 'You are in control.'
              : `${session.controller.email} is in control.`}
        </span>
      </div>
  )
  return (
    <TheaterHost
      session={theater}
      initiallyOpen={openTheater}
      strip={strip}
      handoff={handoff}
      logs={<AgentLogs task={task} />}
      device={<LiveDevice taskId={task.id} session={session} reload={reload} />}
      control={<LiveControl taskId={task.id} session={session} reload={reload} />}
    >
      <div className={`live-tab${logs ? ' with-logs' : ''}`}>
        {banner(
          <>
            <LiveControl taskId={task.id} session={session} reload={reload} takeOnly />
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
        />
        <SaveAsLoginOffer taskId={task.id} tenantId={task.tenant_id} site={o?.origin ?? null} controlReturned={returned} />
        {/* LOGS ON THE LEFT, THE DEVICE ON THE RIGHT (the mockup's ◫ logs). */}
        <div className="live-body">
          {logs && (
            <div className="live-logs">
              <AgentLogs task={task} />
            </div>
          )}
          <LiveScreen
            key={session.session_id}
            taskId={task.id}
            control={mine}
            touch={mine && session.device !== null && session.device.touch}
            onScreen={setScreenNode}
            device={deviceSize(session)}
            caption={<span className="live-caption">{deviceCaption(session)}</span>}
            onControlChange={reload}
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
 * · <path>`, width × height as the device is turned (`deviceSize`). The
 * page's path is not served: the session's origin is scheme, host and port
 * by design (§5.7), and nothing carries the path to swarm-api, so it says so
 * rather than print one nobody measured. A device the gate has not reported
 * is said the same way (LB-SEAM).
 */
export function deviceCaption(session: Pick<LiveSession, 'device' | 'presets'>): string {
  const d = session.device
  const size = deviceSize(session)
  const device =
    d === null
      ? 'Device not reported'
      : size === null
        ? presetLabel(d.preset)
        : `${presetLabel(d.preset)} · ${size.width}×${size.height}`
  return `${device} · path not reported`
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
 * THE TOOLBAR, LEFT TO RIGHT AS THE MOCKUP DRAWS IT (owner decisions
 * 2026-10-12): the device chips, `⟲ rotate`, `touch: on|off`, `◫ logs`, and
 * at the right `⤢ Theater`. "Let any member take control" stays in the
 * toolbar, after Theater, exactly as built (the owner kept it).
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
}: {
  taskId: string
  session: LiveSession
  logs: boolean
  onLogs: () => void
  reload: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  return (
    <div className="live-toolbar" role="toolbar" aria-label="Browser view">
      <LiveToolMenu>
        <LiveDevice taskId={taskId} session={session} reload={reload} />
        <Button size="sm" aria-pressed={logs} onClick={onLogs} title={logs ? 'Hide the logs' : 'Show the logs beside the browser'}>
          ◫ logs
        </Button>
      </LiveToolMenu>
      <span className="live-toolbar-end">
        <TheaterButton />
        {session.you.can_open && (
          <label className="live-open">
            <input
              type="checkbox"
              checked={session.open_to_members}
              disabled={session.open_to_members}
              onChange={(e) => {
                if (!e.target.checked) return
                void openLiveToMembers(taskId).then((r) => {
                  setError(r.status === 'error' ? r.error.message : null)
                  reload()
                })
              }}
            />
            Let any member take control
          </label>
        )}
      </span>
      {error !== null && (
        <span className="live-control-error" role="alert">
          {error}
        </span>
      )}
    </div>
  )
}

// SWITCHED BY THE PERSON IN CONTROL, SHOWN TO EVERYONE ELSE (LB-DEV-B).
// swarm-api says who may switch (`you.can_switch_device`: the controller
// only) and refuses anyone else with 409; the console disables the bar for
// them rather than offer a change that will be refused. A change is a preset
// BY NAME, an orientation or touch -- never a size (invariant 10). The answer
// is the session as it stands: the gate applies the change and reports it,
// so the bar shows the new device from the read that follows, not before.
const DEVICE_WHY = 'Only the person in control can change the device.'

/** The orientation a rotate asks for. */
type Orientation = NonNullable<LiveSession['device']>['orientation']

function rotated(orientation: Orientation): Orientation {
  return orientation === 'portrait' ? 'landscape' : 'portrait'
}

/** The device chips, rotate and touch: the toolbar's, and the same component in the theater's device slot. */
function LiveDevice({ taskId, session, reload }: { taskId: string; session: LiveSession; reload: () => void }) {
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
  const mobile = device === null ? false : (session.presets.find((p) => p.name === device.preset)?.mobile ?? device.touch)
  // The why, said to a screen reader as each disabled control's description.
  const describedBy = why === undefined ? undefined : whyId
  const send = (body: Parameters<typeof setLiveDevice>[1]) => {
    setBusy(true)
    void setLiveDevice(taskId, body).then((r) => {
      setBusy(false)
      setError(r.status === 'error' ? r.error.message : null)
      reload()
    })
  }
  return (
    <>
      <span className="live-chips" role="group" aria-label="Device">
        {names.map((n) => (
          <Button
            key={n}
            size="sm"
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
        disabled={!can || busy}
        title={why}
        aria-describedby={describedBy}
        onClick={() => {
          if (device !== null) send({ preset: device.preset, orientation: rotated(device.orientation) })
        }}
      >
        ⟲ rotate
      </Button>
      <Button
        size="sm"
        aria-pressed={device?.touch ?? false}
        disabled={!can || busy || !mobile}
        title={can && !mobile ? 'This preset has no touch.' : why}
        aria-describedby={describedBy}
        onClick={() => {
          if (device !== null) send({ preset: device.preset, orientation: device.orientation, touch: !device.touch })
        }}
      >
        {device?.touch === true ? 'touch: on' : 'touch: off'}
      </Button>
      {why !== undefined && (
        <span id={whyId} className="sk-vh">
          {why}
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
  disconnect(): void
  clipboardPasteFrom(text: string): void
  addEventListener(type: 'connect' | 'disconnect' | 'securityfailure', listener: (e: Event) => void): void
}

export type RfbConstructor = new (target: HTMLElement, channel: LiveChannel, options?: { shared?: boolean }) => Rfb

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
/** A mobile preset this wide on its short side is a tablet (iPad: 810), and gets the tablet's outline. */
const TABLET_MIN = 600

function LiveScreen({
  taskId,
  control,
  touch,
  onScreen,
  device = null,
  caption,
  onControlChange,
}: {
  taskId: string
  control: boolean
  touch: boolean
  /** Told the element noVNC draws into, and null when it goes: the theater borrows it. */
  onScreen?: (node: HTMLElement | null) => void
  /** The device as turned (`deviceSize`): the frame is fitted to its proportions, in an outline when it is mobile. */
  device?: { width: number; height: number; mobile: boolean } | null
  /** Under the device frame: what it is, its size and the page's path. */
  caption?: ReactNode
  /** Called when swarm-api's status frames say control or the session changed: the pane re-reads the session. */
  onControlChange?: () => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const rfb = useRef<Rfb | null>(null)
  const focused = useRef(false)
  const controlRef = useRef(control)
  controlRef.current = control
  const changedRef = useRef(onControlChange)
  changedRef.current = onControlChange
  const [conn, setConn] = useState<Conn>('connecting')
  const [why, setWhy] = useState<string | null>(null)
  const [hasFocus, setHasFocus] = useState(false)
  const stage = useRef<HTMLDivElement>(null)
  const below = useRef<HTMLDivElement>(null)
  const [room, setRoom] = useState<{ width: number; height: number } | null>(null)

  // THE ROOM THE DEVICE HAS: the stage's box, less the caption under the
  // device and the outline around it. Measured before paint and again
  // whenever the stage changes size (the logs opening, the agent list
  // folding, the window), so the device is never drawn bigger than it.
  useLayoutEffect(() => {
    const node = stage.current
    if (node === null) return
    const measure = () => {
      const next = { width: node.clientWidth, height: node.clientHeight - (below.current?.offsetHeight ?? 0) - STAGE_GAP }
      setRoom((was) => (was !== null && was.width === next.width && was.height === next.height ? was : next))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const watch = new ResizeObserver(measure)
    watch.observe(node)
    return () => watch.disconnect()
  }, [])

  const bezel = device !== null && device.mobile ? MOBILE_BEZEL : DESKTOP_BEZEL
  // Fitted is the box noVNC scales its canvas into -- inside the outline AND
  // the screen's hairline -- so the canvas fills it, edge to edge.
  const edge = bezel + HAIRLINE
  const fit =
    device === null || room === null
      ? null
      : fitDevice({ width: room.width - 2 * edge, height: room.height - 2 * edge }, device)
  const kind = device === null || !device.mobile ? 'desktop' : Math.min(device.width, device.height) >= TABLET_MIN ? 'tablet' : 'phone'

  useEffect(() => {
    onScreen?.(host.current)
    return () => onScreen?.(null)
  }, [onScreen])

  // VIEW-ONLY UNLESS IN CONTROL WITH THE CANVAS FOCUSED. Set on the RFB
  // object directly, not through a render: noVNC focuses its canvas on
  // mousedown before it handles the press, and the focus handler below has to
  // have opened input by then or the first click of a sign-in is dropped.
  const applyInput = useCallback(() => {
    const r = rfb.current
    if (r !== null) r.viewOnly = !(controlRef.current && focused.current)
  }, [])
  useEffect(applyInput, [control, applyInput])

  useEffect(() => {
    let stopped = false
    let tries = 0
    let timer: ReturnType<typeof setTimeout> | null = null
    // swarm-api's status frames (LB-STREAM): never noVNC's, which reads only
    // the binary RFB stream. The first one the tab sees is the state the
    // session read already drew; a later change of controller, of whether
    // this viewer holds control, or of the session closing re-reads it.
    let seen: LiveStatus | null = null
    const onStatus = (s: LiveStatus) => {
      const changed = seen !== null && (s.in_control !== seen.in_control || s.controller !== seen.controller || s.open !== seen.open)
      seen = s
      if (changed && !stopped) changedRef.current?.()
    }
    const retry = (message: string) => {
      if (stopped) return
      tries += 1
      setWhy(message)
      setConn(tries > 5 ? 'failed' : 'reconnecting')
      timer = setTimeout(() => void connect(), Math.min(RECONNECT_MAX_MS, 1000 * 2 ** Math.min(tries, 5)))
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
      const ticket = await mintLiveTicket(taskId)
      if (stopped) return
      if (ticket.status !== 'ok') {
        retry(ticket.status === 'error' ? ticket.error.message : 'No viewer ticket was issued.')
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
          rfb.current = r
          applyInput()
          r.addEventListener('connect', () => {
            tries = 0
            setWhy(null)
            setConn('connected')
          })
          r.addEventListener('disconnect', () => {
            if (rfb.current === r) rfb.current = null
            retry('The live connection closed. Reconnecting with a new ticket.')
          })
        }, onStatus)
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
    }
  }, [taskId, applyInput])

  const onFocus = () => {
    focused.current = true
    applyInput()
    setHasFocus(true)
  }
  const onBlur = (e: { currentTarget: HTMLElement; relatedTarget: EventTarget | null }) => {
    if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return
    focused.current = false
    applyInput()
    setHasFocus(false)
  }
  // A key typed into the agent's browser is the agent's browser's, not the console's.
  const keepKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (controlRef.current && focused.current) e.stopPropagation()
  }
  const gateTouch = (e: SyntheticEvent) => {
    if (!(controlRef.current && touch)) e.stopPropagation()
  }
  const onPaste = (e: ClipboardEvent<HTMLDivElement>) => {
    const r = rfb.current
    if (r === null || !controlRef.current || !focused.current) return
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
      <div ref={stage} className="live-stage">
        <div
          className={`live-frame is-${kind}`}
          data-device={kind}
          style={fit === null ? undefined : { width: `${fit.width + 2 * HAIRLINE}px`, height: `${fit.height + 2 * HAIRLINE}px` }}
        >
          <div
            ref={host}
            className={`live-screen${control ? ' is-control' : ''}`}
            data-conn={conn}
            // Keys here are the remote browser's: the theater's `T` and Esc leave them alone.
            data-live-screen=""
            aria-label={control ? 'The agent’s browser. Click it to type into it.' : 'The agent’s browser, view only.'}
            onFocus={onFocus}
            onBlur={onBlur}
            onKeyDown={keepKey}
            onKeyUp={keepKey}
            onPaste={onPaste}
            onTouchStartCapture={gateTouch}
            onTouchMoveCapture={gateTouch}
            onTouchEndCapture={gateTouch}
            onTouchCancelCapture={gateTouch}
          />
        </div>
        <div ref={below} className="live-stage-caption">
          {caption}
        </div>
      </div>
      <p className="live-screen-say" role="status">
        {conn === 'connecting'
          ? 'Connecting to the agent’s browser.'
          : conn === 'connected'
            ? control
              ? hasFocus
                ? 'Your keys go to the agent’s browser. Click outside it to use the console.'
                : 'Click the browser to type into it.'
              : 'View only.'
            : conn === 'failed'
              ? `The live connection keeps failing: ${why ?? ''}`
              : why}
      </p>
    </div>
  )
}
