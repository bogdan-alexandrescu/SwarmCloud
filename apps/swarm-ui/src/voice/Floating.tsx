/**
 * VOICE IT FOLLOWS THE OWNER ACROSS THE CONSOLE (docs/design/voice-it.md
 * §3.12.2 "The session's lifetime once it lives above the page", §3.12.4
 * "Attention items and the spine marker"; the owner's pick of 11 November
 * 2026, the corner bubble).
 *
 * THE FLOATING VIEW. Off Talk, while a session is live, the shell draws a
 * bubble in a corner: the voice ring, mute, push-to-talk, Stop, the working
 * card's one line, the attention count and "Open Voice it". It never draws a
 * card's content (no title, summary or body): it says that something waits,
 * by its kind, and Talk is where it is read. Collapsed, it is the ring alone;
 * dragged, it keeps to the nearest corner. Both are kept for the tab
 * (sessionStorage), so a route change, which draws it again, keeps them.
 *
 * THE KEYBOARD. Escape stops the session, Space holds push-to-talk and M
 * mutes, all only while focus is inside the view: the console's own
 * shortcuts, a dialog's Escape and its text fields are never captured on
 * another page. The view never calls the holder's `attach`, which is Talk's.
 *
 * THE MARKER. An attention item is a card that came into a state new to the
 * owner (a decision waiting, a mock-up or a diagram drawn) or a visual drawn.
 * Each is stamped with the stream sequence it arrived at. The number counts
 * those after `seen_seq`, the highest sequence Talk has shown, which this
 * layer posts while Talk is on screen and the tab visible, at most once a
 * second (`POST /v1/voice/sessions/{id}/seen`). A seen card that still waits
 * on a decision leaves a dot. The session's end clears both.
 */

