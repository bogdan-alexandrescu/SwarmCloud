import type { Result } from './fetch'
import type { Me, Profile } from './types'

/**
 * THE PERSON'S TIME ZONE (VQ-023, owner decision 2026-10-26, "A per-person
 * setting"; lane TIMEZONE-SETTING, part of #2391). Each person picks ONE zone
 * in Profile, and the console shows every time in it, labelled with the zone.
 * UTC until they pick one.
 *
 * Before this, Overview printed UTC and counted "today" from 00:00 UTC,
 * Outcomes named America/Los_Angeles, and Lanes drew the browser's local time
 * unlabelled -- three clocks, so "today" meant different things on two pages.
 * Every page that names a time of day reads it from here: `useTimeZone`
 * (`useTimeZone.ts`, the React side) for
 * the zone, `formatClock`/`formatDateTime` for the words, `startOfDay` for
 * where "today" begins.
 *
 * WHERE IT IS KEPT: the person's server-side preferences (`time_zone` on
 * `GET /v1/profile`, saved through `PUT /v1/profile/preferences`), so it
 * follows them to every browser and survives sign-out (BGD, 2026-10-26, lane
 * TIMEZONE-SERVER). This browser's local storage, keyed by the signed-in
 * person's email (`swarm.timezone:<email>`), is only a first-paint cache: the
 * page draws in it until the profile read answers, then in the server's
 * zone. Because the key names the person, it is per-user state:
 * `signoutState.ts` clears it on sign-out, like the recent-workflows lists.
 *
 * ONE MOVE, ONCE: before TIMEZONE-SERVER the zone lived only in this cache. A
 * profile whose zone is the default (`time_zone_source: "default"`, nothing
 * saved) while this browser holds one for the person gets that zone written
 * to the server, so nobody loses their pick. Once it is saved the source is
 * `profile` and the server's zone wins from then on.
 *
 * WHO IS SIGNED IN, and their zone, is learned from `GET /v1/profile`: every
 * answered read `api.ts` makes tells this module (`noteProfile`), so Profile's
 * own read is the one, and a page that made none reads it once
 * (`learnPerson`). When that read fails, or answers without a zone (its
 * preferences unread, or an API older than the field), the page keeps the
 * cache and, as before, learns the person from `GET /v1/tenants/me` when a
 * zone is cached at all. Profile tells the person directly (`notePerson`).
 * Until a read answers, the one zone stored here is used: sign-out clears
 * the others, so it is the last person's, and almost always this one's.
 *
 * A SAVE replaces the whole preference set at the revision read (the API's
 * rule), so `setZone` reads the profile fresh and puts it back with the new
 * zone, once more after a 409. Saves run one after another, so the last pick
 * is the one stored. A failed save keeps the pick in this browser. After a
 * pick, no read on this page moves the zone: the pick is the person's latest
 * word, and a read that began before it would undo it.
 *
 * Every storage access is inside try/catch: storage throws in a private
 * window, and a refused write keeps the pick for this page.
 */
export const ZONE_KEY = 'swarm.timezone'

/** The zone everyone starts in, and the one a failed or unknown pick falls back to. */
export const UTC = 'UTC'

/** The canonical name of `zone`, or null when this browser does not know it. */
export function canonicalZone(zone: string): string | null {
  if (zone.trim() === '') return null
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: zone.trim() }).resolvedOptions().timeZone
  } catch {
    return null
  }
}

/** The browser's own zone, or UTC where the browser does not say. */
export function browserZone(): string {
  try {
    const z = Intl.DateTimeFormat().resolvedOptions().timeZone
    return typeof z === 'string' && z !== '' ? z : UTC
  } catch {
    return UTC
  }
}

/**
 * Every IANA zone this browser knows, UTC first, then alphabetical. An
 * engine without `Intl.supportedValuesOf` offers UTC and its own zone; the
 * list is still searchable, and any other name typed is checked on save.
 */
