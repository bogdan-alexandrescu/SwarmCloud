// THEATER MODE: the Browser tab's session, full screen
// (docs/design/live-browser.md §6, LB-5; lane LB-L11b).
//
// THEATER IS A VIEW, NOT A SECOND SESSION. The Browser tab (LiveBrowser.tsx,
// lane L11) owns the one websocket to `/v1/tasks/{id}/live` and the one viewer
// ticket swarm-api minted for it (§3.3, §5.2). Opening the theater must not
// open another: a second socket is a second ticket, a second swarm-api
// instance held, a second `viewer_joined` in the audit, and -- if it raced the
// first -- a viewer cap reached by one person. So the theater BORROWS the
// tab's screen node, the element noVNC draws its canvas into, and moves it
// into its stage; on close it puts it back where it was. noVNC keeps drawing
// into the same canvas throughout, because nothing about its connection
// changed. This file constructs no WebSocket and reads no ticket route; the
// tests count both.
//
// HOW THE TAB USES IT. The tab wraps its body in <TheaterHost> and hands it
// the session (with `screen`, the element its RFB was attached to) and the
// slots that are the tab's own: the origin strip, the logs, the device bar
// and the Take control button. Those are passed in, not rebuilt here, so who
// may take control, who may switch the device, and what the strip shows each
// have one implementation (§3.4, §5.1, §6.1) -- the tab's. <TheaterButton>
// (⤢) and the key `T` open it; the hand-off banner and a graph card open it
// through `useTheater()` or `initiallyOpen`.
//
// THE SCREEN NODE MUST BE ONE REACT DOES NOT FILL. The tab renders it as an
// empty element with a ref and gives it to noVNC, which owns its children.
// React never inserts into it, so moving it does not confuse reconciliation;
// and when the tab unmounts while the theater is open (prev/next), React
// removes the tab's wrapper, not this node, so the move-back finds a detached
// parent and is harmless.
//
// KEYS. Esc closes and `T` opens -- but never while focus is in the screen,
// where every key is the person's, for the remote browser (L11: "keys
// captured only with canvas focus"), and never while it is in a field. A
// person in control leaves with "Esc · back to tab", which is always on screen.
//
// THE MOCKUP (lane LB-UX2a, owner 2026-10-12: "Browser Tab and Theater",
// matched item by item). The top bar is a "LIVE <agent>" pill, then
// ‹ prev agent, next › and Esc · back to tab. The right column is DEVICE and
// OTHER LIVE. ONE SET OF DEVICE CHIPS (QA12-08/24, owner 2026-10-13): the top
// bar's own chips were always disabled while the DEVICE column's -- the tab's
// switcher -- worked, so the top set is gone and DEVICE holds the one that
// switches. The bottom bar is a scrubber over
// the recording with ◀◀ replay and live ●, and the tab's Take control.
//
// PREV AND NEXT ARE FED HERE. Measured on SwarmCloud 2026-10-11/12: the
// Browser tab passed no list, so prev, next and the other live agents were
// never drawn. With no list passed, the host reads the tenant's live browser
// agents itself -- the Agents list's Live filter, `liveBrowserRows`, needs-you
// first -- and goes to the chosen one's `…/browser?theater=1`. A caller with
// its own list (the workflow graph's live steps) still passes it.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

import { loadLiveRecordings, loadTasks, returnLiveControl, type LiveRecording, type LiveSession } from './api'
import { agentName, shortTaskId, useAgentName } from './agentlist'
import { errorHeading, route, tenantQuery, type ApiError } from './fetch'
import { liveBrowserRows, needsYou } from './Handoff'
import { stripVerdict } from './LiveBrowser'
import { theaterPath } from './LiveThumb'
import { StartupProgress } from './StartupProgress'
import { TASK_PAGE_LIMIT } from './pageLimits'
import { HandoffChecklist, TheaterTimeline, type TheaterHandoff } from './TheaterTimeline'
import './styles/theater.css'

export type { TheaterHandoff } from './TheaterTimeline'

/** What the Browser tab hands the theater. Nothing here opens a connection. */
export interface TheaterSession {
  taskId: string
  /** The task's tenant: the prefix every recording of it must sit under (§7). */
  tenantId: string
  attemptId: string
  sessionId: string
  /** The element the tab's noVNC draws into; null until the tab has mounted it. */
  screen: HTMLElement | null
  /** True while the task is RUNNING with a session; false for a finished one. */
  live: boolean
  /** The agent's name, for the theater's head. Defaults to the list's name for it, then the task id. */
  label?: string
  /**
   * The tab's session read, as the tab polled it: the device it reports and
   * who is in control. Read here, never re-read -- the tab owns that poll.
   */
  browser?: LiveSession | null
  /** The tab's reload, run after the checklist returns control, as its Return control button does. */
  reload?: () => void
}

/** One live browser agent, for prev and next (§6: "the other live agents"). */
export interface LiveAgentRef {
  taskId: string
  label: string
  /** `human_wait` is set: the agent is waiting for a person. */
  needsYou: boolean
  /** The device preset it reports, when its caller knows it; the outline says "not reported" otherwise. */
  device?: string | null
}

/**
 * The order prev and next walk: needs-you first (§6, "needs-you first", the
 * Agents list's Live filter), and the caller's order otherwise -- a stable
 * partition, so a workflow's steps stay in step order within each group.
 */
