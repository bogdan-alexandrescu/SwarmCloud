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
 * path. This panel reads `GET /v1/tasks/{id}/live/recordings` and plays a
 * segment from `GET /v1/tasks/{id}/live/recordings/raw?attempt_id=&name=`;
 * until a deployment serves them it says so, and shows nothing.
 *
 * Theater's replay (lane L11b) reads the same segments, THROUGH THE SAME
 * LOADER (`loadLiveRecordings` in api.ts, lane LB-UX2a): two private reads of
 * one route are how the route came to serve two shapes.
 */
import { useEffect, useState } from 'react'
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

async function loadListing(taskId: string): Promise<Listing> {
  if (USE_FIXTURES) return { kind: 'unserved' }
  const r = await loadLiveRecordings(taskId)
  if (r.status === 'error') return r.error.kind === 'not_found' ? { kind: 'unserved' } : { kind: 'failed', error: r.error }
  if (r.status === 'loading') return { kind: 'reading' }
  if (r.status === 'empty') return { kind: 'listed', shown: [], dropped: 0 }
  return { kind: 'listed', shown: r.data.segments, dropped: 0 }
}

/**
 * The recordings of one live-browser task, oldest segment first, each one a
 * press away from a player. No player is mounted until one is chosen: a
 * minute of video per row would read every segment to draw the list.
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
    void loadListing(id).then((l) => {
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
          {listing.shown.length === 0 ? (
            <p className="rec-absent">
              <Mark kind="zero" say="The route answered, and listed no recording of this agent." /> No recording of this agent.
            </p>
          ) : (
            <ul className="rec-list">
              {listing.shown.map((r) => (
                <li key={r.key} data-recording={r.key}>
                  <span className="rec-at">
                    {r.attempt_id} · {r.name}
                  </span>
                  <span className="rec-size">{r.bytes === null ? '' : ` · ${bytesLabel(r.bytes)}`}</span>
                  {r.created_at !== null && <span className="rec-when"> · {new Date(r.created_at).toLocaleString()}</span>}{' '}
                  <Button kind="ghost" size="sm" onClick={() => setPlaying(playing === r.key ? null : r.key)}>
                    {playing === r.key ? 'Close' : 'Play'}
                  </Button>
                  {playing === r.key && (
                    <video className="rec-player" controls preload="metadata" src={recordingSrc(task.id, r)} />
                  )}
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
