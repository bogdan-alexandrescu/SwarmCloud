// MOCK-HOOK FILE
/**
 * VOICE IT TAG-ALONG, CORNER BUBBLE (mock-up; the owner's ask of 11 November
 * 2026). The session that follows the person across the console, drawn from
 * invented fixture data: no server side exists. On only at
 * `?voicetagalong=bubble`, kept for the tab in sessionStorage so a navigation
 * keeps it; `?vtscene=` picks the state a frame opens on.
 */

import { useSyncExternalStore } from 'react'

import { addressToPath } from '../paths'

export type Corner = 'br' | 'bl' | 'tr' | 'tl'
export type Scene = 'talk' | 'rest' | 'speaking' | 'attention' | 'offer' | 'open' | 'muted'

export interface Progress {
  id: string
  at: string
  text: string
  /** Which workflow moved, for the line's lead word. */
  who: string
}

export interface ReviewItem {
  id: string
  kind: 'Mock-up' | 'Plan' | 'Card'
  title: string
  at: string
  seen: boolean
}

export interface Offer {
  id: string
  /** What was asked, in the person's words. */
  asked: string
  /** The page it opens, in the console's own name. */
  page: string
  to: string
  /** The progress line that slides out on the page Yes opens. */
  next: Progress
}

export interface TagAlong {
  on: boolean
  project: string
  speaking: boolean
  muted: boolean
  /** The agent's sentence while it speaks. */
  saying: string | null
  progress: Progress[]
  /** The progress line slid out beside the bubble, null when folded back. */
  peek: Progress | null
  review: ReviewItem[]
  /** The new item announced in a card beside the bubble; once it folds back the count and the spine's marker keep it. */
  notice: string | null
  offer: Offer | null
  open: boolean
  corner: Corner
  ended: boolean
}

const KEY = 'swarm.voicetagalong'

function readOn(): { on: boolean; scene: Scene } {
  if (typeof window === 'undefined') return { on: false, scene: 'rest' }
  const q = new URLSearchParams(window.location.search)
  const scenes: Scene[] = ['talk', 'rest', 'speaking', 'attention', 'offer', 'open', 'muted']
  const asked = q.get('vtscene') as Scene | null
  const scene: Scene = asked !== null && scenes.includes(asked) ? asked : 'rest'
  try {
    if (q.get('voicetagalong') === 'bubble') window.sessionStorage.setItem(KEY, 'bubble')
    return { on: window.sessionStorage.getItem(KEY) === 'bubble', scene }
  } catch {
    return { on: q.get('voicetagalong') === 'bubble', scene }
  }
}

const LATEST: Progress = { id: 'p1', at: '14:02', who: 'Site build', text: 'Menu page built, 2 of 5 pages done' }

const PROGRESS: Progress[] = [
  LATEST,
  { id: 'p2', at: '13:56', who: 'Order form', text: 'Pickup slots wired to the form' },
  { id: 'p3', at: '13:48', who: 'Site build', text: 'Homepage mock-up drawn' },
  { id: 'p4', at: '13:41', who: 'Order form', text: 'Started on the order form' },
]

export const AGENTS_OFFER: Offer = { id: 'of-agents', asked: 'What is the state of our running agents?', page: 'Agents', to: 'work/running', next: LATEST }
export const WORKFLOWS_OFFER: Offer = {
  id: 'of-workflows',
  asked: 'Take me to Workflows',
  page: 'Workflows',
  to: 'work/workflows',
  next: { id: 'p0', at: '14:09', who: 'Order form', text: 'Pickup slots checked on a phone' },
}

const REVIEW: ReviewItem[] = [
  { id: 'r1', kind: 'Mock-up', title: 'Homepage, warm palette', at: '14:03', seen: false },
  { id: 'r2', kind: 'Plan', title: 'Opening-week launch plan', at: '13:30', seen: true },
]

function initial(): TagAlong {
  const { on, scene } = readOn()
  const base: TagAlong = {
    on,
    project: 'Corner Loaf bakery website',
    speaking: false,
    muted: false,
    saying: null,
    progress: PROGRESS,
    peek: null,
    review: REVIEW.map((r) => ({ ...r, seen: true })),
    notice: null,
    offer: null,
    open: false,
    corner: 'br',
    ended: false,
  }
  switch (scene) {
    case 'talk':
      return { ...base, offer: AGENTS_OFFER, saying: 'Two workflows are running on the bakery site, both on track. Want me to open Agents?' }
    case 'rest':
      return { ...base, peek: PROGRESS[0] ?? null }
    case 'speaking':
      return { ...base, speaking: true, saying: 'The menu page is up. Two of five pages are done; the order form is wiring pickup slots.' }
    case 'attention':
      return { ...base, review: REVIEW, notice: 'r1' }
    // From here on the homepage mock-up has been announced and not yet
    // reviewed: the count and the spine's marker keep it until it is opened.
    case 'offer':
      return { ...base, review: REVIEW, offer: WORKFLOWS_OFFER, saying: 'Sure. Open Workflows?' }
    case 'open':
      return { ...base, open: true, review: REVIEW, offer: WORKFLOWS_OFFER, progress: [WORKFLOWS_OFFER.next, ...PROGRESS] }
    case 'muted':
      return { ...base, review: REVIEW, muted: true }
  }
}

let state: TagAlong = initial()
const subs = new Set<() => void>()

function emit(next: TagAlong): void {
  state = next
  for (const s of subs) s()
}

export function useTagAlong(): TagAlong {
  return useSyncExternalStore(
    (fn) => {
      subs.add(fn)
      return () => subs.delete(fn)
    },
    () => state,
  )
}

/** The console's navigation, out of the router as Talk's own (openTrace). */
export function goTo(to: string): void {
  window.history.pushState(null, '', addressToPath(to))
  window.dispatchEvent(new PopStateEvent('popstate'))
}

let peekTimer: ReturnType<typeof setTimeout> | undefined

export const tagAlong = {
  setMuted(muted: boolean) {
    emit({ ...state, muted })
  },
  setOpen(open: boolean) {
    emit({ ...state, open })
  },
  setCorner(corner: Corner) {
    emit({ ...state, corner })
  },
  foldPeek() {
    emit({ ...state, peek: null })
  },
  /** The new item's card folds back; the count and the marker stay. */
  foldNotice() {
    emit({ ...state, notice: null })
  },
  /** A progress line slides out, then folds back. */
  push(p: Progress, holdMs = 6000) {
    emit({ ...state, progress: [p, ...state.progress], peek: p })
    if (peekTimer !== undefined) clearTimeout(peekTimer)
    peekTimer = setTimeout(() => emit({ ...state, peek: null }), holdMs)
  },
  /** Yes, clicked or spoken: the console goes there, the session follows. */
  accept() {
    const o = state.offer
    if (o === null) return
    const next = state.progress.some((p) => p.id === o.next.id) ? state.progress : [o.next, ...state.progress]
    emit({ ...state, offer: null, saying: null, speaking: false, open: false, progress: next, peek: o.next })
    goTo(o.to)
  },
  decline() {
    emit({ ...state, offer: null, saying: null, speaking: false })
  },
  /** Opens Voice it's main view at the item; the item is seen. */
  review(id: string) {
    emit({ ...state, open: false, notice: state.notice === id ? null : state.notice, review: state.review.map((r) => (r.id === id ? { ...r, seen: true } : r)) })
    goTo('voice/talk')
  },
  end() {
    emit({ ...state, ended: true, open: false, offer: null, peek: null, notice: null, speaking: false })
  },
}

export function waiting(s: TagAlong): number {
  return s.review.filter((r) => !r.seen).length
}