export function orderLiveAgents(agents: readonly LiveAgentRef[]): LiveAgentRef[] {
  return [...agents.filter((a) => a.needsYou), ...agents.filter((a) => !a.needsYou)]
}

function neighbours(ordered: readonly LiveAgentRef[], taskId: string): { prev: LiveAgentRef | null; next: LiveAgentRef | null } {
  const i = ordered.findIndex((a) => a.taskId === taskId)
  if (i === -1) return { prev: null, next: null }
  return { prev: ordered[i - 1] ?? null, next: ordered[i + 1] ?? null }
}

/**
 * How often the self-read list of live agents is read again while the theater
 * is open: an agent that starts or finishes shows up in OTHER LIVE within
 * this, and the Agents list idles at the same 30 s.
 */
export const LIVE_AGENTS_POLL_MS = 30_000

/**
 * The tenant's live browser agents, read only while `enabled`: the same page
 * the Agents list reads (`view=summary`), through the Live filter's own rule
 * (`liveBrowserRows`: RUNNING browser agents, needs-you first). Null until
 * the first read answers, and after a failed one -- prev and next then wait,
 * rather than walk a list nobody read.
 */
function useLiveAgents(enabled: boolean): { agents: LiveAgentRef[] | null; failed: boolean } {
  const [agents, setAgents] = useState<LiveAgentRef[] | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!enabled) return
    let live = true
    const tick = async (): Promise<void> => {
      const r = await loadTasks(TASK_PAGE_LIMIT, { view: 'summary' })
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') {
        setAgents(liveBrowserRows(r.data.tasks).map((t) => ({ taskId: t.id, label: agentName(t), needsYou: needsYou(t) })))
        setFailed(false)
      } else if (r.status === 'empty') {
        setAgents([])
        setFailed(false)
      } else {
        setFailed(true)
      }
    }
    void tick()
    const id = setInterval(() => void tick(), LIVE_AGENTS_POLL_MS)
    return () => {
      live = false
      clearInterval(id)
    }
  }, [enabled])
  return { agents, failed }
}

/**
 * Go to another agent's theater through the router: write its path and let
 * App read it back, as Back does (`popstate`, as Overview's `openAgentPath`).
 * `wf` is kept when the walk is a workflow's steps, so its prev and next stay
 * that workflow's.
 */
function openTheaterOf(taskId: string, keepWorkflow: boolean): void {
  const wf = keepWorkflow ? new URLSearchParams(window.location.search).get('wf') : null
  window.history.pushState(null, '', theaterPath(taskId, wf))
  window.dispatchEvent(new PopStateEvent('popstate'))
}

// ---------------------------------------------------------------------------
// Devices (§6.1): the chips, and an agent's outline
// ---------------------------------------------------------------------------

/** The gate's presets (§6.1), as the mockup's chips name them, in its order. */
export const DEVICE_CHIPS: ReadonlyArray<readonly [string, string]> = [
  ['desktop', 'Desktop'],
  ['iphone-15', 'iPhone 15'],
  ['phone-390', 'Phone 390'],
  ['phone-380', 'Phone 380'],
  ['pixel-8', 'Pixel 8'],
  ['iphone-se', 'iPhone SE'],
  ['ipad-7', 'iPad'],
]

function chipLabel(preset: string): string {
  return DEVICE_CHIPS.find(([n]) => n === preset)?.[1] ?? preset
}

type Outline = 'desktop' | 'phone' | 'tablet' | 'unknown'

function outlineOf(preset: string | null | undefined): Outline {
  if (preset === 'desktop') return 'desktop'
  if (preset === 'ipad-7') return 'tablet'
  if (preset === 'iphone-15' || preset === 'phone-390' || preset === 'phone-380' || preset === 'pixel-8' || preset === 'iphone-se') return 'phone'
  return 'unknown'
}

/** The preset's outline, turned on its side when the device is rotated (QA12-24). */
function DeviceOutline({ preset, orientation }: { preset: string | null | undefined; orientation?: string | null }) {
  const kind = outlineOf(preset)
  const landscape = orientation === 'landscape'
  return (
    <span
      className={`th-outline is-${kind}${landscape ? ' is-landscape' : ''}`}
      data-device-outline={kind}
      data-orientation={orientation ?? undefined}
      title={kind === 'unknown' ? 'Device not reported' : chipLabel(preset ?? '')}
      aria-hidden="true"
    />
  )
}

// ---------------------------------------------------------------------------
// Replay (§7)
// ---------------------------------------------------------------------------

/** One recorded segment, as much of it as the theater reads (`LiveRecording`). */
export type RecordingSegment = Pick<LiveRecording, 'attempt_id' | 'seq' | 'object' | 'started_at'>

/** The listing, as much of it as the theater reads (`LiveRecordings`). */
export interface RecordingListing {
  tenant_id: string
  task_id: string
  segments: RecordingSegment[]
}

const SEGMENT_ID = /^[A-Za-z0-9_-]+$/

/**
 * The relay's name for segment `seq` (`swarm_live.recorder.segment_name`):
 * the sequence zero-padded to 13 digits. An unpadded `rec-3.live.webm` is not
 * a name the relay writes, so comparing against it refused every real
 * segment (LB-SEAM, tests/fixtures/live/recordings.json).
 */
