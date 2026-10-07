import './styles/overview.css'
import './styles/names.css'
import { LifecycleBand, RecentFailures, WaitingWhy, agentPath, failuresOf, waitGroups } from './OverviewRegions'
import { addressToPath } from './paths'
import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import {
  loadAccountPool,
  loadCapacity,
  loadLeases,
  loadProviders,
  loadSpend,
  loadStats,
  loadTasks,
  loadWorkflows,
  TASK_PAGE_LIMIT,
  type SpendRollup,
} from './api'
import { PHONE_PAGE_LIMIT, agentName } from './agentlist'
import { totalCostCell, totalCostOf } from './dag'
import { usd } from './measure'
import { blindness, deriveChecks, type Check, type Problem } from './checks'
import type { TopicId } from './help'
import { HelpCard, HelpNote, phoneWidth } from './HelpCard'
import { errorHeading, isPaused, type ApiError, type Result } from './fetch'
import { Button, Dash, NamedMark, Skeleton, StateMark, ToneMark, UsageTrack, WarnMark, type TrackTone } from './components'
import { Absent, Mark } from './primitives'
import { disabledSentence } from './ProfileMatrix'
import { AGED_AFTER_MS, CountNote, PageHead, RefreshControl, staleFoot, timeAgo, useClaimPageAge, useIdleStop, usePoll } from './Shell'
import { AGE_TICK_MS, PageClock, usePageClock, useNow as useAgeClock } from './useNow'
import {
  CONCURRENCY_STATES,
  bindingWindow,
  formatDuration,
  leaseLiveliness,
  elapsed,
  headroomFor,
  needsAHuman,
  overCeiling,
  poolLabel,
  readingOf,
  unreadableFor,
  type AccountsPage,
  type Capacity,
  type LeasePage,
  type Pool,
  type Stats,
  type Task,
  type TaskPage,
  type TaskState,
} from './types'

/**
 * OVERVIEW. The landing screen: what is wrong, how much is waiting, working
 * and done, what is running, why the rest is waiting, what room is left, and
 * what failed.
 *
 * THE LAYOUT IS O1, "LEAD AND LEDGER" (docs/web-ui/mockups/overview.html, the
 * owner's pick 2026-10-01), top to bottom:
 *
 *   1. Needs a look -- the derived checks, one card per problem: a mark, a
 *      title, a line and the link out. No figure over a bar and no list.
 *   2. The lifecycle band -- Waiting / Holding capacity / Finished today, each
 *      broken down by state under its own mark (marks.tsx).
 *   3. Running now beside Cost so far.
 *   4. Waiting, and why (grouped by reason) beside Headroom.
 *   5. Recent failures, each ending in its age and an Open link.
 *
 * There is NO FIGURE ROW between the band and the cards (#503): the band
 * already says how many hold capacity, and the global pool's units are a row
 * of Headroom's pool list.
 *
 * THE INVARIANT IS UNCHANGED. This console must never present an absence as a
 * measurement. A figure nothing reported is not zero -- it is an em dash with
 * its reason as the element's title or accessible name. A partial total is
 * not a total, so every figure counted off the task PAGE says it is "of the N
 * newest read", and the two counted by `/v1/stats` (an exact count() per
 * state) say that.
 *
 * WHAT THIS SCREEN DOES NOT DRAW, and why it is not a placeholder:
 *   - a task's cost so far. `/v1/tasks` serves no cost; it lives on the
 *     attempts, one read per task. The Running card's last column is an em
 *     dash that says so, and Cost so far sums a named sample on refresh.
 *   - infrastructure cost in dollars. No billing integration records Cloud
 *     Run, Firestore or GCS spend, and the cost card's foot names the scope.
 *   - an alert inbox. Nothing stores, routes or acknowledges an alert. The
 *     checks are DERIVED from state that exists, re-derived on every read.
 */
export function OverviewScreen() {
  // THREE REFRESH CADENCES, on purpose. `live` drives the six cheap reads
  // every twenty seconds; `counted` drives `/v1/stats` -- twelve count()
  // queries -- every sixty (OV-16, owner decision 2026-09-25); `heavy` drives
  // the spend fan-out, which re-runs only when a person presses refresh.
  const [live, setLive] = useState(0)
  const [counted, setCounted] = useState(0)
  const [heavy, setHeavy] = useState(0)
  const refresh = useCallback(() => {
    setLive((n) => n + 1)
    setCounted((n) => n + 1)
    setHeavy((n) => n + 1)
  }, [])

  // NEITHER TIMER RUNS WHILE THE TAB IS HIDDEN (#168, docs/web-ui §2.5), AND
  // NEITHER RUNS AFTER FIFTEEN MINUTES WITHOUT INPUT (#117): the head's
  // control says `Paused · resume`, and resuming reads everything at once.
  const { idle, resume } = useIdleStop(true)
  usePoll(OVERVIEW_POLL_MS, () => setLive((n) => n + 1), idle)
  usePoll(STATS_POLL_MS, () => setCounted((n) => n + 1), idle)
  const resumePoll = useCallback(() => {
    resume()
    refresh()
  }, [resume, refresh])
  // ONE AGE ON THIS SCREEN (#98): the head's refresh control carries it, so
  // the frame's head prints none.
  useClaimPageAge(true)
  const clock = useAgeClock(AGE_TICK_MS)

  const capacity = useRead(loadCapacity, live)
  const tasks = useRead(loadListPage, live)
  const leases = useRead(loadLeases, live)
  const providers = useRead(loadProviders, live)
  const accounts = useRead(loadAccountPool, live)
  const workflows = useRead(loadWorkflows, live)
  const stats = useRead(loadStats, counted)

  const spend = useSpend(tasks, heavy)

  const reads: Result<unknown>[] = [
    capacity, tasks, leases, providers, accounts, workflows, stats, spend,
  ]

  // A 401 is a PAGE-level state: an expired IAP session fails every read at
  // once, and eight independently empty cards is the bug this UI is built
  // against.
  if (reads.some((r) => isKind(r, 'unauthenticated') || isKind(r, 'session_expired'))) {
    return (
      <div className="ctl-empty is-failed ov-page-empty">
        <Mark kind="unread" say="The API answered a sign-in page instead of data, so nothing on this screen is a reading of the platform." />
        <h3>Session expired</h3>
        <Button className="retry" onClick={() => window.location.reload()}>
          Reload to sign in
        </Button>
      </div>
    )
  }

  // FOUR OUTCOMES, COUNTED SEPARATELY: landed (`empty` is a real zero),
  // pending, refused for want of admin, and failed. "8/8" printed while every
  // read is in flight is the page vouching for itself before it knows.
  const landed = reads.filter(
    (r) => r.status === 'ok' || r.status === 'empty' || r.status === 'stale',
  ).length
  const pending = reads.filter((r) => r.status === 'loading').length
  const refused = reads.filter((r) => isKind(r, 'admin_required')).length
  const broken = reads.length - landed - pending - refused

  // `Date.now()` is taken here and the dependencies are the reads, not a
  // clock: re-deriving on a ticking clock would re-render every card once a
  // second to move a threshold that is twelve minutes wide.
  const checks = useMemo(
    () =>
      deriveChecks(
        { capacity, tasks, leases, providers, accounts, workflows, stats, phonePage: phoneWidth() },
        Date.now(),
      ),
    [capacity, tasks, leases, providers, accounts, workflows, stats],
  )
  // The newest reading behind this screen, for the head's one age (#98).
  const newest = reads.reduce<number | null>((m, r) => {
    const at = ageOf(r)
    return at === null ? m : m === null ? at : Math.max(m, at)
  }, null)
  const tenant = dataOf(tasks)?.tenant_id ?? null
  const page = dataOf(tasks)?.tasks ?? null
  const waiting = page === null ? null : waitGroups(page)
  const failures = page === null ? null : failuresOf(page, Date.now())

  return (
    // THE HEAD'S CLOCK IS EVERY FOOT'S (#98): a stale foot below and the
    // refresh control above read the same instant, so they cannot disagree.
    <PageClock.Provider value={clock}>
    <div className="ov-page">
      {/* TITLE LEFT, ACTIONS RIGHT, NOTHING UNDER IT (#138, owner ruling
          2026-10-07): the title and the section's `?`, and on the right the
          quiet refresh carrying this screen's one ticking age and its
          cadence (#98, #117). */}
      <PageHead title="Overview">
        <RefreshControl
          readAt={newest}
          now={clock}
          cadence={{ base: OVERVIEW_POLL_MS, wait: OVERVIEW_POLL_MS }}
          // NOT REFRESHED when any read behind the cards failed its refresh
          // and is showing older data, or when the newest is past
          // `AGED_AFTER_MS` -- the same two cases `Screen` says it for.
          stale={reads.some((r) => r.status === 'stale') || (newest !== null && clock - newest > AGED_AFTER_MS)}
          reading={pending > 0}
          idle={idle}
          onRefresh={refresh}
          onResume={resumePoll}
        />
      </PageHead>

      {/* THE COUNT, AS THE NOTE OVER THE FIRST CARD (#138). The scope is not
          decoration: `/v1/stats` and `/v1/tasks` are tenant-scoped while the
          `global` pool is platform-wide. Still reading is not missing (OV-9):
          the dash is for a read that landed without a tenant.

          THE TALLY SAYS HOW MUCH OF THIS PAGE IS REAL: `6/8` means two of
          the reads behind the cards below did not land, and every em dash
          further down is one of those two. Its dot is the severity, its
          accessible name the sentence. */}
      <CountNote>
        {tasks.status === 'loading' ? (
          <span className="ov-reading">tenant reading…</span>
        ) : tenant === null ? (
          <>
            tenant <i className="ctl-em" title="The task read carried no tenant id.">&mdash;</i>
          </>
        ) : (
          `tenant ${tenant}`
        )}
        {' · '}
        <span className="ov-tally" aria-label={readTally(reads.length, landed, pending, refused, broken)}>
          <ToneMark tone={tallyTone(pending, refused, broken)} />
          <span className="ov-num">
            {landed}/{reads.length}
          </span>{' '}
          reads
          <HelpCard topic="absent-vs-zero" />
        </span>
      </CountNote>

      <section className="ov-lead" id="ov-needs" aria-labelledby="ov-needs-h">
        <NeedsALook checks={checks} />
      </section>

      <LifecycleBand stats={stats} tasks={tasks} />

      {/* TWO COLUMNS THAT EACH STACK, NOT ROWS OF PAIRS (owner QA N17/D19,
          2026-10-04). As two rows of two, each row was as tall as its taller
          card, and the shorter one's column was blank beside it: ~157px
          beside Cost so far and ~388px beside Headroom. Each column now
          stacks its own cards at their own heights -- Running now, Waiting
          and Recent failures on the left, Cost so far and Headroom on the
          right -- so a short card is followed by the next card in its
          column, not by blank. The pools stay a full-width row under both
          (N17). On a narrower screen it is one column in O1's order. */}
      <div className="ov-g21 ov-cols">
        <div className="ov-col">
          <section className="ctl-card ov-card ov-running" id="ov-running">
            <RunningCard tasks={tasks} stats={stats} leases={leases} />
          </section>
          <section className="ctl-card ov-card ov-waiting" id="ov-waiting">
            <CardHead
              title="Waiting, and why"
              note={waiting === null ? undefined : `${waiting.reduce((n, g) => n + g.n, 0)} · none cost anything`}
              href="/agents/waiting"
              cta="All waiting"
            />
            <WaitingWhy tasks={tasks} />
          </section>
          <section className="ctl-card ov-card ov-failures" id="ov-failures">
            <CardHead
              title="Recent failures"
              note={failures === null ? undefined : failures.note}
              href="/agents/recent?state=failed"
              cta="All recent"
            />
            <RecentFailures tasks={tasks} />
          </section>
        </div>
        <div className="ov-col">
          <section className="ctl-card ov-card ov-spend" id="ov-spend">
            <CardHead title="Cost so far" href="/timeline" cta="Timeline" explain="token-cost" />
            <SpendBody state={spend} tasks={tasks} />
          </section>
          <section className="ctl-card ov-card ov-headroom" id="ov-headroom">
            <CardHead title="Headroom" href="/capacity/pools" cta="Pools" explain="pools-all-at-once" />
            <HeadroomBody capacity={capacity} accounts={accounts} />
          </section>
        </div>

        {/* THE POOLS ARE THEIR OWN FULL-WIDTH ROW (browser QA N17, 2026-10-04):
            under Headroom, a long pool list made that card ~1100px taller than
            Waiting beside it, and the left column was blank for all of it. */}
        <section className="ctl-card ov-card ov-pools" id="ov-pools">
          <CardHead title="Pools" href="/capacity/pools" cta="Pools" />
          <PoolsBody state={capacity} />
        </section>
      </div>
    </div>
    </PageClock.Provider>
  )
}

