import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Chip, Em } from './AgentDetail'
import { loadCheckpoints, loadTaskLogs } from './api'
import { CheckpointBrowser } from './CheckpointBrowser'
import { Absent, Mark } from './primitives'
import { FailedPanel } from './Shell'
import type { Result } from './fetch'
import {
  bytesLabel,
  CONCURRENCY_STATES,
  timeAgo,
  type AttemptRow,
  type CheckpointFile,
  type CheckpointRecord,
  type CheckpointsPage,
  type LogStream,
  type Task,
  type TaskLogs,
} from './types'

/**
 * CHECKPOINTS AND LOGS, READ FROM THE ROUTES THAT SERVE THEM.
 *
 * THIS IS THE FIRST CALLER OF EITHER LOADER. `loadCheckpoints` (api.ts) and
 * `loadTaskLogs` (api.ts) were written in full -- query building, byte-offset
 * paging, the three-state `status` discipline, a comment explaining why no
 * empty predicate is passed -- and NOTHING HAS EVER CALLED THEM. The routes
 * behind them exist too (`GET /v1/tasks/{id}/checkpoints`,
 * `GET /v1/tasks/{id}/logs`, with `tests/unit/control_plane/
 * test_checkpoint_read_path.py` and `test_log_read_path.py` over both). Both
 * ends of the seam were built and the middle was never joined, which is the
 * defect class this repository keeps producing: nothing about it looks broken.
 *
 * It also retires a claim the product was making that had stopped being true.
 * `AgentDetail`'s log footnote said "there is no log-tail read path on the API,
 * so a running agent's output is not readable here at all". There is one, it
 * serves a live window, and this panel reads it.
 *
 * THE THREE-STATE RULE, which both payloads are built around and which this
 * file must not collapse:
 *
 *   content: null  absent or unreadable. NEVER rendered as text.
 *   content: ''    an object that exists and is empty. A real answer.
 *   resumable: null   the manifest could not be read, so nothing is known.
 *   resumable: false  the manifest was read and says it cannot be resumed.
 *
 * `content ?? ''` and `resumable ?? false` are the two lines that would undo
 * all of it, and neither appears here.
 *
 * ONE LEVEL OF BOX (AG-23, owner decision 2026-09-25). Both panels were boxes
 * inside boxes: a bordered `.ckpt` card per checkpoint and a bordered
 * `.logwin` card per stream, each with a legacy `dl.kv` of label/value rows,
 * a `<details>` holding a second `dl.kv`, and sentences in between -- the
 * measured-empty stream among them, written out as a paragraph where the kit
 * has a mark for exactly that. The decision: flatten onto the shared
 * primitives; files as a table (name, size, age); zero, absent and partial as
 * the kit's marks; and keep only the sentences that state a fact the table
 * cannot. So each panel is now a facts strip, ONE `.ctl-table` -- the one box
 * a panel gets (design-system.md §13.3) -- and a kit mark in any cell whose
 * value is a kind of nothing. The sentences that stay are the two no cell can
 * hold: a checkpoint written and since reclaimed, and a restore pointer that
 * names nothing the listing holds.
 */
export function RunFiles({
  task,
  attempts,
  readAt = null,
  now = null,
}: {
  task: Task
  /**
   * The run's attempt documents, which record every checkpoint each attempt
   * wrote. NULL (or not passed) means they were not read, and then an empty
   * listing is a zero of OBJECTS only -- see `CheckpointsPanel`.
   */
  attempts?: readonly AttemptRow[] | null
  /**
   * When the drawer last read the run (`ScreenReading.fetchedAt`). Both panels
   * re-read when it moves. NULL (or not passed) means nothing re-reads the run
   * -- the acceptance test renders `Run` with no `Screen` -- and the panels
   * read once.
   */
  readAt?: number | null
  /**
   * The drawer's clock, so a checkpoint's age ticks with every other age in
   * the inspector. NULL (or not passed) reads the wall clock at render.
   */
  now?: number | null
}) {
  return (
    <>
      <CheckpointsPanel task={task} attempts={attempts ?? null} readAt={readAt} now={now} />
      <LogsPanel task={task} readAt={readAt} now={now} />
    </>
  )
}

/**
 * An answer, and what was true when it was asked for.
 *
 * `asked` is captured when the read STARTS. The checkpoint panel compares its
 * listing with attempt records, and the two are only comparable when the
 * records were read no later than the listing: the worker uploads a
 * checkpoint before it records it, so anything recorded by then is in the
 * bucket by the time the listing looks. Records read AFTER the listing can
 * name a checkpoint that did not exist when it looked.
 */
interface Held<T, C> {
  task: string
  state: Result<T>
  asked: C
}