export function segmentName(seq: number): string {
  return `rec-${String(seq).padStart(13, '0')}.live.webm`
}

/**
 * The segments this view may play: those whose object is EXACTLY
 * `tenants/<the task's tenant>/tasks/<task>/attempts/<a>/live/rec-<seq>.live.webm`,
 * from a listing that names this task and this tenant.
 *
 * swarm-api already refuses a foreign task with a 404 (§5.2), so on a correct
 * server this drops nothing. It is here because a recording is the one thing
 * in this view that outlives the session, and a listing that ever carried
 * another tenant's object -- a server bug, a cache keyed wrong -- would
 * otherwise put that tenant's frames in front of this one's member
 * (invariant 9, LB-2). Exact equality, not a prefix test, so `..` in a name,
 * an actions file or an object outside `live/` cannot pass.
 */
export function segmentsOfTenant<S extends RecordingSegment>(
  listing: { tenant_id: string; task_id: string; segments: S[] },
  session: Pick<TheaterSession, 'taskId' | 'tenantId'>,
): { kept: S[]; refused: number } {
  const all = Array.isArray(listing.segments) ? listing.segments : []
  if (listing.tenant_id !== session.tenantId || listing.task_id !== session.taskId) return { kept: [], refused: all.length }
  const kept = all.filter(
    (s) =>
      Number.isInteger(s.seq) &&
      s.seq >= 0 &&
      SEGMENT_ID.test(s.attempt_id) &&
      s.object === `tenants/${session.tenantId}/tasks/${session.taskId}/attempts/${s.attempt_id}/live/${segmentName(s.seq)}`,
  )
  return { kept, refused: all.length - kept.length }
}

/**
 * The URL a `<video>` streams one segment from. The browser fetches it itself
 * and can send no `X-Swarm-Tenant`, so the chosen tenant rides as `?tenant=`,
 * as an artifact's raw link does (`artifactRawUrl`). The server resolves the
 * object from the task, attempt and sequence; the object name never travels.
 */
export function recordingUrl(taskId: string, attemptId: string, seq: number): string {
  return route(
    '/v1/tasks/{id}/attempts/{attempt}/live/recordings/{seq}',
    { id: taskId, attempt: attemptId, seq },
    tenantQuery(new URLSearchParams()),
  ).url
}

/**
 * THE SCRUBBER'S UNIT: one segment is one minute of bar. The relay closes a
 * segment every minute (§7), and a segment's own length is not known until
 * its video has been fetched -- which this view does only for the one being
 * played. So the segments are laid end to end at their nominal minute, and
 * a position inside one seeks that far into it, clamped to its real length
 * once its metadata has loaded.
 */
export const SEGMENT_SECONDS = 60

/**
 * How often an open theater reads the recording's listing again (QA12-13:
 * it was read once, so a theater left open never offered the minutes
 * recorded since). The relay closes a segment a minute, so a shorter period
 * would mostly read the same list.
 */
export const RECORDINGS_POLL_MS = 60_000

type Listing =
  | { status: 'loading' }
  | { status: 'none'; error: ApiError | null; refused: number }
  | { status: 'ok'; segments: RecordingSegment[] }

/** Playing segment `at`, from `offset` seconds into it. */
interface Playing {
  at: number
  offset: number
}

/** `iso` plus `seconds`, as an ISO instant; null when `iso` is not a time. */
function plus(iso: string | null, seconds: number): string | null {
  if (iso === null) return null
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? null : new Date(t + seconds * 1000).toISOString()
}

function clock(iso: string | null): string {
  if (iso === null) return 'unknown time'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// ---------------------------------------------------------------------------
// The host: open state, the `T` key, and the way back
// ---------------------------------------------------------------------------

interface TheaterApi {
  open: () => void
  isOpen: boolean
}

const TheaterContext = createContext<TheaterApi | null>(null)

/** Open the theater from anything inside a <TheaterHost> (the hand-off banner, a menu). */
export function useTheater(): TheaterApi | null {
  return useContext(TheaterContext)
}

/** True for a target where a typed `t` is text or belongs to the remote browser. */
function typingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false
  if (target.closest('[data-live-screen]') || target.tagName === 'CANVAS') return true
  if (target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]')) return true
  return false
}

/** Every scrolled ancestor of `el`, and the document, with where each was. */
function scrollPositions(el: Element | null): Array<[Element, number]> {
  const out: Array<[Element, number]> = []
  for (let n = el?.parentElement ?? null; n; n = n.parentElement) {
    if (n.scrollHeight > n.clientHeight && n.scrollTop > 0) out.push([n, n.scrollTop])
  }
  const doc = document.scrollingElement
  if (doc && doc.scrollTop > 0 && !out.some(([n]) => n === doc)) out.push([doc, doc.scrollTop])
  return out
}

