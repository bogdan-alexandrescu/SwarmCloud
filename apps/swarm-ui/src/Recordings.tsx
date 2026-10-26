/**
 * A LIVE-BROWSER TASK'S RECORDINGS (docs/design/live-browser.md §7, LB-4; lane
 * LB-L12, part of #1030).
 *
 * The relay records the frames the person watching saw -- the screen as
 * Chromium drew it, since the gate redacts no part of it (BGD, 2026-10-25;
 * part of #2298) -- as one-minute VP8 segments at
 * `tenants/<t>/tasks/<task>/attempts/<a>/live/rec-<seq>.live.webm`, and a
 * lifecycle rule deletes them after 7 days whatever else the bucket keeps.
 * They are served to members of the task's tenant only, through swarm-api,
 * which checks the tenant on the read exactly as it does on `/live` (§5.2).
 *
 * THE TENANT CHECK IS THE SERVER'S. This panel checks again, by the key, and
 * shows only segments under THIS task's own prefix in THIS task's tenant: a
 * route that one day served a neighbour's segment would otherwise put another
 * tenant's screen in front of this one's members, which LB-2 and invariant 9
 * forbid. What it drops it counts out loud rather than hiding.
 *
 * THE ROUTES ARE LANE L9's, and the design names their behaviour, not their
 * path. This panel reads `GET /v1/tasks/{id}/live/recordings` and plays an
 * attempt's segments, joined into one video (`TakePlayer`), from
 * `GET /v1/tasks/{id}/live/recordings/raw?attempt_id=&name=`;
 * until a deployment serves them it says so, and shows nothing.
 *
 * Theater's replay (lane L11b) reads the same segments, THROUGH THE SAME
 * LOADER (`loadLiveRecordings` in api.ts, lane LB-UX2a): two private reads of
 * one route are how the route came to serve two shapes.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type RefObject } from 'react'
import { USE_FIXTURES, loadLiveRecordings, type LiveRecording } from './api'
import { Button } from './components'
import { errorHeading, route, tenantQuery, type ApiError } from './fetch'
import { Mark } from './primitives'
import { bytesLabel, type Task } from './types'

/** One recorded segment, as much of it as this panel reads (`LiveRecording`). */
export type Recording = Pick<LiveRecording, 'attempt_id' | 'name' | 'key' | 'bytes' | 'created_at'>

/** §7: how long a recording is kept, by the bucket's own suffix rule. */
export const RECORDING_DAYS = 7

const SEGMENT = /^rec-\d+\.live\.webm$/
const ATTEMPT = /^[A-Za-z0-9_-]+$/

/**
 * This segment is one of THIS task's, in THIS task's tenant, under its
 * attempt's `live/` key, with the recording suffix. Everything else the
 * route serves -- another tenant's, another task's, an action log, a log
 * file -- is not shown.
 */
export function ownRecording(task: Pick<Task, 'id' | 'tenant_id'>, r: Recording): boolean {
  if (typeof r.attempt_id !== 'string' || !ATTEMPT.test(r.attempt_id)) return false
  if (typeof r.name !== 'string' || !SEGMENT.test(r.name)) return false
  return r.key === `tenants/${task.tenant_id}/tasks/${task.id}/attempts/${r.attempt_id}/live/${r.name}`
}

/** Where a segment is played from: the tenant rides in the query, as on every raw read. */
export function recordingSrc(taskId: string, r: Pick<Recording, 'attempt_id' | 'name'>): string {
  return route(
    '/v1/tasks/{id}/live/recordings/raw',
    { id: taskId },
    tenantQuery(new URLSearchParams({ attempt_id: r.attempt_id, name: r.name })),
  ).url
}

type Listing =
  | { kind: 'reading' }
  | { kind: 'unserved' }
  | { kind: 'failed'; error: ApiError }
  | { kind: 'listed'; shown: Recording[]; dropped: number }

async function loadListing(task: Pick<Task, 'id' | 'tenant_id'>): Promise<Listing> {
  if (USE_FIXTURES) return fixtureListing(task)
  const r = await loadLiveRecordings(task.id)
  if (r.status === 'error') return r.error.kind === 'not_found' ? { kind: 'unserved' } : { kind: 'failed', error: r.error }
  if (r.status === 'loading') return { kind: 'reading' }
  if (r.status === 'empty') return { kind: 'listed', shown: [], dropped: 0 }
  return { kind: 'listed', shown: r.data.segments, dropped: 0 }
}

/**
 * THE FIXTURE BUILD'S RECORDINGS: twelve one-minute parts of one attempt of
 * the fixture's live-browser agent (`task_b40e5a1c`, api.ts), sized across the
 * 52-778 KiB range measured on swarm.saga.xyz on 2026-10-26 (BT-012), so
 * the list's layout can be reviewed without a deployment. The fixture build
 * serves no video bytes, and its player says so rather than fetching.
 * Every other task is `unserved` there, as before.
 */