/**
 * One read, with its four states kept apart -- and read again whenever the
 * drawer reads again.
 *
 * `Screen` in Shell.tsx owns this for a whole route and draws an `<h1>`, so it
 * cannot be nested inside a panel. This is the same discipline at panel scale:
 * a failure is never rendered as an absence, and a component below it can only
 * be reached with data.
 *
 * IT READ ONCE PER TASK, and the drawer around it now re-reads every 10s. The
 * two panels were the only part of the drawer frozen at open, under a sub-line
 * (`read 3s ago · every 10s`) that reads as covering all of it: the log panel
 * went on saying a task had no attempt beside a RUNNING chip, and the
 * checkpoint listing read at open was compared with attempt records from the
 * latest poll, so every checkpoint written while the drawer was open was drawn
 * as reclaimed. `key` is the drawer's read, so each panel re-reads with it.
 *
 * THREE RULES, each for a failure the naive version has:
 *
 *  - A new read of the SAME task leaves the previous answer on screen until
 *    the next answer replaces it -- whatever that answer is, a failure
 *    included -- as `Screen` does for a poll. Blanking to `Reading…` every
 *    10s would make both panels flash.
 *  - A DIFFERENT task never shows the previous task's answer, even for a
 *    frame: it is `loading` until its own read lands.
 *  - ONE READ IN FLIGHT AT A TIME. A listing slower than the drawer's 10s
 *    would otherwise stack a request per poll, and cancelling the older one
 *    instead would mean a listing slower than 10s never lands at all. When a
 *    read lands and the drawer has read again meanwhile, one more read starts,
 *    with what is true then.
 */
function useRead<T, C = null>(
  load: () => Promise<Result<T>>,
  task: string,
  key: string,
  asked: C,
): Held<T, C> {
  const [held, setHeld] = useState<Held<T, C>>(() => ({
    task,
    state: { status: 'loading', since: Date.now() },
    asked,
  }))
  // What the NEXT read should use: this render's loader, key and context.
  const latest = useRef({ load, task, key, asked })
  latest.current = { load, task, key, asked }
  /** The key of the read in flight, or null when none is. */
  const inFlight = useRef<string | null>(null)
  const mounted = useRef(true)

  // Typed, because it calls itself: a read that lands after the drawer moved
  // on starts the next one.
  const start: () => void = useCallback(() => {
    const ask = latest.current
    inFlight.current = `${ask.task}:${ask.key}`
    ask.load().then((next) => {
      if (!mounted.current) return
      inFlight.current = null
      setHeld({ task: ask.task, state: next, asked: ask.asked })
      const now = latest.current
      if (now.task !== ask.task || now.key !== ask.key) start()
    })
  }, [])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (inFlight.current === null) start()
  }, [task, key, start])

  return held.task === task ? held : { task, state: { status: 'loading', since: Date.now() }, asked }
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="section">
      <h2>{title}</h2>
      {children}
    </section>
  )
}

function Reading() {
  return <p className="muted">Reading…</p>
}

// ---------------------------------------------------------------------------
// Checkpoints
// ---------------------------------------------------------------------------

function CheckpointsPanel({
  task,
  attempts,
  readAt,
  now,
}: {
  task: Task
  attempts: readonly AttemptRow[] | null
  readAt: number | null
  now: number | null
}) {
  // `records` ARE THE ATTEMPTS AS THEY WERE WHEN THIS LISTING WAS ASKED FOR,
  // not as the latest drawer read has them -- see `Held`. Every comparison
  // below is between a listing and records read no later than it.
  const { state, asked: records } = useRead<CheckpointsPage, readonly AttemptRow[] | null>(
    () => loadCheckpoints(task.id),
    task.id,
    `${readAt ?? ''}`,
    attempts,
  )

  if (state.status === 'loading') {
    return (
      <Panel title="Checkpoints">
        <Reading />
      </Panel>
    )
  }
  if (state.status === 'error') {
    return (
      <Panel title="Checkpoints">
        {/* A failed listing is NOT "this task has no checkpoints". The route
            answers 503 for a listing that failed precisely so the two stay
            apart, and drawing an empty panel here would throw that away. */}
        <FailedPanel error={state.error} onRetry={() => window.location.reload()} />
      </Panel>
    )
  }
  // `loadCheckpoints` passes no empty predicate, so `empty` is unreachable --
  // but `Result` has five members and a switch that ignores one is how a state
  // goes unhandled. An explicit branch, with the reason, rather than a cast.
  // A body the route did not send is not a listing, so it is the `not read`
  // mark: nothing can be concluded from it, least of all a zero.
  if (state.status === 'empty') {
    return (
      <Panel title="Checkpoints">
        <Absent
          kind="failed"
          heading="No listing"
          say="The checkpoint route answered with no body. That is not a listing result, and nothing can be concluded from it."
        />
      </Panel>
    )
  }

  const page = state.data
  // A LISTING READ TO ITS END is the only one whose absences mean anything:
  // past a scan cut or on another page, a checkpoint the records name may
  // simply not have been reached.
  const whole = page.listed && !page.truncated && page.next_page_token === null
  const lost = whole ? lostCheckpoints(page, records) : []
  const cut = page.truncated || page.next_page_token !== null
  return (
    <Panel title="Checkpoints">
      {/* THE LINE ABOVE THE TABLE IS A FACTS STRIP (§6.13). It was a muted
          paragraph -- `Prefix <path> · N of M found · the scan limit cut the
          prefix short, so older checkpoints exist beyond these · more pages
          remain` -- and the pointer's sentence under it. The count is the
          fact; that it is not the whole prefix is the `partial` mark, with
          the reason as its accessible name. */}
      <ul className="ctl-facts">
        <li className="ctl-fact">
          <b>prefix</b>
          {/* `ckpt-prefix` IS THE HOOK THE SHEET WRAPS (AG-26). A task's
              checkpoint prefix is one unbroken path with no space to break at,
              and in the 480px inspector it ran out of the drawer. */}
          <code className="mono ckpt-prefix">{page.prefix}</code>
        </li>
        <li className="ctl-fact">
          <b>found</b>
          {page.count} of {page.total_found}
          {cut && (
            <>
              {' '}
              <Mark
                kind="partial"
                say={[
                  page.truncated
                    ? 'The scan limit cut the prefix short, so older checkpoints exist beyond these.'
                    : null,
                  page.next_page_token !== null ? 'More pages of this listing remain unread.' : null,
                ]
                  .filter((x): x is string => x !== null)
                  .join(' ')}
              />
            </>
          )}
        </li>
        <PointerFact page={page} />
      </ul>

      <PointerFinding page={page} />

      {lost.length > 0 && <Lost lost={lost} />}

      {page.checkpoints.length === 0 ? (
        lost.length > 0 ? null : (
          <EmptyListing page={page} whole={whole} attempts={records} />
        )
      ) : (
        <CheckpointTable taskId={task.id} rows={page.checkpoints} now={now} />
      )}
    </Panel>
  )
}

