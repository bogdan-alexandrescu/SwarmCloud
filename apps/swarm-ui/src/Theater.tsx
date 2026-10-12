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
// person in control leaves with the Back button, which is always on screen.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

import { errorHeading, read, route, tenantQuery, type ApiError } from './fetch'
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
  /** The agent's name, for the theater's head. Defaults to the task id. */
  label?: string
}

/** One live browser agent, for prev and next (§6: "the other live agents"). */
export interface LiveAgentRef {
  taskId: string
  label: string
  /** `human_wait` is set: the agent is waiting for a person. */
  needsYou: boolean
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

// ---------------------------------------------------------------------------
// Replay (§7)
// ---------------------------------------------------------------------------

/** One recorded segment, as `GET /v1/tasks/{id}/live/recordings` lists it. */
export interface RecordingSegment {
  attempt_id: string
  seq: number
  /** The object name under the artifact bucket, as the relay wrote it. */
  object: string
  started_at: string
}

/** `GET /v1/tasks/{id}/live/recordings`: every attempt's video segments, oldest first. */
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
 * otherwise put that tenant's masked frames in front of this one's member
 * (invariant 9, LB-2). Exact equality, not a prefix test, so `..` in a name,
 * an actions file or an object outside `live/` cannot pass.
 */
export function segmentsOfTenant(
  listing: RecordingListing,
  session: Pick<TheaterSession, 'taskId' | 'tenantId'>,
): { kept: RecordingSegment[]; refused: number } {
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

export function loadRecordings(taskId: string) {
  return read<RecordingListing>(
    route('/v1/tasks/{id}/live/recordings', { id: taskId }),
    (d) => !Array.isArray(d.segments) || d.segments.length === 0,
  )
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

type Replay =
  | { status: 'loading' }
  | { status: 'none'; error: ApiError | null; refused: number }
  | { status: 'ok'; segments: RecordingSegment[]; at: number }

function clock(iso: string): string {
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
  /** The live browser agents prev and next walk; ordered here, needs-you first. */
  agents?: readonly LiveAgentRef[]
  /** Go to another live agent with the theater kept open (its tab owns its session). */
  onNavigate?: (taskId: string) => void
  /** The tab's log view, the same component its logs toggle shows. */
  logs?: ReactNode
  /** The tab's device switcher, disabled for a viewer by the tab (§6.1, Q13). */
  device?: ReactNode
  /** The tab's Take control / Return control, eligibility decided by the tab (§5.1). */
  control?: ReactNode
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

export function TheaterHost({ session, initiallyOpen = false, children, ...slots }: TheaterHostProps) {
  const [open, setOpen] = useState(initiallyOpen)
  const hostRef = useRef<HTMLDivElement>(null)
  const saved = useRef<{ scroll: Array<[Element, number]>; focus: Element | null } | null>(null)

  const openTheater = useCallback(() => {
    if (saved.current === null) {
      saved.current = { scroll: scrollPositions(hostRef.current), focus: document.activeElement }
    }
    setOpen(true)
  }, [])
  const close = useCallback(() => setOpen(false), [])

  // Esc returns to the tab AT THE SAME SCROLL (§6): restored after the tab is
  // back in the layout, before paint, and focus goes back to what opened it.
  useLayoutEffect(() => {
    if (open || saved.current === null) return
    const { scroll, focus } = saved.current
    saved.current = null
    for (const [el, top] of scroll) el.scrollTop = top
    if (focus instanceof HTMLElement && focus.isConnected) focus.focus()
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
      {open ? <Theater session={session} onClose={close} {...slots} /> : null}
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

export interface TheaterProps extends TheaterSlots {
  session: TheaterSession
  onClose: () => void
}

export function Theater({ session, onClose, agents, onNavigate, logs, device, control, strip, handoff }: TheaterProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const [left, setLeft] = useState<LeftTab>(handoff ? 'handoff' : 'timeline')
  const [replay, setReplay] = useState<Replay | null>(null)
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

  const startReplay = useCallback(async () => {
    setReplay({ status: 'loading' })
    const res = await loadRecordings(session.taskId)
    if (res.status === 'ok' || res.status === 'stale') {
      const { kept, refused } = segmentsOfTenant(res.data, session)
      setReplay(kept.length > 0 ? { status: 'ok', segments: kept, at: 0 } : { status: 'none', error: null, refused })
    } else {
      setReplay({ status: 'none', error: res.status === 'error' ? res.error : null, refused: 0 })
    }
  }, [session])

  const ordered = useMemo(() => orderLiveAgents(agents ?? []), [agents])
  const { prev, next } = neighbours(ordered, session.taskId)
  const label = session.label ?? session.taskId
  const playing = replay?.status === 'ok' ? replay.segments[replay.at] : null

  const leftTabs: Array<[LeftTab, string]> = [
    ...(handoff ? ([['handoff', 'Hand-off']] as Array<[LeftTab, string]>) : []),
    ['timeline', 'Timeline'],
    ['logs', 'Logs'],
  ]

  return createPortal(
    <div ref={rootRef} className="th-root" role="dialog" aria-modal="true" aria-label={`Theater: ${label}`} tabIndex={-1}>
      <header className="th-head">
        <span className="th-title">{label}</span>
        {handoff ? <span className="th-flag">Needs you</span> : null}
        <button type="button" className="ctl-btn th-close" onClick={onClose} title="Back to the tab (Esc)">
          Back to the tab
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
              {id === 'handoff' && handoff ? <HandoffChecklist handoff={handoff} /> : null}
              {id === 'timeline' ? <TheaterTimeline taskId={session.taskId} live={session.live} /> : null}
              {id === 'logs' ? logs ?? <p className="th-note">The tab has no log view for this agent.</p> : null}
            </div>
          ) : null,
        )}
      </aside>

      <main className="th-center">
        {strip ? <div className="th-strip">{strip}</div> : null}
        <div className="th-stage" ref={stageRef} hidden={playing !== null} />
        {playing ? (
          <video
            key={`${playing.attempt_id}/${playing.seq}`}
            className="th-replay"
            data-testid="theater-replay"
            src={recordingUrl(session.taskId, playing.attempt_id, playing.seq)}
            controls
            autoPlay
            muted
            playsInline
            onEnded={() =>
              setReplay((r) => (r?.status === 'ok' && r.at < r.segments.length - 1 ? { ...r, at: r.at + 1 } : r))
            }
          />
        ) : null}
        {session.screen === null && playing === null ? <p className="th-note">The browser has not connected yet.</p> : null}
      </main>

      <aside className="th-right">
        {device ? (
          <section className="th-device" aria-label="Device">
            {device}
          </section>
        ) : null}
        <section className="th-agents" aria-label="Live agents">
          <div className="th-walk">
            <button
              type="button"
              className="ctl-btn"
              aria-label="Previous live agent"
              disabled={!prev || !onNavigate}
              onClick={() => prev && onNavigate?.(prev.taskId)}
            >
              ‹ Prev
            </button>
            <button
              type="button"
              className="ctl-btn"
              aria-label="Next live agent"
              disabled={!next || !onNavigate}
              onClick={() => next && onNavigate?.(next.taskId)}
            >
              Next ›
            </button>
          </div>
          {ordered.length > 0 ? (
            <ul className="th-agent-list">
              {ordered.map((a) => (
                <li
                  key={a.taskId}
                  aria-current={a.taskId === session.taskId ? 'true' : undefined}
                  className={`th-agent${a.taskId === session.taskId ? ' is-on' : ''}${a.needsYou ? ' is-needs' : ''}`}
                >
                  {a.taskId === session.taskId || !onNavigate ? (
                    <span className="th-agent-name">{a.label}</span>
                  ) : (
                    <button type="button" className="th-agent-go" onClick={() => onNavigate(a.taskId)}>
                      {a.label}
                    </button>
                  )}
                  {a.needsYou ? <span className="th-flag">Needs you</span> : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="th-note">No other live browser agent.</p>
          )}
        </section>
      </aside>

      <footer className="th-bottom">
        {replay === null ? (
          <button type="button" className="ctl-btn" onClick={() => void startReplay()}>
            Replay
          </button>
        ) : (
          <>
            <button type="button" className="ctl-btn" onClick={() => setReplay(null)}>
              Back to live
            </button>
            {replay.status === 'loading' ? <span className="th-note">Reading the recording…</span> : null}
            {replay.status === 'none' ? (
              <span className="th-note" role="status">
                {replay.error
                  ? `No recording to play: ${errorHeading(replay.error)}.`
                  : replay.refused > 0
                    ? 'No recording to play: the segments listed are not this tenant’s, and none was opened.'
                    : 'No recording yet. Segments are kept for seven days.'}
              </span>
            ) : null}
            {replay.status === 'ok' ? (
              <div className="th-segments" role="group" aria-label="Recording segments">
                {replay.segments.map((s, i) => (
                  <button
                    key={`${s.attempt_id}/${s.seq}`}
                    type="button"
                    className={`th-seg${i === replay.at ? ' is-on' : ''}`}
                    aria-label={`Segment starting ${clock(s.started_at)}`}
                    aria-pressed={i === replay.at}
                    onClick={() => setReplay({ ...replay, at: i })}
                  >
                    {clock(s.started_at)}
                  </button>
                ))}
              </div>
            ) : null}
          </>
        )}
        <span className="th-spacer" />
        {control}
      </footer>
    </div>,
    document.body,
  )
}
