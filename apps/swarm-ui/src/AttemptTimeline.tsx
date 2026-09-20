import { useCallback } from 'react'
import { loadAgentDetail, loadAttempts } from './api'
import { num, type Result } from './fetch'
import { Screen, timeAgo } from './Shell'
import type { AttemptRow, TaskEvent } from './types'

/**
 * One task, attempt by attempt. Replaces the flat event list in the drawer.
 *
 * WHY GROUPING IS THE POINT. A flat list interleaves three attempts into one
 * column where only the timestamps separate them, so the question this screen
 * is opened for -- what was different about the two that failed -- cannot be
 * read off it. Every event carries `attempt_id` (`_event_to_api`,
 * routes/tasks.py), so the grouping is the API's own, not a guess.
 *
 * AND WHY THE ATTEMPT DOCUMENTS CARRY IT. `result_summary` is written ONCE, by
 * finish(), at terminal state -- a task that failed twice and succeeded on the
 * third carries only attempt three's numbers. Attempts one and two exist only
 * as attempt documents, and their exit codes, errors and peak RSS are
 * reachable nowhere else.
 */

interface AttemptTimeline {
  attempts: AttemptRow[]
  /** null means the event read FAILED. An empty array means there are none. */
  events: TaskEvent[] | null
  eventsDetail: string | null
}

/**
 * Two reads, merged. This composition belongs in api.ts beside `loadHolders`;
 * it is inline only so this screen imports nothing that does not already exist.
 * `loadAgentDetail` supplies the events because api.ts exposes no events-only
 * loader -- costing one extra GET of the task document, which is what to fix
 * when this moves.
 */
async function loadTimeline(taskId: string): Promise<Result<AttemptTimeline>> {
  const [attempts, detail] = await Promise.all([loadAttempts(taskId), loadAgentDetail(taskId)])

  // THE ATTEMPTS READ DECIDES THE SCREEN, and `empty` is passed through rather
  // than flattened to []: a QUEUED or PARKED task has never been admitted and
  // genuinely has no attempt document, which must stay distinguishable from a
  // read that failed.
  if (attempts.status !== 'ok' && attempts.status !== 'stale') return attempts

  // The event read may fail on its own: the attempt cards are trustworthy
  // without it, so failing the screen would hide records that exist nowhere
  // else. The same degrade HoldersBoard makes for pool counters.
  const haveEvents = detail.status === 'ok' || detail.status === 'stale'
  const data: AttemptTimeline = {
    attempts: attempts.data.attempts,
    events: haveEvents ? detail.data.events : null,
    eventsDetail: haveEvents
      ? detail.data.eventsDetail
      : detail.status === 'error'
        ? detail.error.message
        : 'The event read did not complete.',
  }
  return attempts.status === 'stale'
    ? { status: 'stale', data, fetchedAt: attempts.fetchedAt, error: attempts.error }
    : { status: 'ok', data, fetchedAt: attempts.fetchedAt, serverAt: attempts.serverAt }
}

export function AttemptTimelineScreen({ taskId }: { taskId: string }) {
  const load = useCallback(() => loadTimeline(taskId), [taskId])
  return (
    <Screen
      // The task id verbatim -- no truncation, no case change anywhere here. An
      // id a reader cannot paste back into swarmctl is unusable.
      title={taskId}
      load={load}
      summary={(t) => (
        <>
          {t.attempts.length} attempt{t.attempts.length === 1 ? '' : 's'} ·{' '}
          {t.events === null
            ? 'events unavailable'
            : `${t.events.length} event${t.events.length === 1 ? '' : 's'} on this page`}
        </>
      )}
      empty={{
        heading: 'No attempt has been recorded for this task',
        body: 'The attempts query for this task succeeded and returned nothing. A task that has never been admitted — QUEUED, PARKED, or READY and waiting for capacity — has no attempt document, so this is a real zero and not a failed read.',
      }}
    >
      {(t) => <Body t={t} />}
    </Screen>
  )
}