/**
 * The read tally as a sentence, for the strip's accessible name.
 *
 * "all 8 reads landed" is the sentence this has to earn, and it is only true
 * when nothing is in flight, nothing failed and nothing was refused. Said
 * before that it is the page vouching for itself while every card is still a
 * skeleton -- the single most misleading thing a control plane can say,
 * because it is said in the place a reader checks to decide whether to trust
 * the rest.
 */
function readTally(
  total: number,
  landed: number,
  pending: number,
  refused: number,
  broken: number,
): string {
  const parts: string[] = []
  if (broken > 0) parts.push(`${broken} of ${total} reads failed`)
  else parts.push(`${landed} of ${total} reads landed`)
  if (pending > 0) parts.push(`${pending} still arriving`)
  // A refused read is not a landed one. "all 8 landed" here would contradict
  // the Leases check, which is simultaneously reporting itself blind.
  if (refused > 0) parts.push(`${refused} needs admin, so ${refused === 1 ? 'one check' : `${refused} checks`} could not run`)
  return parts.join('; ')
}

/**
 * Which dot the tally wears.
 *
 * STILL READING IS NOT AN ABSENCE AND NOT A FAILURE, so it takes `.ctl-dot`'s
 * DEFAULT -- a hollow ring, deliberately outside the ok/warn/bad triad. An
 * admin gate is information rather than breakage, so it is the accent and
 * never the warning tone.
 */
function tallyTone(pending: number, refused: number, broken: number): string {
  if (broken > 0) return 'is-bad'
  if (pending > 0) return ''
  if (refused > 0) return 'is-info'
  return 'is-ok'
}

// ---------------------------------------------------------------------------
// Read plumbing
// ---------------------------------------------------------------------------

/**
 * How often the cheap reads re-run: every 20 seconds, unchanged by the
 * 2026-10-07 cadence ruling (#117, "Overview stays 20000").
 *
 * NAMED, because the spend panel has to say that it does NOT move on this
 * cadence, and the only honest way to say that is to render the figure the
 * timer actually uses.
 */
export const OVERVIEW_POLL_MS = 20_000

/**
 * How often `/v1/stats` re-runs while this screen is open (OV-16).
 *
 * SIXTY SECONDS, AND IT IS THE OWNER'S NUMBER, not a derived one: the owner
 * set it on 2026-09-25 as the cost call the finding named. The read is twelve
 * Firestore aggregation queries, one count() per state, so it stays off the
 * twenty-second poll -- three times slower than the reads that are single
 * indexed queries -- and it no longer waits for a person to press refresh.
 */
const STATS_POLL_MS = 60_000

/**
 * THE TASK PAGE THIS SCREEN COUNTS OVER IS THE AGENT LIST'S PAGE (OV-10).
 *
 * The failures item links to `#work/running/recent/failed`, a client-side
 * filter over the list's own read, and the decision's guarantee is that the
 * figure and that list describe one population. The list reads
 * `PHONE_PAGE_LIMIT` rows at phone width (§2.5), so this reads the same page
 * there and the api's full page everywhere else; every "among the N most
 * recent" on this screen then names the page the list shows. Decided at each
 * read, as the list decides it, so a rotated phone takes the right page on
 * its next poll.
 *
 * THE LIMIT IS PASSED AT BOTH WIDTHS (#168), the way `Agents.tsx` passes it.
 * A bare `loadTasks()` left the size of the page this screen polls to a
 * default in another file, where a change made for some other caller would
 * change what Overview re-reads every twenty seconds without touching it.
 * `TASK_PAGE_LIMIT` is still the api's constant, so this is a reference to
 * the one statement of it rather than a second one.
 *
 * AND IN THE SUMMARY VIEW (#168): `GET /v1/tasks?view=summary` serves each
 * row without `input`, `metadata` and `result_summary`, most of a row's
 * 10-20 KiB, and nothing on this screen reads them -- the trap in
 * `overview.summaryview.test.tsx` renders the screen from summary rows and
 * fails on any read of the three. The spend rollup samples this page for
 * task ids and `attempt_count` alone; its figures come from each task's
 * attempts read.
 *
 * 50 on a phone was §2.5's interim rule until `view=summary` existed. It
 * stays: the agent list caps its phone page at the same 50, and this screen
 * counts over the page the list shows (OV-10), so the two move together or
 * not at all.
 */
function loadListPage(): Promise<Result<TaskPage>> {
  return loadTasks(phoneWidth() ? PHONE_PAGE_LIMIT : TASK_PAGE_LIMIT, { view: 'summary' })
}

// ---------------------------------------------------------------------------
// Read plumbing
// ---------------------------------------------------------------------------

/**
 * One independent read, re-run when `nonce` changes.
 *
 * Deliberately does NOT keep the previous data across a failure the way
 * `Screen` does. `fetch.read` already promotes a failure-with-previous-data to
 * `stale`, and it only gets `previous` when a caller passes it; on this screen
 * a failed refresh should be visible as a failed refresh in the panel that
 * failed, while the other five stay current. A screen-wide stale rule would
 * dim five correct panels because a sixth stopped answering.
 */
function useRead<T>(load: () => Promise<Result<T>>, nonce: number): Result<T> {
  const [state, setState] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    load().then((r) => {
      if (live) setState(r)
    })
    return () => {
      live = false
    }
    // `load` is recreated per render by most callers; the nonce is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, load])
  return state
}

/**
 * The spend rollup, which must NOT follow the 20-second poll.
 *
 * It is derived from the task page rather than re-reading `/v1/tasks`, because
 * a second copy could disagree with the one the rest of this screen draws --
 * two panels describing different sets of tasks with no way to tell them
 * apart. But the page object is new on every poll, so keying the fan-out to it
 * would fire twelve requests every twenty seconds and make the landing screen
 * the thing that rate-limits the person who opened it.
 *
 * So the fan-out fires on exactly two things: the first page arriving, and the
 * first page to arrive AFTER a person presses refresh.
 *
 * THAT SECOND CLAUSE IS THE WHOLE CARE HERE. `refresh` bumps `heavy` and
 * `live` in the same tick, so at the instant of the press the task page in
 * hand is still the pre-refresh one -- summing it would answer the refresh
 * with the sample the refresh was asked to replace, and every task created
 * since would sit outside a panel whose provenance line claims to cover "the N
 * most recently created tasks that have run". Firing on the page's identity
 * alone is the opposite mistake: the page object is new on every 20-second
 * poll, which would put a twelve-request fan-out on a timer.
 *
 * So the trigger is: a page whose identity differs from the one held when
 * `heavy` last moved, and which has not already been summed for this `heavy`.
 *
 * AND IT TAKES THE WHOLE `Result`, NOT THE PAGE. A task read that lands
 * `empty` or `error` never produces a page at all, so a hook fed only
 * `dataOf(tasks)` sat at `loading` for ever -- and `loading` is a claim that a
 * request is IN FLIGHT. On a tenant that has never submitted a task, which is
 * the ordinary first-run state and not an edge case, that pulsed a skeleton
 * and printed "1 still arriving" in the screen's own provenance line for as
 * long as the page stayed open, while the Running panel two columns away drew
 * "No task exists yet" from the same read.
 *
 * So both TERMINAL upstream outcomes are mirrored into this Result:
 *
 *   - `empty`: the read landed and there are no tasks, so there is nothing to
 *     sum. A real zero -- the same one `loadSpend` returns for a page whose
 *     tasks have no attempts.
 *   - `error`: the read this rollup is BUILT ON failed, so no sum can be
 *     assembled and no attempt read was ever made.
 *
 * `loading` upstream stays `loading` here, because then a request really is in
 * flight and the fan-out starts when it lands.
 *
 * THE MIRROR STOPS AT THE FIRST FAN-OUT. `useRead` reports a failed refresh as
 * `error` rather than `stale`, so without that rule a 500 on the 20-second
 * poll would replace a rollup that really was summed from a page that really
 * landed -- retracting a measurement because a later, different read failed.
 * The card's foot carries the sum's age for exactly that case.
 */
