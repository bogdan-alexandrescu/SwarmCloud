import { useCallback } from 'react'
import { Em, Mark, attemptLabel, isParked, parkedOutcome, type ChipTone } from './AgentDetail'
import { Count, ToneMark } from './components'
import { loadAgentDetail, loadAttempts } from './api'
import { instant, spanText } from './duration'
import { eventKind, isTerminalEvent } from './events'
import { num, type Result } from './fetch'
import { HelpCard } from './HelpCard'
import { Screen, timeAgo } from './Shell'
import { REASON_COPY, TERMINAL_STATES, bytesLabel, type AttemptRow, type Task, type TaskEvent } from './types'

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
  /**
   * The task, from the same read as the events. Null when that read failed:
   * the attempt cards stand without it, and the one thing it is read for --
   * whether a finished task's terminal event is on this page -- is then not
   * claimed either way.
   */
  task: Task | null
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
    task: haveEvents ? detail.data.task : null,
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
        // ONE `real zero`, THE PRIMITIVE'S. `No attempt · real zero` said it a
        // second time beside the mark `Absent` draws in the heading (#145).
        heading: 'No attempt',
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
 *   "The events endpoint orders oldest-first and caps the page server-side,
 *    so past one page..."                          -> #help/event-paging
 *   "Every figure on these cards is that attempt's own: the task's
 *    result_summary is written once..."            -> #help/attempt-documents
 *
 * (The third paragraph also said the endpoint could not page at all, which
 * went false with #19: the route returns `next_page_token` and takes
 * `order=desc`. What is true is that THIS screen reads one page,
 * oldest-first, and does not follow the page token -- which is what
 * `event-paging` says, and the topic the toolbar's `?` opens since AG-19.)
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
  const prev = previousOf(t.events ?? [])
  // THE FULL TIMELINE LIVES HERE NOW (#101), so the proof Details drew with
  // it comes too: a terminal task always writes a terminal event, so a
  // finished task whose page carries none proves the page ends before the
  // run did. Details keeps the same mark over its compact form.
  const endMissing =
    t.task !== null && t.events !== null && TERMINAL_STATES.has(t.task.state) && !t.events.some(isTerminalEvent)
  const lastEvent = t.events?.[t.events.length - 1]
  return (
    <>
      <div className="ctl-toolbar">
        {/* AH-24: AFTER THE LABEL, NEVER AFTER A VALUE. The toolbar's `?` sat at
            its far end, after the event counts -- `12 ev · first page · 1
            blind ?` -- and read as a footnote on the last figure. It follows
            the toolbar's label now. Why it is here is the note at the end. */}
        <span className="ctl-eyebrow">
          attempts
          <HelpCard topic="event-paging" />
        </span>
        <Count n={t.attempts.length} label="attempts" bare />
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
                (design-system.md §8.7.1); that this screen reads one page,
                oldest-first, and does not follow the route's page token is a
                standing caveat about this screen, not a read in flight. The figures are the qualifier and the caveat is
                their accessible name. Blind attempts, below, are what this
                page can PROVE is missing, and they keep the `partial` mark. */}
            {count === 0 ? (
              <>
                <Mark
                  kind="unread"
                  say="Zero events came back, yet a task is written with its submitted event in the same batch — so this is a failed query, not an empty history."
                />{' '}
                {count} ev · first page
              </>
            ) : (
              <span
                className="att-ev-cap"
                aria-label={`${count} events, the first page, oldest first. This screen reads one page and does not follow the page token the events route returns, so newer events may exist that it has not fetched.`}
              >
                {count} ev · first page
              </span>
            )}
            {blind.length > 0 && (
              <>
                {' · '}
                <Mark
                  kind="partial"
                  say={`${blind.length} attempt${blind.length === 1 ? ' has' : 's have'} no events on this page. Past one page the newest events — everything belonging to those attempts — are not fetched by this screen.`}
                />{' '}
                {blind.length} blind
              </>
            )}
            {endMissing && t.task !== null && (
              <>
                {' · '}
                <Mark
                  kind="partial"
                  say={`The task is ${t.task.state.toLowerCase()} and a terminal task writes a terminal event — none is on this page. This screen reads one page of events, oldest-first, and does not follow the page token the events route returns, so the newest events are not on it. The end of this task's history is missing, not absent.`}
                />{' '}
                {lastEvent === undefined ? 'ends early' : `ends at ${eventKind(lastEvent)}`}
              </>
            )}
          </span>
        )}
        {/* THIS SCREEN'S ONE `?` (B7.4), HOISTED OUT OF THE BRANCH IT USED TO
            SIT IN. It was inside the "events were read" arm, so the reader who
            most needed it -- the one looking at `events unread` -- was the one
            it was not drawn for. On the toolbar it renders on every path, in
            the same place, beside every mark this screen can draw.
            It stays a glyph because what it holds cannot be a label: this
            screen reads one page of events, oldest-first, and does not follow
            the page token the route returns (#19), so "this is everything" is
            a claim it is never entitled to make and a reader has no way to
            derive that from the counts in front of them.
            `prose.runs.test.tsx` pins it to this toolbar's label (AH-24).

            IT OPENS `event-paging` (AG-19). It opened `partial-read`, whose
            card is "One message belongs to one failure" -- a topic about
            something else, beside a toolbar about paging. `event-paging` is
            built from this toolbar's own `say` strings. */}
      </div>
      {groups.map((g) => (
        <AttemptCard key={g.key} g={g} eventsRead={t.events !== null} prev={prev} />
      ))}
      <p className="ctl-card-foot">
        <span>reading these cards:</span>
        <a href="#help/attempt-documents">When an attempt document exists</a>
        <a href="#help/absent-vs-zero">Absent is not zero</a>
      </p>
    </>
  )
}