import { useEffect, useId, useRef, useState, useSyncExternalStore, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'

import { USE_FIXTURES } from '../api'
import { Button } from '../components'
import { route, write } from '../fetch'
import { readSessionStream, specOf, type CardListState, type StreamEvent } from './CardList'
import { typingIn } from './mic'
import { useVoiceSession, type VoiceSession } from './session'
import '../styles/voice-floating.css'

// ---- where it sits, kept for the tab ---------------------------------------------

export type Corner = 'br' | 'bl' | 'tr' | 'tl'

export interface Place {
  corner: Corner
  collapsed: boolean
}

const CORNERS: readonly Corner[] = ['br', 'bl', 'tr', 'tl']
export const PLACE_KEY = 'swarm.voice.floating'
const HOME_PLACE: Place = { corner: 'br', collapsed: false }

function readPlace(): Place {
  try {
    const raw: unknown = JSON.parse(window.sessionStorage.getItem(PLACE_KEY) ?? 'null')
    if (typeof raw !== 'object' || raw === null) return HOME_PLACE
    const r = raw as Record<string, unknown>
    const corner = CORNERS.find((c) => c === r.corner) ?? HOME_PLACE.corner
    return { corner, collapsed: r.collapsed === true }
  } catch {
    return HOME_PLACE
  }
}

let place: Place | null = null
const placeSubs = new Set<() => void>()

function currentPlace(): Place {
  if (place === null) place = readPlace()
  return place
}

function setPlace(patch: Partial<Place>): void {
  place = { ...currentPlace(), ...patch }
  try {
    window.sessionStorage.setItem(PLACE_KEY, JSON.stringify(place))
  } catch {
    /* storage refused: the place is kept for this page only */
  }
  for (const fn of [...placeSubs]) fn()
}

/** Reads the place from the tab's storage again (a test's fresh tab). */
export function forgetPlace(): void {
  place = null
}

function usePlace(): Place {
  return useSyncExternalStore((fn) => {
    placeSubs.add(fn)
    return () => placeSubs.delete(fn)
  }, currentPlace)
}

// ---- what waits, and what the agent is doing --------------------------------------

/** One attention candidate: a card (by id) or a visual (`visual:<id>`), stamped with the sequence it arrived at. */
interface Stamp {
  kind: string
  state: string
  version: number
  seq: number
}

/** A delegation in flight: its start and its latest step's phrase. */
export interface Delegation {
  id: string
  since: number
  phrase: string | null
}

export interface Follow {
  session: string | null
  /** `seen_seq`: the highest stream sequence Talk has shown. */
  seen: number
  stamps: ReadonlyMap<string, Stamp>
  work: readonly Delegation[]
}

const NOTHING: Follow = { session: null, seen: 0, stamps: new Map(), work: [] }

/** The states a card waits on the owner in (§3.12.4): the dot stays while one is seen and still here. */
const DECIDES: Readonly<Record<string, readonly string[]>> = {
  review: ['waiting'],
  plan: ['proposed'],
  proposal: ['proposed'],
  rescue: ['proposed'],
  details_input: ['waiting'],
  credential_input: ['waiting'],
  budget: ['waiting'],
  stop: ['waiting'],
}

/** Kinds that are an item when drawn, whatever they wait on. */
const DRAWN: readonly string[] = ['mockup', 'diagram']
const GONE: readonly string[] = ['failed', 'cancelled', 'dismissed']

function decides(s: Stamp): boolean {
  return DECIDES[s.kind]?.includes(s.state) === true
}

function attention(s: Stamp): boolean {
  if (s.kind === 'visual') return true
  return decides(s) || (DRAWN.includes(s.kind) && !GONE.includes(s.state))
}

let follow: Follow = NOTHING
const followSubs = new Set<() => void>()

function emit(next: Follow): void {
  follow = next
  for (const fn of [...followSubs]) fn()
}

function forSession(session: string): Follow {
  return follow.session === session ? follow : { ...NOTHING, session }
}

/**
 * The cards as the holder holds them. A card is stamped with the list's
 * sequence when it is first seen in a state (or, for a drawn kind, a
 * version); a card that stays as it was keeps its stamp.
 */
export function noteCards(session: string, list: CardListState): void {
  const f = forSession(session)
  const stamps = new Map<string, Stamp>()
  for (const [k, v] of f.stamps) if (v.kind === 'visual') stamps.set(k, v)
  for (const c of list.cards) {
    const was = f.stamps.get(c.card_id)
    const same = was !== undefined && was.kind === c.kind && was.state === c.state && (was.version === c.version || !DRAWN.includes(c.kind))
    stamps.set(c.card_id, same ? was : { kind: c.kind, state: c.state, version: c.version, seq: list.lastSeq })
  }
  emit({ ...f, stamps })
}

/** A `visual.upsert` off the stream: a new visual, or one drawn again, is an item. */
export function noteVisual(session: string, ev: StreamEvent): void {
  if (ev.type !== 'visual.upsert') return
  const v = ev.data.visual
  const id = typeof v === 'object' && v !== null ? (v as Record<string, unknown>).visual_id : ev.data.visual_id
  if (typeof id !== 'string' || id === '') return
  const f = forSession(session)
  const stamps = new Map(f.stamps)
  stamps.set(`visual:${id}`, { kind: 'visual', state: 'drawn', version: 0, seq: ev.seq })
  emit({ ...f, stamps })
}

/** Talk has shown the stream up to `seq`; it only moves forward. */
export function noteSeen(session: string, seq: number): void {
  const f = forSession(session)
  if (!Number.isInteger(seq) || seq <= f.seen) return
  emit({ ...f, seen: seq })
}

export const WORK_EVENTS = ['work.delegation', 'work.step'] as const

const ENDS: readonly string[] = ['done', 'failed', 'refused']

/** The delegations in flight after one `work.*` event (§3.12.5): a start adds one, an end removes it, a step names it. */
export function applyWork(ds: readonly Delegation[], ev: StreamEvent, now: number): Delegation[] {
  const id = ev.data.delegation_id
  if (typeof id !== 'string' || id === '') return [...ds]
  const parsed = typeof ev.data.at === 'string' ? Date.parse(ev.data.at) : NaN
  const at = Number.isFinite(parsed) ? parsed : now
  const held = ds.find((d) => d.id === id)
  if (ev.type === 'work.delegation') {
    const state = ev.data.state
    if (typeof state === 'string' && ENDS.includes(state)) return ds.filter((d) => d.id !== id)
    return held === undefined ? [...ds, { id, since: at, phrase: null }] : [...ds]
  }
  if (ev.type === 'work.step') {
    const phrase = typeof ev.data.phrase === 'string' && ev.data.phrase.trim() !== '' ? ev.data.phrase.trim() : null
    if (held === undefined) return [...ds, { id, since: at, phrase }]
    return ds.map((d) => (d.id === id ? { ...d, phrase: phrase ?? d.phrase } : d))
  }
  return [...ds]
}

export function noteWork(session: string, ev: StreamEvent, now = Date.now()): void {
  const f = forSession(session)
  emit({ ...f, work: applyWork(f.work, ev, now) })
}

function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')}`
}