function useSpend(tasks: Result<TaskPage>, heavy: number): Result<SpendRollup> {
  const [state, setState] = useState<Result<SpendRollup>>({
    status: 'loading',
    since: Date.now(),
  })
  const page = dataOf(tasks)
  const latest = useRef<TaskPage | null>(null)
  latest.current = page

  /** The page in hand when `heavy` last changed. Anything else is newer. */
  const atPress = useRef<TaskPage | null>(null)
  /** The `heavy` a rollup has been decided for. Never decided twice. */
  const summedFor = useRef<number | null>(null)
  /**
   * True once a fan-out has been STARTED -- not once it has landed. From that
   * moment this hook answers for itself: while the requests are out `loading`
   * is true, and when they land their outcome stands. Neither may be replaced
   * by a mirror of the task read.
   */
  const dispatched = useRef(false)
  /** The upstream outcome already mirrored, so it is not re-set every poll. */
  const mirrored = useRef<string | null>(null)

  useEffect(() => {
    atPress.current = latest.current
  }, [heavy])

  // DECIDING IS SEPARATE FROM RUNNING, and it has to be. The decision depends
  // on the task page, which is a new object every twenty seconds; the fan-out
  // must not be. With one effect for both, an ordinary poll landing while the
  // twelve requests were in flight would tear them down and start them again
  // -- turning a slow API into a fan-out that restarts for ever, which is the
  // exact failure this hook exists to avoid. So the cheap effect picks the
  // job, and the expensive one is keyed to the job alone.
  const [job, setJob] = useState<{ heavy: number; page: TaskPage } | null>(null)

  useEffect(() => {
    if (page === null) {
      // THERE IS NO PAGE. For one of the three reasons one is still coming;
      // for the other two none ever is, and sitting at `loading` through them
      // asserts a fan-out is in flight that will never start.
      if (dispatched.current) return
      if (tasks.status === 'empty') {
        if (mirrored.current === 'empty') return
        mirrored.current = 'empty'
        setState({ status: 'empty', fetchedAt: tasks.fetchedAt })
      } else if (tasks.status === 'error') {
        // Keyed on the error itself: a later poll that fails differently is a
        // different fact and replaces it. The panel names the task read as the
        // thing that failed, so this is never read as an attempt-read failure.
        const key = `${tasks.error.kind}:${tasks.error.httpStatus ?? 'none'}:${tasks.error.message}`
        if (mirrored.current === key) return
        mirrored.current = key
        setState({ status: 'error', error: tasks.error })
      }
      return
    }

    const wasMirrored = mirrored.current !== null
    mirrored.current = null
    if (summedFor.current === heavy) return
    // The refresh has been pressed and the new task page has not landed yet.
    // The next render that brings one re-runs this effect.
    if (page === atPress.current) return

    summedFor.current = heavy
    dispatched.current = true
    // A first task arrived for a tenant that had none, so the mirrored "no
    // task has run" is about to stop being true and a fan-out really is
    // starting. An absence must not survive into the read that replaces it.
    if (wasMirrored) setState({ status: 'loading', since: Date.now() })
    setJob({ heavy, page })
  }, [heavy, page, tasks])

  useEffect(() => {
    if (job === null) return
    let live = true
    loadSpend(job.page).then((r) => {
      if (live) setState(r)
    })
    return () => {
      live = false
    }
  }, [job])

  return state
}

/**
 * A clock that ticks, so "running for 4m 12s" is true a second later.
 *
 * SCOPE IT TO THE CELL THAT USES IT. This screen is the one the platform
 * expects to be left open all day, and a 1Hz clock held at the top of it
 * re-renders every panel and every tile once a second to move one table
 * column. It is called from `Runtime` alone.
 */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  return now
}

function dataOf<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

function isKind(r: Result<unknown>, kind: ApiError['kind']): boolean {
  return r.status === 'error' && r.error.kind === kind
}

/** When a read produced a reading, how old that reading is. */
function ageOf(r: Result<unknown>): number | null {
  return r.status === 'ok' || r.status === 'stale' ? r.fetchedAt : null
}
/**
 * The checks' own labels, as an English list.
 *
 * Read off `checks` rather than written out, so a seventh check cannot leave
 * the description naming six.
 */