export const FIXTURE_RECORDED_TASK = 'task_b40e5a1c'

function fixtureListing(task: Pick<Task, 'id' | 'tenant_id'>): Listing {
  if (task.id !== FIXTURE_RECORDED_TASK) return { kind: 'unserved' }
  const attempt = `att_${task.id.slice(5)}_1`
  const start = Date.parse('2026-10-19T11:42:00Z')
  const sizes = [53_248, 412_000, 796_672, 640_000, 220_000, 98_000, 512_000, 330_000, 701_000, 150_000, 61_000, 280_000]
  const shown = sizes.map((bytes, i): Recording => {
    const name = `rec-${start + i * 60_000}.live.webm`
    return {
      attempt_id: attempt,
      name,
      key: `tenants/${task.tenant_id}/tasks/${task.id}/attempts/${attempt}/live/${name}`,
      bytes,
      created_at: new Date(start + (i + 1) * 60_000).toISOString(),
    }
  })
  return { kind: 'listed', shown, dropped: 0 }
}

/**
 * One attempt's recording: its one-minute parts, oldest first. The relay
 * cuts a recording into parts (§7: a minute each, and a new one whenever the
 * rate or the display size changes), but a person watching wants the attempt,
 * not twelve files with twelve Play buttons (BT-012).
 */
export interface Take {
  attempt_id: string
  parts: Recording[]
  /** Null when any part's size is unknown: a partial sum would read as the whole. */
  bytes: number | null
  first: string | null
  last: string | null
}

/** The listed segments, grouped by attempt in the order the list put them, each attempt's parts by name. */
export function takesOf(shown: readonly Recording[]): Take[] {
  const order: string[] = []
  const by = new Map<string, Recording[]>()
  for (const r of shown) {
    const parts = by.get(r.attempt_id)
    if (parts === undefined) {
      order.push(r.attempt_id)
      by.set(r.attempt_id, [r])
    } else parts.push(r)
  }
  return order.map((attempt_id) => {
    const parts = [...by.get(attempt_id)!].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
    const sizes = parts.map((p) => p.bytes)
    const times = parts.map((p) => p.created_at).filter((t): t is string => t !== null)
    return {
      attempt_id,
      parts,
      bytes: sizes.some((b) => b === null) ? null : sizes.reduce<number>((n, b) => n + (b ?? 0), 0),
      first: times[0] ?? null,
      last: times[times.length - 1] ?? null,
    }
  })
}

/** What the relay's encoder writes (swarm_live/recorder.py, FfmpegVp8Encoder): VP8 in WebM. */
export const RECORDING_TYPE = 'video/webm; codecs="vp8"'

/**
 * This browser can join parts into one video: Media Source Extensions, with
 * VP8 in WebM. Where it cannot, the player plays the parts one after another.
 */
export function canJoinParts(): boolean {
  const MS = (globalThis as { MediaSource?: typeof MediaSource }).MediaSource
  return typeof MS === 'function' && typeof MS.isTypeSupported === 'function' && MS.isTypeSupported(RECORDING_TYPE)
}

/**
 * THE PLAYER FITS THE PANE IT IS IN (BT-012). The Attempts tab's pane is the
 * one vertical scroller from 561 px up, and was 407 px tall at 1440x757; the
 * player's `max-height: 60vh` came to 454 px, so a 1440x987 frame drawn 609
 * wide (417 tall) never showed with its controls. The player is held to the
 * pane's height less this much, which leaves the attempt's own row above it
 * in view. The stylesheet keeps `60vh` as the cap where no pane bounds it.
 */
export const PANE_ROOM = 48
/** Below this a player is not watchable; the pane scrolls instead. */
export const MIN_PLAYER = 160

function usePaneFit(ref: RefObject<HTMLElement | null>): number | null {
  const [fit, setFit] = useState<number | null>(null)
  useLayoutEffect(() => {
    const pane = ref.current?.closest<HTMLElement>('.ag-split-pane') ?? null
    if (pane === null) return
    const measure = () => {
      const h = pane.clientHeight
      setFit(h > 0 ? Math.max(MIN_PLAYER, h - PANE_ROOM) : null)
    }
    measure()
    if (typeof ResizeObserver !== 'function') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
    const watch = new ResizeObserver(measure)
    watch.observe(pane)
    return () => watch.disconnect()
  }, [ref])
  return fit
}

