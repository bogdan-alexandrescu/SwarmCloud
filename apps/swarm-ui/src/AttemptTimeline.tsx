import { useCallback } from 'react'
import { Chip, Em, Mark, attemptLabel, type ChipTone } from './AgentDetail'
import { loadAgentDetail, loadAttempts } from './api'
import { num, type Result } from './fetch'
import { HelpCard } from './HelpCard'
import { Screen, timeAgo } from './Shell'
import { bytesLabel, formatDuration, type AttemptRow, type TaskEvent } from './types'

/**
 * One task, attempt by attempt. Replaces the flat event list in the drawer, which
 * interleaves three attempts into one column where only the timestamps separate them --
 * so what was different about the two that failed cannot be read off it. Every event
 * carries `attempt_id` (`_event_to_api`, routes/tasks.py), so the grouping is the API's
 * own, not a guess. The attempt documents are the other half: `result_summary` is
 * written ONCE, by finish(), at terminal state, so a task that failed twice and
 * succeeded on the third carries only attempt three's numbers.
 */

interface AttemptTimeline {
  attempts: AttemptRow[]
  /** null means the event read FAILED. An empty array means there are none. */
  events: TaskEvent[] | null
  eventsDetail: string | null
}

/**
 * Two reads, merged. This belongs in api.ts beside `loadHolders`; it is inline only so
 * this screen imports nothing that does not already exist, and `loadAgentDetail` gives
 * the events because api.ts has no events-only loader -- one extra GET to fix there.
 */