function sourceList(checks: Check[]): string {
  const names = checks.map((c) => c.label.toLowerCase())
  if (names.length <= 1) return names[0] ?? 'nothing'
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/**
 * A card foot's freshness clause: `absent` when there is no reading to date,
 * `from 6 min ago` when the reading is stale (a failed refresh, or older than
 * `AGED_AFTER_MS`), and nothing at all while it is fresh -- the head's
 * refresh control carries a fresh age (#98, owner ruling 2026-10-07).
 */
function footFor(r: Result<unknown>, absent: string, now: number): string | null {
  const at = ageOf(r)
  return at === null ? absent : staleFoot(at, now, r.status === 'stale')
}
// ---------------------------------------------------------------------------
// Card chrome
// ---------------------------------------------------------------------------

/**
 * Every card's head: the title, the qualifier, and the link out, which reads
 * the destination's name with an arrow (O1: `All live →`, `Pools →`). There
 * is no way to draw a card here without naming the screen that goes deeper.
 *
 * `explain` publishes a help topic's sentence at the title as a hidden
 * description (`HelpNote`) and draws nothing (B7.4).
 */
function CardHead({
  title,
  note,
  href,
  cta,
  explain,
}: {
  title: string
  note?: string | undefined
  explain?: TopicId
  href: string
  cta: string
}) {
  const descId = useId()
  return (
    <div className="ctl-card-head">
      <h2 className="ctl-card-title" aria-describedby={explain === undefined ? undefined : descId}>
        {title}
        {explain !== undefined && <HelpNote topic={explain} id={descId} />}
      </h2>
      {note !== undefined && <span className="ctl-card-note">{note}</span>}
      <a className="ctl-link ov-link" href={href}>
        {cta} &rarr;
      </a>
    </div>
  )
}

/**
 * The four ways a card can have nothing to draw, kept visually distinct.
 *
 * The shared `Absent` (./primitives.tsx), with `ov-empty`: the in-card variant,
 * which gives up the primitive's own box because the card has already drawn
 * one. The heading is the FACT, three or four words of it; `say` is ALREADY A
 * WHOLE SENTENCE at every call site below, and it is the mark's accessible
 * name.
 *
 * WHICH IS WHY THE `?` HERE IS GONE (B7.4). Nine of this screen's twelve help
 * anchors were on these empty states, each one opening a card beside a mark
 * whose own accessible name said the same thing in more detail. `explain`
 * keeps the topic's sentence published at the heading for assistive
 * technology and draws nothing.
 */
function CardAbsent(props: Omit<Parameters<typeof Absent>[0], 'className'>) {
  return <Absent {...props} className="ov-empty" />
}

/**
 * A card foot's clauses, as a run (OV-14).
 *
 * THE SEPARATOR IS DRAWN, NEVER TYPED. The feet here were one text run with
 * ' · ' between clauses, and a flex item's text wraps inside itself: at 390
 * the Spend foot broke across three lines with a `·` left at the end of two of
 * them and a clause split in half. Each child of this run is one clause, one
 * element, `nowrap`; `.ctl-foot-run` in styles.css draws the dot in the gap to
 * each clause's left and clips the one that would start a line. So a clause
 * never breaks inside itself and a dot never ends or begins a line.
 *
 * Every child must be ONE ELEMENT, and a falsy child is simply not a clause.
 */
function FootRun({ children }: { children: ReactNode }) {
  return <span className="ctl-foot-run">{children}</span>
}

/**
 * A read in flight, at the geometry the content will occupy.
 *
 * A THING THAT IS ABSENT OCCUPIES THE SPACE IT WOULD HAVE OCCUPIED. If the
 * card shrinks when the data does not arrive, the operator cannot see that
 * something should have been there -- the same failure as printing a zero, one
 * layer down.
 */
function Reading({ rows = 3 }: { rows?: number }) {
  return <div className="ctl-ghost ov-ghost" style={{ '--rows': String(rows) } as CSSProperties} aria-hidden />
}
// ---------------------------------------------------------------------------
// Needs a look -- derived, never stored
// ---------------------------------------------------------------------------
//
// The derivation lives in ./checks.ts, where `test/checks.test.mjs` drives it.
// What is here draws what it returns.

/**
 * THE LEAD (O1 `.lead`): a row of check cards, each a mark, a title, two lines
 * and the link out -- the problem's own `href`, never a problem board, because
 * there is none and should not be one.
 *
 * WHAT DID NOT MOVE IS THE HONESTY ENCODING:
 *
 *   - A SHORT ROW OVER BLIND CHECKS AND A SHORT ROW OVER CLEAR ONES ARE
 *     DIFFERENT PICTURES. A blind check is counted on the head line as a
 *     digit (`1 blind`) with the kit's partial mark beside it, and the
 *     sentence naming which and why is their accessible name.
 *   - THE ALL-CLEAR IS ONLY AN ALL-CLEAR WHEN EVERY CHECK RAN, and the
 *     `Absent` mark names which of the three kinds of nothing it is.
 *   - WHILE A CHECK IS STILL READING (OV-9) no count is drawn: the pending
 *     mark, because a count taken before every check ran is not the count.
 */
function NeedsALook({ checks }: { checks: Check[] }) {
  const problems = checks
    .flatMap((c) => (c.status === 'found' ? c.problems : []))
    .sort((a, b) => (a.severity === b.severity ? b.n - a.n : a.severity === 'bad' ? -1 : 1))
  const blind = checks.filter((c): c is Extract<Check, { status: 'blind' }> => c.status === 'blind')
  const reading = checks.filter((c) => c.status === 'reading')
  const clear = checks.filter((c): c is Extract<Check, { status: 'clear' }> => c.status === 'clear')
  const found = checks.filter((c) => c.status === 'found').length
  const ran = clear.length + found
  const blindSay = blind.map((c) => `${c.label.toLowerCase()} — ${c.why}`).join('; ')

  return (
    <>
      <div className="ov-lh">
        <h2 id="ov-needs-h">Needs a look</h2>
        <span
          className="ov-cnt"
          aria-label={
            `${ran} of ${checks.length} checks ran and found ${problems.length} ${problems.length === 1 ? 'problem' : 'problems'}.` +
            (blind.length > 0 ? ` ${blind.length} could not run, so this row is incomplete: ${blindSay}` : '') +
            (clear.length > 0 ? ` Clear: ${clear.map((c) => `${c.label.toLowerCase()} — ${c.note}`).join('; ')}` : '') +
            // WHERE THE CHECKS COME FROM, AND WHAT THIS IS NOT -- derived from
            // the checks themselves, so a seventh cannot go unnamed.
            ` Derived on every read from ${sourceList(checks)}. Nothing stores, routes or acknowledges an alert on this platform, so this is not an inbox.`
          }
        >
          {/* THE LEAD FRACTION, LABELLED (#98, owner ruling 2026-10-07):
              `checks 8/8` is how many of the checks ran, so it cannot be
              read as the reads tally or as a count of problems. */}
          {reading.length > 0
            ? `${reading.length} of ${checks.length} checks still reading`
            : `checks ${ran}/${checks.length} · ${found} found something`}
          {blind.length > 0 && (
            <span className={blind.every((c) => c.admin) ? 'ov-info' : 'ov-warn'}> · {blind.length} blind</span>
          )}
        </span>
        {blind.length > 0 && reading.length === 0 && (
          <Mark
            kind="partial"
            say={`${blind.length} of ${checks.length} checks could not run, so this row is over the ${ran} that did: ${blindSay}`}
          />
        )}
      </div>

      {reading.length > 0 ? (
        // A SKELETON OF THE CARD IT WILL BE (walkthrough F, owner
        // 2026-10-03): the pending mark here was an empty dashed box the
        // width of the row, which read as a broken input. Still no count:
        // the sentence is the row's accessible name.
        <div
          className="ov-atts"
          aria-busy="true"
          aria-label={`${reading.length} of ${checks.length} checks are still reading: ${reading.map((c) => c.label.toLowerCase()).join(', ')}. No count is drawn until they have run.`}
        >
          <div className="ov-att is-skel" aria-hidden="true">
            <span className="ov-att-t">
              <Skeleton title width="45%" />
              <Skeleton width="85%" />
              <Skeleton width="60%" />
            </span>
          </div>
        </div>
      ) : problems.length > 0 ? (
        <div className="ov-atts">
          {problems.map((p, i) => (
            <CheckCard key={`${p.headline}-${i}`} problem={p} />
          ))}
        </div>
      ) : (
        <CardAbsent
          kind={blind.length === 0 ? 'zero' : blind.every((c) => c.admin) ? 'admin' : 'partial'}
          heading={blind.length === 0 ? 'Nothing wrong' : `${blind.length} not checked`}
          say={
            blind.length === 0
              ? `All ${clear.length} checks ran and all ${clear.length} came back clear. This is a real all-clear over the population each check examined, not silence.`
              : `${blind.length} of ${checks.length} checks could not run, so this is a partial all-clear: ${blindSay}`
          }
          explain="all-clear-basis"
        />
      )}
    </>
  )
}

/**
 * One problem as O1's check card. The mark repeats the severity as a shape --
 * the amber triangle for a warning, the red diamond for a failure -- because
 * roughly 8% of male viewers cannot separate the two inks. The detail is the
 * card's second line, clamped to two, and its whole text is the title.
 */
export function CheckCard({ problem: p }: { problem: Problem }) {
  // A SHORT TITLE, THEN TWO LINES OF REGULAR TEXT (visual QA Q5, 2026-10-02;
  // O1's check card). The headline's first clause is the title; the clauses
  // after it -- the age, the workflow and worker ids -- are the second line,
  // one line, cut with an ellipsis and whole in its title; the detail is the
  // third. The whole headline stays the card's accessible name.
  const { title, ids } = splitHeadline(p.headline)
  return (
    <a className={`ov-att is-${p.severity}`} href={toPath(p.href)} aria-label={`${p.headline}. ${p.detail}`}>
      {p.severity === 'bad' ? <BadMark /> : <WarnMark />}
      <span className="ov-att-t">
        <b title={p.headline}>{title}</b>
        {ids !== null && (
          <small className="ov-att-ids" title={ids}>
            {ids}
          </small>
        )}
        <small className={ids === null ? undefined : 'is-one'} title={p.detail}>
          {p.detail}
        </small>
      </span>
      <span className="ov-att-go">{p.linkLabel ?? 'Open'} &rarr;</span>
    </a>
  )
}

/**
 * A check's headline as O1's title and its second line: the title is the
 * first clause, up to the first ` · ` or `: `, and the rest -- where the
 * ages and the ids live -- is the second line. A headline with no clause
 * after it is all title.
 */
/**
 * A check's link as a path. checks.ts still names its destinations in the
 * old hash grammar, which the router redirects; a path is the one the address
 * bar ends on, so the card links there directly (browser QA, 2026-10-04).
 */
export function toPath(href: string): string {
  return href.startsWith('#') ? addressToPath(href.slice(1)) : href
}

export function splitHeadline(headline: string): { title: string; ids: string | null } {
  const cuts = [
    { at: headline.indexOf(' · '), len: 3 },
    { at: headline.indexOf(': '), len: 2 },
  ].filter((c) => c.at > 0)
  if (cuts.length === 0) return { title: headline, ids: null }
  const cut = cuts.reduce((a, b) => (b.at < a.at ? b : a))
  const rest = headline.slice(cut.at + cut.len).trim()
  return rest === '' ? { title: headline, ids: null } : { title: headline.slice(0, cut.at), ids: rest }
}

/** The red diamond, for a problem whose severity is a failure. Not a state. */
function BadMark() {
  return <NamedMark mark="failed" hue="bad" />
}

/**
 * The binding pool's name, with the CALLER'S OWN TENANT taken out of it.
 *
 * `poolLabel` renders `provider:anthropic:tenant:u-bogdan` as
 * "anthropic · u-bogdan", which does not fit a half-width column and is
 * ellipsised to "anthropic · u-…" -- losing nothing useful, because the card's
 * note already says every figure on it is for that tenant. Repeating the
 * tenant on every row costs the characters that identify WHICH pool binds,
 * which is the one thing the column exists for.
 */
function bindingLabel(name: string, tenant: string | undefined): string {
  if (tenant) {
    if (name === `tenant:${tenant}`) return 'your tenant'
    const own = name.endsWith(`:tenant:${tenant}`)
    if (own) return poolLabel(name.slice(0, -`:tenant:${tenant}`.length))
  }
  return poolLabel(name)
}
// ---------------------------------------------------------------------------
// Headroom: what can start, the pools, the account pool
// ---------------------------------------------------------------------------

/**
 * O1's Headroom card: three stacks in ONE column, so nothing in it can be
 * drawn on top of anything else (#503 measured the old two-group layout's
 * profile rows reaching x=1564 on a card ending at 1408, under the account
 * column).
 *
 *   Can start now, by runner profile  one tile per profile: how many more
 *                                     agents, and WHICH POOL BINDS. A task
 *                                     clears every pool in its list at once,
 *                                     so raising any other pool changes
 *                                     nothing -- the trap this card exists for.
 *   Pools                             each pool's units in use over its
 *                                     ceiling, on the shared track.
 *   Subscription accounts             the account pool in one line.
 *
 * TRAP D: `profile.pools` is the CALLING TENANT'S pool list, even for an
 * admin, so this answers "how many more could I start", never "how much
 * capacity does the platform have".
 */
function HeadroomBody({ capacity, accounts }: { capacity: Result<Capacity>; accounts: Result<AccountsPage> }) {
  return (
    <>
      <h3 className="ov-sub2">Can start now, by runner profile</h3>
      <CapacityStacks state={capacity} />
      <h3 className="ov-sub2">Subscription accounts</h3>
      <AccountLine state={accounts} />
    </>
  )
}

function CapacityStacks({ state }: { state: Result<Capacity> }) {
  const now = usePageClock()
  if (state.status === 'loading') return <Reading rows={3} />
  if (state.status === 'error') {
    const b = blindness(state.error)
    return (
      <CardAbsent
        kind={b.admin ? 'admin' : 'failed'}
        heading="Pool state unread"
        say={`${b.why} No figure here is a claim about room.`}
        explain={b.admin ? 'admin-gate-not-failure' : 'read-failed'}
      />
    )
  }
  if (state.status === 'empty') {
    return (
      <CardAbsent
        kind="zero"
        heading="No pools exist"
        say="The read succeeded and returned nothing. This is a real zero, not a failure to read."
        explain="capacity"
      />
    )
  }

  const cap = state.data
  const byName = new Map(cap.pools.map((p) => [p.name, p]))
  const profiles = Object.entries(cap.runner_profiles).sort(([a], [b]) => a.localeCompare(b))
  // `/v1/capacity` carries no tenant id of its own, so whose view this is is
  // read off the tenant-scoped pool names rather than assumed.
  const tenant = cap.pools
    .map((p) => /(?:^|:)tenant:([^:]+)/.exec(p.name)?.[1])
    .find((t): t is string => Boolean(t))

  return (
    <>
      {profiles.length === 0 ? (
        <CardAbsent
          kind="partial"
          heading="No runner profile"
          say={`${cap.pools.length} pools were read and no runner profile came back, so no ceiling is computed here.`}
        />
      ) : (
        <div className="ov-hps">
          {profiles.map(([name, profile]) => (
            <ProfileTile key={name} name={name} profile={profile} byName={byName} tenant={tenant} />
          ))}
        </div>
      )}
      <p className="ov-foot">
        <FootRun>
          <span>{countOf(cap.pools.length, 'pool')}</span>
          <span>{tenant === undefined ? 'no tenant pool read' : `tenant ${tenant}`}</span>
          {footFor(state, 'not read', now) !== null && <span>{footFor(state, 'not read', now)}</span>}
        </FootRun>
      </p>
    </>
  )
}

/**
 * THE POOLS CARD: each pool's units in use over its ceiling, laid across the
 * page's width. Headroom's card already says why when the read did not land;
 * this card says it in one line rather than a second absent card.
 */
function PoolsBody({ state }: { state: Result<Capacity> }) {
  if (state.status === 'loading') return <Reading rows={2} />
  if (state.status !== 'ok') {
    return (
      <p className="ctl-em" title={state.status === 'error' ? `${blindness(state.error).why}` : undefined}>
        {state.status === 'empty' ? 'No pools exist: the read succeeded and returned nothing.' : '— not read; see Headroom'}
      </p>
    )
  }
  return (
    <div className="ov-pls">
      {state.data.pools.map((p) => (
        <PoolRow key={p.name} pool={p} />
      ))}
    </div>
  )
}

/**
 * One runner profile: `+6`, a measured `0` in the warning ink, or an em dash
 * that says which kind of not-measured it is -- never a 0 for a pool nobody
 * read. The line under it is the binding pool, or why there is none.
 */
export function ProfileTile({
  name,
  profile,
  byName,
  tenant,
}: {
  name: string
  profile: Capacity['runner_profiles'][string]
  byName: ReadonlyMap<string, Pool>
  tenant: string | undefined
}) {
  // A DISABLED PROFILE HAS NO HEADROOM (browser QA N5, 2026-10-04): four
  // profiles Pools marks disabled read "+39 can start now" here. The platform
  // refuses them whatever the pools say, so the tile says so and why. Absent
  // `available` is an older API and means available (types.ts).
  if (profile.available === false) {
    const reason = profile.disabled_reason || 'refused by the platform'
    return (
      <div className="ov-hp is-off" title={`${disabledSentence(name, profile.disabled_reason)} Nothing can start on it, whatever the pools hold.`}>
        <span className="ov-idc" title={name}>{name}</span>
        <b className="ov-hp-off">disabled</b>
        <small title={reason}>{reason}</small>
      </div>
    )
  }
  const h = headroomFor(profile)
  const binding = h.binding !== null ? (byName.get(h.binding) ?? null) : null
  const paused = binding !== null && isPaused(binding)
  // More than one pool can refuse at once, and "refusing" rather than "full":
  // one of them may be PAUSED, which has the opposite remedy.
  const why =
    h.agents === null
      ? h.basis === 'uncapped'
        ? 'no pool caps it'
        : `${countOf(h.unread.length, 'pool')} unread`
      : h.blockers.length > 1
        ? `${countOf(h.blockers.length, 'pool')} refusing`
        : paused
          ? `${bindingLabel(binding.name, tenant)} paused`
          : h.binding !== null
            ? `binds ${bindingLabel(h.binding, tenant)}`
            : h.missing.length > 0
              ? `${countOf(h.missing.length, 'pool')} uncapped`
              : 'nothing binds'
  const long =
    h.agents === null
      ? h.basis === 'uncapped'
        ? `${name}: no pool in this profile is configured, so nothing caps it.`
        : `${name}: not measured, ${countOf(h.unread.length, 'required pool')} could not be read.`
      : `${name}: ${countOf(h.agents, 'more agent')} can start; ${profile.units} unit(s) per agent.` +
        (h.blockers.length > 0
          ? ` Refusing: ${h.blockers.map((b) => `${b.pool} (${b.active}/${b.limit ?? 'no limit set'})`).join(', ')}.`
          : h.binding !== null
            ? ` Bound by ${h.binding}.`
            : '')
  return (
    <div className="ov-hp" title={long}>
      <span className="ov-idc" title={name}>{name}</span>
      {h.agents === null ? (
        <b className="ctl-em">&mdash;</b>
      ) : h.agents === 0 ? (
        <b className="is-zero">0</b>
      ) : (
        <b>+{h.agents}</b>
      )}
      <small>{why}</small>
    </div>
  )
}

/**
 * One pool: its name, its units in use on the shared track, and `active/limit`.
 * A pool with no limit set (#374) has no ratio: the track is hatched and the
 * figure says so rather than `/ 0`. Units, never agents: admission counts a
 * resource class's weight.
 */
export function PoolRow({ pool: p }: { pool: Pool }) {
  const limit = p.effective_limit
  const known = limit !== null && limit > 0
  const ratio = known ? p.active / limit : 0
  const paused = isPaused(p)
  const tone: TrackTone | undefined = paused
    ? 'is-paused'
    : overCeiling(p)
      ? 'is-bad'
      : known && ratio >= 0.8
        ? 'is-warn'
        : undefined
  const say = `${poolLabel(p.name)}: ${p.active} of ${limit === null ? 'no limit set' : `${limit}`} units in use${paused ? ', paused' : ''}`
  return (
    <div className="ov-pl" title={say}>
      <span className="ov-idc" title={p.name}>
        {p.name}
      </span>
      <UsageTrack
        pct={known ? ratio * 100 : null}
        tone={tone}
        zeroTitle={`Measured: 0 of ${limit} in use on ${p.name}.`}
        meter={known ? { label: say, now: p.active, max: limit } : { label: say, now: p.active, max: 0 }}
      />
      <b>
        {p.active}/{limit === null ? <span className="ctl-em" title="No limit set: not a ceiling of 0.">&mdash;</span> : limit}
      </b>
    </div>
  )
}

/**
 * The account pool in one line (O1): how many accounts can serve, the best
 * one's binding window, and the ones a person has to sign in again. The
 * per-account rows are the Accounts screen's; this links there.
 *
 * `accountHeadroom` decides which accounts are usable, so a stale, cleared,
 * unpolled, paused or skipped account never counts as room.
 */
function AccountLine({ state }: { state: Result<AccountsPage> }) {
  const now = usePageClock()
  if (state.status === 'loading') return <Reading rows={1} />
  if (state.status === 'error') {
    const b = blindness(state.error)
    return (
      <CardAbsent
        kind={b.admin ? 'admin' : 'failed'}
        heading="Account pool unread"
        say={`${b.why} This says nothing about whether the accounts have room.`}
        explain={b.admin ? 'admin-gate-not-failure' : 'read-failed'}
      />
    )
  }
  if (state.status === 'empty') {
    return (
      <CardAbsent
        kind="zero"
        heading="No account registered"
        say="The read succeeded and returned nothing. This is a real zero, not a failure to read."
        explain="park-on-missing-credential"
      />
    )
  }
  const pool = accountHeadroom(state)
  const signIn = state.data.accounts.filter(needsAHuman).length
  return (
    <div className="ov-acc">
      <span aria-label={pool.foot === undefined ? pool.sub : `${pool.sub}. ${pool.foot}`}>
        <b className="ov-num">{pool.usable ?? 0}</b> of {pool.total} usable
      </span>
      {pool.pct === null || pool.best === null ? (
        <span title={pool.sub}>
          <i className="ctl-em">&mdash;</i> no current reading
        </span>
      ) : (
        <span title={pool.sub}>
          {/* THE AGE IS PART OF THE FIGURE WHEN IT IS OLD: a percentage read
              four days ago is a claim without provenance, so a reading older
              than `AGED_AFTER_MS` says `from 7 min ago`. A fresh one is silent
              (#98, owner ruling 2026-10-07: a tile states freshness only when
              stale); the whole sentence, age included, is the line's title. */}
          best {pool.best.label} at <span className="ov-num">{Math.round(pool.pct)}%</span> of its {pool.best.window} window
          {staleFoot(Date.parse(pool.best.observedAt), now) !== null && ` · ${staleFoot(Date.parse(pool.best.observedAt), now)}`}
        </span>
      )}
      {signIn > 0 && <WarnMark label={`${signIn} needs sign-in`} />}
      <a className="ctl-link ov-link" href="/capacity/accounts">
        Accounts &rarr;
      </a>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Running now
// ---------------------------------------------------------------------------

/**
 * EIGHT ROWS, THEN THE REST IN PLACE (#93), so nothing running is dropped from
 * the landing page and the card does not set the height of its grid row.
 */
const RUNNING_ROWS = 8

/**
 * Why the Cost so far column is a dash for a row served without attempt
 * totals: an API older than the P1 follow-up (2026-10-05), whose
 * `GET /v1/tasks` carried no cost.
 */
const COST_NOT_SERVED =
  'not served: this API’s GET /v1/tasks carries no attempt totals. A task’s cost is on its attempts; the Cost so far card sums a sample on refresh instead.'

/** Why the column is a dash when the API read the attempts and none reported. */
const COST_NOT_YET =
  'not reported yet: no attempt of this task has recorded a cost. An attempt records its cost when it ends.'

/** Why the column is a dash when the API could not read the attempts. */
const COST_UNREAD = 'The attempts behind this task’s cost could not be read. Not $0: unknown.'

/**
 * COST SO FAR IS THE WHOLE TASK'S (owner decision 2026-10-05, P1 follow-up).
 * `GET /v1/tasks` serves every attempt's total on each row, and the cell is
 * the board's own reading of it (`totalCostCell`): `at least` while an
 * attempt has not reported -- a running attempt reports at exit -- and the
 * last attempt's figure as secondary text once there is more than one, the
 * way the task page's Cost cell reads. A null total is a dash, never $0.
 */
function CostSoFar({ task }: { task: Task }) {
  const served = totalCostCell(task)
  const total = totalCostOf(task)
  if (served !== null && total !== null) {
    const n = total.attempts
    return (
      <span title={served.note}>
        <span className="ov-cost">{served.text}</span>
        {n !== null && n > 1 && (
          <span className="ov-sub">last attempt {total.lastUsd === null ? 'not reported' : usd(total.lastUsd)}</span>
        )}
      </span>
    )
  }
  const why = task.attempts_read === 'failed' ? COST_UNREAD : typeof task.attempts === 'number' ? COST_NOT_YET : COST_NOT_SERVED
  return (
    <span className="ov-dash" title={why}>
      &mdash;
    </span>
  )
}

/**
 * Running now: State first, then Agent · profile, Runtime, Cost so far (O1).
 *
 * TWO SOURCES, AND BOTH ARE NAMED. The rows are the task page's -- the 200
 * most recently created tasks (50 at phone width) -- and the exact count is
 * `/v1/stats`'s. An agent running for two days while 200 newer tasks were
 * created is counted and not listed, so the foot prints both when they
 * differ, with the count's age.
 */
function RunningCard({
  tasks,
  stats,
  leases,
}: {
  tasks: Result<TaskPage>
  stats: Result<Stats>
  leases: Result<LeasePage>
}) {
  const now = usePageClock()
  const page = dataOf(tasks)
  const silent = silentByTask(leases)
  const leased = leasedAtByTask(leases)
  const running =
    page === null
      ? []
      : page.tasks
          .filter((t) => CONCURRENCY_STATES.has(t.state))
          // A SILENT worker first (#92), then the longest-running.
          .sort((a, b) => Number(silent.has(b.id)) - Number(silent.has(a.id)) || startKey(a) - startKey(b))
  const head = (
    <CardHead
      title="Running now"
      note={page === null ? undefined : `${running.length} hold capacity`}
      href="/agents/live"
      cta="All live"
    />
  )

  if (tasks.status === 'loading') {
    return (
      <>
        {head}
        <Reading rows={4} />
      </>
    )
  }
  if (tasks.status === 'error') {
    const b = blindness(tasks.error)
    return (
      <>
        {head}
        <CardAbsent
          kind={b.admin ? 'admin' : 'failed'}
          heading="Task list unread"
          say={`${b.why} This card is blind; it is not reporting that nothing is running.`}
          explain={b.admin ? 'admin-gate-not-failure' : 'read-failed'}
        />
      </>
    )
  }
  if (tasks.status === 'empty' || page === null) {
    return (
      <>
        {head}
        <CardAbsent
          kind="zero"
          heading="No task exists"
          say="The read succeeded and returned nothing. This is a real zero, not a failure to read."
          explain="absent-vs-zero"
        />
      </>
    )
  }

  const st = dataOf(stats)
  const counted =
    st === null
      ? null
      : Object.entries(st.tasks_by_state)
          .filter(([s]) => CONCURRENCY_STATES.has(s as TaskState))
          .reduce((n, [, v]) => n + (typeof v === 'number' ? v : 0), 0)
  const countedAt = ageOf(stats)
  // Said only when stale (#98): a fresh count's age is the head's.
  const countedAge = countedAt === null ? null : staleFoot(countedAt, now, stats.status === 'stale')
  // The sentence below names the counts' age on the same rule: only when stale.
  const counts = countedAge === null ? '' : `, ${countedAge},`
  const foot = (
    <p className="ov-foot">
      <FootRun>
        <span>
          {running.length} of {page.tasks.length} newest
        </span>
        {counted !== null && counted !== running.length && <span>stats say {counted}</span>}
        {counted !== null && counted !== running.length && countedAge !== null && <span>counted {countedAge}</span>}
      </FootRun>
    </p>
  )

  if (running.length === 0) {
    return (
      <>
        {head}
        <CardAbsent
          kind="zero"
          heading="Nothing running"
          say={
            `No task on the ${page.tasks.length} most recently created is in LEASED, DISPATCHED, STARTING or RUNNING. ` +
            (counted === null
              ? 'The exact count could not be read, so this is the page’s answer rather than the platform’s.'
              : counted === 0
                ? `The state counts${counts} agree: zero.`
                : `The state counts${counts} say ${counted}; those agents were created before this page begins.`)
          }
        />
        {foot}
      </>
    )
  }

  const shown = running.slice(0, RUNNING_ROWS)
  return (
    <>
      {head}
      <div className="ov-rows">
        <table className="ov-tbl">
          <thead>
            <tr>
              <th scope="col">State</th>
              <th scope="col">Agent · profile</th>
              {/* RUNTIME, AND A NOT-STARTED ROW SAYS SO (AG-3). A LEASED or
                  DISPATCHED task has no `started_at` of this attempt, so
                  `elapsed()` prints its state word there, never the task's age
                  read as time held in the lease. */}
              <th scope="col" className="is-num">
                Runtime
              </th>
              <th scope="col" className="is-num">
                Cost so far
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.map((t) => (
              <RunningRow key={t.id} task={t} silentFor={silent.get(t.id)} leasedAt={leased.get(t.id)} />
            ))}
          </tbody>
        </table>
        {running.length > shown.length && (
          <details className="ov-more ov-running-more">
            <summary>{running.length - shown.length} more</summary>
            <table className="ov-tbl">
              <tbody>
                {running.slice(RUNNING_ROWS).map((t) => (
                  <RunningRow key={t.id} task={t} silentFor={silent.get(t.id)} leasedAt={leased.get(t.id)} />
                ))}
              </tbody>
            </table>
          </details>
        )}
      </div>
      {/* THE PHONE'S LIST (O1 390 frame): mark, name, elapsed. The sheet shows
          one of the two, by width. */}
      <ul className="ov-prun" aria-label="Running now">
        {running.map((t) => (
          <li key={t.id}>
            <StateMark state={t.state} />
            <a className="ov-prun-n" href={agentPath(t)} title={`${agentName(t)} · ${t.id}`}>
              {agentName(t)}
            </a>
            <em>
              <Runtime task={t} leasedAt={leased.get(t.id)} />
            </em>
          </li>
        ))}
      </ul>
      {foot}
    </>
  )
}

/**
 * One running agent. The state is the brand mark (marks.tsx): RUNNING is the
 * teal haloed disc and the three states before it the teal half disc, the
 * same as on Agents -- never the accent blue, which brand §3 keeps off every
 * state (#503).
 */
export function RunningRow({
  task,
  silentFor,
  leasedAt,
}: {
  task: Task
  silentFor?: number | undefined
  /** When the lease this attempt holds was granted (`leasedAtByTask`). */
  leasedAt?: string | undefined
}) {
  return (
    <tr>
      <td>
        <StateMark state={task.state} />
      </td>
      <th scope="row">
        {/* NAMED BY ITS STEP (#94), else its id; the profile under it. */}
        <a className="ov-name" href={agentPath(task)} title={`${agentName(task)} · ${task.id}`}>
          {agentName(task)}
        </a>
        {/* SILENT, ON THE ROW ITSELF (#92), from the lease's heartbeat age. */}
        {silentFor !== undefined && (
          <span className="ov-silent">silent {formatDuration(silentFor * 1000).split(' ')[0]}</span>
        )}
        <span className="ov-sub">
          {task.runner_profile}
          {/* THE WORKFLOW IT IS A STEP OF (#90). */}
          {task.workflow_id && (
            <>
              {' · '}
              <a
                className="ctl-link ov-wf"
                href={`/workflows/${encodeURIComponent(task.workflow_id)}`}
                title={task.step_id ? `step ${task.step_id} of ${task.workflow_id}` : task.workflow_id}
              >
                {task.workflow_id}
              </a>
            </>
          )}
        </span>
      </th>
      <td className="is-num">
        <Runtime task={task} leasedAt={leasedAt} />
      </td>
      <td className="is-num">
        <CostSoFar task={task} />
      </td>
    </tr>
  )
}

/**
 * The seconds each task's worker has been silent, for leases past the grace
 * (#92). `leaseLiveliness` with the page's own thresholds, the same verdict
 * the silent-workers item and Holders draw, so a row is marked exactly when
 * the item counts it.
 */
function silentByTask(leases: Result<LeasePage>): Map<string, number> {
  const out = new Map<string, number>()
  const page = dataOf(leases)
  if (page === null) return out
  for (const l of page.leases) {
    if (leaseLiveliness(l, page.thresholds).kind !== 'alive') out.set(l.task_id, l.silent_seconds)
  }
  return out
}
/**
 * When each task's live lease was granted: the lease at the task's own
 * generation, else its newest unreleased one. A LEASED or DISPATCHED task has
 * no `started_at` of this attempt, and its lease's `created_at` is the one
 * recorded instant it began holding capacity (invariant 3 counts from LEASED).
 */
function leasedAtByTask(leases: Result<LeasePage>): Map<string, string> {
  const out = new Map<string, { at: string; generation: number }>()
  const page = dataOf(leases)
  if (page === null) return new Map()
  for (const l of page.leases) {
    if (l.released) continue
    const had = out.get(l.task_id)
    if (had === undefined || l.generation > had.generation) out.set(l.task_id, { at: l.created_at, generation: l.generation })
  }
  return new Map([...out].map(([id, v]) => [id, v.at]))
}

function startKey(t: Task): number {
  const v = new Date(t.started_at ?? t.created_at).getTime()
  return Number.isFinite(v) ? v : Number.MAX_SAFE_INTEGER
}

/**
 * The one cell on this screen that has to move on its own, and therefore the
 * one place the 1Hz clock lives.
 *
 * A LEASED task has no `started_at` of its own attempt -- lifecycle writes it
 * on DISPATCHED -> STARTING -- so `elapsed` says "leased", never "0s" and
 * never the task's age after the word, and says it on a retry too, whose
 * `started_at` is the previous attempt's. Only STARTING and RUNNING rows
 * tick here.
 */
function Runtime({ task, leasedAt }: { task: Task; leasedAt?: string | undefined }) {
  const now = useNow()
  const e = elapsed(task, now)
  // A ROW BEFORE ITS ATTEMPT STARTS IS TIMED FROM ITS LEASE (browser QA N6,
  // 2026-10-04): this column repeated "dispatched", the State column's word.
  // The state stays in the State column; this says how long it has held the
  // slot, and a dash with why when the lease was not read.
  if (e.phase === 'waiting' && CONCURRENCY_STATES.has(task.state)) {
    const at = leasedAt === undefined ? NaN : new Date(leasedAt).getTime()
    if (!Number.isFinite(at)) {
      return <Dash why={`No attempt has started yet, and the lease that would date it was not read, so how long it has held capacity is unknown.`} />
    }
    return (
      <span title={`Holding capacity since its lease was granted ${timeAgo(at, now)}; no attempt has started yet.`}>
        {formatDuration(now - at)}
      </span>
    )
  }
  return <>{e.text}</>
}

// ---------------------------------------------------------------------------
// 3. Spend
// ---------------------------------------------------------------------------

/**
 * Tokens and token cost, over a named sample.
 *
 * THE SCOPE IS THE CARD. `cost_usd` and the four token counts live on the
 * ATTEMPT and no route aggregates them, so a total can only be assembled one
 * request per task -- which makes the sample size a request count and the
 * figure a sample rather than a bill. The card's note and foot carry that; the
 * five sentences that used to are in the `?`.
 *
 * A NULL IS NOT A ZERO, and it is the difference between a mock run that
 * genuinely cost nothing and a result whose usage failed to parse.
 * `record_usage` in agent_worker/control.py omits a key the runner did not
 * report and says so in its own docstring; this card counts how many attempts
 * carried no figure and draws that count rather than folding them in as zeros.
 */
function SpendBody({ state, tasks }: { state: Result<SpendRollup>; tasks: Result<TaskPage> }) {
  const now = usePageClock()
  if (state.status === 'loading') return <Reading rows={4} />
  if (state.status === 'error') {
    // TWO FAILURES, AND WHAT THE READER DOES NEXT DIFFERS. This rollup is
    // built ON the task page: when the task read is the one that failed, no
    // attempt read was made at all, nothing is known about the attempt route,
    // and the thing to fix is the task list.
    if (tasks.status === 'error') {
      const b = blindness(tasks.error)
      return (
        <CardAbsent
          kind={b.admin ? 'admin' : 'failed'}
          heading="Spend unassembled"
          say={`Spend is summed from the attempts of the most recent tasks, and the task list itself could not be read. ${b.why} No attempt read was made, so nothing here is a statement about spend.`}
        />
      )
    }
    return (
      <CardAbsent
        kind="failed"
        heading="No attempt read completed"
        say={`${errorHeading(state.error)} — ${state.error.message} No figure is shown.`}
        explain="read-failed"
      />
    )
  }
  if (state.status === 'empty') {
    // TWO ZEROS, AND THEY ARE NOT THE SAME ZERO. "Every task on the page has
    // no attempts" describes tasks that exist; on a tenant with no tasks at
    // all it would describe a page that is not there.
    if (tasks.status === 'empty') {
      return (
        <CardAbsent
          kind="zero"
          heading="No task exists"
          say="The task read succeeded and returned nothing — a real zero, so there are no attempts to sum."
          explain="absent-vs-zero"
        />
      )
    }
    return (
      <CardAbsent
        kind="zero"
        heading="No task has run"
        say="Every task on the page has an attempt count of 0 — a real zero."
        explain="attempt-documents"
      />
    )
  }

  const s = state.data
  const unmeasured = s.attempts - s.attemptsWithCost

  return (
    <>
      {/* THE FIGURE, AND THE ONE THING IT IS NOT (O1 `.big`). An absent sum is
          an em dash at the figure step with the absent mark, never `$0.00`. */}
      <div className="ov-big">
        <b
          className={`ov-figure${s.costUsd === null ? ' is-absent' : ''}`}
          data-measured={s.costUsd === null ? 'false' : 'true'}
          // THE PRECISE FIGURE IS THE NAME, the rounded one the picture (#97).
          aria-label={s.costUsd === null ? undefined : `${preciseMoney(s.costUsd)} of token cost`}
        >
          {s.costUsd === null ? <span className="ctl-em">&mdash;</span> : money(s.costUsd)}
        </b>
        <small>token cost · a sample</small>
        {/* A SUM OVER A SAMPLE IS NOT A TOTAL (OV-4), so a measured figure
            carries the kit's partial mark -- always, because it is always a
            sample: the newest tasks that have run, one attempt read per task,
            token cost only. An absent figure carries the absent mark instead. */}
        <span className="ov-figure-mark">
          {s.costUsd === null ? (
            <Mark
              kind="absent"
              say="No attempt in this sample reported a cost. That is an absent measurement and not $0.00: record_usage omits a key the runner did not report."
            />
          ) : (
            <Mark
              kind="partial"
              say={`Summed from the ${countOf(s.attempts, 'attempt')} of the ${s.tasksSampled} newest of ${countOf(s.tasksWithAttempts, 'task')} that have run on the ${s.tasksOnPage} most recently created. A sample of token cost, not a bill: older tasks are outside it, and no billing integration records Cloud Run, Firestore or GCS spend.`}
            />
          )}
        </span>
      </div>

        {/* THE FOUR TOKEN COUNTS, IN WORDS (#97). The proportion bar that
            stood here is gone; `TokenMix` says why. */}
      <TokenMix s={s} />

      {/* PROVENANCE, WHERE PROVENANCE GOES, AS A RUN OF CLAUSES (OV-14).
          THE ORDER IS THE OWNER'S: what the sum covers, then the holes in that
          coverage, then how old it is and how it moves. It holds at every
          width; where a line breaks depends on the counts, and nothing
          promises a particular break. */}
      <p className="ov-foot">
        <FootRun>
          {/* 1. WHAT THE SUM COVERS (OV-4): the attempts it read and the
              window it read them in -- `tasksWithAttempts` was computed and
              never shown, so the coverage the figure is partial against was
              nowhere on the screen. */}
          <span>
            {countOf(s.attempts, 'attempt')}, {s.tasksSampled} newest of {countOf(s.tasksWithAttempts, 'task')}
          </span>
          {/* 2. THE HOLES: attempts that carried no cost figure... */}
          {unmeasured > 0 && <span className="ov-warn">{unmeasured} unmeasured</span>}
          {/* 3. ...and attempt reads that failed. THE COUNT OF WHAT IS
              MISSING, KEPT AS A DIGIT ON THE SURFACE: the size of the hole is
              the thing that must not need a hover. ONE MESSAGE IS ONE
              FAILURE'S -- the rollup keeps only the first error it saw (api.ts,
              loadSpend), so the rest are unexplained rather than explained
              wrongly. NO `?` (B7.4): the accessible name is `partial-read`
              with THIS response's numbers and message in it, which a shared
              topic cannot have. */}
          {s.failedReads > 0 && (
            <span
              className="ov-bad"
              aria-label={`${s.failedReads} of ${s.tasksSampled} attempt reads failed, so their spend is in none of these figures. ${
                s.failedReads === 1
                  ? `It failed with: ${s.failedDetail ?? 'no message was recorded'}`
                  : `One failed with: ${s.failedDetail ?? 'no message was recorded'} — the other ${s.failedReads - 1} are unexplained here.`
              }`}
            >
              {s.failedReads} of {s.tasksSampled} reads failed
            </span>
          )}
          {/* 4. HOW OLD IT IS, and 5. HOW IT MOVES: the sum is on no timer --
              only a press of refresh re-sums it (OV-16 set the cadence of
              `/v1/stats`, not of this). IN PLAIN WORDS (#97): it read `no
              re-poll`, which is the poll's jargon for the same fact. */}
          {footFor(state, 'not summed', now) !== null && <span>{footFor(state, 'not summed', now)}</span>}
          <span>updates only on refresh</span>
        </FootRun>
      </p>
    </>
  )
}

/**
 * The four token counts, each with its word and its figure.
 *
 * THE PROPORTION BAR IS GONE (#97). On a real sample it was 90-95% cache
 * read, with `in` under a pixel wide, so the one thing it drew was that
 * caching works. The issue allowed replacing it with spend by runner profile;
 * the rollup (`SpendRollup`, api.ts) carries totals only, with no split by
 * profile, so there was nothing on hand to draw and the bar was dropped
 * rather than rebuilt from a second read. Its swatches keyed nothing once it
 * went, so they went too.
 *
 * WORDS, NOT ABBREVIATIONS. `c-rd` and `c-wr` were explained nowhere on the
 * screen; `cache read` and `cache write` need no help card, and `input` and
 * `output` are written out beside them so the four read as one list.
 *
 * AN ABSENT COUNT IS AN EM DASH, never a digit: a count no attempt reported is
 * not zero tokens, and a measured zero is.
 */
function TokenMix({ s }: { s: SpendRollup }) {
  const parts = [
    { key: 'input', v: s.inputTokens },
    { key: 'output', v: s.outputTokens },
    { key: 'cache read', v: s.cacheReadTokens },
    { key: 'cache write', v: s.cacheCreationTokens },
  ] as const

  return (
    <dl className="ov-kv">
      {parts.map((p) => (
        <div className="ov-kv-row" key={p.key}>
          <dt>{p.key} tokens</dt>
          <dd>{p.v === null ? <i className="ctl-em" title="Not reported: not zero tokens.">&mdash;</i> : tokens(p.v)}</dd>
        </div>
      ))}
    </dl>
  )
}

/** A number of tokens, never 0 for an absent measurement. */
function tokens(v: number): string {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`
  return String(v)
}

/**
 * Dollars of TOKEN cost, as the headline prints it: TWO DECIMALS AT EVERY SIZE
 * (#97). It was four under ten dollars and two above, so the figure changed
 * shape as it grew and `$9.9981` sat where `$10.00` would be a moment later.
 * The precise sum is `preciseMoney`, on the headline's accessible name.
 *
 * UNDER HALF A CENT IS `<$0.01`, NEVER `$0.00`. A measured cost that rounds to
 * zero would claim the sample was free, which is the absent-vs-zero lie by a
 * different door; an EXACT zero is a measurement and prints as one.
 */
function money(v: number): string {
  if (v > 0 && v < 0.005) return '<$0.01'
  return `$${v.toFixed(2)}`
}

/**
 * The same sum to the micro-dollar, trailing zeros dropped down to two
 * decimals: `$0.0312`, `$12.345678`, `$1.25`. For the accessible name, where
 * a reader who wants the figure behind `$0.03` gets it without a hover.
 */
function preciseMoney(v: number): string {
  return `$${v.toFixed(6).replace(/0{1,4}$/, '')}`
}

// ---------------------------------------------------------------------------
// 4. The subscription pool
// ---------------------------------------------------------------------------

/**
 * How much room the Claude subscription accounts have left.
 *
 * THE BINDING WINDOW, NEVER AN AVERAGE. An account at 5% of its five-hour and
 * 90% of its seven-day is stopped by the seven-day, and averaging them to 47%
 * sends agents at an account that is about to refuse.
 * `Account._binding_remaining` in the broker picks the same way.
 *
 * The POOL's headroom is the BEST usable account's, not the sum and not the
 * mean: one agent is served by one account, so what matters is whether any
 * account can take it. An account that is REAUTH_REQUIRED, never observed, or
 * too stale to trust is excluded from the figure and counted in `usable` --
 * excluded rather than treated as full, because "we do not know" is not
 * "there is room".
 *
 * AND A WINDOW THAT HAS ALREADY RESET IS ONE OF THE THINGS WE DO NOT KNOW.
 * `bindingWindow` scores a `reset: true` window as FULL -- deliberately, so a
 * window that refilled cannot be the binding one while another still has room
 * -- which means it comes back as binding precisely when every window has
 * reset, or on a tie. Its `utilization` then describes the window BEFORE the
 * reset. `readingOf` is the one place that distinction is encoded, so it is
 * asked rather than re-derived: only a `live` reading is a figure.
 *
 * THE FIGURE IS % USED, AND IT NAMES ITS ACCOUNT (OV-1, owner decision
 * 2026-09-25). It was % LEFT -- `72 % left` over account rows printing % used
 * as a bare `%`, so `74` one line below `72` was the fullest account, not a
 * roomier one. One polarity everywhere (this screen, Accounts, `sc`), and the
 * headline says whose figure it is: `28 % used · laptop`. The best account is
 * still the one with the most room, which is the one with the LEAST used.
 *
 * `usable` AND `total` ARE THE COVERAGE, and the headline's track no longer
 * draws them (OV-2): it draws the figure. When `usable < total` the headline
 * carries the kit's partial mark instead, with this function's sentences as
 * its accessible name.
 *
 * EXPORTED so the arithmetic below can be asserted. A verifier found on
 * 2026-09-22 that replacing `accounts.length - unread - projected -
 * notServing` with plain `accounts.length` left the entire suite green. The
 * tile would then have read "best of 3 usable accounts" while its own foot
 * named two of the three as having no reading. Nothing could catch it because
 * nothing could call this.
 */
export function accountHeadroom(state: Result<AccountsPage>): {
  /** The best usable account's binding window, % USED. null when none has one. */
  pct: number | null
  /**
   * That account's id, label, binding window and when its reading was taken.
   * null exactly when `pct` is.
   */
  best: { id: string; label: string; window: string; observedAt: string } | null
  sub: string
  foot: string | undefined
  reading: boolean
  absent: string | null
  /** Accounts this figure could have come from. null when nothing was read. */
  usable: number | null
  /** Accounts in scope, whether or not any reading arrived. */
  total: number
} {
  if (state.status === 'loading') {
    return {
      pct: null,
      best: null,
      sub: 'reading the subscription pool',
      foot: undefined,
      reading: true,
      absent: null,
      usable: null,
      total: 0,
    }
  }
  if (state.status === 'error') {
    return {
      pct: null,
      best: null,
      sub: 'the subscription pool could not be read',
      foot: undefined,
      reading: false,
      absent: null,
      usable: null,
      total: 0,
    }
  }
  if (state.status === 'empty') {
    return {
      pct: null,
      best: null,
      sub: 'no account is registered, so the subscription pool supplies nothing',
      foot: 'read succeeded · a real zero',
      reading: false,
      absent: 'no accounts',
      usable: 0,
      total: 0,
    }
  }

  const accounts = state.data.accounts
  // THE SCOPE IS PART OF THE FIGURE. `/v1/accounts` returns the accounts this
  // TENANT owns or has been lent, never the platform's -- so `accounts.length`
  // is not the number registered, and a sentence here that says "registered"
  // makes a platform-wide claim out of a tenant-wide list.
  const scope = state.data.tenant_id ? `in ${state.data.tenant_id}` : 'across every tenant'
  // The raw id, beside the display string above. `unreadableFor` matches on the
  // id, and `scope` has already been turned into a sentence fragment.
  const scopeId = state.data.tenant_id

  // `pct` IS % USED. The best account is the one with the least of its
  // binding window used, which is the one with the most room.
  let best: { pct: number; key: string; observedAt: string; id: string; label: string } | null = null
  // Accounts whose headroom is UNKNOWN: never polled, or polled with no
  // window in the answer. Named, not counted -- "1 account excluded" does not
  // tell anyone which credential to go and look at.
  const unread: string[] = []
  // Real figures that are no longer current: stale, or describing a window
  // that has since reset. Kept apart from `unread`, because "old information"
  // and "no information" are opposite facts.
  const projected: string[] = []
  // PAUSED, DRAINING and REAUTH_REQUIRED. Deliberate or broken, but not
  // serving, so not headroom either.
  const notServing: string[] = []
  // Accounts the broker is ALREADY SKIPPING for this tenant, because this
  // tenant reported them unreadable. Their headroom is real and unavailable,
  // and it is usually the LARGEST figure on the screen precisely because
  // nothing has been spending it.
  const unreadableHere: string[] = []

  for (const a of accounts) {
    // PAUSED and DRAINING are deliberate operator states, not faults -- but
    // they do not serve, so they are not counted as headroom either.
    if (a.state !== 'AVAILABLE') {
      notServing.push(a.label)
      continue
    }
    if (unreadableFor(a, scopeId)) {
      unreadableHere.push(a.label)
      continue
    }
    // The two ways a reading can be missing, told apart the same way the
    // account rows tell them apart: nobody has ever polled this account, or
    // the poll landed and reported no window. Both are unknown headroom;
    // NEITHER is 100%. `usagepoll.py` fails safe by recording nothing when it
    // cannot read an account's token, so this is the shape the live platform
    // produces.
    if (a.observed_at === null) {
      unread.push(a.label)
      continue
    }
    const w = bindingWindow(a)
    if (w === null) {
      unread.push(a.label)
      continue
    }
    const r = readingOf(a, w.key)
    if (r.kind === 'never' || r.kind === 'absent') {
      unread.push(a.label)
      continue
    }
    if (r.kind !== 'live') {
      // `reset` and `stale`. The account may well have a full window. Nothing
      // has measured it since, and a projection is not headroom.
      projected.push(a.label)
      continue
    }
    const used = Math.max(0, Math.min(100, r.pct))
    if (best === null || used < best.pct) {
      best = { pct: used, key: w.key, observedAt: r.observedAt, id: a.account_id, label: a.label }
    }
  }

  const usable =
    accounts.length -
    unread.length -
    projected.length -
    notServing.length -
    unreadableHere.length

  if (best === null) {
    return {
      pct: null,
      best: null,
      sub: `${countOf(accounts.length, 'account')} ${scope} · ${absenceSentence(unread, projected, notServing, unreadableHere)}`,
      foot: 'a missing reading is not 0% used, and a window that has cleared has not been read since',
      reading: false,
      absent: 'nothing measured',
      usable: 0,
      total: accounts.length,
    }
  }

  return {
    pct: best.pct,
    best: { id: best.id, label: best.label, window: best.key.replace(/_/g, '-'), observedAt: best.observedAt },
    // THE AGE IS PART OF THE FIGURE. A percentage with nothing saying whether
    // it was measured a minute or four days ago is a claim without provenance.
    sub: `${best.label}, the best of ${countOf(usable, 'usable account')}, has ${Math.round(best.pct)}% of its ${best.key.replace('_', '-')} window used · that window binds · read ${timeAgo(best.observedAt)}`,
    // DERIVED, NEVER ASSERTED, AND IT NAMES NAMES. The previous wording --
    // "every registered account has a current reading" -- was a sentence
    // chosen by `unusable === 0` over a tenant-scoped list, and the accounts
    // it was silent about were exactly the ones worth knowing about.
    // An account the pool is SKIPPING for this tenant is named too: it is one
    // of the holes the headline's partial mark is about (OV-2), and a mark
    // whose sentence said every account had a current reading would be
    // contradicting itself.
    foot: unread.length > 0 || projected.length > 0 || notServing.length > 0 || unreadableHere.length > 0
      ? absenceSentence(unread, projected, notServing, unreadableHere)
      : `all ${countOf(accounts.length, 'account')} ${scope} ${accounts.length === 1 ? 'has' : 'have'} a current reading`,
    reading: false,
    absent: null,
    usable,
    total: accounts.length,
  }
}

/** "1 account", "3 accounts". A figure the reader can read out loud. */
function countOf(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

/**
 * What the pool figure does NOT cover, in words, naming the accounts.
 *
 * Three clauses, kept separate on purpose. An account nobody has polled has
 * unknown headroom; an account whose reading is stale or whose window has
 * cleared has a real figure that is no longer current; an account that is
 * paused or needs signing in has headroom that cannot be spent. Collapsing
 * them into one count -- "N accounts excluded -- paused, stale, cleared or
 * needing sign-in" -- was the old wording, and it did not even list the case
 * that actually applies here.
 *
 * A fourth, `skipped`: accounts the pool will not hand this tenant because it
 * reported them unreadable. Their headroom is real and unavailable.
 *
 * THIS IS AN ACCESSIBLE NAME RATHER THAN A PARAGRAPH: the headline's partial
 * mark carries it (OV-2), and so does the headline itself when nothing could
 * be measured. The sentence is what a screen reader gets.
 */
function absenceSentence(
  unread: string[],
  projected: string[],
  notServing: string[],
  skipped: string[] = [],
): string {
  const parts: string[] = []
  if (unread.length > 0) {
    parts.push(
      `${unread.join(', ')} ${unread.length === 1 ? 'has' : 'have'} no reading, so ${unread.length === 1 ? 'its' : 'their'} headroom is unknown rather than full`,
    )
  }
  if (projected.length > 0) {
    parts.push(
      `${projected.join(', ')} ${projected.length === 1 ? 'is' : 'are'} stale or cleared, so the last figure describes an earlier window`,
    )
  }
  if (notServing.length > 0) {
    parts.push(`${notServing.join(', ')} not serving`)
  }
  if (skipped.length > 0) {
    parts.push(`${skipped.join(', ')} ${skipped.length === 1 ? 'is' : 'are'} being skipped by the pool for this tenant`)
  }
  // Only reachable with every list empty when the caller has a figure, and
  // that caller words it itself; this is the honest fallback either way.
  return parts.length > 0 ? parts.join(' · ') : 'nothing has been read'
}
