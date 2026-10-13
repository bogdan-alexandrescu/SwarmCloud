/**
 * THE NOTIFICATION FEED, READ ONCE FOR THE WHOLE FRAME (#1206, proposal A).
 *
 * Two places draw it: the bell in the header bar (Notifications.tsx) and the
 * Admin rail item's pill (Spine.tsx, `admin_pending`). They read ONE store
 * rather than one poll each, so the pill and the bell never disagree about a
 * request one of them has seen and the other has not, and the API is asked
 * once per tick rather than twice.
 *
 * The store polls while anything is subscribed, on the frame's own cadence
 * (`NOTIFICATIONS_POLL_MS`, the 30 s the capacity meter and the panel counts
 * use), again the moment the window regains focus -- the case the issue is
 * about: a request that sat unseen while the admin was in another tab -- and
 * again when the tenant is switched. A hidden tab skips the timed reads, as
 * the frame's other reads do.
 *
 * A mark is OPTIMISTIC: the item reads as read and the counts drop at once,
 * then the post goes and the feed is read again. Any read that was already in
 * flight when the mark was made is discarded on landing (`seq`), so an answer
 * from before the mark cannot put the count back.
 */
import { loadNotifications, markAllNotificationsRead, markNotificationsRead, type NotificationFeed, type NotificationItem } from './api'
import { subscribeTenant, type Result } from './fetch'

/** The frame's read cadence (Spine.tsx reads capacity and stats every 30 s). */
export const NOTIFICATIONS_POLL_MS = 30_000

let state: Result<NotificationFeed> = { status: 'loading', since: Date.now() }
const listeners = new Set<() => void>()
let seq = 0
let timer: ReturnType<typeof setInterval> | undefined
let stopTenant: (() => void) | undefined

function emit(): void {
  for (const l of listeners) l()
}

function set(next: Result<NotificationFeed>): void {
  state = next
  emit()
}

/** The feed's data, whether fresh or kept through a failed re-read. */
export function feedData(r: Result<NotificationFeed>): NotificationFeed | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

/** Read the feed now. An answer older than a later read or mark is dropped. */
export async function refreshNotifications(): Promise<void> {
  const mine = ++seq
  const r = await loadNotifications()
  if (mine !== seq) return
  // A RE-READ THAT FAILS KEEPS WHAT WAS READ, marked stale (fetch.ts
  // `Result`): a blip must not empty the bell, and must not pretend to be
  // fresh either. A 404 is the exception: the route is gone, so is the bell.
  const had = feedData(state)
  if (r.status === 'error' && had !== null && r.error.kind !== 'not_found') {
    set({ status: 'stale', data: had, fetchedAt: state.status === 'ok' || state.status === 'stale' ? state.fetchedAt : Date.now(), error: r.error })
    return
  }
  set(r)
}

function onFocus(): void {
  void refreshNotifications()
}

function start(): void {
  void refreshNotifications()
  timer = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return
    void refreshNotifications()
  }, NOTIFICATIONS_POLL_MS)
  if (typeof window !== 'undefined') window.addEventListener('focus', onFocus)
  stopTenant = subscribeTenant(() => {
    set({ status: 'loading', since: Date.now() })
    void refreshNotifications()
  })
}

function stop(): void {
  if (timer !== undefined) clearInterval(timer)
  timer = undefined
  if (typeof window !== 'undefined') window.removeEventListener('focus', onFocus)
  stopTenant?.()
  stopTenant = undefined
  // Nothing is drawing it: the next subscriber reads afresh rather than
  // showing an answer from whenever the last one left.
  seq += 1
  state = { status: 'loading', since: Date.now() }
}

export function subscribeNotifications(listener: () => void): () => void {
  listeners.add(listener)
  if (listeners.size === 1) start()
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) stop()
  }
}

export function notificationsSnapshot(): Result<NotificationFeed> {
  return state
}

/** The feed with `ids` (every item, for null) read, and the counts to match. */
export function withRead(feed: NotificationFeed, ids: readonly string[] | null): NotificationFeed {
  const hit = (i: NotificationItem) => ids === null || ids.includes(i.id)
  const items = feed.items.map((i) => (!i.read && hit(i) ? { ...i, read: true } : i))
  const cleared = (needs: boolean) => feed.items.filter((i) => i.needs_you === needs && !i.read && hit(i)).length
  return {
    ...feed,
    items,
    unread_needs_you: Math.max(0, feed.unread_needs_you - cleared(true)),
    unread_updates: Math.max(0, feed.unread_updates - cleared(false)),
  }
}

function markLocally(ids: readonly string[] | null): void {
  const had = feedData(state)
  if (had === null) return
  seq += 1
  set(state.status === 'stale' ? { ...state, data: withRead(had, ids) } : { status: 'ok', data: withRead(had, ids), fetchedAt: Date.now() })
}

/** Mark items read: at once on screen, then on the server, then read again. */
export async function markRead(ids: readonly string[]): Promise<void> {
  if (ids.length === 0) return
  markLocally(ids)
  await markNotificationsRead(ids)
  await refreshNotifications()
}

/** Mark every item read. */
export async function markAllRead(): Promise<void> {
  markLocally(null)
  await markAllNotificationsRead()
  await refreshNotifications()
}