async function loadTimeline(taskId: string): Promise<Result<AttemptTimeline>> {
  const [attempts, detail] = await Promise.all([loadAttempts(taskId), loadAgentDetail(taskId)])

  // THE ATTEMPTS READ DECIDES THE SCREEN, and `empty` is passed through rather than
  // flattened to []: a QUEUED or PARKED task genuinely has no attempt document, which
  // must stay distinguishable from a read that failed.
  if (attempts.status !== 'ok' && attempts.status !== 'stale') return attempts

  // The event read may fail on its own: the attempt cards are trustworthy without it,
  // and failing the screen would hide records that exist nowhere else.
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

/**
 * `plural` IS GONE.
 *
 * It existed so this screen could write "3 events" rather than "3 event(s)",
 * which is correct English and was, at every call site, a figure wearing a
 * noun. The noun is the key beside the figure now -- `ev`, `att`, `ckpt` --
 * said once per column rather than once per value.
 */

export function AttemptTimelineScreen({ taskId }: { taskId: string }) {
  const load = useCallback(() => loadTimeline(taskId), [taskId])
  return (
    <Screen
      // Verbatim: an id a reader cannot paste back into swarmctl is unusable.
      title={taskId}
      load={load}
      summary={(t) =>
        `${t.attempts.length} att · ` +
        (t.events === null ? 'events unread' : `${t.events.length} ev`)
      }
      empty={{
        heading: 'No attempt · real zero',
        // ONE SENTENCE. Which states have no attempt document, and why, is
        // `#help/attempt-documents` -- a topic that already exists and says it
        // better than a two-clause sentence with a parenthetical in it.
        // NO `?` (B7.4). "succeeded and returned nothing" is already the
        // distinction `attempt-documents` exists to protect -- a measured zero
        // rather than a read that did not land. Which states never write an
        // attempt document is in the rail's Help section; this screen keeps its
        // one glyph for the PARTIAL case below, which is the one a reader
        // cannot resolve from the surface.
        body: <>The attempts query succeeded and returned nothing.</>,
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
 * Events under their attempt, oldest attempt first: a retried task only parses in the
 * order it happened. The route returns attempts newest first, so they are re-sorted on
 * `created_at` (ISO-8601 UTC, so lexicographic), not on `generation` -- which fences an
 * attempt and is not promised to be dense.
 */
function grouped(t: AttemptTimeline): Group[] {
  const byAttempt = new Map<string, TaskEvent[]>()
  const preface: TaskEvent[] = []
  // `?? []` only skips the loop when the event read FAILED. Nothing downstream then
  // claims an attempt had no events -- AttemptCard stays silent, and Body says why.
  for (const e of t.events ?? []) {
    if (e.attempt_id === null) preface.push(e)
    else {
      const list = byAttempt.get(e.attempt_id)
      if (list) list.push(e)
      else byAttempt.set(e.attempt_id, [e])
    }
  }

  const groups: Group[] = [{ key: '@preface', attempt: null, label: 'Before any attempt', events: preface }]
  const ordered = [...t.attempts].sort((x, y) => x.created_at.localeCompare(y.created_at))
  ordered.forEach((a, i) => {
    // THE DETAIL PANE'S NAME FOR THE SAME ATTEMPT (AG-21). This was
    // `Attempt · gen N` -- no ordinal -- beside a detail pane that said
    // `Attempt 1` and `Attempt 1 · generation 1` for it.
    const label = attemptLabel(i + 1, a.generation)
    groups.push({ key: a.attempt_id, attempt: a, label, events: byAttempt.get(a.attempt_id) ?? [] })
    byAttempt.delete(a.attempt_id)
  })
  // What is left names an attempt no document on this page describes. Folding these into
  // the preface would file real attempt events under "before any attempt", a lie.
  for (const [id, events] of byAttempt) {
    groups.push({ key: id, attempt: null, label: `Attempt ${id} · no document`, events })
  }
  return groups
}

/**
 * WHAT LEFT THIS SCREEN, AND WHERE IT WENT.
 *
 * Three paragraphs stood between the toolbar and the first attempt card, and a
 * fourth under the last one:
 *
 *   "The event history could not be read. A failed read, not a task with no
 *    events..."                                    -> a `not read` mark
 *   "Zero events came back, yet a task is written with its submitted event in
 *    the same batch..."                            -> a `not read` mark
 *   "The events endpoint orders oldest-first, caps the page server-side and
 *    returns no page token..."                     -> #help/partial-read
 *   "Every figure on these cards is that attempt's own: the task's
 *    result_summary is written once..."            -> #help/attempt-documents
 *
 * The first two are facts about THIS read and keep a visible encoding; the
 * last two are invariants of the route and the platform, true of every task
 * there has ever been, which is the material §8.4(5) sends to the `?`. What is
 * on the glass is the toolbar: a count, a coverage fraction, and one mark per
 * distinct absence.
 */
function Body({ t }: { t: AttemptTimeline }) {
  const groups = grouped(t)
  const blind = groups.filter((g) => g.attempt !== null && g.events.length === 0)
  const count = t.events?.length ?? 0
  return (
    <>
      <div className="ctl-toolbar">
        <span className="ctl-eyebrow">attempts</span>
        <span className="count-chip">{t.attempts.length}</span>
        {t.events === null ? (
          <span className="is-end ctl-card-note">
            <Mark
              kind="unread"
              say={`The event history could not be read. This is a failed read, not a task with no events; the attempt cards below are complete. ${t.eventsDetail ?? ''}`}
            />{' '}
            events unread
          </span>
        ) : (
          <span className="is-end ctl-card-note">
            {/* create_tasks writes the task and its `submitted` event in ONE
                batch (store.py), so zero events is a failed query wearing a
                success code -- which is a different mark from a page that is
                merely short.

                A PAGE THAT IS MERELY SHORT GETS NO MARK (AG-9). It drew
                `pending` -- the word `reading`, the dotted in-flight shape --
                permanently, on a toolbar whose read had landed. `reading` is
                the one mark of the six that means "still asking"
                (design-system.md §8.7.1); that the route pages oldest-first
                with no token is a standing caveat about the route, not a read
                in flight. The figures are the qualifier and the caveat is
                their accessible name. Blind attempts, below, are what this
                page can PROVE is missing, and they keep the `partial` mark. */}
            {count === 0 ? (
              <>
                <Mark
                  kind="unread"
                  say="Zero events came back, yet a task is written with its submitted event in the same batch — so this is a failed query, not an empty history."
                />{' '}
                {count} ev · no page token
              </>
            ) : (
              <span
                className="att-ev-cap"
                aria-label={`${count} events, one page, oldest first. The events endpoint caps the page server-side and returns no page token, so newer events may exist and are unreachable from this screen.`}
              >
                {count} ev · no page token
              </span>
            )}
            {blind.length > 0 && (
              <>
                {' · '}
                <Mark
                  kind="partial"
                  say={`${blind.length} attempt${blind.length === 1 ? ' has' : 's have'} no events on this page. Past one page the newest events — everything belonging to those attempts — cannot be fetched at all.`}
                />{' '}
                {blind.length} blind
              </>
            )}
          </span>
        )}
        {/* THIS SCREEN'S ONE `?` (B7.4), HOISTED OUT OF THE BRANCH IT USED TO
            SIT IN. It was inside the "events were read" arm, so the reader who
            most needed it -- the one looking at `events unread` -- was the one
            it was not drawn for. On the toolbar it renders on every path, in
            the same place, beside every mark this screen can draw.
            It stays a glyph because what it holds cannot be a label: the events
            endpoint pages oldest-first and returns no page token, so "this is
            everything" is a claim this screen is never entitled to make and a
            reader has no way to derive that from the counts in front of them.
            `prose.runs.test.tsx` pins it to this toolbar. */}
        <HelpCard topic="partial-read" />
      </div>
      {groups.map((g) => (
        <AttemptCard key={g.key} g={g} eventsRead={t.events !== null} />
      ))}
      <p className="ctl-card-foot">
        <span>reading these cards:</span>
        <a href="#help/attempt-documents">When an attempt document exists</a>
        <a href="#help/absent-vs-zero">Absent is not zero</a>
      </p>
    </>
  )
}

function AttemptCard({ g, eventsRead }: { g: Group; eventsRead: boolean }) {
  const a = g.attempt
  const out = a === null ? null : outcome(a)
  const ranText = a === null ? null : ran(a)
  return (
    <section className="ctl-card att-card">
      <div className="ctl-card-head">
        {/* THE OUTCOME IS A STATE, SO IT IS A CHIP (§6.6). Both of these were
            `.tag`: a bordered box, 13px mono 600, UPPERCASE and tracked. Four
            attempt cards put eight of them on one pane, and `OOM NEAR MISS`
            was wide enough to break its own box onto a second line -- a chip
            whose label wraps is a paragraph with a border. As chips they are a
            mark and a lowercase word, in the same vocabulary as the run's own
            state two panes away. */}
        <h2 className="ctl-card-title">
          {g.label}
          {out && <Chip tone={out.tone}>{out.label}</Chip>}
          {a?.oom_near_miss && <Chip tone="bad">OOM near miss</Chip>}
        </h2>
        <span className="ctl-card-note">
          {g.events.length} ev{a !== null && ` · ${a.backend}`}
        </span>
      </div>

      <div className="ctl-card-body">
        {a && (
          // FIVE `<dt>/<dd>` PAIRS BECAME ONE STRIP, and every absence keeps
          // its key and its slot. A row that disappears when its value is null
          // is indistinguishable from a row that was never going to be there.
          <ul className="ctl-facts">
            <li className="ctl-fact">
              <b>att</b>
              <span className="mono">{a.attempt_id}</span>
            </li>
            <li className={`ctl-fact${ranText === null ? ' is-absent' : ''}`}>
              <b>ran</b>
              {ranText ?? (
                <>
                  <Em />{' '}
                  <Mark
                    kind={a.started_at === null ? 'zero' : 'absent'}
                    say={
                      a.started_at === null
                        ? 'This attempt was created and nothing ever ran, so there is no duration to measure. That is a fact about the attempt rather than a missing record.'
                        : 'This attempt has a start time and no readable finish time, so how long it ran cannot be computed from what was recorded.'
                    }
                  />
                </>
              )}
            </li>
            {/* A null exit code means still running or never finished. It is
                NOT a zero, and the chip in the heading says which of the
                two -- so the cell carries the em dash and nothing else. */}
            <li className={`ctl-fact${a.exit_code === null ? ' is-absent' : ''}`}>
              <b>exit</b>
              {a.exit_code === null ? <Em /> : num(a.exit_code)}
            </li>
            <li className={`ctl-fact${a.peak_rss_bytes === null ? ' is-absent' : ''}`}>
              <b>rss</b>
              {a.peak_rss_bytes === null ? (
                <>
                  <Em />{' '}
                  <Mark
                    kind="absent"
                    say="Peak RSS is written at the end of an attempt. This attempt has none recorded, so what it used is unknown — it is a missing record, never a zero."
                  />
                </>
              ) : (
                // `bytesLabel`, AS THE DETAIL PANE PRINTS IT (AG-21). This
                // was `/ 1e9` and `GB`: the same attempt read `18.6 MiB` in
                // one pane and `0.02 GB` in the other -- decimal against the
                // binary GiB the ceilings are in, and two figures a reader has
                // to convert before they can believe they agree.
                bytesLabel(a.peak_rss_bytes)
              )}
            </li>
            <li className="ctl-fact">
              <b>ckpt</b>
              {a.checkpoints.length}
            </li>
          </ul>
        )}

        {a?.error != null && <pre className="err full">{a.error}</pre>}

        {g.events.length === 0 && eventsRead && (
          <p className="att-none">
            <Mark
              kind="partial"
              say="No event on this page belongs to this attempt — the page ends before them, or none were written. The two cannot be told apart from here."
            />{' '}
            none on this page
          </p>
        )}
        {g.events.length > 0 && (
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
      </div>
    </section>
  )
}

/**
 * The outcome chip. Exit 0 is the only success; a null exit code is three different things
 * depending on what else the document says -- never a zero, and never a failure.
 */
function outcome(a: AttemptRow): { label: string; tone: ChipTone } {
  if (a.exit_code === 0) return { label: 'exit 0', tone: 'ok' }
  // `bad`, not `.tag`'s `full`. `full` was a pool word borrowed for a failure
  // colour; the chip vocabulary names the thing it means, and `is-bad` is the
  // diamond -- the one mark on the screen with corners.
  if (a.exit_code !== null) return { label: `exit ${a.exit_code}`, tone: 'bad' }
  // NEVER STARTED IS NOT A FAILURE AND NOT A ZERO. `unknown` is the hollow
  // ring: the state exists and there is no outcome to report, which is exactly
  // what the tone is reserved for (§1.3, §6.6).
  if (a.started_at === null) return { label: 'never started', tone: 'unknown' }
  // TWO WORDS, NOT A CLAUSE. "running, no exit code yet" and "ended with no
  // exit code recorded" were sentences inside a chip; the tone and the `exit`
  // fact beside them already carry which of the two this is, and a chip whose
  // label wraps to a second line is a paragraph with a border.
  if (a.completed_at === null) return { label: 'running', tone: 'live' }
  return { label: 'no exit code', tone: 'wait' }
}

/**
 * How long it ran, or null when that cannot be computed.
 *
 * NULL RATHER THAN A SENTENCE. "Never started — created, then nothing ran."
 * was a string in a value slot, which is the one thing this console must not
 * do: a value slot holds a measurement or it holds the absence encoding, never
 * a narration of why there is no measurement. The caller draws `—` plus a mark
 * and the narration is the mark's accessible name.
 */
function ran(a: AttemptRow): string | null {
  if (a.started_at === null) return null
  if (a.completed_at === null) return `${timeAgo(a.started_at)} · open`
  const ms = new Date(a.completed_at).getTime() - new Date(a.started_at).getTime()
  if (!Number.isFinite(ms)) return null
  // `formatDuration`, the one duration formatter (AG-21). `769s` here beside
  // `12m 49s` for the same attempt in the detail pane was two formatters for
  // one number -- the disagreement `formatDuration`'s own comment exists to
  // prevent.
  return formatDuration(ms)
}