function AttemptCard({
  g,
  eventsRead,
  prev,
}: {
  g: Group
  eventsRead: boolean
  /** Each event's predecessor on the page, by event id (`previousOf`). */
  prev: ReadonlyMap<string, TaskEvent>
}) {
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
          {out && <ToneMark tone={out.tone}>{out.label}</ToneMark>}
          {a?.oom_near_miss && <ToneMark tone="bad">OOM near miss</ToneMark>}
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
            {/* THE LEASE AND THE EXECUTION LIVE HERE (agent-details-v3.html
                A): per-attempt ids are this tab's subject, and Details
                printed them as three mono lines on every attempt card. */}
            <li className="ctl-fact">
              <b>lease</b>
              <span className="mono">{a.lease_id}</span>
            </li>
            <li className={`ctl-fact${a.execution_name === null ? ' is-absent' : ''}`}>
              <b>exec</b>
              {a.execution_name === null ? (
                <>
                  <Em />{' '}
                  <Mark
                    kind="zero"
                    say="This attempt was never dispatched, so no execution was ever named. That is a fact about the attempt, not a missing record."
                  />
                </>
              ) : (
                <span className="mono">{a.execution_name}</span>
              )}
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
            {/* `peak rss` (G2-11): the figure is the sampled high-water mark,
                written at exit -- `rss` alone read as the value at exit. */}
            <li className={`ctl-fact${a.peak_rss_bytes === null ? ' is-absent' : ''}`}>
              <b>peak rss</b>
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

        {/* A PARKED ATTEMPT'S `error` IS ITS PARK REASON, NOT A STDERR TAIL
            (#163), so it does not go in the red `pre`. The token is already
            in the chip; the line under it is the reason's copy, and only when
            this screen has copy for it -- `reasonCopy`'s raw-token fallback
            would print the chip's word a second time. */}
        {a?.error != null && !isParked(a) && <pre className="err full">{a.error}</pre>}
        {a !== null && isParked(a) && a.error != null && REASON_COPY[a.error] !== undefined && (
          <p className="blocker-copy">{REASON_COPY[a.error]}</p>
        )}

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
                {/* `eventKind`, as Details names it: a stored `cancelled` that
                    was only the request reads `cancel_requested`. */}
                <span className="ev-type">{eventKind(e)}</span>
                <EventWhen e={e} prev={prev.get(e.event_id) ?? null} />
                {/* The fencing generation the event was written under. A stale
                    worker's events carry the OLD one -- that is how a reclaim
                    reads here. */}
                {e.generation !== null && (
                  <span className="ev-gen" title="Fencing generation for this event">
                    gen {e.generation}
                  </span>
                )}
                {/* Only the reconciler labels itself. THE TEST IS THE VALUE,
                    NOT THE KEY: the worker's quota_exhausted events also carry
                    a detail.source, describing where the quota signal came
                    from, so a presence check badges them as reconciler work. */}
                {e.detail?.['source'] === 'reconciler' && <span className="ev-badge">reconciler</span>}
                {/* THE JSON IS BEHIND A CLOSED DISCLOSURE (#101). Every
                    event's full detail, open, made the sequence -- which is
                    what a timeline is read for -- a scroll through braces. */}
                {e.detail && Object.keys(e.detail).length > 0 && (
                  <details className="ev-more">
                    <summary>detail</summary>
                    <pre className="ev-detail">{JSON.stringify(e.detail, null, 2)}</pre>
                  </details>
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
  // PARKED IS NOT A FAILURE (#163). The worker ends a parked attempt with
  // `exit_code: 75` and the ParkReason in `error`, and without this branch the
  // line below drew it as `exit 75` in the failure tone -- a quota park read
  // as a crash. `info` is the neutral bar: a fact about the attempt, not a
  // verdict on it. The reason is printed verbatim, the token the platform
  // wrote, as the detail pane prints `park_reason`.
  if (isParked(a)) return parkedOutcome(a)
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
  const done = instant(a.completed_at)
  const started = instant(a.started_at)
  if (done === null || started === null) return null
  // `spanText`, the one duration formatter the inspector draws with (AG-21,
  // #102). `769s` here beside `12m 49s` for the same attempt in the detail
  // pane was two formatters for one number; the Details card reads the same
  // helper over the same two instants, so the two panes cannot round apart.
  return spanText(done - started)
}

// ---------------------------------------------------------------------------
// When an event happened, as a sequence (#101)
// ---------------------------------------------------------------------------

/**
 * Each event's predecessor, by event id, in the order the page holds them --
 * which is the route's `at` ascending. ACROSS attempt groups, not within one:
 * the gap between a task's `submitted` and its first attempt's `started` is
 * the queue wait, and it is the gap a reader most wants.
 */
function previousOf(events: readonly TaskEvent[]): Map<string, TaskEvent> {
  const out = new Map<string, TaskEvent>()
  for (let i = 1; i < events.length; i += 1) {
    const e = events[i]
    const p = events[i - 1]
    if (e !== undefined && p !== undefined) out.set(e.event_id, p)
  }
  return out
}

/**
 * A gap between two events: `+3m 09s`.
 *
 * THE SECONDS ARE PADDED, AND ONLY HERE. A column of gaps is read down, and
 * `+3m 9s` over `+3m 10s` does not line up; a duration standing alone
 * (`spanText`) does not need to. The rounding is `spanText`'s -- whole
 * seconds, a measured sub-second gap kept in milliseconds -- so a gap and a
 * `ran` over the same two instants never disagree by one.
 */
export function gapText(ms: number): string {
  if (!Number.isFinite(ms)) return '—'
  const sign = ms < 0 ? '−' : '+'
  const a = Math.abs(ms)
  if (a < 60_000) return `${sign}${spanText(a)}`
  const s = Math.round(a / 1000)
  const m = Math.floor(s / 60)
  if (m < 60) return `${sign}${m}m ${String(s % 60).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${sign}${h}h ${String(m % 60).padStart(2, '0')}m`
  return `${sign}${spanText(a)}`
}

/**
 * An instant as recorded, in UTC, to the second: `2026-09-22 10:02:09 UTC`.
 * UTC because the event document is, and a reader matching this against a
 * log line should not have to know which zone the browser is in.
 */
export function absTime(iso: string): string {
  const t = instant(iso)
  if (t === null) return iso
  return `${new Date(t).toISOString().slice(0, 19).replace('T', ' ')} UTC`
}

/**
 * WHEN, AS THE GAP FROM THE EVENT BEFORE, with the absolute time in `title`
 * and -- because a title is a hover a keyboard never gets -- in a span the
 * sheet shows while the element has focus. The first event on the page has
 * no predecessor, so it keeps its age.
 */
export function EventWhen({ e, prev }: { e: TaskEvent; prev: TaskEvent | null }) {
  const at = instant(e.at)
  const before = prev === null ? null : instant(prev.at)
  const abs = absTime(e.at)
  return (
    <time className="ev-at" dateTime={e.at} title={abs} tabIndex={0}>
      {at !== null && before !== null ? gapText(at - before) : timeAgo(e.at)}
      <span className="ev-abs">{abs}</span>
    </time>
  )
}