/**
 * WHAT AN EMPTY LISTING MAY BE CALLED, when no record names a checkpoint it
 * lost -- and it is the shared empty state, with the kit's mark, rather than
 * a muted sentence (AG-23).
 *
 * THE DEFECT this discipline came from, measured on the live inspector on
 * 2026-09-24: the run's figures said `Checkpoints 1`, the pointer line said
 * the pointer named a checkpoint "no longer in the bucket; it may have been
 * reclaimed", and the line under it called the empty listing a real zero of
 * checkpoints written. A listing that finds nothing is a measured zero of
 * OBJECTS. It is a zero of checkpoints WRITTEN only when the listing was read
 * to its end AND the attempt records -- which list every checkpoint each
 * attempt wrote -- were read and name none. A record that does name one is
 * `Lost`, above, and never reaches here.
 *
 * Four answers, four encodings, and one sentence on the glass: that the
 * records themselves are unread, which no heading can carry.
 */
function EmptyListing({
  page,
  whole,
  attempts,
}: {
  page: CheckpointsPage
  whole: boolean
  attempts: readonly AttemptRow[] | null
}) {
  if (!page.listed) {
    return (
      <Absent
        kind="failed"
        heading="Listing incomplete"
        say="The listing did not complete, so whether this task has checkpoints is unknown."
      />
    )
  }
  if (!whole) {
    return (
      <Absent
        kind="partial"
        heading="None before the cut"
        say="The listing was cut before it found one, so nothing is known past the cut. This is not a zero."
      />
    )
  }
  if (attempts === null) {
    return (
      <Absent
        kind="partial"
        heading="None listed"
        say="The listing found no checkpoint, and the attempt records could not be read."
      >
        The attempt records could not be read, so whether one was written and since reclaimed is unknown.
      </Absent>
    )
  }
  return (
    <Absent
      kind="zero"
      heading="None written"
      say="The listing succeeded, was read to its end, and no attempt record names a checkpoint: this task has written none."
    />
  )
}

/** A checkpoint a record says was written, which the listing does not hold. */
interface Recorded {
  /** Null when only the pointer names it: the page serves its id, not its attempt. */
  attemptId: string | null
  checkpointId: string
  byAttempt: boolean
  byPointer: boolean
}

/**
 * Every checkpoint a record names that a WHOLE listing does not hold.
 *
 * Two records can name one: each attempt document's `checkpoints` list (the
 * worker appends to it after the upload, and it is what the run's
 * `Checkpoints N` figure counts), and the task's `latest_checkpoint` pointer,
 * whose verdict the route serves as `missing`. Matched on attempt AND id: ids
 * restart per attempt, so `ckpt-00001` exists once for every attempt that
 * checkpointed, and a match on the id alone would call one attempt's
 * checkpoint present because another attempt's is.
 */
function lostCheckpoints(page: CheckpointsPage, attempts: readonly AttemptRow[] | null): Recorded[] {
  const listed = new Set(page.checkpoints.map((c) => `${c.attempt_id}/${c.checkpoint_id}`))
  const lost: Recorded[] = []
  for (const a of attempts ?? []) {
    for (const id of a.checkpoints) {
      if (!listed.has(`${a.attempt_id}/${id}`)) {
        lost.push({ attemptId: a.attempt_id, checkpointId: id, byAttempt: true, byPointer: false })
      }
    }
  }
  const p = page.latest_checkpoint
  if (p.status === 'missing' && p.checkpoint_id !== null) {
    // The same checkpoint said twice when exactly one attempt record lost that
    // id; otherwise the pointer is its own line, because which attempt it
    // names is not served and is not guessed here.
    const id = p.checkpoint_id
    const same = lost.filter((r) => r.checkpointId === id)
    const only = same.length === 1 ? same[0] : undefined
    if (only !== undefined) only.byPointer = true
    else lost.push({ attemptId: null, checkpointId: id, byAttempt: false, byPointer: true })
  }
  return lost
}

/**
 * WRITTEN, THEN RECLAIMED: the absence explained, never a zero. The records
 * say the checkpoint existed and the listing, read to its end, says it does
 * not now. ONE OF THE SENTENCES THIS PANEL KEEPS (AG-23): no row of a table
 * of what IS in the bucket can say what was and no longer is.
 */