interface Group {
  key: string
  /** null for the pre-admission group, and for events naming an unknown attempt. */
  attempt: AttemptRow | null
  label: string
  events: TaskEvent[]
}

/**
 * Events under their attempt, oldest attempt first: a retried task only parses
 * in the order it happened. The route returns attempts newest first, so they
 * are re-sorted on `created_at` -- ISO-8601 UTC, which compares
 * lexicographically -- not on `generation`, which fences an attempt and is not
 * promised to be dense.
 */
function grouped(t: AttemptTimeline): Group[] {
  const byAttempt = new Map<string, TaskEvent[]>()
  const preface: TaskEvent[] = []
  for (const e of t.events ?? []) {
    if (e.attempt_id === null) {
      preface.push(e)
      continue
    }
    const list = byAttempt.get(e.attempt_id)
    if (list) list.push(e)
    else byAttempt.set(e.attempt_id, [e])
  }

  const groups: Group[] = [
    { key: '@preface', attempt: null, label: 'Before any attempt', events: preface },
  ]
  for (const a of [...t.attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))) {
    const label = `Attempt · generation ${a.generation}`
    groups.push({ key: a.attempt_id, attempt: a, label, events: byAttempt.get(a.attempt_id) ?? [] })
    byAttempt.delete(a.attempt_id)
  }
  // What is left names an attempt no document on this page describes. Folding
  // these into the preface would file real attempt events under "before any
  // attempt", which is a lie about when they happened.
  for (const [id, events] of byAttempt) {
    groups.push({ key: id, attempt: null, label: `Attempt ${id}`, events })
  }
  return groups
}

function Body({ t }: { t: AttemptTimeline }) {
  const groups = grouped(t)
  return (
    <>
      {t.events === null ? (
        <div className="state partial" role="status">
          <h3>The event history could not be read</h3>
          <p>
            A failed read, not a task that produced no events. The attempts below
            loaded and are complete. {t.eventsDetail}
          </p>
        </div>
      ) : (
        <PageNote groups={groups} count={t.events.length} />
      )}
      {groups.map((g) => (
        <AttemptCard key={g.key} g={g} eventsRead={t.events !== null} />
      ))}
      <p className="muted small">
        Every number on these cards is per attempt, from the attempt documents.
        The task&apos;s <code>result_summary</code> is written once at terminal
        state and describes only the last attempt. A null exit code, token count
        or cost is an absence of a record, never a zero.
      </p>
    </>
  )
}

/** What the event page does and does not cover. */
function PageNote({ groups, count }: { groups: Group[]; count: number }) {
  const blind = groups.filter((g) => g.attempt !== null && g.events.length === 0)
  const orphan = groups.filter((g) => g.attempt === null && g.key !== '@preface')

  return (
    <>
      {/* create_tasks writes the task and its `submitted` event in ONE batch
          (store.py), so a task that exists has at least one event. Zero rows
          under a 200 is a failed query wearing a success code. */}
      {count === 0 && (
        <p className="warn-text">
          Zero events came back, yet a task is written together with its{' '}
          <code>submitted</code> event in one batch — so this is a failed query,
          not an empty history.
        </p>
      )}
      {blind.length > 0 && (
        <div className="state partial" role="status">
          <h3>
            {blind.length} attempt{blind.length === 1 ? ' has' : 's have'} no events here
          </h3>
          <p>
            The events endpoint orders oldest-first, caps the page server-side and
            returns no page token — so once a task outruns one page the newest
            events, including everything belonging to those attempts, cannot be
            fetched at all. Missing, not absent.
          </p>
        </div>
      )}
      {orphan.length > 0 && (
        <p className="warn-text">
          {orphan.length} attempt id{orphan.length === 1 ? '' : 's'} appear in these
          events with no attempt document on this page — there it is the attempt
          list that is short, not the events.
        </p>
      )}
      <p className="provenance">
        {count} event{count === 1 ? '' : 's'} on this page, the oldest {count} this task
        recorded · no page token, so newer events may exist and are unreachable
      </p>
    </>
  )
}