export function allZones(): string[] {
  const listed = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf
  let zones: string[] = []
  try {
    zones = typeof listed === 'function' ? listed('timeZone') : []
  } catch {
    zones = []
  }
  const rest = [...new Set([...zones, browserZone()])].filter((z) => z !== UTC).sort()
  return [UTC, ...rest]
}

// ---------------------------------------------------------------------------
// The store: one zone per page, shared by every component that reads it.
// ---------------------------------------------------------------------------

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

function keyFor(email: string): string {
  return `${ZONE_KEY}:${email.trim().toLowerCase()}`
}

/** Every zone this browser holds, by key. */
function storedKeys(): string[] {
  const s = storage()
  if (s === null) return []
  try {
    const keys: string[] = []
    for (let i = 0; i < s.length; i += 1) {
      const k = s.key(i)
      if (k !== null && k.startsWith(`${ZONE_KEY}:`)) keys.push(k)
    }
    return keys
  } catch {
    return []
  }
}

function readKey(key: string): string {
  try {
    const v = storage()?.getItem(key) ?? null
    return v === null ? UTC : (canonicalZone(v) ?? UTC)
  } catch {
    return UTC
  }
}

/** Before the person is known: the one stored zone, else UTC. */
function guessZone(): string {
  const keys = storedKeys()
  return keys.length === 1 ? readKey(keys[0]!) : UTC
}

let person: string | null = null
let zone: string = guessZone()
let asked = false
/** Counts picks: after one, no profile read moves the zone on this page. */
let picks = 0
/** A profile read has named the person on this page, so `learnPerson` need not read. */
let seen = false
/** The one move of a cached zone to the server has been tried on this page. */
let moved = false
/** The saves in flight, one after another. */
let saving: Promise<boolean> = Promise.resolve(true)
const listeners = new Set<() => void>()

function publish(next: string): void {
  if (next === zone) return
  zone = next
  for (const fn of listeners) fn()
}