function Lost({ lost }: { lost: Recorded[] }) {
  const one = lost.length === 1
  return (
    <p className="ckpt-lost">
      <strong>Written, then reclaimed</strong>
      {': '}
      {lost.map((r, i) => (
        <Fragment key={`${r.attemptId ?? ''}/${r.checkpointId}/${r.byPointer ? 'p' : 'a'}`}>
          {i > 0 && ', '}
          <code className="mono">{r.attemptId === null ? r.checkpointId : `${r.attemptId}/${r.checkpointId}`}</code>
          <span className="muted small">
            {' '}
            ({[r.byAttempt && 'attempt record', r.byPointer && 'restore pointer'].filter(Boolean).join(' and ')})
          </span>
        </Fragment>
      ))}
      . {one ? 'It is' : 'They are'} recorded as written, and the listing, read to its end, no
      longer finds {one ? 'it' : 'them'} in the bucket.
    </p>
  )
}

/**
 * WHAT A RETRY WOULD RESTORE FROM, as the strip's `restore` fact -- which is
 * not always what `task.latest_checkpoint` points at.
 *
 * THE WORKER'S RULE (`_restore_checkpoint`, agent_worker/lifecycle.py): try
 * the pointer with `find_by_uri`, and when that resolves to nothing -- no
 * pointer, a pointer outside this task's prefix, a checkpoint no longer in
 * the bucket -- fall back to `find_latest`, the newest committed checkpoint
 * whose manifest it can read and owns. So:
 *
 *   present   the pointer's own checkpoint, and its table row says `latest`;
 *   otherwise `newest committed` and the listing's newest resumable row,
 *             ordered as the worker orders them (the manifest's `created_at`,
 *             then `seq`), or `none listed`.
 *
 * THIS SAID SOMETHING ELSE, and both were false (fix-up on #170). For
 * `missing` and `outside_this_task` it printed the pointer, which the server
 * itself warns is "a restore source that will never be used" (inspect.py
 * `_pointer_status`) -- and for `outside_this_task` that was a raw `gs://`
 * path in a `span` nothing lets wrap. For `unset` it said "a retry starts
 * from the beginning", which is true only when no committed checkpoint is
 * listed at all: `find_latest` runs whether or not a pointer was ever set.
 *
 * THE ANSWER IS QUALIFIED WHERE THE LISTING CANNOT VOUCH FOR IT, with the
 * kit's marks and never a sentence on the glass: `partial` for a cut listing
 * and for a newer row that is committed but not resumable here (a worker
 * skips one it refuses, but would try one whose archive has gone, and fail);
 * `not read` for a manifest nobody could read, which may be the newest a
 * worker finds.
 */
function PointerFact({ page }: { page: CheckpointsPage }) {
  const p = page.latest_checkpoint
  if (p.status === 'present' && p.checkpoint_id !== null) {
    return (
      <li className="ctl-fact">
        <b>restore</b>
        <code className="mono">{p.checkpoint_id}</code>
      </li>
    )
  }
  const f = restoreFallback(page)
  return (
    <li className="ctl-fact">
      <b>restore</b>
      newest committed ·{' '}
      {f.row === null ? 'none listed' : <code className="mono">{idOf(f.row)}</code>}
      {f.partial.length > 0 && (
        <>
          {' '}
          <Mark kind="partial" say={f.partial.join(' ')} />
        </>
      )}
      {f.unread.length > 0 && (
        <>
          {' '}
          <Mark kind="unread" say={f.unread.join(' ')} />
        </>
      )}
    </li>
  )
}

/** A checkpoint named with its attempt: ids restart per attempt. */
function idOf(c: CheckpointRecord): string {
  return `${c.attempt_id}/${c.checkpoint_id}`
}

/**
 * `find_latest`'s answer, as far as this listing can give it.
 *
 * The worker compares `(created_at, seq)` from each manifest -- `created_at`
 * as the ISO string it is -- so this does exactly that, over the rows whose
 * manifest was read. A row whose manifest was not read has no key and no
 * resumability; it is counted into the `not read` qualifier instead of being
 * ordered by a guess.
 */
function restoreFallback(page: CheckpointsPage): {
  row: CheckpointRecord | null
  partial: string[]
  unread: string[]
} {
  const read = page.checkpoints
    .filter((c) => c.resumable !== null)
    .sort((a, b) => {
      const at = (b.created_at ?? '').localeCompare(a.created_at ?? '')
      return at !== 0 ? at : (b.seq ?? -1) - (a.seq ?? -1)
    })
  const i = read.findIndex((c) => c.resumable === true)
  const row = i === -1 ? null : read[i]!
  const skipped = (i === -1 ? read : read.slice(0, i)).filter((c) => c.resumable === false)

  const partial: string[] = []
  if (page.truncated) {
    partial.push('The scan limit cut this listing short, so a newer committed checkpoint may be past the cut.')
  }
  if (page.next_page_token !== null) {
    partial.push('More pages of this listing remain unread, and a newer committed checkpoint may be on one of them.')
  }
  if (skipped.length > 0) {
    const names = skipped.map(idOf).join(', ')
    partial.push(
      `${names} ${skipped.length === 1 ? 'is' : 'are'} ${row === null ? '' : 'newer and '}not resumable here. A resuming worker takes the newest checkpoint whose manifest it can read and owns, so it may try ${skipped.length === 1 ? 'that one' : 'one of those'} first.`,
    )
  }
  const unread: string[] = []
  if (!page.listed) {
    unread.push('The listing did not complete, so which checkpoint a retry would restore from is unknown.')
  }
  const blind = page.checkpoints.filter((c) => c.resumable === null).length
  if (blind > 0) {
    unread.push(
      `${blind} checkpoint manifest${blind === 1 ? '' : 's'} could not be read here, and a worker that can read ${blind === 1 ? 'it' : 'one'} may restore from ${blind === 1 ? 'it' : 'that one'} instead.`,
    )
  }
  return { row, partial, unread }
}