export interface TheaterSlots {
  /**
   * The live browser agents prev and next walk; ordered here, needs-you
   * first. Passed by a caller with a list of its own (the workflow graph's
   * live steps); when it is not, the host reads the tenant's live browser
   * agents itself while the theater is open (`useLiveAgents`).
   */
  agents?: readonly LiveAgentRef[]
  /**
   * Go to another live agent with the theater kept open (its tab owns its
   * session). By default, its `…/browser?theater=1` through the router.
   */
  onNavigate?: (taskId: string) => void
  /** The tab's log view, the same component its logs toggle shows. */
  logs?: ReactNode
  /**
   * The tab's device switcher (§6.1, Q13): it switches the preset, rotates and
   * turns touch on or off for the person in control (`you.can_switch_device`,
   * LB-DEV-B), and is disabled for everyone else -- the tab decides, once.
   */
  device?: ReactNode
  /** The tab's Take control / Return control, eligibility decided by the tab (§5.1). */
  control?: ReactNode
  /**
   * The tab's Take control alone (`<LiveControl takeOnly />`), for the
   * hand-off checklist: the same route and the same asks as `control`.
   */
  take?: ReactNode
  /** The tab's origin strip (§3.4, §5.7): drawn above the canvas here too. */
  strip?: ReactNode
  /** `Task.human_wait`, when set: the left column becomes the checklist (§6). */
  handoff?: TheaterHandoff | null
}

export interface TheaterHostProps extends TheaterSlots {
  session: TheaterSession
  /** Open on mount: the graph card's ⤢ and the banner's "Open in theater" land here. */
  initiallyOpen?: boolean
  children: ReactNode
}

/**
 * THE ADDRESS FOLLOWS THE THEATER (QA12-16). Opening by ⤢, `T` or the banner
 * did not put `?theater=1` in the address, so a reload or a copied link
 * dropped it. Replaced, not pushed: open and close are a view of the same
 * tab, and Back should leave the tab, not toggle the theater. Only on a
 * Browser tab's address (`…/browser`), the one `?theater=1` is read from
 * (paths.ts); the rest of the query (`wf`) is kept.
 */
function syncTheaterQuery(open: boolean): void {
  const { pathname, search, hash } = window.location
  if (!/\/browser\/?$/.test(pathname)) return
  const q = new URLSearchParams(search)
  if ((q.get('theater') === '1') === open) return
  if (open) q.set('theater', '1')
  else q.delete('theater')
  const rest = q.toString()
  window.history.replaceState(window.history.state, '', `${pathname}${rest ? `?${rest}` : ''}${hash}`)
}

export function TheaterHost({ session, initiallyOpen = false, children, ...slots }: TheaterHostProps) {
  const [open, setOpen] = useState(initiallyOpen)
  const selfRead = slots.agents === undefined
  const loaded = useLiveAgents(open && selfRead)
  // NOT READ YET IS NOT NONE (BT-008, #2423): this was `loaded ?? []`, so the
  // theater said "No other live browser agent." for the seconds before the
  // first read answered, while four were running. The theater is handed the
  // null and says it is reading.
  const agents = selfRead ? loaded.agents : slots.agents
  const agentsFailed = selfRead && loaded.failed
  const onNavigate = useMemo(
    () => slots.onNavigate ?? ((taskId: string) => openTheaterOf(taskId, !selfRead)),
    [slots.onNavigate, selfRead],
  )
  const hostRef = useRef<HTMLDivElement>(null)
  const saved = useRef<{ scroll: Array<[Element, number]>; focus: Element | null } | null>(null)

  const openTheater = useCallback(() => {
    if (saved.current === null) {
      saved.current = { scroll: scrollPositions(hostRef.current), focus: document.activeElement }
    }
    setOpen(true)
  }, [])
  const close = useCallback(() => setOpen(false), [])

  // Not on mount when closed: a tab that opens without the theater leaves an
  // address it did not write alone. Every change after, and an open mount.
  const synced = useRef(false)
  useEffect(() => {
    if (!synced.current && !open) return
    synced.current = true
    syncTheaterQuery(open)
  }, [open])

  // Esc returns to the tab AT THE SAME SCROLL (§6): restored after the tab is
  // back in the layout, before paint. FOCUS GOES TO ⤢ (LB-V2, visual QA pass
  // 5): the button that opens the theater again, also when it was opened by
  // `T`, the banner or `?theater=1`, where nothing in the tab held focus; only
  // a tab with no ⤢ gets back what was focused before.
  const wasOpen = useRef(open)
  useLayoutEffect(() => {
    const closed = wasOpen.current && !open
    wasOpen.current = open
    if (!closed) return
    const { scroll, focus } = saved.current ?? { scroll: [], focus: null }
    saved.current = null
    for (const [el, top] of scroll) el.scrollTop = top
    const button = hostRef.current?.querySelector<HTMLElement>('[data-theater-button]')
    if (button) button.focus()
    else if (focus instanceof HTMLElement && focus.isConnected) focus.focus()
  }, [open])

  useEffect(() => {
    if (open) return
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 't' && e.key !== 'T') return
      if (e.ctrlKey || e.metaKey || e.altKey || e.repeat || e.defaultPrevented) return
      if (typingTarget(e.target)) return
      e.preventDefault()
      openTheater()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, openTheater])

  const api = useMemo(() => ({ open: openTheater, isOpen: open }), [openTheater, open])
  return (
    <TheaterContext.Provider value={api}>
      <div ref={hostRef} className="th-host">
        {children}
      </div>
      {open ? <Theater session={session} onClose={close} {...slots} agents={agents} agentsFailed={agentsFailed} onNavigate={onNavigate} /> : null}
    </TheaterContext.Provider>
  )
}