/** The working card's one line: the oldest delegation's step and its elapsed time, and how many more run. */
export function workLine(ds: readonly Delegation[], now: number): string | null {
  const first = ds[0]
  if (first === undefined) return null
  const more = ds.length - 1
  return `${first.phrase ?? 'Working'} · ${clock(now - first.since)}${more > 0 ? ` · and ${more} more` : ''}`
}

/** The session's end: nothing waits, nothing runs. */
export function clearFollow(): void {
  if (follow !== NOTHING) emit(NOTHING)
}

function useFollow(): Follow {
  return useSyncExternalStore((fn) => {
    followSubs.add(fn)
    return () => followSubs.delete(fn)
  }, () => follow)
}

export interface Marker {
  /** Items after `seen_seq`. */
  n: number
  /** A seen card still waits on a decision. */
  dot: boolean
  /** The newest unseen item's kind, as the console names it; never its content. */
  kind: string | null
}

export function markerOf(f: Follow): Marker | null {
  let n = 0
  let dot = false
  let newest: Stamp | null = null
  for (const s of f.stamps.values()) {
    if (!attention(s)) continue
    if (s.seq > f.seen) {
      n += 1
      if (newest === null || s.seq > newest.seq) newest = s
    } else if (decides(s)) dot = true
  }
  if (n === 0 && !dot) return null
  const kind = newest === null ? null : newest.kind === 'visual' ? 'Visual' : specOf(newest.kind)?.label ?? null
  return { n, dot, kind }
}

/** The marker as the spine draws it now. */
export function currentMarker(): Marker | null {
  return markerOf(follow)
}

/** The count as drawn: `9+` past nine. */
export function countWord(n: number): string {
  return n > 9 ? '9+' : String(n)
}

function markerWhy(m: Marker): string {
  const parts: string[] = []
  if (m.n > 0) parts.push(`${m.n} new in Voice it${m.kind === null ? '' : `: ${m.kind}`}`)
  if (m.dot) parts.push('a card waits on you')
  return parts.join('; ')
}

/** The spine's Voice it marker: the unseen number, and a dot while a seen card still waits. */
export function VoiceMarker() {
  const m = markerOf(useFollow())
  if (m === null) return null
  const why = markerWhy(m)
  return (
    <>
      {m.n > 0 && (
        <i className="sk-badge vtb-mark" title={why} aria-label={why}>
          {countWord(m.n)}
        </i>
      )}
      {m.dot && <i className={m.n > 0 ? 'sk-dot vtb-wait is-beside' : 'sk-dot vtb-wait'} title={why} aria-label={m.n > 0 ? undefined : why} />}
    </>
  )
}

// ---- the layer the shell draws ---------------------------------------------------

export const VOICE_SEEN = '/v1/voice/sessions/{id}/seen'
const SEEN_EVERY_MS = 1_000

/** The holder, or null where the shell is drawn without one (a screen's own test). */
function useHeld(): VoiceSession | null {
  try {
    return useVoiceSession()
  } catch {
    return null
  }
}

/**
 * Drawn by the shell on every screen. On Talk it draws nothing and posts what
 * Talk has shown; off Talk it draws the floating view while the session is
 * live. On every screen it keeps the stamps, the seen sequence and the
 * delegations in flight, so the spine's marker is right wherever the owner is.
 */