/**
 * THE POINTER'S FINDINGS, one sentence each -- kept (AG-23) because both are
 * about what a resume would DO, which no cell can say.
 *
 * `outside_this_task` is a FINDING, not a formatting case: a resuming worker
 * ignores such a pointer and falls back to the newest committed checkpoint it
 * can find, so the pointer is not the restore source. The strip's `restore`
 * fact names what is. This sentence said the task "would restart from
 * nothing", in the same sentence as the server's detail saying it falls back
 * -- and that is false whenever a resumable checkpoint is listed.
 *
 * THE SERVER'S DETAIL CONTINUES THE CLAUSE, AFTER A DASH. It is written
 * lowercase ("the pointer names a checkpoint of this task that is no longer
 * in the bucket; ...") and was appended after a full stop, so the panel
 * carried a sentence that began with a lowercase word.
 */
function PointerFinding({ page }: { page: CheckpointsPage }) {
  const p = page.latest_checkpoint
  const tail = p.detail ? <> — {p.detail.replace(/\.$/, '')}.</> : '.'
  switch (p.status) {
    case 'unset':
    case 'present':
      return null
    case 'missing':
      return (
        <p className="rollup untrusted">
          The task points at <code>{p.pointer}</code> and the listing did not find it{tail}
        </p>
      )
    case 'outside_this_task':
      // The server's detail IS the finding when it is sent; the clause after
      // the pointer is ours only when it is not, so it is never said twice.
      return (
        <p className="rollup untrusted">
          The task points at <code>{p.pointer}</code>
          {p.detail ? (
            tail
          ) : (
            <>
              , which is not under this task&apos;s prefix, so a resuming worker ignores it and falls back to the newest
              committed checkpoint it can find.
            </>
          )}
        </p>
      )
  }
}

/**
 * THE CHECKPOINTS, ONE ROW EACH, IN THE ONE BOX THE PANEL GETS -- and their
 * objects, as rows of the same table.
 *
 * THREE COLUMNS, AS DECIDED: name, size, age (AG-23). The first pass added a
 * fourth, Resume; whether a retry would restore from a checkpoint is now a
 * line under its name, beside the other facts about it (fix-up on #170).
 *
 * THE FILES ARE ROWS. A checkpoint is stored as objects -- its manifest and
 * its archive -- and the decision is "files as a table (name, size, age)".
 * They were a disclosure of `name · size` spans inside the Size cell. Opened
 * from that cell, each is now a row directly under its checkpoint, with its
 * own name, size and age; the listing serves no time for an object, so that
 * age is the `not measured` mark (#172 asks the route for it) rather than the
 * checkpoint's age borrowed.
 *
 * `is-stacked` for the reason the attempt panel's checkpoint table is: the
 * inspector is a size container (`ctl-inspector`) never wider than 720px, so
 * each row becomes a stacked record keyed by `data-label`, and the explicit
 * `role`s keep the ARIA table that changing `display` drops.
 *
 * THE FILE BROWSERS OPEN BELOW THE TABLE, NOT INSIDE IT. Each was a bordered
 * inset inside the bordered checkpoint card -- the second level of box the
 * decision removes. Opened from a row, each is a region named for its
 * checkpoint, in row order, and several can be open at once.
 */
