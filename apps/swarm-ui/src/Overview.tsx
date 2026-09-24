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
  type SpendRollup,
} from './api'
import { blindness, deriveChecks, type Check, type Problem } from './checks'
import type { TopicId } from './help'
import { HelpCard, HelpNote } from './HelpCard'
import { errorHeading, isPaused, type ApiError, type Result } from './fetch'
import { timeAgo } from './Shell'
import {
  CONCURRENCY_STATES,
  bindingWindow,
  elapsed,
  headroomFor,
  isProjected,
  needsAHuman,
  overCeiling,
  poolLabel,
  readingOf,
  stateTone,
  unreadableFor,
  type AccountReading,
  type AccountsPage,
  type Capacity,
  type Pool,
  type Stats,
  type Task,
  type TaskPage,
  type TaskState,
} from './types'

/**
 * OVERVIEW. The landing screen: what is running, what is wrong, what capacity
 * is left, what it is costing.
 *
 * A DASHBOARD, NOT A DOCUMENT. This screen used to answer its five questions in
 * prose -- 309 rendered words on a HEALTHY platform, eight sentences of which
 * said nothing was wrong eight different ways. It now answers them in figures,
 * dials, tracks and marks, and `prose.budget.test.tsx` holds the count.
 *
 * THE INVARIANT IS UNCHANGED AND THE MEDIUM IS WHAT MOVED.
 *
 *   This console must never present an absence as a measurement. A figure
 *   nothing reported is not zero. A state nobody derived is not QUEUED. A
 *   partial total is not a total.
 *
 * That was carried by paragraphs. It is now carried by the absence vocabulary
 * in `styles.css` §B4.4 -- `.ctl-mark`, `.ctl-em`, the hatched track, the
 * hatched dial arc, `.ctl-pending` -- each of which is visible without
 * hovering anything, survives greyscale and a screenshot, and is ATTACHED to
 * the figure it qualifies. That last property is the one the prose never had:
 * a paragraph can sit beside a figure it does not describe; an attribute on
 * the figure cannot.
 *
 * WHERE THE SENTENCES WENT, in the order the design system allows them
 * (docs/web-ui/design-system.md §8.4): the unit and the figure itself, then
 * `.ctl-card-note` beside the title, then `.ctl-card-foot` for provenance,
 * then the `?` card, then `docs/`. Every mark also carries the full sentence
 * as its accessible name, so the words are one keystroke away and are not
 * behind a hover -- which is precisely how the previous attempt failed.
 *
 * FIVE QUESTIONS, FIVE CARDS, and each ends in a link to the screen that goes
 * deeper -- except the derived checks, which have no deeper screen because
 * there is no problem board and should not be one; every row of that card
 * links to the OBJECT it is about instead.
 *
 *   1. CAPACITY -- used against available, and WHICH pool binds each runner
 *      profile. The conjunction is the whole trap: a task must clear every
 *      pool in its list at the same moment, so its ceiling is the MINIMUM
 *      across them. Raising the pool that is not binding changes nothing.
 *   2. WHAT IS RUNNING, with runtime so far.
 *   3. WHAT IS WRONG, derived from real state -- never from an alert model,
 *      because this platform has none. Workflows count as running things here.
 *   4. SPEND, in tokens and dollars of token cost, with the scope named.
 *   5. THE SUBSCRIPTION POOL's headroom, which is the ceiling that binds
 *      first in practice.
 *
 * EIGHT READS, INDEPENDENTLY. One failing must not blank the page and must not
 * leave the page looking complete, so each card owns its own state and the
 * facts strip in the page head counts what landed, what is still in flight,
 * what failed and what was refused for want of admin. Those are four different
 * things and the strip's dot plus its accessible name say which.
 *
 * WHAT THIS SCREEN DOES NOT DRAW, and why it is not a placeholder:
 *   - infrastructure cost in dollars. There is no billing integration of any
 *     kind, so Cloud Run, Firestore and GCS spend are simply not recorded. The
 *     spend card's foot names the scope rather than showing a figure that
 *     would be read as the whole bill.
 *   - an alert inbox. Nothing stores, routes or acknowledges an alert. What is
 *     here is DERIVED from state that exists, re-derived on every read, and
 *     the coverage dial says which checks could not run rather than implying
 *     silence is calm.
 */