/** Call `fn` whenever the page's zone changes; returns the unsubscribe. */
export function subscribeZone(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/** The zone every time on the page is shown in. */
export function currentZone(): string {
  return zone
}

/** Who is signed in: their cached zone becomes the page's until the profile read answers. */
export function notePerson(email: string): void {
  if (email.trim() === '') return
  if (person === email) return
  person = email
  publish(readKey(keyFor(email)))
}

/** Keep `z` as this person's first-paint cache. UTC removes the key: UTC is what nothing stored means. */
function cache(email: string, z: string): void {
  try {
    const s = storage()
    if (z === UTC) s?.removeItem(keyFor(email))
    else s?.setItem(keyFor(email), z)
  } catch {
    /* kept in memory for this page only */
  }
}

/** What `api.ts` lends this module, loaded only when a read or a save is made (see `learnPerson`). */
type ProfileApi = Pick<typeof import('./api'), 'loadProfile' | 'putPreferences'>

function profileApi(): Promise<ProfileApi> {
  return import('./api')
}

const answered = (r: Result<Profile>): r is Extract<Result<Profile>, { status: 'ok' | 'stale' }> =>
  r.status === 'ok' || r.status === 'stale'

/** Whether the read says what the server holds: a zone, or that none is saved. */
const servesZone = (p: Profile) => p.time_zone_source === 'profile' || p.time_zone_source === 'default'

/**
 * Put `z` in the server's preferences: the whole set as just read, with the
 * new zone. False, and nothing stored, when the preferences cannot be read
 * or saved; once more on a 409 (a save elsewhere since the read).
 */
async function putZone(z: string, onlyIfUnsaved: boolean, tries = 2): Promise<boolean> {
  const api = await profileApi()
  const r = await api.loadProfile()
  if (!answered(r)) return false
  const p = r.data
  if (p.revision === null || p.notifications === undefined || !servesZone(p)) return false
  if (onlyIfUnsaved && p.time_zone_source !== 'default') return true
  if (p.time_zone_source === 'profile' && p.time_zone === z) return true
  // The move is not the person's act: never let it clear a stored default
  // workspace the API is keeping while it cannot honour it (`ignored`).
  if (onlyIfUnsaved && p.default_workspace.ignored !== null) return false
  const w = await api.putPreferences({
    display_name: p.identity.display_name,
    default_workspace: p.default_workspace.source === 'profile' ? p.default_workspace.tenant_id : null,
    notifications: p.notifications.preferences,
    revision: p.revision,
    time_zone: z,
  })
  if (w.status === 'ok' || w.status === 'stale') return true
  if (w.status === 'error' && w.error.httpStatus === 409 && tries > 1) return putZone(z, onlyIfUnsaved, tries - 1)
  return false
}

function saveZone(z: string, onlyIfUnsaved = false): Promise<boolean> {
  const run = () => putZone(z, onlyIfUnsaved).catch(() => false)
  saving = saving.then(run, run)
  return saving
}

/** The last save's outcome, once every save in flight has finished. For tests. */
export function zoneSaved(): Promise<boolean> {
  return saving
}

/** Take the person and their zone from a profile read; false when it names nobody. */
function adoptProfile(p: Profile): boolean {
  const email = p.identity.email
  if (email.trim() === '') return false
  seen = true
  notePerson(email)
  if (picks > 0 || !servesZone(p)) return true
  if (p.time_zone_source === 'profile') {
    const z = canonicalZone(p.time_zone ?? '') ?? UTC
    cache(email, z)
    publish(z)
    return true
  }
  // Nothing saved on the server: a zone this browser kept moves there, once.
  const kept = readKey(keyFor(email))
  publish(kept)
  if (kept !== UTC && !moved) {
    moved = true
    void saveZone(kept, true)
  }
  return true
}

/** What every answered `GET /v1/profile` tells the page: who is signed in, and their zone. `api.ts` calls it. */
export function noteProfile(p: Profile): void {
  adoptProfile(p)
}

/**
 * Pick `next` for the signed-in person. Returns the canonical zone, or null
 * when the zone is not one this browser knows (nothing changes). The page
 * shows it at once and caches it here; the server's preferences are saved
 * after (`zoneSaved` says how that went).
 */
export function setZone(next: string): string | null {
  const canonical = canonicalZone(next)
  if (canonical === null) return null
  picks += 1
  if (person !== null) {
    cache(person, canonical)
    void saveZone(canonical)
  }
  publish(canonical)
  return canonical
}

/**
 * Learn who is signed in and their zone, once per page: `GET /v1/profile`,
 * else (as before the server held the zone) `load` when a zone is cached.
 * `load` is `loadMe` from `api.ts`, passed in by `useTimeZone.ts`. The API is
 * imported only when the read is made, never at load: `overviewReads.ts` --
 * and through it the pure check layer that `node --test` loads
 * (test/checks.test.mjs) -- reads the zone from here, and this module stays
 * free of React and of the API's own imports.
 */
export function learnPerson(load: (options: { frame?: boolean }) => Promise<Result<Me>>): void {
  if (asked) return
  asked = true
  if (seen) return
  // Inside a promise, so a read that throws before it returns leaves the guess.
  void Promise.resolve()
    .then(() => profileApi())
    .then((api) => api.loadProfile())
    .then(
      // `loadProfile` has told `noteProfile` already; a mocked one has not.
      (r) => answered(r) && adoptProfile(r.data),
      () => false,
    )
    .then((adopted) => {
      if (adopted || person !== null || storedKeys().length === 0) return undefined
      return load({ frame: true }).then((r) => {
        if (person !== null || (r.status !== 'ok' && r.status !== 'stale')) return
        const email = r.data?.principal?.email
        if (typeof email === 'string') notePerson(email)
      })
    })
    .catch(() => undefined)
}

/** Forget the person and read storage again, as a fresh page would. For tests. */
export function resetTimeZone(): void {
  person = null
  asked = false
  picks = 0
  seen = false
  moved = false
  saving = Promise.resolve(true)
  zone = guessZone()
  for (const fn of listeners) fn()
}

// ---------------------------------------------------------------------------
// The formatter: every time with its zone's label.
// ---------------------------------------------------------------------------

type Instant = string | number | Date

function toMs(at: Instant): number {
  return at instanceof Date ? at.getTime() : typeof at === 'number' ? at : new Date(at).getTime()
}

interface Wall {
  year: number
  month: number
  day: number
  hour: number
  minute: number
  second: number
  label: string
}

const formatters = new Map<string, Intl.DateTimeFormat>()

function formatterFor(z: string): Intl.DateTimeFormat {
  let f = formatters.get(z)
  if (f === undefined) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: z,
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      second: 'numeric',
      timeZoneName: 'short',
    })
    formatters.set(z, f)
  }
  return f
}