export function VoiceFloatLayer({ onTalk, go }: { onTalk: boolean; go: (to: string) => void }) {
  const held = useHeld()
  const phase = held?.phase ?? 'idle'
  const streamId = held?.streamId ?? null
  const cards = held?.cards ?? null
  const feed = held?.feed ?? null

  useEffect(() => {
    if (phase === 'idle') clearFollow()
  }, [phase])

  useEffect(() => {
    if (streamId !== null && cards !== null) noteCards(streamId, cards)
  }, [streamId, cards])

  useEffect(() => {
    if (streamId === null || feed === null) return
    return feed.listen((ev) => noteVisual(streamId, ev))
  }, [streamId, feed])

  // The delegations' events, on the session's one shared stream.
  const lastSeq = useRef(0)
  lastSeq.current = cards?.lastSeq ?? 0
  useEffect(() => {
    if (streamId === null) return
    return readSessionStream(streamId, lastSeq.current, { types: WORK_EVENTS, onEvent: (ev) => noteWork(streamId, ev) })
  }, [streamId])

  // Seen: what Talk has shown, while it is on screen and the tab is visible, at most once a second.
  const shown = cards?.lastSeq ?? 0
  const posted = useRef(0)
  const [visible, setVisible] = useState(() => typeof document === 'undefined' || document.visibilityState !== 'hidden')
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', on)
    return () => document.removeEventListener('visibilitychange', on)
  }, [])
  useEffect(() => {
    if (!onTalk || !visible || streamId === null || shown <= 0) return
    const id = streamId
    const send = () => {
      posted.current = Date.now()
      noteSeen(id, shown)
      if (USE_FIXTURES) return
      void write(route(VOICE_SEEN, { id }), 'POST', { seq: shown }).then((r) => {
        if (r.status !== 'ok') return
        const back = (r.data as Record<string, unknown> | null)?.seen_seq
        if (typeof back === 'number') noteSeen(id, back)
      })
    }
    const wait = posted.current + SEEN_EVERY_MS - Date.now()
    if (wait <= 0) {
      send()
      return
    }
    const t = setTimeout(send, wait)
    return () => clearTimeout(t)
  }, [onTalk, visible, streamId, shown])

  if (held === null || onTalk || phase !== 'live') return null
  return <FloatingView held={held} go={go} />
}

// ---- the floating view ------------------------------------------------------------

const C = 50
const POINTS = 72

/** A closed path around the centre: the Ring's radial trace, small. */
function radial(t: number, base: number, amp: number): string {
  let d = ''
  for (let i = 0; i < POINTS; i++) {
    const a = (i / POINTS) * Math.PI * 2 - Math.PI / 2
    const env = 0.55 + 0.45 * Math.sin(t * 3.1) * Math.sin(t * 1.7 + 1)
    const v = Math.abs(Math.sin(a * 5 + t * 6) * 0.6 + Math.sin(a * 9 - t * 9) * 0.3 + Math.sin(a * 3 + t * 2) * 0.25) * env
    const r = base + amp * Math.min(1, v)
    d += `${i === 0 ? 'M' : 'L'}${(C + r * Math.cos(a)).toFixed(1)},${(C + r * Math.sin(a)).toFixed(1)} `
  }
  return `${d}Z`
}