function CheckpointTable({
  taskId,
  rows,
  now,
}: {
  taskId: string
  rows: readonly CheckpointRecord[]
  now: number | null
}) {
  const [open, setOpen] = useState<readonly string[]>([])
  const [listed, setListed] = useState<readonly string[]>([])
  const keyOf = (c: CheckpointRecord) => `${c.attempt_id}/${c.checkpoint_id}`
  const flip = (k: string) => (o: readonly string[]) => (o.includes(k) ? o.filter((x) => x !== k) : [...o, k])
  const toggle = (k: string) => setOpen(flip(k))
  return (
    <>
      <div className="ctl-table is-stacked">
        <table role="table">
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">Checkpoint</th>
              <th role="columnheader" scope="col" className="is-num">Size</th>
              <th role="columnheader" scope="col">Age</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {rows.map((c) => (
              <Fragment key={keyOf(c)}>
                <CheckpointRow
                  record={c}
                  now={now}
                  browsing={open.includes(keyOf(c))}
                  onBrowse={() => toggle(keyOf(c))}
                  listing={listed.includes(keyOf(c))}
                  onList={() => setListed(flip(keyOf(c)))}
                />
                {listed.includes(keyOf(c)) && c.objects.map((o) => <ObjectRow key={o.key} object={o} />)}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {rows
        .filter((c) => open.includes(keyOf(c)))
        .map((c) => (
          // What is INSIDE the checkpoint (A3): the tree, one file, the
          // archive. See CheckpointBrowser.tsx; it owns its own reads and its
          // own absent states.
          <CheckpointBrowser
            key={keyOf(c)}
            taskId={taskId}
            attemptId={c.attempt_id}
            checkpointId={c.checkpoint_id}
            onClose={() => toggle(keyOf(c))}
          />
        ))}
    </>
  )
}

/**
 * ONE CHECKPOINT, AS A ROW: name, size, age -- and, under its name, whether a
 * retry would use it, which is the question the old card's five `dl.kv` rows
 * were answering.
 *
 * WHERE THE FIVE ROWS WENT. `Attempt` is the name's second line, and an
 * attempt with no document is the `not measured` mark there rather than a
 * sentence. `Files inside` is on that line too, beside the button that opens
 * them. `Stored` is the size, with the objects it sums one click away as rows
 * of their own. `Manifest` and `Resumable` are the name's third line, `resume`
 * (it was a fourth column; the decision is three): a manifest that did not
 * parse makes resumability unknown, which is the `not read` mark -- NEVER
 * "no" -- and a manifest that was never written is said in three words under
 * the answer. The server's own detail strings are each a line of their own
 * under the value they qualify, never appended after a full stop.
 */
function CheckpointRow({
  record,
  now,
  browsing,
  onBrowse,
  listing,
  onList,
}: {
  record: CheckpointRecord
  now: number | null
  browsing: boolean
  onBrowse: () => void
  /** Whether this checkpoint's objects are open as rows under it. */
  listing: boolean
  onList: () => void
}) {
  const n = record.objects.length
  return (
    <tr role="row">
      <th role="rowheader" scope="row">
        <span className="mono">{record.checkpoint_id}</span>{' '}
        {record.is_latest_pointer && <Chip tone="info">latest</Chip>}{' '}
        <button type="button" className="copy" aria-expanded={browsing} onClick={onBrowse}>
          files
        </button>
        <span className="ctl-sub">
          {record.attempt_id}
          {!record.attempt_known && (
            <>
              {' '}
              <Mark
                kind="absent"
                say="No attempt document was found for this checkpoint's attempt. That is a missing record, not an attempt that never ran."
              />
            </>
          )}
          {record.label !== null && ` · ${record.label}`}
          {' · '}
          {/* FROM THE MANIFEST, so a manifest that did not parse has no count:
              the em dash, never a 0. */}
          {record.file_count === null ? <Em /> : record.file_count} files
        </span>
        {/* THREE VALUES. `resumable ?? false` here would turn "we could not
            tell" into "a retry cannot use this", which is a different and
            much more alarming sentence. */}
        <span className="ctl-sub" data-testid="rf-resume">
          resume{' '}
          {record.resumable === null ? (
            <>
              <Em />{' '}
              <Mark
                kind="unread"
                say="Whether a retry would restore from this checkpoint: cannot tell — the manifest could not be read."
              />
            </>
          ) : record.resumable ? (
            'yes'
          ) : (
            'no'
          )}
        </span>
        {record.manifest === 'absent' && <span className="ctl-sub">manifest never written</span>}
        {record.manifest_detail !== null && <span className="ctl-sub">{record.manifest_detail}</span>}
        {record.resumable_detail !== null && <span className="ctl-sub">{record.resumable_detail}</span>}
      </th>
      <td role="cell" data-label="Size" className="is-num">
        {/* Summed over objects the listing actually saw, so a zero here is
            measured. The objects themselves are rows, one click away. */}
        {bytesLabel(record.stored_bytes)}
        {n > 0 && (
          <span className="ctl-sub">
            <button type="button" className="copy" aria-expanded={listing} onClick={onList}>
              {n} object{n === 1 ? '' : 's'}
            </button>
          </span>
        )}
      </td>
      <td role="cell" data-label="Age">
        {record.created_at === null ? (
          <>
            <Em />{' '}
            <Mark
              kind={record.manifest === 'unreadable' ? 'unread' : 'absent'}
              say={
                record.manifest === 'unreadable'
                  ? 'The manifest carries when this checkpoint was written, and it could not be read, so its age is unknown.'
                  : 'No creation time was recorded for this checkpoint, so its age is unknown.'
              }
            />
          </>
        ) : (
          timeAgo(record.created_at, now ?? Date.now())
        )}
      </td>
    </tr>
  )
}

/**
 * ONE OBJECT OF A CHECKPOINT, AS A ROW OF THE SAME TABLE: name, size, age.
 *
 * The size is the listing's, so it is measured. The age is not served: the
 * checkpoint route lists an object as `{name, key, bytes}` (inspect.py
 * `_checkpoint_row`), although the bucket reports a time for every object.
 * So it is the em dash and the `not measured` mark, never the checkpoint's
 * own age standing in for it. #172 asks the route to serve the time.
 */
function ObjectRow({ object }: { object: CheckpointFile }) {
  return (
    <tr role="row" className="rf-object">
      <th role="rowheader" scope="row">
        <span className="mono">{object.name}</span>
      </th>
      <td role="cell" data-label="Size" className="is-num">
        {bytesLabel(object.bytes)}
      </td>
      <td role="cell" data-label="Age">
        <Em />{' '}
        <Mark
          kind="absent"
          say="The checkpoint listing serves no time for a checkpoint's objects, so this object's age is not known here. It is not the checkpoint's age."
        />
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

function LogsPanel({ task, readAt, now }: { task: Task; readAt: number | null; now: number | null }) {
  // RE-READ WITH THE DRAWER. Read once, this panel said "no attempt yet"
  // beside a RUNNING chip for as long as the drawer stayed open, and kept a
  // `live tail -- the attempt is still writing` header over a task that had
  // finished, without ever fetching the final log. The drawer's read moving
  // is what moves it; when the drawer stops re-reading a finished task, the
  // read that stopped it was the last, and the final log is what it fetched.
  const { state } = useRead<TaskLogs>(() => loadTaskLogs(task.id), task.id, `${readAt ?? ''}`, null)

  if (state.status === 'loading') {
    return (
      <Panel title="Output, as the agent wrote it">
        <Reading />
      </Panel>
    )
  }
  if (state.status === 'error') {
    return (
      <Panel title="Output, as the agent wrote it">
        <FailedPanel error={state.error} onRetry={() => window.location.reload()} />
      </Panel>
    )
  }
  if (state.status === 'empty') {
    return (
      <Panel title="Output, as the agent wrote it">
        <Absent
          kind="failed"
          heading="No stream result"
          say="The log route answered with no body, which is not a stream result, and nothing can be concluded from it."
        />
      </Panel>
    )
  }

  const logs = state.data
  const applied = logs.redaction.applied_at_read_time
  const rules = logs.redaction.rules
  // NO ATTEMPT, NO TABLE (fix-up on #170). The route still names both streams
  // when there is no attempt (`_no_attempt_entry`, inspect.py), each absent
  // and each carrying "this task has no attempt yet, so no log object can
  // exist" -- so the panel said the one fact three times: in the strip, then
  // once per row. It is the empty state's heading, once, and nothing else.
  const noAttempt = logs.attempt.status === 'no_attempt_yet' || logs.attempt.status === 'unknown_attempt'
  return (
    <Panel title="Output, as the agent wrote it">
      <ul className="ctl-facts">
        {!noAttempt && <li className="ctl-fact">{attemptLine(logs)}</li>}
        {/* STATED BY THE SERVER RATHER THAN ASSUMED HERE, and drawn by the rule
            the artifact viewer's masked count follows (AG-5): a measured fact,
            in plain ink when masking ran and in `--warn` when this API says it
            did not -- a deployment where redaction stopped would otherwise
            look identical to a working one. */}
        <li className="ctl-fact">
          <b>masking</b>
          <span className={`art-masked${applied ? '' : ' is-warn'}`}>
            {applied ? `at read time · ${rules} rule${rules === 1 ? '' : 's'}` : 'not applied at read time'}
          </span>
        </li>
      </ul>
      {logs.attempt.status === 'no_attempt_yet' ? (
        // A MEASURED NOTHING: no attempt has run, so no log can exist -- the
        // state a QUEUED or PARKED task is legitimately in.
        <Absent kind="zero" heading="No attempt yet" say="This task has no attempt yet, so nothing has written a log." />
      ) : logs.attempt.status === 'unknown_attempt' ? (
        // Not a zero: the attempt asked for is not one this task has, so
        // nothing about its logs was read.
        <Absent
          kind="failed"
          heading="No such attempt"
          say="The attempt asked for is not one this task has, so there is no log of it to read."
        />
      ) : logs.streams.length === 0 ? (
        // A MEASURED NOTHING. The route answered and named no stream.
        <Absent kind="zero" heading="No stream" say="The log route answered and returned no stream for this attempt." />
      ) : (
        <>
          <div className="ctl-table is-stacked">
            <table role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Stream</th>
                  <th role="columnheader" scope="col" className="is-num">Size</th>
                  <th role="columnheader" scope="col">Age</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {logs.streams.map((s) => (
                  <Stream key={s.stream} stream={s} logs={logs} task={task} now={now} />
                ))}
              </tbody>
            </table>
          </div>
          {/* THE WINDOWS THEMSELVES, below the table and not inside it: the
              table says what each stream is, the window is what the agent
              wrote, and only a stream with text in it has one. */}
          {logs.streams
            .filter((s) => s.status === 'ok' && s.content !== null && s.content !== '')
            .map((s) => (
              <div key={s.stream} className="rf-window">
                <span className="ctl-eyebrow">{s.stream}</span>
                <pre className="logwin-body">{s.content}</pre>
              </div>
            ))}
        </>
      )}
    </Panel>
  )
}

/** Which attempt the window below belongs to. `no_attempt_yet` is the state a
 *  QUEUED or PARKED task is legitimately in and must not read as a failure. */
function attemptLine(logs: TaskLogs): string {
  switch (logs.attempt.status) {
    case 'latest':
      return `Latest attempt${logs.attempt_id ? ` ${logs.attempt_id}` : ''}`
    case 'requested':
      return `Attempt ${logs.attempt_id ?? 'as requested'}`
    case 'unknown_attempt':
      return 'The attempt asked for is not one this task has'
    case 'no_attempt_yet':
      return 'This task has no attempt yet, so there is nothing to have written a log'
  }
}

/**
 * ONE STREAM, AS A ROW: name, size, age.
 *
 * THE FOUR ANSWERS THE ROUTE CAN GIVE ARE FOUR ENCODINGS IN THE SIZE CELL,
 * and each used to be a paragraph:
 *
 *   absent          `not measured`: no object, nothing was ever uploaded
 *   unreadable      `not read`: the object is there and the read failed
 *   ok, no content  `not read`: success with nothing to hand over
 *   ok, ''          `real zero`: the object exists and is empty -- the
 *                   paragraph saying so is now the mark's accessible name
 *
 * The server's `detail` is a line of its own under the value, never appended
 * after a full stop (AG-23).
 *
 * THE AGE, and where each answer comes from (the route serves no object time;
 * #172 asks for it):
 *
 *   final, attempt ended      the attempt's end, which is when the worker
 *                             uploads the final log (`_upload_outputs`);
 *   final, no end recorded    the em dash and `not measured`, never a bare
 *                             dash that reads as nothing to say;
 *   live, still being written the word `live` -- only while the attempt has
 *                             no end AND the task holds a slot;
 *   live, nothing writing it  the em dash and `partial`: `source=auto` serves
 *                             the tail exactly when the final log is absent,
 *                             which is what a worker killed before its upload
 *                             leaves behind, and a parked task's attempt never
 *                             records an end at all (`park()` writes the task,
 *                             not the attempt). It is the last tail published,
 *                             not the stream, and nothing is still writing it.
 *
 * The table called every tail `live` (fix-up on #170): a FAILED task's
 * leftover output read as an agent still writing, for as long as anyone
 * opened it.
 */
function Stream({
  stream,
  logs,
  task,
  now,
}: {
  stream: LogStream
  logs: TaskLogs
  task: Task
  now: number | null
}) {
  const ended = logs.attempt.completed_at
  const writing = ended === null && CONCURRENCY_STATES.has(task.state)
  return (
    <tr role="row">
      <th role="rowheader" scope="row">
        <span className="mono">{stream.stream}</span>
        {stream.uri !== null && (
          <>
            {/* CH-13's long value (#87 follow-up, 2026-09-25). Stacked, the
                row header cuts the uri to one line (styles.css §B6.3's `.uri`
                rule), so the whole of it is in the title and in the copy
                beside it -- the `copy gsutil` the inspector's other uris
                carry, with `cat` because a log is read, as the attempt's
                log-location facts already copy it. A cut uri is a
                different uri. */}
            <span className="ctl-sub uri" title={stream.uri}>
              {stream.uri}
            </span>
            <button
              type="button"
              className="copy"
              onClick={() => navigator.clipboard?.writeText(`gsutil cat ${stream.uri}`)}
            >
              copy gsutil
            </button>
          </>
        )}
      </th>
      <td role="cell" data-label="Size" className="is-num">
        {stream.status === 'absent' && (
          <>
            <Em />{' '}
            <Mark kind="absent" say={`No object exists for ${stream.stream}: nothing was uploaded for this stream.`} />
          </>
        )}
        {stream.status === 'unreadable' && (
          <>
            <Em />{' '}
            <Mark
              kind="unread"
              say={`The ${stream.stream} object exists and could not be read. Nothing here says the stream is empty.`}
            />
          </>
        )}
        {stream.status === 'ok' && stream.content === null && (
          /* `status: ok` with no content is the server saying it served a
             window it cannot hand over. Rendering the empty string in its
             place would print an empty box that reads as a silent agent. */
          <>
            <Em />{' '}
            <Mark kind="unread" say="The read reported success and returned no content, so this window cannot be shown." />
          </>
        )}
        {stream.status === 'ok' && stream.content === '' && (
          <>
            {bytesLabel(0)}{' '}
            <Mark
              kind="zero"
              say={`This object exists and is empty: the agent wrote nothing to ${stream.stream}. A measured empty stream, not a failed read.`}
            />
          </>
        )}
        {stream.status === 'ok' && stream.content !== null && stream.content !== '' && <WindowSize stream={stream} />}
        {stream.detail !== null && <span className="ctl-sub">{stream.detail}</span>}
      </td>
      <td role="cell" data-label="Age">
        {stream.source === 'live' ? (
          writing ? (
            'live'
          ) : (
            <>
              <Em />{' '}
              <Mark
                kind="partial"
                say="The last tail the worker published before this attempt stopped. No final log was uploaded, so this is not the whole stream, and nothing is writing it now."
              />
            </>
          )
        ) : stream.source === 'final' ? (
          ended !== null ? (
            timeAgo(ended, now ?? Date.now())
          ) : (
            <>
              <Em />{' '}
              <Mark
                kind="absent"
                say="This attempt's end is not recorded, and the final log is uploaded when it ends, so when this log was written is unknown here."
              />
            </>
          )
        ) : (
          <Em />
        )}
      </td>
    </tr>
  )
}

/**
 * How much of a stream the window holds: the whole object as one figure, or
 * a window of it as `returned of total` with the `partial` mark -- the kit's
 * encoding for "some of it arrived and the rest was not read".
 */
function WindowSize({ stream }: { stream: LogStream }) {
  const total = stream.total_bytes
  const whole = total !== null && stream.offset === 0 && !stream.truncated && stream.returned_bytes >= total
  return (
    <>
      {whole ? (
        bytesLabel(total)
      ) : (
        <>
          {bytesLabel(stream.returned_bytes)} of {total === null ? <Em /> : bytesLabel(total)}{' '}
          <Mark
            kind="partial"
            say={`A window of this stream, not the whole of it: from byte ${stream.offset}${stream.next_offset !== null ? `, with more from byte ${stream.next_offset}` : ''}.`}
          />
        </>
      )}
      {stream.tail_window !== null && (
        <span className="ctl-sub">
          live window at byte {stream.tail_window.object_offset} of {bytesLabel(stream.tail_window.stream_size)}
        </span>
      )}
      {stream.redacted && stream.redaction_count > 0 && (
        <span className="ctl-sub">
          <span className="art-masked is-warn">{stream.redaction_count}</span> masked in this window
        </span>
      )}
    </>
  )
}