export function OverviewScreen() {
  // TWO REFRESH CADENCES, on purpose.
  //
  // `live` drives the five cheap reads and re-runs on a timer: capacity, the
  // task page, leases, providers and accounts are all single indexed reads and
  // are the ones that actually move.
  //
  // `heavy` drives the two expensive ones and re-runs ONLY when a person asks.
  // `/v1/stats` is one Firestore count() per state -- twelve aggregation
  // queries -- and the spend rollup is a fan-out of one request per sampled
  // task. Polling either would make the screen that is open all day the most
  // expensive thing on the platform.
  //
  // BOTH THEREFORE CARRY THEIR READ AGE, in the tile foot and, for spend, in
  // the card foot as well. A figure that does not re-poll sitting beside five
  // that refresh every twenty seconds is indistinguishable from them unless it
  // says how old it is, and the one that goes stale is denominated in dollars.
  const [live, setLive] = useState(0)
  const [heavy, setHeavy] = useState(0)
  // The id the subscription-account group's heading publishes its explanation
  // at. It is here rather than inside the group because the group is inline
  // JSX in this component's return, not a component of its own.
  const accountsHelpId = useId()
  const refresh = useCallback(() => {
    setLive((n) => n + 1)
    setHeavy((n) => n + 1)
  }, [])

  useEffect(() => {
    const id = setInterval(() => setLive((n) => n + 1), POLL_MS)
    return () => clearInterval(id)
  }, [])

  const capacity = useRead(loadCapacity, live)
  const tasks = useRead(loadTasks, live)
  const leases = useRead(loadLeases, live)
  const providers = useRead(loadProviders, live)
  const accounts = useRead(loadAccountPool, live)
  // ON THE LIVE CADENCE, with the other five, because a workflow that has
  // stopped moving is the fact this screen was worst at reporting and a figure
  // that only refreshes when someone presses a button cannot report it.
  const workflows = useRead(loadWorkflows, live)
  const stats = useRead(loadStats, heavy)

  const spend = useSpend(tasks, heavy)

  // Typed as `Result<unknown>` because this list only ever asks about a read's
  // OUTCOME, never its payload. Leaving it to inference makes it a union of
  // differently-parameterised Results that matches no single `Result<T>`.
  const reads: Result<unknown>[] = [
    capacity, tasks, leases, providers, accounts, workflows, stats, spend,
  ]

  // A 401 is a PAGE-level state, not a card-level one: an expired IAP session
  // fails all of them at once, and seven independently empty cards is the bug
  // this whole UI is built against.
  if (reads.some((r) => isKind(r, 'unauthenticated') || isKind(r, 'session_expired'))) {
    return (
      <div className="ctl-empty is-failed ov-page-empty">
        <Mark kind="unread" say="The API answered a sign-in page instead of data, so nothing on this screen is a reading of the platform." />
        <h3>Session expired</h3>
        <button className="retry" onClick={() => window.location.reload()}>
          Reload to sign in
        </button>
      </div>
    )
  }

  // FOUR OUTCOMES, COUNTED SEPARATELY, because they are four different facts
  // about this screen and collapsing any two of them produces a claim that is
  // false at first paint.
  //
  //   - landed: a reading arrived (`empty` is a reading: it is a real zero).
  //   - pending: still in flight. On first paint that is ALL of them, and
  //     "8/8" printed then is the screen's own provenance lying about itself.
  //   - refused: 403 on the one admin route here. Not a failure -- a non-admin
  //     legitimately cannot make it -- but it did not land either.
  //   - failed: everything else.
  const landed = reads.filter(
    (r) => r.status === 'ok' || r.status === 'empty' || r.status === 'stale',
  ).length
  const pending = reads.filter((r) => r.status === 'loading').length
  const refused = reads.filter((r) => isKind(r, 'admin_required')).length
  const broken = reads.length - landed - pending - refused

  // `Date.now()` IS TAKEN HERE, not inside the checks, and the dependency list
  // is deliberately the reads rather than a clock. Two checks measure an age
  // and one of them decides whether a workflow is stalled; re-deriving on a
  // ticking clock would re-render every card on this screen once a second to
  // move a threshold that is ten minutes wide.
  const checks = useMemo(
    () =>
      deriveChecks(
        { capacity, tasks, leases, providers, accounts, workflows, stats },
        Date.now(),
      ),
    [capacity, tasks, leases, providers, accounts, workflows, stats],
  )
  // `found` IS NOT COUNTED HERE ANY MORE, and that is the point. It existed to
  // feed two things: the `has-alarm` class on the old five-card grid, and the
  // Attention tile's figure. Both are gone -- the grid because three panels in
  // two tracks has no orphan to manage, the tile because the lead says the
  // same number at page rank one line above where the tile stood. The lead
  // derives its own count from the same `checks`, so there is no second place
  // for the figure to be computed and therefore no way for the two to disagree.
  const tenant = dataOf(tasks)?.tenant_id ?? null

  return (
    <>
      <style>{OVERVIEW_CSS}</style>

      {/* TITLE, THEN PROVENANCE ON ONE LINE UNDER IT.
          -------------------------------------------------------------------
          This used to be a title on the left and a right-aligned cluster on
          the right: three `.ctl-fact`s and a bordered `refresh` button, pinned
          to the far edge of a 1145px column. Two problems with that, and the
          second is the structural one.

          It disagreed with every other screen in the product. `Screen`
          (Shell.tsx) renders a title with a summary line UNDER it -- "26
          loaded · 6 live · u-bogdan · read just now refresh" -- on Agents,
          Runtimes, Pools, Accounts and the rest. The landing page was the one
          screen with a different header, which is the most expensive place in
          a console to be inconsistent.

          And a right-aligned strip 900px from the title it qualifies is not
          read as belonging to it. Provenance is a subtitle: it goes where a
          subtitle goes.

          The button went with it. `refresh` is one of three controls on this
          screen that do not navigate, and it is the only one that wore a box;
          `.ctl-link`'s ink-plus-underline is the affordance every other
          in-page control here uses. */}
      <div className="ctl-page-head ov-head">
        <h1>Overview</h1>
        <ul className="ctl-facts ov-prov">
          <li className="ctl-fact">
            <b>scope</b>
            {/* THE SCOPE IS NOT DECORATION. `/v1/stats` and `/v1/tasks` are
                tenant-scoped while the `global` pool is platform-wide, so a
                figure on this screen means nothing until you know which of
                the two it is. */}
            {tenant === null ? <i className="ctl-em">&mdash;</i> : tenant}
          </li>
          <li
            className="ctl-fact ov-tally"
            aria-label={readTally(reads.length, landed, pending, refused, broken)}
          >
            {/* THIS SCREEN'S ONE `?` (B7.4), AND IT IS ON THE READS TALLY ON
                PURPOSE. Overview carried twelve help anchors -- nine on empty
                states whose marks already spell themselves out, one on a `20s`
                that is the cadence it was explaining, one on a card title and
                one on a fraction. All twelve are gone except this, and the
                sentence each of them published is still at its own label, as a
                hidden description, for a screen reader.
                The tally is where the key belongs because it is the figure that
                says HOW MUCH OF THIS PAGE IS REAL: `6/8` means two of the eight
                reads behind the cards below did not land, and every em dash
                further down the page is one of those two. `absent-vs-zero` is
                the rule that makes that readable -- a figure nobody measured is
                never drawn as a zero -- and it is the one rule this whole
                console is built around, so it is the one worth a glyph at the
                top of the screen somebody lands on first. */}
            <b>
              reads
              <HelpCard topic="absent-vs-zero" />
            </b>
            {/* THE DOT IS THE SEVERITY AND THE FRACTION IS THE FACT. "8/8"
                with a green disc and "6/8" with a red diamond are different
                pictures before either is read, and the sentence that used to
                be here is this element's accessible name. */}
            <i className={`ctl-dot ${tallyTone(pending, refused, broken)}`} aria-hidden />
            <span className="ov-num">
              {landed}/{reads.length}
            </span>
          </li>
          <li
            className="ctl-fact"
            // INTERPOLATED, NEVER TYPED OUT. This used to be the words
            // "every 20 seconds" three hundred lines from the constant.
            aria-label={`re-read every ${POLL_MS / 1000} seconds`}
          >
            {/* NO `?`. `poll 20s` IS `poll-cadence` -- the topic said the page
                re-reads on a fixed timer and named the interval, and the label
                and the figure beside it say both, in three characters, without
                anything to open. The accessible name above states it as a
                sentence for a reader who gets the strip read to them. */}
            <b>poll</b>
            <span className="ov-num">{POLL_MS / 1000}s</span>
          </li>
          <li className="ctl-fact">
            <button className="ctl-link ov-refresh" onClick={refresh}>
              refresh
            </button>
          </li>
        </ul>
      </div>

      {/* REGION 1 OF TWO -- THE LEAD.
          -------------------------------------------------------------------
          THE ONE QUESTION THAT CHANGES WHAT SOMEBODY DOES NEXT GETS THE TOP OF
          THE PAGE, UNBOXED, AT PAGE RANK.

          It used to be drawn twice at tile rank: a third of the metric strip
          ("Attention · 10 things") and then the first card of a five-card
          grid, in a box the same size and weight as Spend. A control plane
          that gives "what is wrong" the same silhouette as "what did it cost"
          has told its reader that the two are equally urgent, and the reader
          believes it.

          It is a `.section` rather than a `.ctl-card` because a region is a
          change of subject rather than an object (design-system.md §13.3), and
          this one is the subject of the page. The duplicate tile is GONE --
          §14 of the sheet is about an absence occupying its space, not about
          a fact occupying two. */}
      <section className="section ov-lead">
        <AttentionLead checks={checks} />
      </section>

      {/* REGION 2 -- THE STATE OF THE PLATFORM.
          Four figures, then the three panels that hold the detail behind
          them. One hairline separates it from the lead, which is §13.3's
          region rule spent once on the page's one real change of subject
          rather than four times on a list of boxes. */}
      <section className="section ov-state">
        <MetricStrip
          capacity={capacity}
          stats={stats}
          accounts={accounts}
          spend={spend}
        />

        {/* THE GRID IS ASYMMETRIC ON PURPOSE AND IT HOLDS THREE PANELS, NOT
            FIVE. The old grid was `repeat(N, 1fr)` with ten parity selectors
            underneath it, because five equal cards never fill a three-track
            row and the last one had to be widened by whichever shortfall the
            track count produced. Three panels in a stated two-track layout
            has no orphan, so all ten of those rules are deleted along with
            the bug class they were managing.

            WIDTH IS ALLOCATED BY WHAT NEEDS IT. "Running" is a table and gets
            two thirds; "Spend" is one figure and a bar and gets a third;
            "Headroom" is a four-column utilisation row five times over and
            gets the whole width -- which is also the fix for F1 of the
            overflow inventory, where the same rows in a 400px card rendered
            `mock · 1…` for `mock · 15 can start`. */}
        <div className="ov-grid">
          <section className="ctl-card ov-running">
            <CardHead title="Running" href="#work/running" cta="agents" />
            <RunningBody tasks={tasks} stats={stats} />
          </section>

          <section className="ctl-card ov-spend">
            {/* `#work/timeline`, and `cta="timeline"` with it. The Timeline
                pane moved out of a section called History when the nav
                collapsed to three, so both the address AND the word on the
                link changed -- a link still reading "history" would name a
                section this product no longer has. */}
            <CardHead title="Spend" href="#work/timeline" cta="timeline" explain="token-cost" />
            <SpendBody state={spend} tasks={tasks} />
          </section>

          {/* TWO CARDS BECAME ONE PANEL, AND THAT IS AN INFORMATION
              ARCHITECTURE CHANGE RATHER THAN A LAYOUT ONE.
              ---------------------------------------------------------------
              "Capacity" and "Subscription pool" were two boxes at opposite
              ends of a five-card grid answering ONE question: can I start more
              work, and what stops me. They are the two ceilings, and they bind
              in sequence -- a task clears every pool in its list, then takes a
              subscription account. Reading them as unrelated panels is how an
              operator raises the pool that is not binding, which is the exact
              mistake `CapacityBody`'s own comment says this screen exists to
              prevent.

              One panel, one title, two labelled groups inside it. The groups
              keep their own provenance foot, because they are two different
              reads and a single foot would have to average two ages. */}
          <section className="ctl-card ov-headroom">
            <CardHead
              title="Headroom"
              note={tenant === null ? undefined : `tenant ${tenant}`}
              href="#capacity/profiles"
              cta="pools"
              explain="pools-all-at-once"
            />
            <div className="ov-groups">
              <div className="ov-group">
                <h3 className="ov-grouphead">By runner profile</h3>
                <CapacityBody state={capacity} />
              </div>
              <div className="ov-group">
                {/* WHICH WINDOW A UTILISATION FIGURE IS OF is `binding-window`,
                    and it is published here as a description rather than as a
                    `?`: the rows underneath already print the window each
                    figure belongs to, so the glyph was opening a card to say
                    what the row beside it says. */}
                <h3 className="ov-grouphead" aria-describedby={accountsHelpId}>
                  By subscription account
                  <HelpNote topic="binding-window" id={accountsHelpId} />
                </h3>
                <AccountsBody state={accounts} />
              </div>
            </div>
          </section>
        </div>
      </section>
    </>
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
 * How often the cheap reads re-run.
 *
 * NAMED, because the spend panel has to say that it does NOT move on this
 * cadence, and the only honest way to say that is to render the figure the
 * timer actually uses.
 */
const POLL_MS = 20_000

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
 * The tile carries the sum's age for exactly that case.
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
 * re-renders every panel, every tile and the injected <style> element once a
 * second to move one table column. It is called from `Runtime` alone.
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

// ---------------------------------------------------------------------------
// The absence vocabulary
// ---------------------------------------------------------------------------

/**
 * SIX KINDS OF NOTHING, SIX WORDS, ONE SET OF SILHOUETTES.
 *
 * These are `measure.ts`'s words and `styles.css` §B4.4's marks, and a screen
 * does not get to invent a seventh phrasing of "we do not know". The border
 * style carries the kind -- solid for a measurement, dashed for a failure,
 * hatched for an absence, dotted for a read in flight -- so the six stay apart
 * in greyscale and in the screenshot that gets pasted into an incident
 * channel, which is where these screens are actually read.
 */
const MARK_WORD = {
  zero: 'real zero',
  absent: 'not measured',
  unread: 'not read',
  partial: 'partial',
  admin: 'admin only',
  pending: 'reading',
} as const

type MarkKind = keyof typeof MARK_WORD

/**
 * The mark, and the sentence that used to be a paragraph.
 *
 * `say` is the accessible name. THIS IS THE WHOLE MECHANISM OF THE MIGRATION
 * and it is not a `title=`: a tooltip has no visible anchor and no keyboard
 * route, which is why the previous attempt at this turned the suite red. A
 * mark is visible without hovering anything, is in the tab order of the
 * assistive tree, and is attached to the figure it qualifies -- and a
 * paragraph can sit next to a figure it does not describe while an attribute
 * on the figure cannot.
 */
function Mark({ kind, say }: { kind: MarkKind; say: string }) {
  return (
    <span className={`ctl-mark is-${kind}`} role="img" aria-label={say}>
      {MARK_WORD[kind]}
    </span>
  )
}

/**
 * THE DIAL, and the reason the unfilled arc is always painted.
 *
 * Hetzner paints the whole ring in every meter they ship and fills the
 * measured part of it. A partial total then LOOKS partial, because the
 * unmeasured remainder is visibly present and visibly not filled. That is the
 * invariant rendered rather than narrated.
 *
 * `measured` is the share of the POPULATION this figure speaks for, not the
 * figure itself. A subscription headroom of 100% read off one of three
 * accounts is not a 100% ring: it is a ring filled a third of the way and
 * hatched the rest, with 100% in the middle. Those are two different facts and
 * the dial is the only place on the screen that can hold both at once.
 */
function Dial({
  kind,
  measured,
  say,
  children,
}: {
  kind: 'measured' | 'partial' | 'unknown' | 'zero'
  /** 0-100, the share of the population that reported. */
  measured: number
  say: string
  children: ReactNode
}) {
  const pct = Math.max(0, Math.min(100, Math.round(measured)))
  return (
    <div
      className={`ctl-dial ov-dial${kind === 'measured' ? '' : ` is-${kind}`}`}
      // Strings, not numbers: React appends `px` to a numeric value for a
      // known length property, and a custom property that arrives as `57px`
      // makes `calc(var(--pct) * 1%)` invalid and the arc disappear.
      style={{ '--pct': String(pct), '--measured': String(pct) } as CSSProperties}
      role="img"
      aria-label={say}
      data-partial={kind === 'partial' ? 'yes' : 'no'}
      data-measured={kind === 'unknown' ? 'false' : 'true'}
    >
      <b className="ctl-dial-figure">{children}</b>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The fact strip
// ---------------------------------------------------------------------------

/**
 * FOUR FIGURES, FOUR DOORWAYS, AND NO FIFTH.
 *
 * WHAT CHANGED, AND WHY IT IS A STRUCTURAL CHANGE RATHER THAN A COSMETIC ONE.
 *
 *   1. THE ATTENTION TILE IS GONE. It said the same thing the lead directly
 *      above it now says at page rank -- `10 things` over `10 things need
 *      attention` -- and it said it in the same strip as four figures nobody
 *      has to act on. A fact drawn twice is not emphasis; it is a reader
 *      checking whether the two numbers agree. The coverage that used to be
 *      its foot ("8/8 ran · 1 blind") moved with it, to the lead's qualifier,
 *      where it is beside the list it qualifies.
 *
 *   2. THE THREE LIVE FIGURES DROPPED THEIR PROVENANCE FOOT AND SPEND KEPT
 *      ITS. Running, Units held and Headroom are all on the same twenty-second
 *      poll, and the page head says so once (`poll 20s`, `reads 8/8`); four
 *      copies of "read just now" under four figures that were read by the same
 *      timer is the same fact four times. Spend is the one read on this screen
 *      that does NOT re-poll, so a figure hours old would otherwise sit beside
 *      three that refreshed twenty seconds ago and look exactly like them --
 *      and it is denominated in dollars. Its foot is unconditional and carries
 *      its coverage as well as its age.
 *
 *      THE OTHER THREE ARE NOT SILENT WHEN THEY GO STALE. `footFor` still
 *      renders under any figure whose reading is older than two poll periods,
 *      so the foot appearing now MEANS something -- this figure is not from
 *      the current poll -- where before it was there whatever happened.
 *
 * EVERY FIGURE HAS FOUR RENDERINGS AND THEY MUST NOT CONVERGE:
 *
 *   - a figure;
 *   - `.ctl-pending`, a moving bar at the geometry the figure will occupy: the
 *     read is in flight and there is nothing to say yet;
 *   - `.ctl-mark.is-absent`, hatched: the platform genuinely has no such
 *     figure;
 *   - `.ctl-mark.is-unread`, dashed and amber: the platform may well have it,
 *     we did not get it.
 *
 * The last three are all "no number", and the temptation is to let one fall
 * into the next. It must not: "not measured" is a claim ABOUT THE PLATFORM,
 * and drawing it over a request that is still in flight tells the reader the
 * figure does not exist when what is true is that it has not arrived. So
 * `reading` is a prop, it outranks `absent`, and every caller passes it.
 */
function MetricStrip({
  capacity,
  stats,
  accounts,
  spend,
}: {
  capacity: Result<Capacity>
  stats: Result<Stats>
  accounts: Result<AccountsPage>
  spend: Result<SpendRollup>
}) {
  const cap = dataOf(capacity)
  const st = dataOf(stats)
  const global = cap?.pools.find((p) => p.name === 'global') ?? null

  const inFlight =
    st === null
      ? null
      : Object.entries(st.tasks_by_state)
          .filter(([s]) => CONCURRENCY_STATES.has(s as TaskState))
          .reduce((n, [, v]) => n + (typeof v === 'number' ? v : 0), 0)

  const pool = accountHeadroom(accounts)
  const sp = dataOf(spend)

  return (
    <div className="ctl-metrics">
      <Tile
        href="#work/running"
        label="Running"
        value={inFlight}
        unit="agents"
        foot={staleFoot(stats, 'counted')}
        reading={stats.status === 'loading'}
        unread={stats.status === 'error' ? errorHeading(stats.error) : null}
        tone={inFlight !== null && inFlight > 0 ? 'good' : undefined}
        say="Agents in LEASED, DISPATCHED, STARTING or RUNNING — the four states that reserve capacity. Counted by /v1/stats, one aggregation query per state."
      />

      <Tile
        href="#capacity/pools"
        label="Units held"
        value={global ? global.active : null}
        unit={global ? `of ${global.effective_limit}` : undefined}
        foot={staleFoot(capacity, 'read')}
        reading={capacity.status === 'loading'}
        unread={capacity.status === 'error' ? errorHeading(capacity.error) : null}
        absent={
          capacity.status !== 'error' && cap !== null && global === null
            ? 'No global pool is configured, so nothing reports a platform-wide unit count.'
            : null
        }
        tone={global && overCeiling(global) ? 'alert' : undefined}
        // "units", never "agents": admission increments by the resource
        // class's weight, so 8 may be two large agents or eight standard ones.
        say="Weighted units held on the platform-wide global pool. Admission counts a resource class's weight, so this is not a count of agents."
      />

      <Tile
        href="#capacity/accounts"
        // "Account headroom", not "Headroom". The panel below is called
        // Headroom and covers BOTH ceilings -- the pools and the subscription
        // accounts -- while this figure is the account half alone. Two things
        // one word apart, one of which is a subset of the other, is how a
        // reader concludes the platform has 37% of its capacity left when what
        // is true is that its best account does.
        label="Account headroom"
        value={pool.pct === null ? null : Math.round(pool.pct)}
        unit={pool.pct === null ? undefined : '% left'}
        foot={pool.usable === null ? undefined : `best of ${pool.usable}/${pool.total}`}
        reading={pool.reading}
        unread={accounts.status === 'error' ? errorHeading(accounts.error) : null}
        absent={pool.absent === null ? null : pool.foot ?? pool.sub}
        tone={pool.pct !== null && pool.pct < 15 ? 'alert' : undefined}
        say={pool.sub}
      />

      <Tile
        href="#work/timeline"
        label="Token spend"
        value={sp && sp.costUsd !== null ? money(sp.costUsd) : null}
        // THE AGE IS NOT OPTIONAL ON THIS ONE, which is why it calls `footFor`
        // and its three neighbours call `staleFoot`. See the note above.
        foot={sp ? `${sp.attemptsWithCost}/${sp.attempts} · ${footFor(spend, 'summed') ?? 'not summed'}` : footFor(spend, 'summed')}
        reading={spend.status === 'loading'}
        unread={spend.status === 'error' ? errorHeading(spend.error) : null}
        absent={
          spend.status === 'empty'
            ? 'No task has run, so there is no attempt to sum.'
            : sp && sp.costUsd === null
              ? 'No attempt in this sample reported a cost. That is an absent measurement, not $0.00.'
              : null
        }
        say="Token cost summed one request per sampled task. It is a sample rather than a bill, and it never includes infrastructure: no billing integration of any kind records Cloud Run, Firestore or GCS spend."
      />
    </div>
  )
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

/** "read 40s ago", or nothing at all when there is no reading to date. */
function footFor(r: Result<unknown>, verb: string): string | undefined {
  const at = ageOf(r)
  return at === null ? undefined : `${verb} ${timeAgo(at)}`
}

/**
 * The same provenance line, but ONLY WHEN IT SAYS SOMETHING.
 *
 * Four identical "read just now" lines under four figures that were read by
 * the same twenty-second timer is one fact printed four times -- and the page
 * head already prints it once, as `poll 20s` beside the read tally. What is
 * worth a line is a figure that is NOT from the current poll, which is what a
 * paused tab, a failed re-read or a stale cache produces.
 *
 * TWO POLL PERIODS, NOT ONE. One period is the ordinary gap between a poll
 * landing and the next one firing, so a threshold there would make the foot
 * flicker on and off once every twenty seconds under a healthy platform --
 * chrome that moves is chrome a reader learns to ignore.
 *
 * A read with no `fetchedAt` at all still renders nothing, exactly as before:
 * `footFor` has no age to print and inventing one is the whole class of bug
 * this file exists against.
 */
function staleFoot(r: Result<unknown>, verb: string): string | undefined {
  const at = ageOf(r)
  if (at === null) return undefined
  return Date.now() - at > POLL_MS * 2 ? footFor(r, verb) : undefined
}

function Tile({
  href,
  label,
  value,
  unit,
  foot,
  reading,
  unread,
  absent,
  tone,
  say,
}: {
  /** Omitted only where the answer is already on this screen. */
  href?: string | undefined
  label: string
  /** The figure. null means there is none, and the three props below say why. */
  value: number | string | null
  unit?: string | undefined
  /** The qualifier, in the mono chrome layer. A count, never a definition. */
  foot?: string | undefined
  /** True while the read is IN FLIGHT. Not an absence -- not yet anything. */
  reading?: boolean | undefined
  /** Set when the READ failed: the platform may have this, we did not get it. */
  unread?: string | null
  /** Set when the platform genuinely has no such figure. */
  absent?: string | null
  tone?: 'alert' | 'good' | undefined
  /** The sentence. The tile's accessible name, and nothing renders it. */
  say: string
}) {
  // ORDER MATTERS, AND THIS IS THE ORDER. A failed read outranks everything:
  // it must never fall through to a figure from an earlier state or to a
  // reassuring absence. `reading` comes next and outranks BOTH the figure and
  // the absence.
  const state = unread ? 'unread' : reading ? 'reading' : value === null ? 'absent' : 'value'
  const cls =
    state === 'unread'
      ? 'is-unread'
      : state === 'reading'
        ? 'ov-reading'
        : state === 'absent'
          ? 'is-absent'
          : tone === 'alert'
            ? 'is-alert'
            : tone === 'good'
              ? 'is-good'
              : ''

  // An <a> with no href is not a link and is not focusable, so a tile with
  // nowhere to go is a plain element rather than an anchor that looks like one.
  const Box = href === undefined ? 'div' : 'a'

  return (
    <Box className={`ctl-metric ov-tile ${cls}`} href={href} aria-label={`${label}. ${say}`}>
      <span className="ctl-metric-label">{label}</span>
      <span className="ctl-metric-value">
        {state === 'unread' ? (
          <Mark kind="unread" say={unread ?? 'The read failed.'} />
        ) : state === 'reading' ? (
          // NO WORD AT ALL. A read in flight is the one absence that is
          // temporary, and the moving bar at the figure's own geometry is the
          // whole statement -- lighter than the hatch and visibly still going,
          // so it cannot be read as "nobody reported this". It carries no text
          // because a gradient has no luminance any contrast gate can measure.
          <i className="ctl-pending ov-pending" aria-hidden />
        ) : state === 'absent' ? (
          <Mark kind="absent" say={absent ?? 'Nothing recorded this figure.'} />
        ) : (
          <>
            {value}
            {unit && <span className="ctl-metric-unit">{unit}</span>}
          </>
        )}
      </span>
      {foot && <span className="ctl-metric-foot">{foot}</span>}
    </Box>
  )
}

// ---------------------------------------------------------------------------
// Card chrome
// ---------------------------------------------------------------------------

/**
 * Every card's header carries the link out, the qualifier and the `?`.
 *
 * "Nothing is a dead end" is a property of the component rather than of each
 * author's diligence: there is no way to draw a card here without naming the
 * screen that goes deeper.
 *
 * `note` IS THE SLOT THE PARAGRAPHS COLLAPSED INTO. Right-aligned, muted,
 * mono, one line, no verb required: `8 of 8 checks`, `tenant eng`,
 * `4 of 4 attempts`. A qualifier that wraps under the title has become a
 * subtitle, which is a paragraph with better manners.
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
  /**
   * The topic that used to be a paragraph under this card, and then a `?` on
   * this title, and is now NEITHER on the glass (B7.4).
   *
   * IT PUBLISHES THE SENTENCE WITHOUT DRAWING A WIDGET. `<HelpNote>` renders a
   * visually hidden node and the title points `aria-describedby` at it, so a
   * screen reader still gets the explanation at the title while a sighted
   * reader gets a card head with nothing extra in it. This screen carried
   * twelve help anchors for four cards; the count is what the density pass was
   * about, and deleting the glyph without keeping the description would have
   * taken the explanation away from the one reader who could not get it from
   * the layout instead.
   */
  explain?: TopicId
  /**
   * Omitted by exactly one card, "Needs attention", and the reason is that
   * there is no screen that is a deeper version of it.
   */
  href?: string | undefined
  cta?: string | undefined
}) {
  const descId = useId()
  return (
    <div className="ctl-card-head">
      <h2
        className="ctl-card-title"
        aria-describedby={explain === undefined ? undefined : descId}
      >
        {title}
        {explain !== undefined && <HelpNote topic={explain} id={descId} />}
      </h2>
      {note !== undefined && <span className="ctl-card-note">{note}</span>}
      {href !== undefined && cta !== undefined && (
        <a className="ctl-link ov-link" href={href}>
          {cta} &rarr;
        </a>
      )}
    </div>
  )
}

/**
 * The four ways a card can have nothing to draw, kept visually distinct.
 *
 * THE MARK IS THE MARKER, and it replaced a paragraph. `is-failed`,
 * `is-partial` and `is-admin` are colour, and colour is not a distinction a
 * screenshot in an incident channel preserves; the mark is two words, a border
 * style and a fill, and it survives both. The heading is the FACT, three or
 * four words of it. The explanation is the mark's accessible name, and it is
 * ALREADY A WHOLE SENTENCE -- `say` is written out at every call site below.
 *
 * WHICH IS WHY THE `?` HERE IS GONE (B7.4). Nine of this screen's twelve help
 * anchors were on these empty states, each one opening a card beside a mark
 * whose own accessible name said the same thing in more detail. A reader with a
 * screen reader heard it twice; a reader without one saw a glyph that repeated
 * the two words next to it. `explain` keeps the topic's sentence published at
 * the heading for assistive technology and draws nothing.
 */
function Absent({
  kind,
  heading,
  say,
  explain,
}: {
  kind: 'zero' | 'failed' | 'partial' | 'admin'
  heading: string
  say: string
  explain?: TopicId
}) {
  const cls = kind === 'zero' ? '' : ` is-${kind}`
  const descId = useId()
  const mark: MarkKind =
    kind === 'zero' ? 'zero' : kind === 'failed' ? 'unread' : kind === 'partial' ? 'partial' : 'admin'
  return (
    <div className={`ctl-empty ov-empty${cls}`}>
      <Mark kind={mark} say={say} />
      <h3 aria-describedby={explain === undefined ? undefined : descId}>
        {heading}
        {explain !== undefined && <HelpNote topic={explain} id={descId} />}
      </h3>
    </div>
  )
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
// 1. Capacity, by what binds it
// ---------------------------------------------------------------------------

/**
 * One row per runner profile: how many more could start, and WHICH POOL STOPS
 * MORE.
 *
 * This is the screen's most load-bearing card and the reason is arithmetic. A
 * task must clear EVERY pool in its list at the same moment, so the number of
 * agents it can still start is the minimum across them divided by the
 * profile's weight -- never a sum, and never the pool an operator happens to
 * be looking at. Raising a pool that is not the binding one changes nothing at
 * all, and that is a change people make repeatedly because no screen showed
 * them which pool was binding.
 *
 * TRAP D: `profile.pools` is the CALLING TENANT'S pool list, including for an
 * admin, because service.capacity() calls pool_names_for(ctx.tenant_id)
 * unconditionally. So this answers "how many more could I start", never "how
 * much capacity does the platform have". The scope is the card's note, every
 * time, and a platform-wide figure is not faked by substituting another
 * tenant's pools.
 */
function CapacityBody({ state }: { state: Result<Capacity> }) {
  if (state.status === 'loading') {
    return (
      <div className="ctl-card-body">
        <Reading />
      </div>
    )
  }
  if (state.status === 'error') {
    const b = blindness(state.error)
    return (
      <div className="ctl-card-body">
        <Absent
          kind={b.admin ? 'admin' : 'failed'}
          heading="Pool state unread"
          say={`${b.why} No figure here is a claim about room.`}
          explain={b.admin ? 'admin-gate-not-failure' : 'read-failed'}
        />
      </div>
    )
  }
  if (state.status === 'empty') {
    return (
      <div className="ctl-card-body">
        <Absent
          kind="zero"
          heading="No pools exist"
          say="The read succeeded and returned nothing. This is a real zero, not a failure to read."
          explain="capacity"
        />
      </div>
    )
  }

  const cap = state.data
  const byName = new Map(cap.pools.map((p) => [p.name, p]))
  const profiles = Object.entries(cap.runner_profiles).sort(([a], [b]) => a.localeCompare(b))

  if (profiles.length === 0) {
    return (
      <div className="ctl-card-body">
        <Absent
          kind="partial"
          heading="No runner profile"
          say={`${cap.pools.length} pools were read and no runner profile came back, so no ceiling is computed here.`}
        />
      </div>
    )
  }

  // Any tenant-scoped pool name tells us whose view this is. `/v1/capacity`
  // carries no tenant id of its own, so it is read off the names rather than
  // assumed.
  const tenant = cap.pools
    .map((p) => /(?:^|:)tenant:([^:]+)/.exec(p.name)?.[1])
    .find((t): t is string => Boolean(t))

  return (
    <>
      <div className="ctl-card-body">
        {profiles.map(([name, profile]) => (
          <ProfileRow
            key={name}
            name={name}
            profile={profile}
            byName={byName}
            tenant={tenant}
          />
        ))}
      </div>
      <p className="ctl-card-foot">
        {cap.pools.length} pools · {footFor(state, 'read') ?? 'not read'}
      </p>
    </>
  )
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

/**
 * THE UTILISATION TRACK, and the three things it has to keep apart.
 *
 * `pct === null`  nothing measured it. Hatched, NO fill -- an unfilled plain
 *                 track reads as "0% used", which is a claim.
 * `pct === 0`     MEASURED zero. This used to render as a zero-width fill on a
 *                 near-white track, which is pixel-for-pixel a widget that
 *                 failed to paint. A measured zero gets a visible BASELINE
 *                 TICK at the origin, so "nothing is running" is legible as a
 *                 reading.
 * `pct > 0`       an ordinary fill.
 *
 * The tick is deliberately NOT a minimum width on the fill. A 3px fill would
 * say "a little is in use", which is the absent-as-zero lie inverted: it would
 * make 0 and 0.4% identical instead of making 0 and unmeasured identical.
 */
function UtilTrack({
  pct,
  fillClass,
  over,
  zeroTitle,
}: {
  /** null when nothing measured it. 0 is a reading. */
  pct: number | null
  fillClass: string
  over: boolean
  /** What the baseline tick means, for the one case that needs explaining. */
  zeroTitle: string
}) {
  if (pct === null) return <span className="ctl-util-track is-unknown" />
  if (pct === 0) {
    return (
      <span className="ctl-util-track is-zero" title={zeroTitle}>
        <i className="ctl-util-zero" aria-hidden />
      </span>
    )
  }
  return (
    <span className="ctl-util-track">
      <i className={`ctl-util-fill ${fillClass}`.trimEnd()} style={{ width: `${Math.min(100, pct)}%` }} />
      {/* Held above the ceiling: hatched rather than clipped at 100%, because a
          bar pinned full hides the one thing worth seeing. */}
      {over && <i className="ctl-util-over" style={{ width: '14%' }} />}
    </span>
  )
}

function ProfileRow({
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
  // Read off `profile.admission`, which the server computed from
  // `evaluate_capacity`. `h.agents` can now be NULL -- a pool that could not
  // be read is not a zero -- and every branch below has to say which it is.
  const h = headroomFor(profile)
  const binding = h.binding !== null ? (byName.get(h.binding) ?? null) : null

  // THE BAR IS THE BINDING POOL'S, not an average and not the global pool's.
  // Averaging a profile's pools would draw a comfortable half-full bar for a
  // profile that cannot start anything because one of its six pools is at
  // zero -- precisely the failure this card exists to make visible.
  const known = binding !== null && binding.effective_limit > 0
  const ratio = known ? binding.active / binding.effective_limit : 0
  const paused = binding !== null && isPaused(binding)
  const over = binding !== null && overCeiling(binding)

  const fillClass = paused
    ? 'is-paused'
    : over || h.agents === 0
      ? 'is-bad'
      : ratio > 0.8
        ? 'is-warn'
        : ''

  return (
    <div className="ctl-util">
      <span className="ctl-util-name" title={`${name} · ${profile.units} unit(s) per agent`}>
        <b>{name}</b>{' '}
        {h.agents === null ? (
          /* An em dash, never a 0. `uncapped` means nothing limits this;
             `unknown` means a required pool could not be read and the true
             figure may be anything, including zero. Two different sentences
             because they have two different remedies. */
          <span
            className="ctl-em"
            title={
              h.basis === 'uncapped'
                ? 'No pool in this profile is configured, so nothing caps it.'
                : `Not measured: ${h.unread.length} required pool(s) could not be read.`
            }
          >
            · &mdash;
          </span>
        ) : h.agents === 0 ? (
          <span className="ov-stop">
            · 0 can start
            {/* The count, because more than one pool can refuse at the same
                moment and a card that implies one sends an operator to raise a
                ceiling that changes nothing. */}
            {h.blockers.length > 1 && ` (${h.blockers.length})`}
          </span>
        ) : (
          <>· {h.agents} can start</>
        )}
      </span>

      <UtilTrack
        pct={known ? ratio * 100 : null}
        fillClass={fillClass}
        over={over}
        zeroTitle={
          binding
            ? `Measured: 0 of ${binding.effective_limit} in use on ${binding.name}. The bar has a baseline because this zero is a reading.`
            : 'Measured zero.'
        }
      />

      <span className="ctl-util-figure">
        {binding ? (
          <>
            {binding.active}
            <span className="ctl-util-of"> / {binding.effective_limit}</span>
          </>
        ) : (
          <span className="ctl-em">&mdash;</span>
        )}
      </span>

      <span
        className="ctl-util-by"
        title={
          h.blockers.length > 0
            ? h.blockers.map((b) => `${b.pool} (${b.active}/${b.limit})`).join(', ')
            : (h.binding ?? undefined)
        }
      >
        {/* More than one pool can be at its ceiling at once. This column has
            room for one name, so when several refuse it says SO rather than
            picking one. "refusing", not "full": one of them may be PAUSED,
            which is a different fact with the opposite remedy. */}
        {h.blockers.length > 1
          ? `${h.blockers.length} refusing`
          : paused
            ? 'paused'
            : !h.complete
              ? `${h.unread.length} unread`
              : h.binding
                ? bindingLabel(h.binding, tenant)
                : h.missing.length > 0
                  ? `${h.missing.length} uncapped`
                  : '—'}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 2. Running now
// ---------------------------------------------------------------------------

const RUNNING_ROWS = 4

/**
 * What is running, longest-running first.
 *
 * TWO SOURCES, DELIBERATELY, AND BOTH ARE NAMED. The COUNT comes from
 * `/v1/stats`, an exact Firestore count() per state. The ROWS come from
 * `/v1/tasks?limit=200`, the 200 most recently CREATED tasks (store.py:408
 * orders created_at DESCENDING) -- so an agent running for two days while 200
 * newer tasks were created is counted and not listed. The caption prints both
 * figures rather than quietly showing whichever is smaller, because the gap
 * between them is itself information.
 */
function RunningBody({
  tasks,
  stats,
}: {
  tasks: Result<TaskPage>
  stats: Result<Stats>
}) {
  if (tasks.status === 'loading') {
    return (
      <div className="ctl-card-body">
        <Reading rows={4} />
      </div>
    )
  }
  if (tasks.status === 'error') {
    const b = blindness(tasks.error)
    return (
      <div className="ctl-card-body">
        <Absent
          kind={b.admin ? 'admin' : 'failed'}
          heading="Task list unread"
          say={`${b.why} This card is blind; it is not reporting that nothing is running.`}
          explain={b.admin ? 'admin-gate-not-failure' : 'read-failed'}
        />
      </div>
    )
  }
  if (tasks.status === 'empty') {
    return (
      <div className="ctl-card-body">
        <Absent
          kind="zero"
          heading="No task exists"
          say="The read succeeded and returned nothing. This is a real zero, not a failure to read."
          explain="absent-vs-zero"
        />
      </div>
    )
  }

  const page = tasks.data
  const running = page.tasks
    .filter((t) => CONCURRENCY_STATES.has(t.state))
    // Oldest start first: the longest-running agent is the one worth seeing,
    // and it is the one a fixed-height card would otherwise cut off.
    .sort((a, b) => startKey(a) - startKey(b))

  const st = dataOf(stats)
  const counted =
    st === null
      ? null
      : Object.entries(st.tasks_by_state)
          .filter(([s]) => CONCURRENCY_STATES.has(s as TaskState))
          .reduce((n, [, v]) => n + (typeof v === 'number' ? v : 0), 0)

  if (running.length === 0) {
    return (
      <>
        <div className="ctl-card-body">
          <Absent
            kind="zero"
            heading="Nothing running"
            say={
              `No task on the ${page.tasks.length} most recently created is in LEASED, DISPATCHED, STARTING or RUNNING. ` +
              (counted === null
                ? 'The exact count could not be read, so this is the page’s answer rather than the platform’s.'
                : counted === 0
                  ? 'The state counts agree: zero.'
                  : `The state counts say ${counted}; those agents were created before this page begins.`)
            }
          />
        </div>
        <p className="ctl-card-foot">
          0 of {page.tasks.length} newest
          {counted !== null && counted !== 0 && <> · stats say {counted}</>}
        </p>
      </>
    )
  }

  const shown = running.slice(0, RUNNING_ROWS)

  return (
    <>
      <div className="ctl-card-body is-flush ctl-table ov-rows">
        <table>
          <thead>
            <tr>
              <th scope="col">Agent</th>
              <th scope="col">State</th>
              <th scope="col" className="is-num">
                Runtime
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.map((t) => (
              <RunningRow key={t.id} task={t} />
            ))}
          </tbody>
        </table>
      </div>
      {/* THE GAP BETWEEN THE TWO SOURCES IS THE INFORMATION, so both figures
          stay on the surface as digits. `/v1/stats` counting more than the
          page holds is not a discrepancy to hide: it is agents older than the
          200 most recently created. */}
      <p className="ctl-card-foot">
        {running.length} of {page.tasks.length} newest
        {counted !== null && counted !== running.length && <> · stats say {counted}</>}
        {running.length > shown.length && <> · showing {shown.length}</>}
      </p>
    </>
  )
}

function startKey(t: Task): number {
  const v = new Date(t.started_at ?? t.created_at).getTime()
  return Number.isFinite(v) ? v : Number.MAX_SAFE_INTEGER
}

function RunningRow({ task }: { task: Task }) {
  return (
    <tr>
      <th scope="row">
        <a className="ctl-link ov-link" href={`#work/task/${encodeURIComponent(task.id)}`}>
          {task.runner_profile}
        </a>
        <span className="ctl-sub">{task.id}</span>
      </th>
      <td>
        {/* The word is mandatory; the dot is the shape that repeats it.
            Roughly 8% of male viewers cannot separate this card's amber from
            its red.

            THE SECOND GLYPH IS GONE. This row used to render
            `{stateGlyph(task.state)} {task.state}` INSIDE the chip, next to the
            `<i>` that already draws the same state as a shape -- two dots for
            one fact, and unlike Agents.tsx:350 and AgentDetail.tsx:481 this one
            was not `aria-hidden`, so a screen reader announced a bare "●"
            before the word. design-system.md sec 6.6 records it as the last
            piece of the owner's "decorative double dot" and the first job of
            this phase. The pill was hiding it; with the pill gone it was
            plainly two dots. The `<i>` keeps the shape vocabulary, so nothing
            that carried information was removed. */}
        <span className={`ctl-chip ${chipTone(task.state)}`}>
          <i aria-hidden />
          {task.state}
        </span>
      </td>
      <td className="is-num">
        <Runtime task={task} />
      </td>
    </tr>
  )
}

/**
 * The one cell on this screen that has to move on its own, and therefore the
 * one place the 1Hz clock lives.
 *
 * A LEASED task has no `started_at` -- lifecycle writes it on
 * DISPATCHED -> STARTING -- so `elapsed` says "queued 4m", never "0s".
 */
function Runtime({ task }: { task: Task }) {
  const now = useNow()
  return <>{elapsed(task, now).text}</>
}

function chipTone(state: TaskState): string {
  switch (stateTone(state)) {
    case 'ok':
      return 'is-ok'
    case 'bad':
      return 'is-bad'
    case 'live':
      return 'is-live'
    default:
      return 'is-unknown'
  }
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
  if (state.status === 'loading') {
    return (
      <div className="ctl-card-body">
        <Reading rows={4} />
      </div>
    )
  }
  if (state.status === 'error') {
    // TWO FAILURES, AND WHAT THE READER DOES NEXT DIFFERS. This rollup is
    // built ON the task page: when the task read is the one that failed, no
    // attempt read was made at all, nothing is known about the attempt route,
    // and the thing to fix is the task list.
    if (tasks.status === 'error') {
      const b = blindness(tasks.error)
      return (
        <div className="ctl-card-body">
          <Absent
            kind={b.admin ? 'admin' : 'failed'}
            heading="Spend unassembled"
            say={`Spend is summed from the attempts of the most recent tasks, and the task list itself could not be read. ${b.why} No attempt read was made, so nothing here is a statement about spend.`}
          />
        </div>
      )
    }
    return (
      <div className="ctl-card-body">
        <Absent
          kind="failed"
          heading="No attempt read completed"
          say={`${errorHeading(state.error)} — ${state.error.message} No figure is shown.`}
          explain="read-failed"
        />
      </div>
    )
  }
  if (state.status === 'empty') {
    // TWO ZEROS, AND THEY ARE NOT THE SAME ZERO. "Every task on the page has
    // no attempts" describes tasks that exist; on a tenant with no tasks at
    // all it would describe a page that is not there.
    if (tasks.status === 'empty') {
      return (
        <div className="ctl-card-body">
          <Absent
            kind="zero"
            heading="No task exists"
            say="The task read succeeded and returned nothing — a real zero, so there are no attempts to sum."
            explain="absent-vs-zero"
          />
        </div>
      )
    }
    return (
      <div className="ctl-card-body">
        <Absent
          kind="zero"
          heading="No task has run"
          say="Every task on the page has an attempt count of 0 — a real zero."
          explain="attempt-documents"
        />
      </div>
    )
  }

  const s = state.data
  const unmeasured = s.attempts - s.attemptsWithCost

  return (
    <>
      <div className="ctl-card-body">
        {/* THE FIGURE, AND THE ONE THING IT IS NOT. `.ctl-figure.is-absent`
            drops to --t-body on purpose: at 30px the words "not reported"
            read as a quantity, which is the exact confusion this state exists
            to prevent. Nothing but a measured number gets the figure step. */}
        <b
          className={`ctl-figure ov-figure${s.costUsd === null ? ' is-absent' : ''}`}
          data-measured={s.costUsd === null ? 'false' : 'true'}
        >
          {s.costUsd === null ? <span className="ctl-em">&mdash;</span> : money(s.costUsd)}
        </b>
        {s.costUsd === null && (
          <div className="ov-figure-mark">
            <Mark
              kind="absent"
              say="No attempt in this sample reported a cost. That is an absent measurement and not $0.00: record_usage omits a key the runner did not report."
            />
          </div>
        )}

        {/* THE TOKEN MIX, AS A PROPORTION RATHER THAN FOUR NUMBERS IN A LIST.
            Hand-rolled: four `<i>` widths off one total, in four tones of ONE
            series hue (see `.ov-s1` in OVERVIEW_CSS). A segment whose count is ABSENT
            is not drawn at all and its fact keeps its slot below with an em
            dash -- a missing segment and a zero-width segment are the same
            picture, so the em dash is what tells them apart. */}
        <TokenMix s={s} />
      </div>

      {/* PROVENANCE, IN ONE LINE, WHERE PROVENANCE GOES. Four paragraphs used
          to say this: the sample size, the span, the age of the sum, that it
          does not re-poll, and how many attempts carried no cost. They are
          counts, so they are drawn as counts. */}
      <p className="ctl-card-foot">
        {s.attempts} attempts / {s.tasksSampled} tasks
        {' · '}
        {footFor(state, 'summed') ?? 'not summed'} · no re-poll
        {unmeasured > 0 && (
          <>
            {' · '}
            <span className="ov-warn">{unmeasured} unmeasured</span>
          </>
        )}
        {/* THE COUNT OF WHAT IS MISSING, KEPT AS A DIGIT ON THE SURFACE. These
            figures are a sum over a sample with a hole in it, and the size of
            the hole is the thing that must not need a hover. ONE MESSAGE IS
            ONE FAILURE'S -- the rollup keeps only the first error it saw
            (api.ts, loadSpend), so the rest are unexplained rather than
            explained wrongly. */}
        {s.failedReads > 0 && (
          <>
            {' · '}
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
            {/* NO `?` (B7.4). The accessible name above is `partial-read`,
                written out longer than the topic and with THIS response's
                numbers and THIS failure's message in it, which a shared topic
                cannot have. A glyph here would open a generic version of the
                sentence already attached to the figure. */}
          </>
        )}
      </p>
    </>
  )
}

/**
 * The four token counts as one proportion, plus their figures.
 *
 * A BAR AND A FACTS STRIP, NOT A DEFINITION LIST. The `<dl>` this replaced
 * spent a 13px uppercase key and a line of its own on each of four numbers
 * that only mean anything against each other; the bar puts them against each
 * other and the strip keeps the digits.
 *
 * AN ABSENT COUNT DRAWS NO SEGMENT. A zero-width segment and a segment that
 * was never measured are the same picture, so an absent count is left out of
 * the bar entirely and its fact carries `.ctl-em` -- and when EVERY count is
 * absent the bar is not drawn at all, because an empty track reads as "0
 * tokens", which is a claim.
 */
function TokenMix({ s }: { s: SpendRollup }) {
  const parts = [
    { key: 'in', v: s.inputTokens, series: 1 },
    { key: 'out', v: s.outputTokens, series: 2 },
    { key: 'c-rd', v: s.cacheReadTokens, series: 3 },
    { key: 'c-wr', v: s.cacheCreationTokens, series: 4 },
  ] as const
  const total = parts.reduce((n, p) => n + (p.v ?? 0), 0)
  const anyMeasured = parts.some((p) => p.v !== null)

  return (
    <>
      {anyMeasured && total > 0 ? (
        <div
          className="ov-mix"
          role="img"
          aria-label={parts
            .map((p) => `${p.key} ${p.v === null ? 'not measured' : p.v}`)
            .join(', ')}
        >
          {parts.map((p) =>
            p.v === null || p.v === 0 ? null : (
              <i
                key={p.key}
                className={`ov-mix-seg ov-s${p.series}`}
                style={{ width: `${(p.v / total) * 100}%` }}
              />
            ),
          )}
        </div>
      ) : (
        // Every count absent, or a measured total of zero with no proportion
        // to draw. Either way there is no scale, so no track is drawn: an
        // empty track is a claim that the scale starts somewhere.
        <div className="ov-mix is-unknown" role="img" aria-label="No attempt in this sample reported a token count, so there is no proportion to draw." />
      )}
      {/* THE SWATCH IS ON THE WORD. The bar above was four hues and the legend
          under it named them in plain grey, so the only way to learn which
          segment was `c-rd` was to guess from the order -- the owner's "the
          spend bar is a rainbow ... with no legend near it".
          design-system.md sec 1.6 is explicit that the five series sit in a band
          1.36:1 from end to end and are therefore NOT separable in greyscale,
          so a multi-series chart carries a legend naming every series and never
          relies on the segment's colour to say which segment it is. This is
          that legend. The bar is now one hue at four tones rather than four
          hues, so the swatch keys a segment by LIGHTNESS as well -- which a
          greyscale screenshot keeps -- and the proportion stays.

          A SERIES THAT REPORTED NOTHING GETS A HOLLOW SWATCH, not a solid one.
          It has no segment on the bar, and a solid swatch beside an em dash
          would be a key to a colour that is not there -- an absence drawn as a
          measurement, which is the one thing this console may not do. The
          swatch still occupies its space, so the column does not reflow when a
          count arrives. It is aria-hidden throughout: the bar's own aria-label
          already names every series and its value. */}
      <ul className="ctl-facts ov-mix-facts">
        {parts.map((p) => (
          <li className={`ctl-fact${p.v === null ? ' is-absent' : ''}`} key={p.key}>
            <i
              className={p.v === null ? 'ov-swatch is-absent' : `ov-swatch ov-s${p.series}`}
              aria-hidden
            />
            <b>{p.key}</b>
            {p.v === null ? <i className="ctl-em">&mdash;</i> : <span className="ov-num">{tokens(p.v)}</span>}
          </li>
        ))}
      </ul>
    </>
  )
}

/** A number of tokens, never 0 for an absent measurement. */
function tokens(v: number): string {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`
  return String(v)
}

/**
 * Dollars of TOKEN cost. Four decimals under ten dollars because a single
 * attempt is routinely worth $0.0312, and rounding that to $0.03 loses a third
 * of the figures on this screen to "$0.00".
 */
function money(v: number): string {
  return v < 10 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`
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
 * `usable` AND `total` ARE WHAT THE DIAL DRAWS. The ring is the share of the
 * pool this figure speaks for, and the figure in the middle is the headroom.
 * A headroom of 100% read off one of three accounts is not a full ring: it is
 * a ring filled a third of the way and hatched the rest, with 100% in it.
 *
 * EXPORTED so the arithmetic below can be asserted. A verifier found on
 * 2026-09-22 that replacing `accounts.length - unread - projected -
 * notServing` with plain `accounts.length` left the entire suite green. The
 * tile would then have read "best of 3 usable accounts" while its own foot
 * named two of the three as having no reading. Nothing could catch it because
 * nothing could call this.
 */
export function accountHeadroom(state: Result<AccountsPage>): {
  pct: number | null
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

  let best: { pct: number; key: string; observedAt: string } | null = null
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
    const left = Math.max(0, Math.min(100, 100 - r.pct))
    if (best === null || left > best.pct) {
      best = { pct: left, key: w.key, observedAt: r.observedAt }
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
      sub: `${countOf(accounts.length, 'account')} ${scope} · ${absenceSentence(unread, projected, notServing)}`,
      foot: 'a missing reading is not 0% used, and a window that has cleared has not been read since',
      reading: false,
      absent: 'nothing measured',
      usable: 0,
      total: accounts.length,
    }
  }

  return {
    pct: best.pct,
    // THE AGE IS PART OF THE FIGURE. Every other tile on the strip carries the
    // age of the read behind it; this one showed a percentage with nothing
    // saying whether it was measured a minute or four days ago.
    sub: `best of ${countOf(usable, 'usable account')} · its ${best.key.replace('_', '-')} window binds · read ${timeAgo(best.observedAt)}`,
    // DERIVED, NEVER ASSERTED, AND IT NAMES NAMES. The previous wording --
    // "every registered account has a current reading" -- was a sentence
    // chosen by `unusable === 0` over a tenant-scoped list, and the accounts
    // it was silent about were exactly the ones worth knowing about.
    foot: unread.length > 0 || projected.length > 0 || notServing.length > 0
      ? absenceSentence(unread, projected, notServing)
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
 * THIS IS NOW AN ACCESSIBLE NAME RATHER THAN A PARAGRAPH. The counts it
 * summarises are drawn: the dial's hatched arc is the accounts with no
 * reading, and the card's note is `best of 1/3`. The sentence is what a
 * screen reader gets and what the `?` expands.
 */
function absenceSentence(
  unread: string[],
  projected: string[],
  notServing: string[],
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
  // Only reachable with every list empty when the caller has a figure, and
  // that caller words it itself; this is the honest fallback either way.
  return parts.length > 0 ? parts.join(' · ') : 'nothing has been read'
}

const ACCOUNT_ROWS = 3

function AccountsBody({ state }: { state: Result<AccountsPage> }) {
  if (state.status === 'loading') {
    return (
      <div className="ctl-card-body">
        <Reading rows={3} />
      </div>
    )
  }
  if (state.status === 'error') {
    const b = blindness(state.error)
    return (
      <div className="ctl-card-body">
        <Absent
          kind={b.admin ? 'admin' : 'failed'}
          heading="Account pool unread"
          say={`${b.why} This says nothing about whether the accounts have room.`}
          explain={b.admin ? 'admin-gate-not-failure' : 'read-failed'}
        />
      </div>
    )
  }
  if (state.status === 'empty') {
    return (
      <div className="ctl-card-body">
        <Absent
          kind="zero"
          heading="No account registered"
          say="The read succeeded and returned nothing. This is a real zero, not a failure to read."
          explain="park-on-missing-credential"
        />
      </div>
    )
  }

  const pool = accountHeadroom(state)
  const total = state.data.accounts.length
  const accounts = [...state.data.accounts]
    .map((a) => ({ a, w: bindingWindow(a) }))
    // Worst first, and "needs a person" IS the worst. Sorting on room alone
    // sank the one account that had stopped working, because a REAUTH_REQUIRED
    // account's windows have usually reset and it therefore looks the emptiest.
    .sort((x, y) => rank(x.a, x.w) - rank(y.a, y.w))
    .slice(0, ACCOUNT_ROWS)

  // THE RING IS THE COVERAGE, THE FIGURE IS THE HEADROOM, and the two are
  // different facts. `.is-partial` hatches the accounts that reported nothing,
  // so a confident 100% read off one of three is visibly one third of a ring.
  const coverage = total === 0 ? 0 : ((pool.usable ?? 0) / total) * 100
  const dialKind =
    pool.pct === null
      ? 'unknown'
      : (pool.usable ?? 0) < total
        ? 'partial'
        : pool.pct === 0
          ? 'zero'
          : 'measured'

  return (
    <>
      <div className="ctl-card-body">
        <div className="ov-dialrow">
          <Dial kind={dialKind} measured={coverage} say={pool.sub}>
            {pool.pct === null ? (
              <span className="ctl-em">&mdash;</span>
            ) : (
              <>
                {Math.round(pool.pct)}
                <span className="ctl-figure-unit">% left</span>
              </>
            )}
          </Dial>
          <div className="ov-dialrow-rows">
            {accounts.map(({ a, w }) => {
              // FIVE KINDS OF READING, AND ONLY ONE OF THEM IS A CURRENT
              // FIGURE. `readingOf` is the same function the Accounts screen
              // uses, so the two screens cannot disagree about the same
              // account: an account whose binding window has reset reads
              // "~90% · cleared" here and "cleared" there.
              const r: AccountReading =
                a.observed_at === null
                  ? { kind: 'never' }
                  : w === null
                    ? { kind: 'absent' }
                    : readingOf(a, w.key)
              // `null`, never 0: the width of a bar nobody measured is not
              // zero, it does not exist.
              const pct =
                r.kind === 'live' || r.kind === 'stale' || r.kind === 'reset' ? r.pct : null
              const projected = isProjected(r)
              const windowName = w?.key.replace(/_/g, '-') ?? null
              return (
                <div className="ctl-util" key={a.account_id}>
                  <span className="ctl-util-name" title={a.account_id}>
                    <b>{a.label}</b> <span className="ov-num">· {a.assigned}</span>
                  </span>
                  {/* Never observed, or no reading for the binding window:
                      hatched with no fill. A plain empty track here would
                      claim 0% used, which for an account nobody has polled is
                      a number nobody measured. A PROJECTED reading does get a
                      bar -- the figure is real -- but a grey one, never the
                      red or amber that says a ceiling is being approached
                      now. */}
                  <UtilTrack
                    pct={pct}
                    fillClass={
                      projected
                        ? 'ov-projected'
                        : a.state !== 'AVAILABLE'
                          ? 'is-paused'
                          : pct !== null && pct > 90
                            ? 'is-bad'
                            : pct !== null && pct > 75
                              ? 'is-warn'
                              : ''
                    }
                    over={false}
                    zeroTitle={`Measured: this account reported 0% of its binding window used. The bar has a baseline because this zero is a reading, not a missing one.`}
                  />
                  <span
                    className="ctl-util-figure"
                    title={
                      r.kind === 'reset' && windowName
                        ? `The ${windowName} window, the binding one, passed its reset ${timeAgo(r.resetsAt)}. It has cleared; no reading taken since has arrived, so this figure describes the window before it.`
                        : r.kind === 'stale'
                          ? `Read ${timeAgo(r.observedAt)}, which is past the broker's staleness window. The figure is real and describes an earlier moment.`
                          : undefined
                    }
                  >
                    {pct !== null ? (
                      <>
                        {/* The same mark `cs status` and the Accounts screen
                            use for a figure that is real but not current. */}
                        {projected && <span className="ov-tilde">~</span>}
                        {Math.round(pct)}%
                      </>
                    ) : (
                      <span className="ctl-em">&mdash;</span>
                    )}
                  </span>
                  {/* A CURRENT READING CARRIES ITS AGE TOO. `stale` and
                      `cleared` have said how old they are for as long as this
                      card has existed; a `live` reading printed only the
                      window name, so the one row with a confident figure was
                      the one row that did not say when it was measured. */}
                  <span className="ctl-util-by">
                    {needsAHuman(a)
                      ? 'sign in again'
                      : // Before every reading word, because it outranks all
                        // of them: whatever this row's figure says, the pool
                        // will not hand this account to this tenant while the
                        // report stands.
                        unreadableFor(a, state.data.tenant_id)
                        ? 'pool is skipping it'
                        : r.kind === 'never'
                        ? 'never polled'
                        : r.kind === 'absent'
                          ? 'no window reported'
                          : r.kind === 'reset'
                            ? 'cleared'
                            : r.kind === 'stale'
                              ? `stale ${timeAgo(r.observedAt)}`
                              : a.state !== 'AVAILABLE'
                                ? a.state.toLowerCase()
                                : r.kind === 'live' && windowName !== null
                                  ? `${windowName} · ${timeAgo(r.observedAt)}`
                                  : (windowName ?? '—')}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      </div>
      {/* THE TRUNCATION MARKER STAYS, because a list showing three of nine and
          a list showing all three are the same picture otherwise. The two
          rules that used to be spelled out here -- which window binds, and
          what the tilde means -- are drawn in the rows themselves: a hatched
          track with no fill, an em dash, a tilde. */}
      <p className="ctl-card-foot">
        {countOf(total, 'account')}
        {total > accounts.length && <> · showing {accounts.length}</>}
        {state.data.tenant_id ? ` · ${state.data.tenant_id}` : ' · every tenant'}
      </p>
    </>
  )
}

/** Sort key: least room first. An unreadable account sorts last, not first. */
function remaining(w: ReturnType<typeof bindingWindow>): number {
  if (!w) return 2
  return w.window.reset ? 1 : Math.max(0, 1 - w.window.utilization)
}

/** `remaining`, with the one state a person has to act on pulled to the top. */
function rank(a: AccountsPage['accounts'][number], w: ReturnType<typeof bindingWindow>): number {
  return needsAHuman(a) ? -1 : remaining(w)
}

// ---------------------------------------------------------------------------
// 5. Needs attention -- derived, never stored
// ---------------------------------------------------------------------------
//
// The derivation itself lives in ./checks.ts. It moved there so it could be
// RUN: it is the only part of this screen whose failure mode is silence, and
// `apps/swarm-ui/test/checks.test.mjs` drives it directly. What is left here
// draws what it returns.

/** How many problems are open by default. The rest are one click away, here. */
const ATTENTION_ROWS = 4
/**
 * THE LEAD -- and it is a region of the page now, not the first card of a grid.
 *
 * WHAT MOVED, AND WHY IT IS A LEVEL CHANGE RATHER THAN A RESTYLE.
 *
 * This was a `.ctl-card`: a bordered, rounded, --surface panel with a head, a
 * body, a full-bleed provenance foot, and a two-column `.ov-dialrow` inside it
 * holding the ring on the left and the list on the right. Above it, a fifth of
 * the metric strip said `Attention · 10 things` in a box of exactly the same
 * weight as `Token spend`. So the screen drew the one question that changes
 * what somebody does next TWICE, both times at the rank of a tile, in the same
 * silhouette as four figures nobody has to act on.
 *
 * It is now the page's opening statement: a ring, a title at --t-title, the
 * coverage beside it, and the problems as rows on the page background. No box,
 * no card head, no card foot. §13.3 of design-system.md is the rule -- a
 * REGION is a change of subject and is never a box; a PANEL is an object and
 * draws the one box there is. "What is wrong" is the page's subject, not one
 * of its objects.
 *
 * WHAT DID NOT MOVE -- THE HONESTY ENCODING, WHICH IS THE WHOLE POINT OF THE
 * COMPONENT.
 *
 *   - THE COVERAGE DIAL IS UNCHANGED and still replaces ninety words. The ring
 *     is the checks; the filled arc is the ones that RAN and the hatched arc
 *     is the ones that could not. A short problem list over four blind checks
 *     and a short list over eight clear ones are different pictures before
 *     either is read, which is the whole point, because only one of them is
 *     good news. `data-partial`, `is-partial` and `--pct` are the same three
 *     attributes `honesty.prose.test.tsx` mutates against.
 *   - THE ALL-CLEAR IS STILL ONLY AN ALL-CLEAR WHEN EVERY CHECK RAN, and the
 *     `Absent` mark still names which of the three kinds of nothing it is.
 *   - THE COVERAGE COUNTS ARE STILL UNCONDITIONAL. They were `.ctl-card-foot`;
 *     they are the qualifier on the title's own line, which is closer to the
 *     list they qualify than the bottom of a card was. `N blind` is still a
 *     digit on the surface with the sentence as its accessible name.
 */
function AttentionLead({ checks }: { checks: Check[] }) {
  const problems = checks
    .flatMap((c) => (c.status === 'found' ? c.problems : []))
    .sort((a, b) => (a.severity === b.severity ? b.n - a.n : a.severity === 'bad' ? -1 : 1))
  const blind = checks.filter((c): c is Extract<Check, { status: 'blind' }> => c.status === 'blind')
  const reading = checks.filter((c) => c.status === 'reading')
  const clear = checks.filter((c): c is Extract<Check, { status: 'clear' }> => c.status === 'clear')
  const ran = clear.length + checks.filter((c) => c.status === 'found').length
  const complete = blind.length === 0 && reading.length === 0

  // THE ALL-CLEAR IS ONLY AN ALL-CLEAR WHEN EVERY CHECK RAN. Some of what
  // could be wrong was never looked at, and a dial hatched for the part
  // nobody examined says that without a sentence.
  const dialKind = ran === 0 ? 'unknown' : complete ? 'measured' : 'partial'

  return (
    <>
      <div className="ov-lead-head">
        <Dial
          kind={dialKind}
          measured={checks.length === 0 ? 0 : (ran / checks.length) * 100}
          say={
            `${ran} of ${checks.length} checks ran and found ${problems.length} ${problems.length === 1 ? 'problem' : 'problems'}.` +
            (blind.length > 0
              ? ` ${blind.length} could not run, so this list is incomplete: ${blind.map((c) => `${c.label.toLowerCase()} — ${c.why}`).join('; ')}`
              : '') +
            (reading.length > 0
              ? ` ${reading.length} still reading: ${reading.map((c) => c.label.toLowerCase()).join(', ')}.`
              : '') +
            (clear.length > 0
              ? ` Clear: ${clear.map((c) => `${c.label.toLowerCase()} — ${c.note}`).join('; ')}`
              : '') +
            // WHERE THE CHECKS COME FROM, AND WHAT THIS IS NOT. This sentence
            // was the Attention tile's accessible name; the tile is gone (it
            // said the lead's own figure a second time) and the claim is not,
            // because "nothing stores, routes or acknowledges an alert here"
            // is the one thing a reader must not assume the opposite of. It is
            // DERIVED FROM THE CHECKS rather than typed out: the hand-written
            // version named five sources for six checks, and the one it left
            // out, dispatch, is the loudest problem this screen can draw.
            ` Derived on every read from ${sourceList(checks)}. Nothing stores, routes or acknowledges an alert on this platform, so this is not an inbox.`
          }
        >
          {ran === 0 ? (
            <span className="ctl-em">&mdash;</span>
          ) : (
            <>
              {problems.length}
              <span className="ctl-figure-unit">open</span>
            </>
          )}
        </Dial>

        <div className="ov-lead-say">
          {/* THE COUNT IS IN THE TITLE WHEN THERE IS ONE, which is the whole
              reason the tile could go: `10 things need attention` at --t-title
              is the same fact the tile carried, said once, at the rank the
              fact deserves. With nothing found the title is the subject alone
              and the `Absent` mark below it carries which kind of nothing. */}
          <h2 className="ov-lead-title">
            {problems.length === 0
              ? 'Needs attention'
              : `${problems.length} ${problems.length === 1 ? 'thing needs' : 'things need'} attention`}
          </h2>
          {/* NEVER OPTIONAL. What could not be checked is not a footnote: it
              is the reason a short list is or is not good news. Each figure is
              a digit on the surface and the names are the accessible name, the
              same split every other figure on this screen makes. */}
          <p className="ov-lead-cover ov-checks">
            <span
              aria-label={
                clear.length === 0
                  ? 'no check came back clear'
                  : `clear: ${clear.map((c) => `${c.label.toLowerCase()} — ${c.note}`).join('; ')}`
              }
            >
              {ran}/{checks.length} ran
            </span>
            {blind.length > 0 && (
              <>
                {' · '}
                <span
                  className={blind.every((c) => c.admin) ? 'ov-info' : 'ov-warn'}
                  aria-label={`${blind.length} could not run, so this list is incomplete: ${blind.map((c) => `${c.label.toLowerCase()} — ${c.why}`).join('; ')}`}
                >
                  {blind.length} blind
                </span>
              </>
            )}
            {reading.length > 0 && (
              <>
                {' · '}
                <span aria-label={`still reading: ${reading.map((c) => c.label.toLowerCase()).join(', ')}`}>
                  {reading.length} reading
                </span>
              </>
            )}
          </p>
        </div>
      </div>

      <div className="ov-lead-body">
        {problems.length === 0 && reading.length === 0 && (
          <Absent
            kind={blind.length === 0 ? 'zero' : blind.every((c) => c.admin) ? 'admin' : 'partial'}
            heading={blind.length === 0 ? 'Nothing wrong' : `${blind.length} not checked`}
            say={
              blind.length === 0
                ? `All ${clear.length} checks ran and all ${clear.length} came back clear. This is a real all-clear over the population each check examined, not silence.`
                : `${blind.length} of ${checks.length} checks could not run, so this is a partial all-clear: ${blind.map((c) => `${c.label.toLowerCase()} — ${c.why}`).join('; ')}`
            }
            explain="all-clear-basis"
          />
        )}

        {problems.length > 0 && (
          <ul className="ov-problems">
            {problems.slice(0, ATTENTION_ROWS).map((p, i) => (
              <ProblemRow key={`${p.headline}-${i}`} problem={p} />
            ))}
          </ul>
        )}

        {/* Cut, never dropped -- and the rest open HERE. This used to link
            to a trouble board, which no longer exists and should not:
            there is no problem section at any level, so the overflow
            cannot be somebody else's problem. */}
        {problems.length > ATTENTION_ROWS && (
          <details className="ov-more">
            <summary>{problems.length - ATTENTION_ROWS} more</summary>
            <ul className="ov-problems">
              {problems.slice(ATTENTION_ROWS).map((p, i) => (
                <ProblemRow key={`${p.headline}-more-${i}`} problem={p} />
              ))}
            </ul>
          </details>
        )}
      </div>
    </>
  )
}

/**
 * One problem, with the link it goes out by.
 *
 * THE HEADLINE IS THE FACT AND THE DETAIL IS THE EXPLANATION, so the headline
 * is on the surface and the detail is the row's accessible name. A dot repeats
 * the severity as a shape, because roughly 8% of male viewers cannot separate
 * this card's amber from its red and a screenshot of it keeps neither.
 */
function ProblemRow({ problem: p }: { problem: Problem }) {
  return (
    <li className="ov-problem" aria-label={`${p.headline}. ${p.detail}`}>
      <i className={`ctl-dot ${p.severity === 'bad' ? 'is-bad' : 'is-warn'}`} aria-hidden />
      <b>{p.headline}</b>
      <a className="ctl-link ov-link" href={p.href}>
        {p.linkLabel ?? 'open'} &rarr;
      </a>
    </li>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

/**
 * WHY THIS IS STILL HERE AND NOT IN styles.css.
 *
 * docs/web-ui/design-system.md §9.5 asks for this block to be folded into the
 * primitive sheet. It is not folded in THIS pass and the reason is specific
 * rather than lazy: `test_ui_contrast.py` and `test_state_colour_discriminability.py`
 * scan `styles.css` as text and grade every `color`/`background` pair in it,
 * and this pass shipped without running the Python suites. Moving 200 lines
 * under a gate nobody ran is how a screen lands red. Everything below uses
 * ONLY tokens that already exist -- no new colour, no new mix, no new spacing
 * step -- so the fold is a move rather than a rewrite when someone does it
 * with the gates in front of them.
 *
 * Every selector is `ov-`-prefixed and can therefore reach nothing outside
 * this file.
 *
 * THE LAYOUT IS TRACK-COUNT-EXPLICIT AND THAT IS DELIBERATE. An auto-fitting
 * grid needs no breakpoints to maintain, and in exchange nothing -- not the
 * stylesheet, not this file -- can know how many tracks it produced. That is
 * exactly what the rules below need in order to stop the page ending with a
 * blank right column under a column that is still going. Two stated
 * breakpoints cost two lines; a page that wastes a third of a 1600px screen
 * costs it every time anyone opens the product.
 *
 * THE BREAKPOINTS ARE THE SYSTEM'S. Overview used to carry a private set at
 * 641, 720 and 1400. Those are retired: the card grid now turns at 900 and
 * 1280, which are two of the five the design system names (§7.1), and the
 * `.ctl-util` override turns at 900 rather than 641.
 */
const OVERVIEW_CSS = `
/* ---- page head ---------------------------------------------------------- */

/* TITLE OVER PROVENANCE, WHICH IS WHAT EVERY OTHER SCREEN IN THIS PRODUCT
   ALREADY DOES. ".ctl-page-head" is a wrapping flex row with an ".is-end"
   slot on the right; this screen stops using that slot and stacks instead,
   because a strip pinned 900px from the title it qualifies is not read as
   belonging to it. "Screen" (Shell.tsx) renders exactly this shape on Agents,
   Runtimes, Pools and the rest, so the landing page stops being the one
   screen with a different header. */
.ov-head { display: block; }
.ov-prov {
  padding: 0;
  margin: 6px 0 0;
}

/* Every figure that can change is tabular, everywhere. Not optional in a
   column of them, and the read tally changes on every poll. */
.ov-num {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--text);
}
.ov-tally { gap: 6px; }

/* THE BOX CAME OFF THE REFRESH CONTROL. It was the one control on this screen
   wearing a 1px --line border, a radius and a --surface fill -- a button
   silhouette for something that re-runs eight reads and changes no state.
   §1.3's rule for an in-page control is ink plus an underline (.ctl-link), and
   the three other controls on this screen already use it; a fourth answer to
   "this is clickable" is what the primitive exists to stop. What is left here
   is the reset a <button> needs in order to be a link. */
.ov-refresh {
  padding: 0;
  border: 0;
  background: none;
  font: var(--t-meta)/var(--lh-meta) var(--mono);
  cursor: pointer;
}

/* ---- the panel grid ----------------------------------------------------- */

/* THE PARITY SELECTORS ARE GONE WITH THE GRID THAT NEEDED THEM.
   ---------------------------------------------------------------------------
   The old grid was five equal cards in one to three "1fr" tracks, and ten
   nth-child rules underneath it widening whichever card landed last so the
   page did not end with a blank right column under a column that was still
   going. Every one of those rules existed to manage an orphan that only
   exists because five equal objects never fill a three-track row.

   There are three panels now and the layout is ASYMMETRIC, which removes the
   orphan by construction: two tracks, and the panel that needs the width takes
   it. "Running" is a table and takes two thirds; "Spend" is one figure and a
   bar and takes a third; "Headroom" spans both, because it draws five
   four-column utilisation rows and a 400px column is what turned
   "mock · 15 can start" into "mock · 1…" (F1 of the overflow inventory).

   One breakpoint, and it is the system's (§7.1). Below it everything stacks,
   which is what a phone wants and what the old three-stage grid spent two
   breakpoints and ten selectors arriving at. */
.ov-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--ctl-s5);
  align-items: start;
}
@media (min-width: 1280px) {
  /* TWO EQUAL TRACKS, NOT 2:1. Measured at 1440: the running table has three
     columns -- a name, a state chip and an elapsed time -- and two thirds of
     the page put 420px of nothing between the state and the runtime. A track
     wider than its content is not generosity, it is a gap the eye has to
     cross. Headroom is the panel that genuinely needs the width (five
     four-column utilisation rows beside three account rows) and it is the one
     that spans. */
  .ov-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ov-headroom { grid-column: 1 / -1; }
}

/* ---- the lead ----------------------------------------------------------- */

/* THE REGION'S OWN RHYTHM. ".section" already supplies the large break and the
   one hairline between the two regions of this page; what is set here is the
   internal spacing, which is one step down so the lead reads as one block
   rather than as two. */
.ov-lead-head {
  display: flex;
  align-items: center;
  gap: var(--ctl-s3);
  flex-wrap: wrap;
}
.ov-lead-say { flex: 1 1 260px; min-width: 0; }
/* --t-title, which is the page's second rank and the rank this fact has. It
   was --t-lead inside a card head, one step below, competing with four other
   card heads of exactly the same size. */
.ov-lead-title {
  margin: 0;
  font-size: var(--t-title);
  line-height: var(--lh-title);
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text);
}
/* THE COVERAGE, ON THE TITLE'S OWN BLOCK RATHER THAN IN A FOOT AT THE BOTTOM
   OF A CARD. It is chrome about the list -- mono, micro, faint -- and it is
   now within one line of the list it qualifies instead of below it. */
.ov-lead-cover {
  display: block;
  margin: 2px 0 0;
  font: var(--t-micro)/var(--lh-micro) var(--mono);
  color: var(--text-faint);
}
/* The rows sit under the ring rather than beside it. The old two-column
   ".ov-dialrow" put a 96px ring in a third-width card and gave the problem
   headlines about 230px, which is why they were the shortest sentences on the
   screen. Full width, and they are sentences again. */
.ov-lead-body { margin-top: var(--ctl-s3); }
/* The lead's ring is smaller than a card's: it is a qualifier on a title, not
   the subject of a panel. */
.ov-lead .ov-dial { --dial-size: 64px; }
/* The all-clear reads at the lead's rank, not at a card body's. */
.ov-lead-body > .ctl-empty.ov-empty > h3 {
  font-size: var(--t-lead);
  line-height: var(--lh-lead);
}

/* ---- the fact strip ----------------------------------------------------- */

/* THE WHOLE FACT IS THE DOORWAY. The answer to every figure on the strip is on
   another screen, and making the figure itself the link removes a step.

   THE HOVER IS AN UNDERLINE, NOT A BORDER, because there is no border any
   more (§B6.1). It is also not a transform: a 1px lift on an unboxed fact
   moves the text and nothing else, which reads as a rendering glitch rather
   than as an affordance. */
a.ov-tile {
  display: block;
  text-decoration: none;
  color: inherit;
}
a.ov-tile:hover .ctl-metric-value {
  text-decoration: underline;
  text-decoration-color: var(--line-soft);
  text-underline-offset: 4px;
}
a.ov-tile:hover .ctl-metric-label { color: var(--text-dim); }
a.ov-tile:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; border-radius: var(--ctl-radius-sm); }

/* A MARK IS NOT A FIGURE, so the value slot stops being a figure-sized number
   the moment it stops holding one. ".ctl-metric.is-absent" already drops the
   step; this aligns the mark on the same baseline the digit sat on so the
   strip does not jump between states. */
.ov-tile .ctl-metric-value { display: flex; align-items: center; min-height: 30px; }

/* READING. The third absence, and it must not look like either of the other
   two: .ctl-metric.is-absent is hatched and says the platform has no such
   figure, .ctl-metric.is-unread is dashed and amber and says the read failed.
   This one is neither -- the request is still out -- so the fact keeps its
   ordinary unpainted rule and the slot holds a bar that is visibly still
   moving, at the geometry the figure will occupy. It carries no text: a
   gradient has no luminance a contrast gate can measure. */
.ov-pending { display: block; width: 64px; height: 18px; }

/* A read in flight inside a card draws the rules of the table that is coming,
   so the card does not change height when the rows arrive. */
.ov-ghost { margin: 2px 0; }

/* THE TABLE GIVES UP ITS OWN EDGES INSIDE A CARD, and that is a geometry fix
   as much as a visual one. .ctl-table carries a border, a radius and a fill,
   so a body wearing it is a SURFACE sitting directly on .ctl-card-foot, which
   is another one -- and .ctl-card is a flex column with no gap, so the two
   touch. spacing.test.tsx reports exactly that. A gutter is the wrong answer:
   the card has already drawn this box, and a bordered table inset inside a
   bordered card is a box in a box. The scroll behaviour is what the primitive
   is here for and it stays.

   (No backticks in this comment: it lives inside a JS template literal, where
   one ends the CSS.) */
.ctl-card-body.ov-rows {
  border: 0;
  border-radius: 0;
  background: none;
}

/* ---- cards -------------------------------------------------------------- */

/* EIGHTEEN ACCENT PAINTS ON ONE SCREEN, AND NOW NONE.
   Measured on this view before the change: --info was painted 23 times inside
   main.work -- 18 of them these links ("open ->", "agents ->", "history ->",
   "pools ->"), one the "6 more" disclosure, and four the live chips' dots.
   design-system.md sec 1.3 budgets the accent at once or twice per screen, and
   sec 11.3 named .ov-link as one of the five screen-private link treatments
   that .ctl-link exists for them to collapse into. This is that collapse: the
   three call sites now carry "ctl-link ov-link", the primitive paints it (ink
   plus a --line-soft underline, accent on hover and focus), and what is left
   here is the LAYOUT ONLY.

   The colour, the text-decoration, the transparent bottom border and the focus
   rule all had to go rather than be overridden to match: this block is injected
   as a <style> AFTER styles.css, so at equal specificity every one of them
   would have out-ranked the primitive and quietly reinstated the blue.

   The four --info paints that remain are .ctl-chip.is-live > i, which is the
   state channel and not an affordance. */
.ov-link {
  flex: none;
  font: var(--t-micro)/var(--lh-micro) var(--mono);
  white-space: nowrap;
}

/* An empty state INSIDE a card rather than as the page. The card already draws
   the box and the title, so a second bordered surface inside it is one box too
   many -- and a tone wash under a mark that already names the kind is
   redundant twice over. The MARK is the greyscale-safe signal and the h3
   colour is the second one; the box is the card's. */
.ctl-empty.ov-empty {
  border: 0;
  background: none;
  padding: 0;
  display: flex;
  align-items: center;
  gap: var(--ctl-s2);
  flex-wrap: wrap;
}
/* Not --t-title. A card's own title is --t-lead and an empty state inside it
   cannot be louder than the card it is in. */
.ctl-empty.ov-empty > h3 {
  margin: 0;
  font-size: var(--t-lead);
  line-height: var(--lh-lead);
}
/* The page-level one keeps its box: there is no card around it. */
.ov-page-empty { display: flex; align-items: flex-start; gap: var(--ctl-s3); flex-wrap: wrap; }
.ov-page-empty > h3 { margin: 0; flex: 1 1 auto; }
.ov-page-empty > .retry { margin-top: 0; }

/* ---- the dial row ------------------------------------------------------- */

/* The dial is the card's proportion and the rows are its detail. NO
   BREAKPOINT: the rows carry a 260px flex basis and the row wraps when the
   ring and the rows stop fitting beside each other, which at 390px is always.
   A width that answers for itself is one fewer number to keep in step with
   the other five. */
.ov-dialrow {
  display: flex;
  align-items: center;
  gap: var(--ctl-s3);
  flex-wrap: wrap;
}
.ov-dialrow-rows { flex: 1 1 260px; min-width: 0; }
.ov-dial { --dial-size: 96px; flex: none; }
.ov-dial .ctl-dial-figure {
  font-size: var(--t-figure);
  line-height: var(--lh-figure);
  font-weight: 600;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}
/* A ring nobody could fill holds no figure, so what is in the middle is an em
   dash and it must not wear the figure step: at 30px a dash reads as a
   quantity. */
.ov-dial.is-unknown .ctl-dial-figure {
  font-size: var(--t-body);
  line-height: var(--lh-body);
  font-weight: 500;
}

/* ---- spend -------------------------------------------------------------- */

.ov-figure { margin: 0 0 var(--ctl-s2); }
.ov-figure-mark { margin: calc(-1 * var(--ctl-s1)) 0 var(--ctl-s2); }

/* THE TOKEN MIX. Four segments, one total, hand-rolled -- the proportion is
   the point and four numbers in a list is not one. Height and radius are the
   track's, because every proportion in this product is drawn at that size. */
.ov-mix {
  display: flex;
  height: var(--track-h);
  border-radius: var(--track-radius);
  overflow: hidden;
  background: var(--surface-2);
  margin-bottom: var(--ctl-s2);
}
.ov-mix-seg { height: 100%; }
/* NOTHING MEASURED, SO NO SCALE. Hatched rather than left empty: an empty
   track claims the scale starts somewhere and the value is at the start of
   it, which is the absent-as-zero lie in bar form. */
.ov-mix.is-unknown { background: var(--ctl-hatch); }
/* ONE METRIC, ONE HUE, FOUR TONES. SERIES, NEVER STATE.
   A segment drawn in --ok is read as a verdict, so these were --series-1..4:
   blue, teal, violet, amber. That was four saturated hues in one 8px rule --
   two of them the colours a reader has learned for PARKED (violet) and WARN
   (amber) -- and the series block itself records that those five sit in a
   1.36:1 band, NOT separable in greyscale. The keyed legend (design-system.md
   §12.1) told a colour reader which swatch was which; a greyscale screenshot
   still showed four identical greys.
   Tokens are one metric split four ways, so they get ONE series (--series-1,
   the single-series slot) at four tones.
   THE TONES STEP TOWARD THE INK, NEVER TOWARD THE CARD. The first ramp mixed
   toward --surface and bought its greyscale steps by fading three of the four
   into the card: c-wr's 8px swatch measured 1.35:1 on white and 1.42:1 on the
   dark card, under the 3:1 every series fill promises (styles.css, THE SERIES
   PALETTE; WCAG 1.4.11). --series-1 is already the floor of that band -- 3.36
   on light --surface-2 -- so any step toward the card goes under it, and the
   only direction with room is toward --text: lighter on the dark card, darker
   on the light one. At 100/75/50/25% the worst fill is --series-1 itself
   (3.36, light --surface-2), and every other one is >= 4.77 on --bg, --surface
   and --surface-2 in both themes; every pair is >= 1.32:1 (dark c-rd/c-wr).
   Order is the reading order: input is the series hue itself, and each later
   count sits one step nearer the ink, so c-wr is the heaviest mark. The .ov-sN
   class names are unchanged, so each swatch still takes its fill from the same
   class its segment does. encoding.hues.test.ts holds both floors. */
.ov-s1 { background: var(--series-1); }
.ov-s2 { background: color-mix(in srgb, var(--series-1) 75%, var(--text)); }
.ov-s3 { background: color-mix(in srgb, var(--series-1) 50%, var(--text)); }
.ov-s4 { background: color-mix(in srgb, var(--series-1) 25%, var(--text)); }
.ov-mix-facts { padding: 0; }

/* THE KEY TO THE FOUR TONES, NEXT TO THE WORD THEY BELONG TO.
   8px square, --track-radius so it is the same corner the bar it keys is drawn
   with, and it takes its fill from the SAME .ov-sN class the segment does --
   one declaration per series, so a segment and its key cannot drift apart.
   A square rather than a disc on purpose: .ctl-dot's vocabulary is STATE, and a
   series is an identity, not a verdict (sec 1.6). */
.ov-swatch {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: var(--track-radius);
  /* No margin: .ctl-fact is an inline-flex with a 5px gap and adding to it
     would make this one gap in the row wider than the other two. align-self
     because the strip aligns on the BASELINE and an empty <i> has none, which
     would drop the square to the bottom of the line box. */
  align-self: center;
}
/* NOTHING REPORTED, SO NO KEY TO ANYTHING. Hollow, in the absence tone, at the
   same size: the row keeps its rhythm and the swatch stops claiming a segment
   that was never drawn. The em dash beside it carries the fact. */
.ov-swatch.is-absent {
  background: none;
  border: 1px solid var(--ctl-absent);
}

/* ---- headroom: one panel, two groups ------------------------------------ */

/* TWO CARDS BECAME TWO GROUPS INSIDE ONE PANEL, and the padding moved up with
   them: the panel owns the inset once, and each group draws no edge, no fill
   and no radius of its own. §13.3 -- a panel is the one box; what repeats
   inside it is a row, and a row draws nothing. */
.ov-groups {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--ctl-s5);
  padding: var(--ctl-pad-chrome);
}
@media (min-width: 900px) {
  /* The two ceilings side by side, which is the whole reason they are one
     panel: a task clears every pool in its list AND THEN takes a subscription
     account, so the two are read together or not at all. */
  .ov-groups { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.ov-group { min-width: 0; }
/* THE RING STACKS ABOVE ITS ROWS INSIDE A GROUP, RATHER THAN STANDING BESIDE
   THEM. ".ov-dialrow" is a two-column flex built for a card whose whole body
   was one dial and one list; inside a half-width group it indented every
   account row by the dial's 96px plus the gap, so the two groups in this panel
   started their rows 120px apart and read as unrelated. Stacked, the ring is
   the group's own summary figure and the rows below it line up with the
   profiles on the left. The dial itself is untouched -- it still carries the
   coverage as a filled-versus-hatched arc, which is the fact the strip's
   figure above does NOT carry. */
.ov-group .ov-dialrow { display: block; }
.ov-group .ov-dial { margin-bottom: var(--ctl-s2); }
/* THE GROUP LABEL IS A RANK BELOW THE PANEL TITLE AND A RANK ABOVE THE ROWS.
   --t-meta in the mono/faint label treatment, which is §13.2's label rank --
   mono plus --text-faint, with no uppercase and no tracking, because those
   were the third and fourth channels on a distinction that already had two. */
.ov-grouphead {
  display: flex;
  align-items: center;
  gap: var(--ctl-s2);
  margin: 0 0 var(--ctl-s2);
  font: 600 var(--t-meta)/var(--lh-meta) var(--mono);
  color: var(--text-faint);
}
/* The group's body and provenance take the panel's inset from .ov-groups, so
   they set none of their own. */
.ov-group .ctl-card-body { padding: 0; }
/* THE FOOT STOPS BEING A FULL-BLEED BAR AND BECOMES A CAPTION, because there
   are two of them in one panel and two --surface-2 bars stacked inside one box
   is two boxes with the lines rubbed out. It keeps the mono/micro/faint
   treatment, which is what said "this is about the reading, not the reading"
   before the fill did. TWO FEET RATHER THAN ONE, deliberately: these are two
   independent reads and a single merged foot would have to average two ages. */
.ov-group .ctl-card-foot {
  margin: var(--ctl-s3) 0 0;
  padding: 0;
  background: none;
  border-radius: 0;
}

/* ---- capacity and accounts --------------------------------------------- */

/* The utilisation primitive is sized for a full-width screen; inside the
   headroom panel it shares the width with the account group, so the name still
   needs a stated floor rather than whatever four fixed tracks leave it.

   F1 OF THE 2026-09-23 OVERFLOW INVENTORY IS FIXED BY THE PANEL'S NEW WIDTH,
   NOT BY THIS RULE. The capacity rows were in a 400px third-of-a-grid card,
   where "mock · 15 can start" (160px of content) got a 79px name column and
   rendered "mock · 1…" -- a prefix of a number standing where the number was.
   The panel spans the grid now, so the group is ~550px at 1440 and the same
   template gives the name about 300. The stated minimum below is what keeps it
   from happening again when the panel is narrower than that.

   ABOVE 900px ONLY. The primitive deliberately drops the track and the "set
   by" column on a phone and keeps the name and the figure; an unscoped
   override here would out-specify that and put four columns back into 358px.
   It is the primitive's decision to make, not this screen's. */
@media (min-width: 900px) {
  .ov-dialrow-rows .ctl-util,
  .ov-group .ctl-card-body > .ctl-util {
    grid-template-columns: minmax(14ch, 1fr) minmax(40px, 88px) max-content minmax(0, 96px);
    gap: var(--ctl-s2);
    padding: 4px 0;
  }
}

/* "0 can start" is the one phrase on this screen that changes what someone
   does next, so it is not left as ordinary grey text. */
.ov-stop { color: var(--bad); font-weight: 500; }

/* A figure that is REAL but not CURRENT: its window reset, or the poll is past
   the staleness window. Grey, never the red or amber that says a ceiling is
   being approached now, and the tilde beside it is the same mark that cs
   status and the Accounts screen use for the same two cases. */
.ctl-util-fill.ov-projected { background: var(--ctl-absent); }
.ov-tilde { color: var(--ctl-absent); margin-right: 1px; }

/* ---- attention ---------------------------------------------------------- */

.ov-problems { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--ctl-s2); }
/* THE LINK FOLLOWS THE SENTENCE. In a third-width card the third track was
   pinned to an edge 230px away and that read as a column; out here on the full
   page the same rule put "open ->" 1,100px from the headline it opens, with a
   white gap between them that a reader has to cross to connect the two. A row
   that is a sentence plus its verb keeps the verb next to the sentence, so the
   track list stops at the content and "justify-content: start" holds the row
   there rather than stretching it to the region's width. */
.ov-problem {
  display: grid;
  grid-template-columns: 8px minmax(0, auto) max-content;
  justify-content: start;
  gap: var(--ctl-s3);
  align-items: baseline;
  font-size: var(--t-body);
  line-height: var(--lh-body);
  color: var(--text-dim);
}
.ov-problem > b { color: var(--text); font-weight: 500; min-width: 0; }

/* The problems that did not fit. A disclosure rather than a link out, because
   there is no board to link to. */
.ov-more { margin-top: var(--ctl-s2); }
.ov-more > summary {
  cursor: pointer;
  /* --t-meta, not --t-micro: this is a CONTROL, and the micro step is for
     things you read, not things you click. */
  font: var(--t-meta)/var(--lh-meta) var(--mono);
  /* NOT THE ACCENT. This is the nineteenth --info paint the audit counted, and
     it is the one control on the card that does not navigate -- it opens the
     rest of a list that is already on this screen. The disclosure triangle
     below is the affordance, and it is a SHAPE, which is what sec 1.3 asks a
     control to lead with; it also rotates on open, so the state is carried
     without colour at all. */
  color: var(--text-dim);
  list-style: none;
}
.ov-more > summary:hover { color: var(--text); }
.ov-more > summary::-webkit-details-marker { display: none; }
.ov-more > summary::before { content: '\\25B8  '; }
.ov-more[open] > summary::before { content: '\\25BE  '; }
.ov-more > summary:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; border-radius: var(--ctl-radius-sm); }
.ov-more > .ov-problems { margin-top: var(--ctl-s2); }

/* ---- tones in the provenance strip -------------------------------------- */

/* An admin gate is information, not breakage, so the figure that reports only
   admin gates is the accent and never the amber used for a real failure. */
.ov-info { color: var(--info); }
.ov-warn { color: var(--warn); }
.ov-bad { color: var(--bad); }
.ov-checks:empty { display: none; }
`