function AttemptCard({ g, eventsRead }: { g: Group; eventsRead: boolean }) {
  const a = g.attempt
  const out = a === null ? null : outcome(a)
  return (
    <section className="section panel">
      <h2>
        {g.label}
        {out && <span className={`tag ${out.tone}`}>{out.label}</span>}
        <span className="count-chip">
          {g.events.length} event{g.events.length === 1 ? '' : 's'}
        </span>
      </h2>

      {a && (
        <dl className="kv">
          <dt>Attempt</dt>
          <dd className="mono">{a.attempt_id}</dd>
          <dt>Ran on</dt>
          <dd>
            {a.backend} · <span className="mono">{a.execution_name ?? '—'}</span>
            {a.oom_near_miss && <span className="tag full">OOM near miss</span>}
          </dd>
          <dt>Duration</dt>
          <dd>{ran(a)}</dd>
          {/* A null exit code means still running or never finished. It is NOT
              a zero, and the chip in the heading says which of the two. */}
          <dt>Exit code</dt>
          <dd>{num(a.exit_code)}</dd>
          <dt>Peak RSS</dt>
          <dd>
            {a.peak_rss_bytes === null ? '—' : `${(a.peak_rss_bytes / 1e9).toFixed(2)} GB`} ·{' '}
            {a.checkpoints.length} checkpoint{a.checkpoints.length === 1 ? '' : 's'}
          </dd>
          {/* An em dash, never $0.00: only the CLI-agent runners report usage,
              and an attempt that reported none did not run for free. */}
          <dt>Usage</dt>
          <dd>
            {num(a.input_tokens)} in / {num(a.output_tokens)} out ·{' '}
            {a.cost_usd === null ? '—' : `$${a.cost_usd.toFixed(4)}`}
          </dd>
          {a.error !== null && (
            <>
              <dt>Error</dt>
              <dd><pre className="err full">{a.error}</pre></dd>
            </>
          )}
        </dl>
      )}

      {g.events.length === 0 ? (
        <p className="muted">
          {eventsRead
            ? 'No event on this page belongs here — the page ends before it, or none were written.'
            : 'The event read failed, so nothing here can say what this attempt did.'}
        </p>
      ) : (
        <ol className="timeline">
          {g.events.map((e) => (
            <li key={e.event_id}>
              <span className="ev-type">{e.type}</span>
              <span className="ev-at">{timeAgo(e.at)}</span>
              {/* The fencing generation the event was written under. A stale
                  worker's events carry the OLD one -- that is how a reclaim
                  reads here. */}
              {e.generation !== null && <span className="ev-gen">gen {e.generation}</span>}
              {e.detail && Object.keys(e.detail).length > 0 && (
                <pre className="ev-detail">{JSON.stringify(e.detail, null, 2)}</pre>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

/**
 * The outcome chip. Exit 0 is the only success; null is three different things
 * depending on what else the document says -- never a zero, never "failed".
 */
function outcome(a: AttemptRow): { label: string; tone: string } {
  if (a.exit_code === 0) return { label: 'exit 0', tone: 'ok' }
  if (a.exit_code !== null) return { label: `exit ${a.exit_code}`, tone: 'full' }
  if (a.started_at === null) return { label: 'never started', tone: 'unknown' }
  if (a.completed_at === null) return { label: 'running, no exit code yet', tone: 'live' }
  return { label: 'ended with no exit code recorded', tone: 'wait' }
}

function ran(a: AttemptRow): string {
  if (a.started_at === null) return 'Never started — created, then nothing ran.'
  if (a.completed_at === null) return `Started ${timeAgo(a.started_at)}, not finished.`
  const ms = new Date(a.completed_at).getTime() - new Date(a.started_at).getTime()
  if (!Number.isFinite(ms)) return `Started ${timeAgo(a.started_at)}, finish time unreadable.`
  return `${Math.round(ms / 1000)}s, finished ${timeAgo(a.completed_at)}`
}