function appendPart(buffer: SourceBuffer, bytes: ArrayBuffer): Promise<void> {
  return new Promise((resolve, reject) => {
    const done = () => {
      buffer.removeEventListener('updateend', done)
      buffer.removeEventListener('error', failed)
      resolve()
    }
    const failed = () => {
      buffer.removeEventListener('updateend', done)
      buffer.removeEventListener('error', failed)
      reject(new Error('the browser could not decode it'))
    }
    buffer.addEventListener('updateend', done)
    buffer.addEventListener('error', failed)
    buffer.appendBuffer(bytes)
  })
}

/** A part's read: the IAP cookie, same origin only, as every read in fetch.ts. */
const PART_READ = { sameOrigin: 'same-origin' } as const satisfies Record<string, RequestCredentials>

type Joining = { read: number; done: boolean; failed: string | null }

/**
 * ONE ATTEMPT, ONE VIDEO (BT-012). Where the browser has Media Source
 * Extensions, every part is read in order -- through the same tenant-checked
 * raw route a `<video src>` used, with the IAP cookie and nothing else -- and
 * appended to one source buffer in `sequence` mode, so each part's frames
 * follow the last's: one timeline, one duration (the frames' own, not the
 * part's metadata, so parts recorded before the relay wrote durations join
 * too), one scrubber across the attempt. Playing starts as soon as the first
 * part is in. A part that cannot be read ends the video there and says which.
 *
 * Without MSE the parts play one after another in the same player, the next
 * starting when one ends; each part now carries its own duration
 * (recorder.py), so each one's scrubber works.
 */
export function TakePlayer({ taskId, take }: { taskId: string; take: Take }) {
  const ref = useRef<HTMLVideoElement>(null)
  const fit = usePaneFit(ref)
  const [join] = useState(canJoinParts)
  const [part, setPart] = useState(0)
  const [joining, setJoining] = useState<Joining>({ read: 0, done: false, failed: null })
  const { parts } = take
  const n = parts.length

  useEffect(() => {
    const video = ref.current
    if (video !== null && typeof video.scrollIntoView === 'function') video.scrollIntoView({ block: 'nearest' })
  }, [])

  useEffect(() => {
    const video = ref.current
    if (!join || USE_FIXTURES || video === null) return
    let gone = false
    const stop = new AbortController()
    const source = new MediaSource()
    const url = URL.createObjectURL(source)
    setJoining({ read: 0, done: false, failed: null })
    const feed = async () => {
      URL.revokeObjectURL(url)
      const buffer = source.addSourceBuffer(RECORDING_TYPE)
      buffer.mode = 'sequence'
      let failed: string | null = null
      for (let i = 0; i < parts.length; i++) {
        try {
          const res = await fetch(recordingSrc(taskId, parts[i]!), { credentials: PART_READ.sameOrigin, signal: stop.signal })
          if (!res.ok) throw new Error(`the route answered ${res.status}`)
          const bytes = await res.arrayBuffer()
          if (gone) return
          await appendPart(buffer, bytes)
        } catch (err) {
          if (gone) return
          failed = `Part ${i + 1} of ${parts.length} could not be read: ${err instanceof Error ? err.message : 'the read failed'}.`
          break
        }
        if (gone) return
        setJoining((j) => ({ ...j, read: i + 1 }))
      }
      if (gone) return
      if (source.readyState === 'open') source.endOfStream()
      setJoining((j) => ({ ...j, done: true, failed }))
    }
    const opened = () => void feed()
    source.addEventListener('sourceopen', opened, { once: true })
    video.src = url
    return () => {
      gone = true
      stop.abort()
      source.removeEventListener('sourceopen', opened)
      URL.revokeObjectURL(url)
      video.removeAttribute('src')
    }
  }, [join, taskId, parts])

  const style = (fit === null ? undefined : { '--rec-fit': `${fit}px` }) as CSSProperties | undefined
  const queued = !join && !USE_FIXTURES
  return (
    <div className="rec-play">
      <video
        ref={ref}
        className="rec-player"
        style={style}
        controls
        preload="metadata"
        autoPlay={queued && part > 0}
        src={queued ? recordingSrc(taskId, parts[part]!) : undefined}
        onEnded={queued ? () => setPart((p) => (p + 1 < n ? p + 1 : p)) : undefined}
        data-part={queued ? part + 1 : undefined}
      />
      <p className="rec-status" role={joining.failed !== null ? 'alert' : undefined}>
        {USE_FIXTURES
          ? 'The fixture build serves no recording, so nothing plays here.'
          : queued
            ? `Part ${part + 1} of ${n}. This browser cannot join the parts, so the next plays when this one ends.`
            : joining.failed !== null
              ? `${joining.failed} The video ends at the parts before it.`
              : joining.done
                ? n === 1
                  ? 'One part.'
                  : `${n} parts, joined into one video.`
                : `Joining the parts into one video: ${joining.read} of ${n} read.`}
      </p>
    </div>
  )
}