/** ⤢ Theater, for the Browser tab's toolbar and the graph card. */
export function TheaterButton({ className }: { className?: string }) {
  const theater = useTheater()
  if (!theater) return null
  return (
    <button
      type="button"
      className={className ?? 'ctl-btn th-open'}
      aria-label="Open theater (T)"
      title="Theater (T)"
      data-theater-button=""
      onClick={theater.open}
    >
      <span aria-hidden="true">⤢</span> Theater
    </button>
  )
}

// ---------------------------------------------------------------------------
// The theater itself
// ---------------------------------------------------------------------------

/** A phone: ⤢ goes native full screen there, landscape preferred (§6). */
const PHONE = '(pointer: coarse) and (max-width: 640px)'

type LeftTab = 'handoff' | 'timeline' | 'logs'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), video[controls], [tabindex]:not([tabindex="-1"])'

/** What Tab may land on inside `root`: not disabled, not in a hidden panel. */
function focusables(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.closest('[hidden]') === null)
}

export interface TheaterProps extends Omit<TheaterSlots, 'agents'> {
  session: TheaterSession
  onClose: () => void
  /**
   * The live browser agents, ordered here; `null` while the host's own read
   * has not answered yet (BT-008): OTHER LIVE then says it is reading, never
   * that there is none.
   */
  agents?: readonly LiveAgentRef[] | null
  /** The host's last read of the live agents failed; with `agents` null nothing was ever read. */
  agentsFailed?: boolean
}