/** The wall clock in `z` at `ms`, with the zone's short label ("PDT", "UTC", "GMT+2"). */
function wallAt(ms: number, z: string): Wall {
  const parts: Record<string, string> = {}
  for (const p of formatterFor(z).formatToParts(new Date(ms))) parts[p.type] = p.value
  const label = z === UTC ? UTC : (parts.timeZoneName ?? z)
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour) % 24,
    minute: Number(parts.minute),
    second: Number(parts.second),
    label,
  }
}

const pad = (n: number) => String(n).padStart(2, '0')
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** The zone's short label at an instant: "PDT" in summer, "PST" in winter, "UTC". */
export function zoneLabel(at: Instant, z: string = zone): string {
  const ms = toMs(at)
  return wallAt(Number.isFinite(ms) ? ms : Date.now(), z).label
}

/** "14:05 PDT": the time of day in `z`, labelled. An unreadable instant is returned as given. */
export function formatClock(at: Instant, z: string = zone): string {
  const ms = toMs(at)
  if (!Number.isFinite(ms)) return String(at)
  const w = wallAt(ms, z)
  return `${pad(w.hour)}:${pad(w.minute)} ${w.label}`
}

/** "Oct 26 14:05:09 PDT": the date and time in `z`, labelled, to the second. */
export function formatDateTime(at: Instant, z: string = zone): string {
  const ms = toMs(at)
  if (!Number.isFinite(ms)) return String(at)
  const w = wallAt(ms, z)
  return `${MONTHS[w.month - 1]} ${w.day} ${pad(w.hour)}:${pad(w.minute)}:${pad(w.second)} ${w.label}`
}

/** How far `z`'s wall clock is ahead of UTC at `ms`, in ms. */
function offsetAt(ms: number, z: string): number {
  const w = wallAt(ms, z)
  const asUtc = Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second)
  return asUtc - Math.floor(ms / 1000) * 1000
}

/**
 * The instant "today" starts in `z`: local midnight of the day `now` falls
 * in. Found from the zone's offset, then the offset again at that instant,
 * because a day that changes to or from summer time has a different offset
 * at midnight than at noon. A zone that skips midnight itself starts the day
 * at the first instant that reads as that date.
 */
export function startOfDay(now: Instant, z: string = zone): number {
  const ms = toMs(now)
  const w = wallAt(ms, z)
  const midnight = Date.UTC(w.year, w.month - 1, w.day)
  let at = midnight - offsetAt(midnight, z)
  at = midnight - offsetAt(at, z)
  // A skipped midnight lands on the day before: step to the first hour of the date.
  const got = wallAt(at, z)
  if (got.day !== w.day) at += 3_600_000 - (got.minute * 60_000 + got.second * 1000)
  return at
}

/** Whether `at` falls on the same day as `now` in `z`. */
export function isToday(at: Instant | null, now: Instant, z: string = zone): boolean {
  if (at === null) return false
  const ms = toMs(at)
  if (!Number.isFinite(ms)) return false
  const from = startOfDay(now, z)
  return ms >= from && ms < startOfDay(from + 36 * 3_600_000, z)
}