function still(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** The voice, small: the Ring's base circle, its halo, and its trace while the agent speaks. */
function Voice({ speaking, muted }: { speaking: boolean; muted: boolean }) {
  const gid = useId().replace(/:/g, '')
  const wave = useRef<SVGPathElement | null>(null)
  const glow = useRef<SVGPathElement | null>(null)
  useEffect(() => {
    const raf = typeof window.requestAnimationFrame === 'function' ? window.requestAnimationFrame.bind(window) : null
    if (!speaking || still() || raf === null) {
      wave.current?.setAttribute('d', '')
      glow.current?.setAttribute('d', '')
      return
    }
    let frame = 0
    const t0 = performance.now()
    const draw = (now: number) => {
      const d = radial((now - t0) / 1000, 27, 17)
      wave.current?.setAttribute('d', d)
      glow.current?.setAttribute('d', d)
      frame = raf(draw)
    }
    frame = raf(draw)
    return () => window.cancelAnimationFrame(frame)
  }, [speaking])
  return (
    <svg className={`vtb-voice is-${muted ? 'muted' : speaking ? 'speaking' : 'listening'}`} viewBox="0 0 100 100" aria-hidden="true">
      <defs>
        <radialGradient id={`h${gid}`} cx="0.5" cy="0.5" r="0.5">
          <stop offset="0.4" stopColor="currentColor" stopOpacity="0" />
          <stop offset="0.62" stopColor="currentColor" stopOpacity="0.22" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </radialGradient>
        <filter id={`g${gid}`} x="-30%" y="-30%" width="160%" height="160%">
          <feGaussianBlur stdDeviation="2.4" />
        </filter>
      </defs>
      <circle className="vtb-halo" cx={C} cy={C} r={48} fill={`url(#h${gid})`} />
      <circle className="vtb-base" cx={C} cy={C} r={30} />
      <circle className="vtb-still" cx={C} cy={C} r={30} />
      <path ref={glow} className="vtb-glow" d="" filter={`url(#g${gid})`} />
      <path ref={wave} className="vtb-wave" d="" />
    </svg>
  )
}

function MicGlyph({ off }: { off: boolean }) {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      {off && <path d="M4 4l16 16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />}
    </svg>
  )
}

function FoldGlyph({ open }: { open: boolean }) {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <path d={open ? 'M6 12h12' : 'M6 12h12M12 6v12'} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

/** The floating view itself: drawn by `VoiceFloatLayer` off Talk while the session is live. */
export function FloatingView({ held, go }: { held: VoiceSession; go: (to: string) => void }) {
  const { mic, setMuted, setMode, hold, end, voicePlaying, lines, level, talkMode } = held
  const { corner, collapsed } = usePlace()
  const f = useFollow()
  const marker = markerOf(f)
  const [now, setNow] = useState(() => Date.now())
  const [drag, setDrag] = useState<{ x: number; y: number } | null>(null)
  const start = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const box = useRef<HTMLDivElement | null>(null)
  const working = f.work.length > 0

  useEffect(() => {
    if (!working) return
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(t)
  }, [working])

  // THE PAGE ENDS WHERE THE BUBBLE BEGINS: the page's scroller gives up a band
  // on the bubble's side (`--vtb-band`), so it never covers a row or an
  // action; inside the scroller `--dock-h` grows by the band too, so the
  // columns and drawers that end above the dock end above the bubble.
  useEffect(() => {
    const el = box.current
    const main = el?.closest('.sk-main')
    const scroller = main?.querySelector<HTMLElement>(':scope > .ctl-scroll')
    if (el == null || main == null || scroller == null) return
    const root = document.documentElement
    const top = corner === 'tr' || corner === 'tl'
    const publish = () => {
      const r = el.getBoundingClientRect()
      const m = main.getBoundingClientRect()
      const dock = parseFloat(root.style.getPropertyValue('--dock-h')) || 28
      const band = Math.max(0, Math.ceil(top ? r.bottom - m.top + 12 : m.bottom - dock - r.top + 12))
      // Set only on a change: the root's style is observed, and an equal write is still a mutation.
      const v = `${band}px`
      if (root.style.getPropertyValue('--vtb-band') !== v) root.style.setProperty('--vtb-band', v)
      if (top) scroller.style.removeProperty('--dock-h')
      else scroller.style.setProperty('--dock-h', `${dock + band}px`)
    }
    publish()
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(publish) : null
    ro?.observe(el)
    const mo = new MutationObserver(publish)
    mo.observe(root, { attributes: true, attributeFilter: ['style'] })
    window.addEventListener('resize', publish)
    return () => {
      ro?.disconnect()
      mo.disconnect()
      window.removeEventListener('resize', publish)
      root.style.removeProperty('--vtb-band')
      scroller.style.removeProperty('--dock-h')
    }
  }, [corner, collapsed])

  // A LEFT CORNER STARTS WHERE THE PAGE DOES: `--vtb-left` is the page's
  // (`.sk-main`) left edge, so the bubble sits clear of the spine and of the
  // side panel, open or folded, never over its switcher or its theme button.
  useEffect(() => {
    const el = box.current
    const main = el?.closest('.sk-main')
    if (el == null || main == null) return
    const publish = () => {
      const v = `${Math.max(0, Math.round(main.getBoundingClientRect().left))}px`
      if (el.style.getPropertyValue('--vtb-left') !== v) el.style.setProperty('--vtb-left', v)
    }
    publish()
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(publish) : null
    ro?.observe(main)
    window.addEventListener('resize', publish)
    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', publish)
    }
  }, [])

  const textMode = talkMode === 'text'
  const speaking = textMode ? level !== null : voicePlaying || lines.some((l) => l.side === 'agent' && !l.final)
  const stateWord = mic.muted ? 'muted' : speaking ? 'speaking' : 'listening'
  const line = workLine(f.work, now)
  const ptt = mic.mode === 'ptt'

  // Space and M answer only here, never in a field of the view's own.
  const onKey = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      void end()
      return
    }
    if (e.ctrlKey || e.metaKey || e.altKey || typingIn(e.target)) return
    if (e.code === 'Space' || e.key === ' ') {
      if (!ptt) return
      e.preventDefault()
      if (!mic.held) hold(true)
    } else if ((e.key === 'm' || e.key === 'M') && !e.repeat) {
      e.preventDefault()
      setMuted(!mic.muted)
    }
  }
  // On push-to-talk a released Space is the hold's end, never a press of the button that has focus (Stop).
  const onKeyUp = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!(e.code === 'Space' || e.key === ' ') || !ptt || typingIn(e.target)) return
    e.preventDefault()
    if (mic.held) hold(false)
  }

  const down = (e: ReactPointerEvent<HTMLButtonElement>) => {
    start.current = { x: e.clientX, y: e.clientY, moved: false }
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }
  const move = (e: ReactPointerEvent<HTMLButtonElement>) => {
    const st = start.current
    if (st === null) return
    if (!st.moved && Math.hypot(e.clientX - st.x, e.clientY - st.y) < 6) return
    st.moved = true
    setDrag({ x: e.clientX, y: e.clientY })
  }
  const up = (e: ReactPointerEvent<HTMLButtonElement>) => {
    const st = start.current
    start.current = null
    if (st === null) return
    if (!st.moved) {
      setPlace({ collapsed: !collapsed })
      return
    }
    const right = e.clientX > window.innerWidth / 2
    const bottom = e.clientY > window.innerHeight / 2
    setPlace({ corner: `${bottom ? 'b' : 't'}${right ? 'r' : 'l'}` as Corner })
    setDrag(null)
  }

  const label = `Voice it, ${stateWord}${marker !== null && marker.n > 0 ? `, ${marker.n} new` : ''}`
  return (
    <div
      ref={box}
      className={`vtb is-${corner}${drag !== null ? ' is-dragging' : ''}${collapsed ? ' is-collapsed' : ''}`}
      role="region"
      aria-label="Voice it"
      onKeyDown={onKey}
      onKeyUp={onKeyUp}
    >
      <div className="vtb-row">
        {!collapsed && (
          <div className="vtb-pill">
            {line !== null && (
              <span className="vtb-pill-line" role="status">
                <i className="vtb-live" aria-hidden="true" />
                <span className="vtb-pill-t">{line}</span>
              </span>
            )}
            {marker !== null && marker.n > 0 && marker.kind !== null && (
              <span className="vtb-pill-who" role="status">
                New: {marker.kind}
              </span>
            )}
            <span className="vtb-acts">
              <Button kind="secondary" size="sm" aria-pressed={ptt} title="Push-to-talk" onClick={() => setMode(ptt ? 'open' : 'ptt')}>
                {ptt ? 'Open mic' : 'Push-to-talk'}
              </Button>
              {ptt && (
                <Button
                  kind="secondary"
                  size="sm"
                  aria-pressed={mic.held}
                  onPointerDown={() => hold(true)}
                  onPointerUp={() => hold(false)}
                  onPointerLeave={() => hold(false)}
                >
                  Hold to talk
                </Button>
              )}
              <Button kind="secondary" size="sm" onClick={() => go('voice/talk')}>
                Open Voice it
              </Button>
              <Button kind="danger" size="sm" title="Stop the session (Escape)" onClick={() => void end()}>
                Stop
              </Button>
            </span>
          </div>
        )}
        <div className="vtb-orb" style={drag !== null ? { position: 'fixed', left: drag.x - 32, top: drag.y - 32 } : undefined}>
          <button
            type="button"
            className={`vtb-core is-${stateWord}`}
            aria-expanded={!collapsed}
            aria-label={label}
            title="Voice it: press to fold, drag to move"
            onPointerDown={down}
            onPointerMove={move}
            onPointerUp={up}
            onPointerCancel={() => {
              start.current = null
              setDrag(null)
            }}
            onClick={(e) => {
              // A keyboard press folds; a pointer press already did on its way up.
              if (e.detail === 0) setPlace({ collapsed: !collapsed })
            }}
          >
            <Voice speaking={speaking && !mic.muted} muted={mic.muted} />
          </button>
          <button
            type="button"
            className={`vtb-mic${mic.muted ? ' is-off' : ''}`}
            aria-pressed={mic.muted}
            aria-label={mic.muted ? 'Unmute' : 'Mute'}
            title={mic.muted ? 'Unmute (M)' : 'Mute (M)'}
            onClick={() => setMuted(!mic.muted)}
          >
            <MicGlyph off={mic.muted} />
          </button>
          <span className="vtb-fold" aria-hidden="true">
            <FoldGlyph open={!collapsed} />
          </span>
          {marker !== null && marker.n > 0 && (
            <span className="vtb-count" aria-hidden="true">
              {countWord(marker.n)}
            </span>
          )}
          {marker !== null && marker.dot && marker.n === 0 && <span className="vtb-count is-dot" aria-hidden="true" />}
        </div>
      </div>
    </div>
  )
}