function when(t: string | null): string {
  return t === null ? '' : new Date(t).toLocaleString()
}

/**
 * The recordings of one live-browser task, one row per attempt, oldest first;
 * each attempt's parts are listed under it. No player is mounted until one is
 * chosen: a player per row would read every part to draw the list.
 */
export function RecordingsPanel({ task }: { task: Pick<Task, 'id' | 'tenant_id'> }) {
  const [listing, setListing] = useState<Listing>({ kind: 'reading' })
  const [playing, setPlaying] = useState<string | null>(null)
  // Keyed on the id and the tenant, not the row: the list re-reads its rows
  // every few seconds, and a new object for the same task is not a new list.
  const { id, tenant_id } = task
  useEffect(() => {
    let live = true
    setListing({ kind: 'reading' })
    void loadListing({ id, tenant_id }).then((l) => {
      if (!live) return
      if (l.kind !== 'listed') {
        setListing(l)
        return
      }
      const shown = l.shown.filter((r) => ownRecording({ id, tenant_id }, r))
      shown.sort((a, b) => (a.attempt_id === b.attempt_id ? (a.name < b.name ? -1 : 1) : (a.created_at ?? '') < (b.created_at ?? '') ? -1 : 1))
      setListing({ kind: 'listed', shown, dropped: l.shown.length - shown.length })
    })
    return () => {
      live = false
    }
  }, [id, tenant_id])

  // Memoised on the listing: the player restarts its read whenever its parts
  // are a new array, and the panel re-renders on every Play and Close.
  const takes = useMemo(() => (listing.kind === 'listed' ? takesOf(listing.shown) : []), [listing])
  return (
    <section className="ctl-card rec-panel" aria-labelledby={`rec-h-${task.id}`}>
      <h3 id={`rec-h-${task.id}`}>Browser recordings</h3>
      <p className="rec-note">
        The frames as the person watching saw them. Kept {RECORDING_DAYS} days, then deleted.
      </p>
      {listing.kind === 'reading' ? (
        <p className="art-loading">
          <Mark kind="pending" say="Reading this agent's recordings. The read is in flight." />
        </p>
      ) : listing.kind === 'unserved' ? (
        <p className="rec-absent">
          <Mark kind="unread" say="The recordings route answered 404: this deployment does not serve it yet." /> Recordings are
          not served by this deployment yet.
        </p>
      ) : listing.kind === 'failed' ? (
        <p className="rec-absent" role="alert">
          <Mark kind="unread" say={listing.error.message} /> {errorHeading(listing.error)}: {listing.error.message}
        </p>
      ) : (
        <>
          {takes.length === 0 ? (
            <p className="rec-absent">
              <Mark kind="zero" say="The route answered, and listed no recording of this agent." /> No recording of this agent.
            </p>
          ) : (
            <ul className="rec-list">
              {takes.map((t) => (
                <li key={t.attempt_id} className="rec-take" data-attempt={t.attempt_id}>
                  <span className="rec-what">
                    <span className="rec-at">{t.attempt_id}</span>
                    <span className="rec-size">
                      {' · '}
                      {t.parts.length === 1 ? 'one part' : `${t.parts.length} parts`}
                      {t.bytes === null ? '' : ` · ${bytesLabel(t.bytes)}`}
                    </span>
                    {t.first !== null && (
                      <span className="rec-when">
                        {' · '}
                        {t.first === t.last ? when(t.first) : `${when(t.first)} to ${when(t.last)}`}
                      </span>
                    )}
                  </span>
                  <span className="rec-act">
                    <Button kind="ghost" size="sm" onClick={() => setPlaying(playing === t.attempt_id ? null : t.attempt_id)}>
                      {playing === t.attempt_id ? 'Close' : 'Play'}
                    </Button>
                  </span>
                  {playing === t.attempt_id && <TakePlayer taskId={task.id} take={t} />}
                  <details className="rec-parts">
                    <summary>{t.parts.length === 1 ? 'The part' : `The ${t.parts.length} parts`}</summary>
                    <ol>
                      {t.parts.map((r) => (
                        <li key={r.key} data-recording={r.key}>
                          {r.name}
                          {r.bytes === null ? '' : ` · ${bytesLabel(r.bytes)}`}
                          {r.created_at !== null && ` · ${when(r.created_at)}`}
                        </li>
                      ))}
                    </ol>
                  </details>
                </li>
              ))}
            </ul>
          )}
          {listing.dropped > 0 && (
            <p className="rec-dropped" role="note">
              {listing.dropped} {listing.dropped === 1 ? 'entry' : 'entries'} the route listed are not this agent's recordings
              in this tenant, and are not shown.
            </p>
          )}
        </>
      )}
    </section>
  )
}