export function Theater({ session, onClose, agents, agentsFailed = false, onNavigate, logs, device, control, take, strip, handoff }: TheaterProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const [left, setLeft] = useState<LeftTab>(handoff ? 'handoff' : 'timeline')
  const [listing, setListing] = useState<Listing>({ status: 'loading' })
  const [playing, setPlaying] = useState<Playing | null>(null)
  const [played, setPlayed] = useState(0)
  const [seen, setSeen] = useState<ReadonlySet<LeftTab>>(() => new Set([left]))
  useEffect(() => {
    setSeen((was) => (was.has(left) ? was : new Set([...was, left])))
  }, [left])

  // A hand-off that arrives while the theater is open turns the column into
  // the checklist; one that resolves gives the timeline back.
  useEffect(() => {
    setLeft((was) => (handoff ? 'handoff' : was === 'handoff' ? 'timeline' : was))
  }, [handoff])

  // BORROW THE SCREEN. Layout effect, so the canvas never paints in both
  // places or in neither.
  useLayoutEffect(() => {
    const node = session.screen
    const stage = stageRef.current
    if (!node || !stage) return
    const home = node.parentNode
    const after = node.nextSibling
    stage.appendChild(node)
    return () => {
      if (!home) return
      home.insertBefore(node, after && after.parentNode === home ? after : null)
    }
  }, [session.screen])

  useEffect(() => {
    rootRef.current?.focus()
  }, [])

  // Focus that leaves by any other way -- a script, a click on something
  // behind -- is brought back. A toast or a dialog the theater opened sits in
  // its own portal above it, and stays usable: only the page's own regions
  // are refused.
  useEffect(() => {
    const onFocus = (e: FocusEvent): void => {
      const root = rootRef.current
      const t = e.target
      if (!root || !(t instanceof Element) || root.contains(t)) return
      if (t.closest('[role="dialog"], [role="alertdialog"], .c-toasts')) return
      root.focus()
    }
    document.addEventListener('focusin', onFocus)
    return () => document.removeEventListener('focusin', onFocus)
  }, [])

  // ESC CLOSES THE THEATER AND NOTHING BEHIND IT (LB-V2, visual QA pass 5:
  // Esc went from /agents/live/<task>/browser to /agents/live). The theater is
  // portalled to <body>, but React bubbles a key through the portal to the
  // agent split's own `onKeyDown`, which closed the split too. So the key is
  // taken here, on the root, and stopped: neither the split's `onKeyDown` nor
  // its window listener hears it. A key pressed in the screen is the remote
  // browser's -- the theater does not close, and the split still never hears
  // it. The window listener below is for a key with no focus inside (the
  // page body); the split's window listener stands down for that one while
  // a theater is open (`[data-theater]`, AgentSplit.tsx).
  //
  // TAB STAYS INSIDE (QA12-10: the root is aria-modal, but Tab walked out to
  // the page's nav behind it). The last control wraps to the first and back.
  // Not in the screen: there Tab is a key for the remote browser.
  const onRootKey = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (e.key === 'Tab' && !e.defaultPrevented && rootRef.current) {
      if (e.target instanceof Element && e.target.closest('[data-live-screen]')) return
      const all = focusables(rootRef.current)
      const first = all[0]
      const last = all[all.length - 1]
      if (first === undefined || last === undefined) return
      const at = document.activeElement
      if (e.shiftKey && (at === first || at === rootRef.current)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && at === last) {
        e.preventDefault()
        first.focus()
      }
      return
    }
    if (e.key !== 'Escape' || e.defaultPrevented) return
    e.stopPropagation()
    if (e.target instanceof Element && e.target.closest('[data-live-screen]')) return
    e.preventDefault()
    onClose()
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      if (e.target instanceof Element && e.target.closest('[data-live-screen]')) return
      e.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // On a phone, native full screen in landscape. Leaving full screen by the
  // system gesture is leaving the theater.
  useEffect(() => {
    const root = rootRef.current
    if (!root || typeof window.matchMedia !== 'function' || !window.matchMedia(PHONE).matches) return
    if (typeof root.requestFullscreen !== 'function') return
    let entered = false
    const onChange = (): void => {
      if (document.fullscreenElement === root) entered = true
      else if (entered) onClose()
    }
    document.addEventListener('fullscreenchange', onChange)
    root
      .requestFullscreen()
      .then(() => {
        const orientation = screen.orientation as ScreenOrientation & { lock?: (o: string) => Promise<void> }
        return orientation?.lock?.('landscape')
      })
      .catch(() => {
        /* refused: the overlay is already full window, which is the fallback */
      })
    return () => {
      document.removeEventListener('fullscreenchange', onChange)
      if (document.fullscreenElement === root) void document.exitFullscreen().catch(() => {})
    }
  }, [onClose])

  // THE RECORDING'S LISTING, read when the theater opens, so the scrubber
  // has its segments to lay out, and AGAIN EVERY RECORDINGS_POLL_MS while the
  // session is live (QA12-13), so the minutes recorded since are offered.
  // A re-read never stops a replay, and a failed one keeps the list it had.
  // Only the listing: no segment's video is fetched until one is played.
  const { taskId, tenantId } = session
  const sessionLive = session.live
  useEffect(() => {
    let live = true
    setListing({ status: 'loading' })
    setPlaying(null)
    const read = (first: boolean): void => {
      void loadLiveRecordings(taskId).then((res) => {
        if (!live) return
        if (res.status === 'ok' || res.status === 'stale') {
          const { kept, refused } = segmentsOfTenant(res.data, { taskId, tenantId })
          setListing((was) =>
            kept.length > 0
              ? { status: 'ok', segments: kept }
              : first || was.status !== 'ok'
                ? { status: 'none', error: null, refused }
                : was,
          )
        } else if (first) {
          setListing({ status: 'none', error: res.status === 'error' ? res.error : null, refused: 0 })
        }
      })
    }
    read(true)
    const id = sessionLive ? setInterval(() => read(false), RECORDINGS_POLL_MS) : null
    return () => {
      live = false
      if (id !== null) clearInterval(id)
    }
  }, [taskId, tenantId, sessionLive])

  const segments = listing.status === 'ok' ? listing.segments : []
  const total = segments.length * SEGMENT_SECONDS
  const segment = playing !== null ? segments[playing.at] ?? null : null

  /** Play from `seconds` into the recording, the segments laid end to end. */
  const seek = useCallback(
    (seconds: number) => {
      if (segments.length === 0) return
      const at = Math.min(segments.length - 1, Math.max(0, Math.floor(seconds / SEGMENT_SECONDS)))
      const offset = Math.max(0, seconds - at * SEGMENT_SECONDS)
      setPlaying({ at, offset })
      setPlayed(offset)
    },
    [segments.length],
  )

  // A seek inside the segment already playing moves its video; a seek into
  // another one mounts that one's video, which seeks on its metadata.
  useEffect(() => {
    const v = videoRef.current
    if (playing === null || !v || v.readyState < 1) return
    v.currentTime = Math.min(playing.offset, Number.isFinite(v.duration) ? v.duration : playing.offset)
  }, [playing])

  const listed = useMemo(() => agents ?? [], [agents])
  const ordered = useMemo(() => {
    // This agent is walked from even when the list read did not carry it
    // (a page that ended before it): placed by its own needs-you.
    const all = listed.some((a) => a.taskId === session.taskId)
      ? listed
      : [{ taskId: session.taskId, label: session.label ?? session.taskId, needsYou: handoff != null }, ...listed]
    return orderLiveAgents(all)
  }, [listed, session.taskId, session.label, handoff])
  const { prev, next } = neighbours(ordered, session.taskId)
  const others = ordered.filter((a) => a.taskId !== session.taskId)
  const unread = agents === null
  // "LIVE <agent name>" (LB-V2, visual QA pass 5 read "LIVE task_…"): the
  // tab passes no label, and the self-read list answers after the first paint
  // (or not at all, past its page), so the name the split recorded when it
  // read this task comes first. The id is the pill's title.
  const known = useAgentName(session.taskId)
  const label = session.label ?? known ?? listed.find((a) => a.taskId === session.taskId)?.label ?? session.taskId

  const browser = session.browser ?? null
  const current = browser?.device ?? null
  const youControl = browser !== null && browser.controller !== null && browser.controller.email === browser.you.email
  // THE STRIP'S OWN VERDICT ticks "check the strip" (QA12-23): green, for
  // the site the hand-off named. The same rule the strip is drawn by
  // (`stripVerdict`), so the tick and the colour never disagree.
  const stripOk =
    handoff != null && browser !== null && browser.human_wait !== null && stripVerdict(browser).tone === 'ok'
  // LiveControl's own eligibility (§5.1), for the checklist to say who holds
  // control instead of offering a button the tab would not draw.
  const canTake =
    browser !== null && browser.state === 'open' && browser.you.can_control && (browser.controller === null || browser.you.can_preempt)
  const reload = session.reload

  // THE DEVICE COLUMN IS A LIST (LB-V2, visual QA pass 5: the chip row was
  // clipped at "iPhone SE"). The choices are the tab's own device bar (one
  // implementation of who may switch, §6.1), stacked by theater.css; each
  // chip is marked here with its preset's outline, which the stylesheet draws
  // before the name, as the mockup's list does. The bar names a chip by its
  // label, so the label is read back to the preset it stands for.
  const deviceRef = useRef<HTMLDivElement>(null)
  const presets = browser?.presets
  useLayoutEffect(() => {
    const chips = deviceRef.current?.querySelectorAll<HTMLElement>('.live-chip') ?? []
    for (const chip of chips) {
      const text = chip.textContent ?? ''
      const name = DEVICE_CHIPS.find(([, t]) => t === text)?.[0] ?? text
      const kind = outlineOf(name)
      const mobile = presets?.find((p) => p.name === name)?.mobile
      chip.dataset.outline = kind !== 'unknown' || mobile === undefined ? kind : mobile ? 'phone' : 'desktop'
      // The chosen chip's outline turns with the device (QA12-24).
      if (current !== null && name === current.preset) chip.dataset.orientation = current.orientation
      else delete chip.dataset.orientation
    }
  })
  // The bottom bar's Return control, for the checklist: the same route, then
  // the tab's reload, so the strip and the bar redraw from the server.
  const returnControl = useCallback(async (): Promise<string | null> => {
    const r = await returnLiveControl(taskId)
    reload?.()
    return r.status === 'error' ? r.error.message : null
  }, [taskId, reload])

  const leftTabs: Array<[LeftTab, string]> = [
    ...(handoff ? ([['handoff', 'Hand-off']] as Array<[LeftTab, string]>) : []),
    ['timeline', 'Timeline'],
    ['logs', 'Logs'],
  ]

  const replaying = segment !== null && playing !== null

  return createPortal(
    <div
      ref={rootRef}
      className="th-root"
      role="dialog"
      aria-modal="true"
      aria-label={`Theater: ${label}`}
      tabIndex={-1}
      data-theater=""
      onKeyDown={onRootKey}
    >
      <header className="th-head">
        {/* A REPLAY SAYS SO (QA12-13): the pill states what the stage shows. */}
        <span className="th-pill" data-testid="theater-live-pill" title={session.taskId}>
          <span className={`th-pill-live${replaying ? ' is-replay' : session.live ? '' : ' is-ended'}`}>
            {replaying ? 'REPLAY' : session.live ? 'LIVE' : 'ENDED'}
          </span>
          <span className="th-title">{label}</span>
        </span>
        {/* While you hold control, "Needs you" is answered: you are (QA12-05). */}
        {youControl ? (
          <span className="th-flag is-yours">You are in control</span>
        ) : handoff ? (
          <span className="th-flag">Needs you</span>
        ) : null}
        <span className="th-spacer" />
        <button
          type="button"
          className="ctl-btn"
          disabled={!prev || !onNavigate}
          title={prev ? prev.label : unread ? 'Reading the live browser agents' : 'No live agent before this one'}
          onClick={() => prev && onNavigate?.(prev.taskId)}
        >
          ‹ prev agent
        </button>
        <button
          type="button"
          className="ctl-btn"
          disabled={!next || !onNavigate}
          title={next ? next.label : unread ? 'Reading the live browser agents' : 'No live agent after this one'}
          onClick={() => next && onNavigate?.(next.taskId)}
        >
          next ›
        </button>
        <button type="button" className="ctl-btn th-close" onClick={onClose} title="Back to the tab (Esc)">
          Esc · back to tab
        </button>
      </header>

      <aside className="th-left">
        <div className="th-tabs" role="tablist" aria-label="Left column">
          {leftTabs.map(([id, name]) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`th-tab-${id}`}
              aria-selected={left === id}
              aria-controls={`th-panel-${id}`}
              className={`th-tab${left === id ? ' is-on' : ''}`}
              onClick={() => setLeft(id)}
            >
              {name}
            </button>
          ))}
        </div>
        {/* Each panel is mounted the first time it is chosen and kept, hidden,
            after: switching back to the timeline does not re-read it, and the
            logs are not read until someone asks for them. */}
        {leftTabs.map(([id]) =>
          seen.has(id) || left === id ? (
            <div
              key={id}
              className="th-left-body"
              role="tabpanel"
              id={`th-panel-${id}`}
              aria-labelledby={`th-tab-${id}`}
              hidden={left !== id}
            >
              {id === 'handoff' && handoff ? (
                <HandoffChecklist
                  handoff={handoff}
                  youControl={youControl}
                  stripOk={stripOk}
                  onReturn={browser ? returnControl : undefined}
                  take={take}
                  canTake={browser ? canTake : undefined}
                  holder={browser?.controller?.email ?? null}
                />
              ) : null}
              {id === 'timeline' ? <TheaterTimeline taskId={session.taskId} live={session.live} /> : null}
              {id === 'logs' ? logs ?? <p className="th-note">The tab has no log view for this agent.</p> : null}
            </div>
          ) : null,
        )}
      </aside>

      <main className="th-center">
        {/* During a replay the strip still reads the LIVE page: dimmed, and it says so. */}
        {strip ? (
          <div className={`th-strip${replaying ? ' is-replay' : ''}`}>
            {replaying ? <span className="th-strip-tag">Live now, not the replay:</span> : null}
            {strip}
          </div>
        ) : null}
        {replaying ? (
          <p className="th-replay-tag" data-testid="theater-replay-tag" role="status">
            Replay · {clock(plus(segment.started_at, played))}
          </p>
        ) : null}
        <div className="th-stage" ref={stageRef} hidden={segment !== null} />
        {replaying ? (
          <video
            ref={videoRef}
            key={`${segment.attempt_id}/${segment.seq}`}
            className="th-replay"
            data-testid="theater-replay"
            src={recordingUrl(session.taskId, segment.attempt_id, segment.seq)}
            autoPlay
            muted
            playsInline
            onLoadedMetadata={(e) => {
              const v = e.currentTarget
              if (playing.offset > 0) v.currentTime = Math.min(playing.offset, Number.isFinite(v.duration) ? v.duration : playing.offset)
            }}
            onTimeUpdate={(e) => setPlayed(Math.min(SEGMENT_SECONDS, e.currentTarget.currentTime))}
            onEnded={() =>
              setPlaying((p) => (p !== null && p.at < segments.length - 1 ? { at: p.at + 1, offset: 0 } : p))
            }
          />
        ) : null}
        {session.screen === null && segment === null ? <p className="th-note">The browser has not connected yet.</p> : null}
        {/* The start-up over the screen area until the first frame (owner request 2026-10-13). */}
        {session.live && segment === null ? <StartupProgress taskId={session.taskId} screen={session.screen} overlay hideWhenDone /> : null}
      </main>

      <aside className="th-right">
        <section className="th-device" aria-labelledby="th-h-device">
          <h3 id="th-h-device" className="th-h">
            DEVICE
          </h3>
          <p className="th-device-now">
            <DeviceOutline preset={current?.preset} orientation={current?.orientation} />
            {current === null
              ? 'Not reported'
              : `${chipLabel(current.preset)} · ${current.orientation}${current.touch ? ' · touch' : ''}`}
          </p>
          <div className="th-device-ctl" ref={deviceRef}>
            {device}
          </div>
        </section>
        <section className="th-agents" aria-labelledby="th-h-others">
          <h3 id="th-h-others" className="th-h">
            OTHER LIVE
          </h3>
          {others.length > 0 ? (
            <ul className="th-agent-list">
              {others.map((a) => (
                <li key={a.taskId} className={`th-agent${a.needsYou ? ' is-needs' : ''}`}>
                  <DeviceOutline preset={a.device} />
                  {onNavigate ? (
                    <button type="button" className="th-agent-go" onClick={() => onNavigate(a.taskId)}>
                      {a.label}
                    </button>
                  ) : (
                    <span className="th-agent-name">{a.label}</span>
                  )}
                  <span className="th-agent-id" title={a.taskId}>
                    {shortTaskId(a.taskId)}
                  </span>
                  {a.needsYou ? <span className="th-flag">Needs you</span> : null}
                </li>
              ))}
            </ul>
          ) : unread ? (
            <p className="th-note" role="status">
              {agentsFailed
                ? `The live browser agents could not be read; reading again every ${LIVE_AGENTS_POLL_MS / 1000} s.`
                : 'Reading the live browser agents…'}
            </p>
          ) : (
            <p className="th-note">No other live browser agent.</p>
          )}
        </section>
      </aside>

      <footer className="th-bottom">
        <button
          type="button"
          className="ctl-btn th-mode"
          aria-pressed={playing !== null}
          disabled={segments.length === 0}
          title={segments.length === 0 ? 'No recording to replay yet.' : 'Replay the recording from its start'}
          onClick={() => seek(0)}
        >
          ◀◀ replay
        </button>
        <div className="th-scrub" data-testid="theater-scrubber">
          <div className="th-scrub-track" aria-hidden="true">
            {segments.map((s, i) => (
              <span
                key={`${s.attempt_id}/${s.seq}`}
                data-segment=""
                className={`th-scrub-seg${playing?.at === i ? ' is-on' : ''}`}
                title={`Segment written ${clock(s.started_at)}`}
              />
            ))}
          </div>
          <input
            type="range"
            className="th-scrub-input"
            aria-label="Recording"
            min={0}
            max={total}
            step={1}
            value={playing === null ? total : Math.min(total, playing.at * SEGMENT_SECONDS + played)}
            aria-valuetext={
              playing === null
                ? 'Live'
                : `Segment ${playing.at + 1} of ${segments.length}, written ${clock(segments[playing.at]?.started_at ?? null)}`
            }
            disabled={segments.length === 0}
            onChange={(e) => seek(Number(e.currentTarget.value))}
          />
        </div>
        <button
          type="button"
          className="ctl-btn th-mode"
          aria-pressed={playing === null}
          title="Back to the live browser"
          onClick={() => setPlaying(null)}
        >
          live <span className="th-live-dot">●</span>
        </button>
        {listing.status === 'loading' ? <span className="th-note">Reading the recording…</span> : null}
        {listing.status === 'none' ? (
          <span className="th-note" role="status">
            {listing.error
              ? `No recording to play: ${errorHeading(listing.error)}.`
              : listing.refused > 0
                ? 'No recording to play: the segments listed are not this tenant’s, and none was opened.'
                : 'No recording yet. Segments are kept for seven days.'}
          </span>
        ) : null}
        <span className="th-spacer" />
        {control}
      </footer>
    </div>,
    document.body,
  )
}
