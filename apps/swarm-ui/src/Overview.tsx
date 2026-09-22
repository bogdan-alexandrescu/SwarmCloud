import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
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
  stateGlyph,
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
 * OVERVIEW. The landing screen, and the one-stop shop.
 *
 * It answers five questions on one laptop screen, and each ends in a link to
 * the screen that goes deeper -- except the derived checks, which have no
 * deeper screen because there is no problem board and should not be one; every
 * row of that pane links to the OBJECT it is about instead. Nothing here is a
 * dead end and nothing here is the last word: this screen's job is to tell you
 * which of the other four sections to open, in the first three seconds.
 *
 *   1. CAPACITY -- used against available, and WHICH pool binds each runner
 *      profile. The conjunction is the whole trap: a task must clear every
 *      pool in its list at the same moment, so its ceiling is the MINIMUM
 *      across them. Raising the pool that is not binding changes nothing, and
 *      an operator who cannot see which one binds raises the wrong one.
 *   2. WHAT IS RUNNING, with runtime so far.
 *   3. WHAT IS WRONG, derived from real state -- never from an alert model,
 *      because this platform has none. WORKFLOWS COUNT AS RUNNING THINGS
 *      HERE. This screen used not to contain the word: with a three-step
 *      workflow in the tenant holding one READY step and two PARKED ones it
 *      said "nothing is running" and was, sentence by sentence, correct --
 *      because a workflow that has stopped moving breaks no lease, holds no
 *      capacity and fails no task, so not one of the six checks could see it.
 *      `checks.ts` now asks about workflows and about parked work directly.
 *   4. SPEND, in tokens and dollars of token cost, with the scope named.
 *   5. THE SUBSCRIPTION POOL's headroom, which is the ceiling that binds
 *      first in practice and used to be buried three clicks down.
 *
 * EIGHT READS, INDEPENDENTLY -- seven routes plus the spend rollup fanned out
 * over them. One failing must not blank the page and must not leave the page
 * looking complete, so each panel owns its own state and the line under the
 * title counts what landed, what is still in flight, what failed and what was
 * refused for want of admin. Those are four different things and the line says
 * which. `Screen` is deliberately not used here because it has exactly one
 * load and one failure, and this screen has eight of each.
 *
 * WHAT THIS SCREEN DOES NOT DRAW, and why it is not a placeholder:
 *   - infrastructure cost in dollars. There is no billing integration of any
 *     kind, so Cloud Run, Firestore and GCS spend are simply not recorded.
 *     The spend panel says so in words rather than showing a figure that
 *     would be read as the whole bill.
 *   - an alert inbox. Nothing stores, routes or acknowledges an alert. What
 *     is here is DERIVED from state that exists, re-derived on every read, and
 *     it says which checks could not run rather than implying silence is calm.
 *   - checkpoint contents. Only a uri, an id and a byte size are recorded.
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
  // expensive thing on the platform, and would spend the caller's 20 rps
  // bucket on figures that change slowly.
  //
  // BOTH THEREFORE CARRY THEIR READ AGE, on the tile and, for spend, in the
  // panel as well. A figure that does not re-poll sitting beside five that
  // refresh every twenty seconds is indistinguishable from them unless it says
  // how old it is, and the one that goes stale is the one denominated in
  // dollars.
  const [live, setLive] = useState(0)
  const [heavy, setHeavy] = useState(0)
  const refresh = useCallback(() => {
    setLive((n) => n + 1)
    setHeavy((n) => n + 1)
  }, [])

  useEffect(() => {
    const id = setInterval(() => setLive((n) => n + 1), 20_000)
    return () => clearInterval(id)
  }, [])

  const capacity = useRead(loadCapacity, live)
  const tasks = useRead(loadTasks, live)
  const leases = useRead(loadLeases, live)
  const providers = useRead(loadProviders, live)
  const accounts = useRead(loadAccountPool, live)
  // ON THE LIVE CADENCE, with the other five, because a workflow that has
  // stopped moving is the fact this screen was worst at reporting and a figure
  // that only refreshes when someone presses a button cannot report it. It is
  // one indexed list read of the same class as the others; the route derives
  // each row's state from steps it has already loaded, so the browser does not
  // pay for a second join.
  const workflows = useRead(loadWorkflows, live)
  const stats = useRead(loadStats, heavy)

  const spend = useSpend(tasks, heavy)

  // Typed as `Result<unknown>` because this list only ever asks about a read's
  // OUTCOME, never its payload. Leaving it to inference makes it a union of
  // differently-parameterised Results that matches no single `Result<T>`.
  const reads: Result<unknown>[] = [
    capacity, tasks, leases, providers, accounts, workflows, stats, spend,
  ]

  // A 401 is a PAGE-level state, not a panel-level one: an expired IAP session
  // fails all of them at once, and seven independently empty panels is the bug
  // this whole UI is built against.
  if (reads.some((r) => isKind(r, 'unauthenticated') || isKind(r, 'session_expired'))) {
    return (
      <div className="state failed">
        <h3>Your session expired</h3>
        <p>
          The API answered a sign-in page instead of data. Nothing on this
          screen is a reading of the platform right now.
        </p>
        <button className="retry" onClick={() => window.location.reload()}>
          Reload to sign in
        </button>
      </div>
    )
  }

  // FOUR OUTCOMES, COUNTED SEPARATELY, because they are four different facts
  // about this screen and collapsing any two of them produces a sentence that
  // is false at first paint.
  //
  //   - landed: a reading arrived (`empty` is a reading: it is a real zero).
  //   - pending: still in flight. On first paint that is ALL of them, and
  //     "all 7 reads landed" printed then is the screen's own provenance line
  //     lying about the screen.
  //   - refused: 403 on the one admin route here. Not a failure -- a non-admin
  //     legitimately cannot make it -- but it did not land either, and the
  //     Leases check below reports itself blind at the same moment.
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
  // ticking clock would re-render every panel on this screen once a second to
  // move a threshold that is ten minutes wide. A read lands every twenty
  // seconds, which is 3% of the narrowest age this panel reports.
  const checks = useMemo(
    () =>
      deriveChecks(
        { capacity, tasks, leases, providers, accounts, workflows, stats },
        Date.now(),
      ),
    [capacity, tasks, leases, providers, accounts, workflows, stats],
  )
  const found = checks.reduce((n, c) => n + (c.status === 'found' ? c.problems.length : 0), 0)

  return (
    <>
      <style>{OVERVIEW_CSS}</style>

      {/* Title and provenance on ONE line. The section's own question is
          already printed directly above this by the shell, so a stacked title
          block here spends forty pixels of a screen whose whole promise is
          that it fits without scrolling. */}
      <div className="ov-topline">
        <div className="head">
          {/* NO ENVIRONMENT BADGE. Every other screen carries a hardcoded
              "dev" here; nothing in this app reads an environment, and on the
              one screen whose whole claim is that each figure declares where
              it came from, a word nobody measured is the loudest thing on it.
              It comes back when the API reports its own environment. */}
          <h1>Overview</h1>
        </div>
        <p className="sub">
          <Scope tasks={tasks} /> · <Provenance
            total={reads.length}
            landed={landed}
            pending={pending}
            refused={refused}
            broken={broken}
          />{' '}
          <button onClick={refresh}>refresh</button>
        </p>
      </div>

      <MetricStrip
        capacity={capacity}
        stats={stats}
        accounts={accounts}
        spend={spend}
        checks={checks}
        found={found}
      />

      {/* `has-alarm` rather than an inline span on the panel, because the
          stylesheet needs to know it. The rules that stop this grid ending with
          a blank right column are parity selectors over the panels, and a panel
          that silently spans two tracks shifts every parity below it. */}
      <div className={`ov-cols${found > 0 ? ' has-alarm' : ''}`}>
        {/* THE ALARM TAKES THE TOP WHEN IT IS RINGING. With something to say it
            spans both columns and is the first panel under the tiles; when
            every check came back clear it collapses into one column and gets
            out of the way. A control plane whose problem list is the same size
            whether or not there are problems teaches you to stop looking.

            This is the one pane with no "open →" of its own, and that is not an
            oversight: there is no problem board to open. Every row carries the
            link to the object it is about, and the rows that do not fit expand
            in place rather than pointing at a screen that does not exist. */}
        <section className="section panel ov-alarm">
          <PanelHead title="Needs attention" count={found} />
          <AttentionBody checks={checks} />
        </section>

        <section className="section panel">
          <PanelHead
            title="Capacity, by what binds it"
            href="#pools/profiles"
            cta="all pools"
          />
          <CapacityBody state={capacity} />
        </section>

        <section className="section panel">
          <PanelHead title="Running now" href="#agents/running" cta="all agents" />
          <RunningBody tasks={tasks} stats={stats} />
        </section>

        <section className="section panel">
          <PanelHead title="Spend" href="#history/timeline" cta="history" />
          <SpendBody state={spend} tasks={tasks} />
        </section>

        <section className="section panel">
          <PanelHead title="Subscription pool" href="#pools/accounts" cta="all accounts" />
          <AccountsBody state={accounts} />
        </section>
      </div>
    </>
  )
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

/**
 * THE SCOPE LINE, and it is not decoration.
 *
 * `/v1/stats` and `/v1/tasks` are tenant-scoped while the `global` pool is
 * platform-wide, so a figure on this screen means nothing until you know which
 * of the two it is. Every panel below declares its own scope; this says whose
 * view the screen as a whole is.
 */
function Scope({ tasks }: { tasks: Result<TaskPage> }) {
  const page = dataOf(tasks)
  if (!page?.tenant_id) return <>your tenant</>
  return (
    <>
      tenant <strong>{page.tenant_id}</strong>
    </>
  )
}

/**
 * How much of this screen is actually a reading, in one clause.
 *
 * "all 7 reads landed" is the sentence this has to earn, and it is only true
 * when nothing is in flight, nothing failed and nothing was refused. Printed
 * before that it is the page vouching for itself while every panel is still a
 * skeleton -- which is the single most misleading thing a control plane can
 * say, because it is said in the place a reader checks to find out whether to
 * trust the rest.
 */
function Provenance({
  total,
  landed,
  pending,
  refused,
  broken,
}: {
  total: number
  landed: number
  pending: number
  refused: number
  broken: number
}) {
  if (broken > 0) {
    return (
      <strong className="ov-broken">
        {broken} of {total} reads failed
        {pending > 0 && ` · ${pending} still arriving`}
        {refused > 0 && ` · ${refused} admin only`}
      </strong>
    )
  }
  if (pending > 0) {
    return (
      <>
        {landed} of {total} reads landed · {pending} still arriving
        {refused > 0 && ` · ${refused} admin only`}
      </>
    )
  }
  // Nothing failed and nothing is outstanding, but a refused read is not a
  // landed one. Saying "all 7 landed" here would contradict the Leases check
  // below, which is simultaneously reporting itself blind.
  if (refused > 0) {
    return (
      <>
        {landed} of {total} reads landed ·{' '}
        <span className="ov-info">
          {refused} needs admin, so {refused === 1 ? 'one check' : `${refused} checks`} below could
          not run
        </span>
      </>
    )
  }
  return (
    <>
      all {total} reads landed
    </>
  )
}

// ---------------------------------------------------------------------------
// The metric strip
// ---------------------------------------------------------------------------

/**
 * Five tiles, four doorways. Four of them are anchors, because the answer to
 * the number is on another screen and making the number itself the link
 * removes a step; "Needs attention" is not, because its answer is the pane
 * directly below it and a link to the screen you are on is a click that does
 * nothing.
 *
 * Every tile has FOUR renderings and they must not converge:
 *
 *   - a figure;
 *   - "reading…", a pulsing bar: the read is in flight and there is nothing to
 *     say yet;
 *   - "not recorded", dashed and grey: the platform genuinely has no such
 *     figure;
 *   - the error, dashed and amber: the platform may well have it, we did not
 *     get it.
 *
 * The last three are all "no number", and the temptation is to let a tile fall
 * from one into the next. It must not: "not recorded" is a claim ABOUT THE
 * PLATFORM, and printing it over a request that is still in flight tells the
 * reader the figure does not exist when what is true is that it has not
 * arrived. Against a real API that lasts as long as the request, and a hung
 * request leaves the claim on screen for ever. So `reading` is a prop, it
 * outranks `absent`, and every caller passes it.
 */
function MetricStrip({
  capacity,
  stats,
  accounts,
  spend,
  checks,
  found,
}: {
  capacity: Result<Capacity>
  stats: Result<Stats>
  accounts: Result<AccountsPage>
  spend: Result<SpendRollup>
  checks: Check[]
  found: number
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

  const blind = checks.filter((c) => c.status === 'blind').length
  const ran = checks.filter((c) => c.status === 'clear' || c.status === 'found').length
  const reading = checks.filter((c) => c.status === 'reading').length

  const pool = accountHeadroom(accounts)
  const sp = dataOf(spend)

  return (
    <div className="ctl-metrics">
      <Tile
        href="#agents/running"
        label="Running now"
        value={inFlight}
        unit="agents"
        sub="LEASED, DISPATCHED, STARTING, RUNNING — the states that reserve capacity"
        foot={footFor(stats, 'counted')}
        reading={stats.status === 'loading'}
        unread={stats.status === 'error' ? errorHeading(stats.error) : null}
        tone={inFlight !== null && inFlight > 0 ? 'good' : undefined}
      />

      <Tile
        href="#pools/pools"
        label="Units held"
        value={global ? global.active : null}
        unit={global ? `of ${global.effective_limit}` : undefined}
        // "units", never "agents": admission increments by the resource
        // class's weight, so 8 may be two large agents or eight standard ones.
        sub="weighted units on the platform-wide global pool — not a count of agents"
        foot={footFor(capacity, 'read')}
        reading={capacity.status === 'loading'}
        unread={capacity.status === 'error' ? errorHeading(capacity.error) : null}
        absent={
          capacity.status !== 'error' && cap !== null && global === null
            ? 'no global pool'
            : null
        }
        tone={global && overCeiling(global) ? 'alert' : undefined}
      />

      <Tile
        // THE ONE TILE THAT IS NOT A DOORWAY, because its answer is on this
        // screen: the pane below holds the same checks in full, and each of
        // its rows links to the object it is about. It used to point at the
        // trouble board; a link to the page you are already on is a click that
        // does nothing, which is worse than no link.
        label="Needs attention"
        value={ran === 0 ? null : found}
        unit={found === 1 ? 'thing' : 'things'}
        // DERIVED FROM THE CHECKS THEMSELVES, never from a list typed out
        // here. The hand-written version named five sources for six checks and
        // the one it left out, dispatch, is the loudest problem this screen
        // can draw -- a reader told "1 thing" over a caption with no dispatch
        // in it does not expect a platform-wide pause.
        // "nothing found in …" is a result and is only said once something has
        // actually looked. While the checks are still reading, the line names
        // what they are reading and nothing more.
        sub={
          ran > 0 && found === 0
            ? `nothing found in ${sourceList(checks)}`
            : `derived from ${sourceList(checks)}`
        }
        // THE FOOT IS THE HONEST PART. A "0" with two checks blind is a
        // reassurance nobody earned, so the count of checks that could not run
        // sits under the figure every time rather than only when it is
        // convenient.
        foot={
          ran === 0 && reading > 0
            ? `${reading} of ${checks.length} checks still reading${blind > 0 ? ` · ${blind} could not run` : ''}`
            : ran === 0
              ? `all ${checks.length} checks were blind`
              : `${ran} of ${checks.length} checks ran${blind > 0 ? ` · ${blind} could not` : ''}${reading > 0 ? ` · ${reading} still reading` : ''}`
        }
        // Three states, not two. Every check still in flight is a READING;
        // every check blind with none in flight is a failure to read; and the
        // amber "no check ran" treatment over a page that has been open for
        // 200ms was the second of those printed over the first.
        reading={ran === 0 && reading > 0}
        unread={ran === 0 && reading === 0 && blind > 0 ? 'no check could run' : null}
        tone={found > 0 ? 'alert' : ran > 0 && blind === 0 && reading === 0 ? 'good' : undefined}
      />

      <Tile
        href="#history/timeline"
        label="Token spend"
        value={sp && sp.costUsd !== null ? money(sp.costUsd) : null}
        sub={
          sp
            ? `${sp.attemptsWithCost} of ${sp.attempts} attempts, over the ${sp.tasksSampled} most recent tasks that ran`
            : 'summed from the attempts of the most recent tasks that ran'
        }
        // THE AGE IS NOT OPTIONAL ON THIS ONE. Spend is the only read on the
        // screen that does not re-poll -- it moves when someone presses
        // refresh and at no other time -- so without its age a figure hours
        // old sits beside five that refreshed twenty seconds ago and looks
        // exactly like them. The scope caveat stays too: this is the number
        // someone screenshots, and "spend" with no qualifier reads as the bill.
        foot={`${footFor(spend, 'summed') ?? 'not summed yet'} · refresh only · token cost, never infra`}
        reading={spend.status === 'loading'}
        unread={spend.status === 'error' ? errorHeading(spend.error) : null}
        absent={
          spend.status === 'empty'
            ? 'no task has run'
            : sp && sp.costUsd === null
              ? 'no attempt reported cost'
              : null
        }
      />

      <Tile
        href="#pools/accounts"
        label="Subscription headroom"
        value={pool.pct === null ? null : Math.round(pool.pct)}
        unit={pool.pct === null ? undefined : '% left'}
        sub={pool.sub}
        foot={pool.foot}
        reading={pool.reading}
        unread={accounts.status === 'error' ? errorHeading(accounts.error) : null}
        absent={pool.absent}
        tone={pool.pct !== null && pool.pct < 15 ? 'alert' : undefined}
      />
    </div>
  )
}

/**
 * The checks' own labels, as an English list.
 *
 * Read off `checks` rather than written out, so a seventh check cannot leave
 * the caption describing six.
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

function Tile({
  href,
  label,
  value,
  unit,
  sub,
  foot,
  reading,
  unread,
  absent,
  tone,
}: {
  /** Omitted only where the answer is already on this screen. */
  href?: string | undefined
  label: string
  /** The figure. null means there is none, and the three props below say why. */
  value: number | string | null
  unit?: string | undefined
  sub: string
  foot?: string | undefined
  /** True while the read is IN FLIGHT. Not an absence -- not yet anything. */
  reading?: boolean | undefined
  /** Set when the READ failed: the platform may have this, we did not get it. */
  unread?: string | null
  /** Set when the platform genuinely has no such figure. */
  absent?: string | null
  tone?: 'alert' | 'good' | undefined
}) {
  // ORDER MATTERS, AND THIS IS THE ORDER.
  //
  // A failed read outranks everything: it must never fall through to a figure
  // from an earlier state or to a reassuring absence. `reading` comes next and
  // outranks BOTH the figure and the absence -- a tile with a figure in hand
  // is not re-drawn as a skeleton mid-refresh (callers only pass `reading` for
  // a first load, where `value` is null anyway), and a tile with nothing in
  // hand must say "reading", never "not recorded", which is a statement about
  // the platform rather than about this request.
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
  // The hover and focus rules are scoped to `a.ov-tile` and simply do not
  // apply to it.
  const Box = href === undefined ? 'div' : 'a'

  return (
    <Box className={`ctl-metric ov-tile ${cls}`} href={href}>
      <span className="ctl-metric-label">{label}</span>
      <span className="ctl-metric-value">
        {state === 'unread' ? (
          unread
        ) : state === 'reading' ? (
          // The bar is decoration and carries no meaning a screen reader could
          // use; the word beside it is the state, so the word is the thing
          // that is announced.
          <>
            <span className="skeleton ov-bar" aria-hidden />
            <span className="ov-reading-word">reading…</span>
          </>
        ) : state === 'absent' ? (
          absent ?? 'not recorded'
        ) : (
          <>
            {value}
            {unit && <span className="ctl-metric-unit">{unit}</span>}
          </>
        )}
      </span>
      <span className="ctl-metric-sub">{sub}</span>
      {foot && <span className="ctl-metric-foot">{foot}</span>}
    </Box>
  )
}

// ---------------------------------------------------------------------------
// Panel chrome
// ---------------------------------------------------------------------------

/**
 * Every panel's header carries the link out. "Nothing is a dead end" is a
 * property of the component rather than of each author's diligence: there is
 * no way to draw a panel here without naming the screen that goes deeper.
 */
function PanelHead({
  title,
  count,
  href,
  cta,
}: {
  title: string
  count?: number
  /**
   * Omitted by exactly one panel, "Needs attention", and the reason is that
   * there is no screen that is a deeper version of it: it is derived from six
   * reads across four sections, each of its rows already links to the object
   * it is about, and the rows that do not fit expand in place. Every OTHER
   * panel draws one table and has a screen that draws the whole of it, so
   * leaving this off is a decision rather than a default.
   */
  href?: string | undefined
  cta?: string | undefined
}) {
  return (
    <div className="ov-h">
      <h2>
        {title}
        {typeof count === 'number' && count > 0 && <span className="ov-count">{count}</span>}
      </h2>
      {href !== undefined && cta !== undefined && (
        <a className="ov-link" href={href}>
          {cta} →
        </a>
      )}
    </div>
  )
}

/** The four ways a panel can have nothing to draw, kept visually distinct. */
function Nothing({
  kind,
  heading,
  children,
}: {
  kind: 'zero' | 'failed' | 'partial' | 'admin'
  heading: string
  children: ReactNode
}) {
  const cls = kind === 'zero' ? '' : `is-${kind}`
  return (
    <div className={`ctl-empty ov-tight ${cls}`}>
      <h3>{heading}</h3>
      <p>{children}</p>
    </div>
  )
}

function Reading() {
  return <div className="skeleton ov-skel" aria-hidden />
}

// ---------------------------------------------------------------------------
// 1. Capacity, by what binds it
// ---------------------------------------------------------------------------

/**
 * One row per runner profile: how many more could start, and WHICH POOL STOPS
 * MORE.
 *
 * This is the screen's most load-bearing panel and the reason is arithmetic. A
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
 * much capacity does the platform have". The scope is stated on the heading,
 * every time, and a platform-wide figure is not faked by substituting another
 * tenant's pools.
 */
function CapacityBody({ state }: { state: Result<Capacity> }) {
  if (state.status === 'loading') return <Reading />
  if (state.status === 'error') {
    const b = blindness(state.error)
    return (
      <Nothing kind={b.admin ? 'admin' : 'failed'} heading="Pool state could not be read">
        {b.why} Nothing on this panel is a claim about whether there is room —
        that is different from there being none.
      </Nothing>
    )
  }
  if (state.status === 'empty') {
    return (
      <Nothing kind="zero" heading="No pools exist">
        The read succeeded and returned nothing. Pools are created at
        provisioning time, so an environment with none has not been fully
        applied.
      </Nothing>
    )
  }

  const cap = state.data
  const byName = new Map(cap.pools.map((p) => [p.name, p]))
  const profiles = Object.entries(cap.runner_profiles).sort(([a], [b]) => a.localeCompare(b))

  if (profiles.length === 0) {
    return (
      <Nothing kind="partial" heading="No runner profile came back">
        {cap.pools.length} pools were read, but the response carried no runner
        profiles — so there is nothing to compute a per-profile ceiling from.
        The pool table itself is intact.
      </Nothing>
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
      <p className="ov-lead">
        A task clears <b>every</b> pool in its list at once, so its ceiling is
        the <b>minimum</b> across them. Raising a pool that is not the binding
        one changes nothing.
      </p>
      <div className="ov-card">
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
      <p className="provenance">
        for tenant {tenant ?? '(the caller)'} — not a platform figure ·{' '}
        {cap.pools.length} pools read
      </p>
    </>
  )
}

/**
 * The binding pool's name, with the CALLER'S OWN TENANT taken out of it.
 *
 * `poolLabel` renders `provider:anthropic:tenant:u-bogdan` as
 * "anthropic · u-bogdan", which does not fit a half-width column and is
 * ellipsised to "anthropic · u-…" -- losing nothing useful, because the panel
 * heading already says every figure on it is for that tenant. Repeating the
 * tenant on every row costs the characters that identify WHICH pool binds,
 * which is the one thing the column exists for. Another tenant's name is kept
 * verbatim, and the raw pool name is always on the title.
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
 *                 failed to paint: the same page is scrupulous about this
 *                 distinction in prose and threw it away in the one place it is
 *                 drawn. A measured zero now gets a visible BASELINE TICK at the
 *                 origin, so "nothing is running" is legible as a reading.
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
  // zero -- which is precisely the failure this panel exists to make visible.
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
        {/* Short on purpose: this sits in a column that is ~200px wide in a
            two-up layout, and a truncated headroom figure is worse than a terse
            one. */}
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
            · none can start
            {/* The count, because more than one pool can refuse at the same
                moment and a panel that implies one sends an operator to raise
                a ceiling that changes nothing. */}
            {h.blockers.length > 1 && ` (${h.blockers.length} pools)`}
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
          <span className="ctl-em">—</span>
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
            picking one -- the detail is on the Capacity board. */}
        {/* "refusing", not "full": one of them may be PAUSED, which is a
            different fact with the opposite remedy, and a single word here
            cannot carry both. */}
        {h.blockers.length > 1
          ? `${h.blockers.length} pools refusing`
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
 * `/v1/stats`, which is an exact Firestore count() per state. The ROWS come
 * from `/v1/tasks?limit=200`, which is the 200 most recently CREATED tasks
 * (store.py:408 orders created_at DESCENDING) -- so an agent that has been
 * running for two days while 200 newer tasks were created is counted and not
 * listed. The caption prints both numbers rather than quietly showing whichever
 * is smaller, because the gap between them is itself information.
 */
function RunningBody({
  tasks,
  stats,
}: {
  tasks: Result<TaskPage>
  stats: Result<Stats>
}) {
  if (tasks.status === 'loading') return <Reading />
  if (tasks.status === 'error') {
    const b = blindness(tasks.error)
    return (
      <Nothing kind={b.admin ? 'admin' : 'failed'} heading="The task list could not be read">
        {b.why} This panel is blind; it is not reporting that nothing is
        running.
      </Nothing>
    )
  }
  if (tasks.status === 'empty') {
    return (
      <Nothing kind="zero" heading="No task exists yet">
        The read succeeded and returned nothing at all — not one task has ever
        been submitted for this tenant.
      </Nothing>
    )
  }

  const page = tasks.data
  const running = page.tasks
    .filter((t) => CONCURRENCY_STATES.has(t.state))
    // Oldest start first: the longest-running agent is the one worth seeing,
    // and it is the one a fixed-height panel would otherwise cut off.
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
      <Nothing kind="zero" heading="Nothing is running">
        No task on the {page.tasks.length} most recently created is in LEASED,
        DISPATCHED, STARTING or RUNNING.{' '}
        {counted === null
          ? 'The exact count could not be read, so this is the page’s answer rather than the platform’s.'
          : counted === 0
            ? 'The state counts agree: zero.'
            : `The state counts say ${counted} — those agents were created before this page begins.`}
      </Nothing>
    )
  }

  const shown = running.slice(0, RUNNING_ROWS)

  return (
    <div className="ctl-table">
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
        <caption>
          {running.length} on the {page.tasks.length} most recently created
          tasks
          {counted !== null && counted !== running.length && (
            <>
              {' '}
              · <code>/v1/stats</code> counts <b>{counted}</b> in these states
            </>
          )}
          {running.length > shown.length && <> · showing the {shown.length} longest-running</>}
        </caption>
      </table>
    </div>
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
        <a className="ov-link" href={`#agents/task/${encodeURIComponent(task.id)}`}>
          {task.runner_profile}
        </a>
        {/* The id, and only the id. `whyAgent` used to be appended here and
            could never print: it answers for PARKED, READY, FAILED and
            CANCELLED, and every row in this table is LEASED, DISPATCHED,
            STARTING or RUNNING by construction. A call that always returns ''
            reads as a "why" column that is mysteriously always empty. */}
        <span className="ctl-sub">{task.id}</span>
      </th>
      <td>
        {/* The word is mandatory; the dot is decoration. Roughly 8% of male
            viewers cannot separate this panel's amber from its red. */}
        <span className={`ctl-chip ${chipTone(task.state)}`}>
          <i aria-hidden />
          {stateGlyph(task.state)} {task.state}
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
 * THE SCOPE IS THE PANEL. `cost_usd` and the four token counts live on the
 * ATTEMPT and no route aggregates them, so a total can only be assembled one
 * request per task -- which makes the sample size a request count and the
 * figure a sample rather than a bill. Every sentence here exists to stop this
 * number being read as "what this tenant has spent".
 *
 * A NULL IS NOT A ZERO, and it is the difference between a mock run that
 * genuinely cost nothing and a result whose usage failed to parse.
 * `record_usage` in agent_worker/control.py omits a key the runner did not
 * report and says so in its own docstring; this panel counts how many attempts
 * carried no figure and prints that count rather than folding them in as
 * zeros.
 */
function SpendBody({ state, tasks }: { state: Result<SpendRollup>; tasks: Result<TaskPage> }) {
  if (state.status === 'loading') {
    // A READ IS IN FLIGHT, and now that is the only way to reach this line:
    // either the task page has not landed (the fan-out starts when it does) or
    // the twelve attempt reads are out. `useSpend` mirrors a task read that
    // landed `empty` or failed into this Result, so a tenant whose fan-out
    // will never start does not watch a skeleton pulse for it.
    return <Reading />
  }
  if (state.status === 'error') {
    // TWO FAILURES, AND WHAT THE READER DOES NEXT DIFFERS. This rollup is
    // built ON the task page: when the task read is the one that failed, no
    // attempt read was made at all, nothing is known about the attempt route,
    // and the thing to fix is the task list. Saying "no attempt read
    // completed" there would send someone after a route that was never called.
    if (tasks.status === 'error') {
      const b = blindness(tasks.error)
      return (
        <Nothing
          kind={b.admin ? 'admin' : 'failed'}
          heading="Spend could not be assembled"
        >
          It is summed from the attempts of the most recent tasks, and the task
          list itself could not be read. {b.why} No attempt read was made, so
          nothing here is a statement about spend.
        </Nothing>
      )
    }
    return (
      <Nothing kind="failed" heading="No attempt read completed">
        {errorHeading(state.error)} — {state.error.message} No figure is shown,
        because a partial sum here would be indistinguishable from a small bill.
      </Nothing>
    )
  }
  if (state.status === 'empty') {
    // TWO ZEROS, AND THEY ARE NOT THE SAME ZERO. "Every task on the page has
    // no attempts" describes tasks that exist; on a tenant with no tasks at
    // all it would describe a page that is not there. The Running panel draws
    // "No task exists yet" from this same read and the two must not disagree.
    if (tasks.status === 'empty') {
      return (
        <Nothing kind="zero" heading="No task exists yet">
          The task read succeeded and returned nothing at all — not one task has
          ever been submitted for this tenant, so there are no attempts to sum
          and no request is outstanding. This is a real zero.
        </Nothing>
      )
    }
    return (
      <Nothing kind="zero" heading="No task has ever run">
        Every task on the page has an attempt count of zero, so there are no
        attempts to sum. The read succeeded — this is a real zero.
      </Nothing>
    )
  }

  const s = state.data
  const unmeasured = s.attempts - s.attemptsWithCost

  return (
    <>
      <dl className="ov-kv">
        <div>
          <dt>Cost</dt>
          <dd className="ov-figure">
            {s.costUsd === null ? <span className="ctl-em">not reported</span> : money(s.costUsd)}
          </dd>
        </div>
        <div>
          <dt>Input</dt>
          <dd>{tokens(s.inputTokens)}</dd>
        </div>
        <div>
          <dt>Output</dt>
          <dd>{tokens(s.outputTokens)}</dd>
        </div>
        <div>
          <dt>Cache read</dt>
          <dd>{tokens(s.cacheReadTokens)}</dd>
        </div>
        <div>
          <dt>Cache write</dt>
          <dd>{tokens(s.cacheCreationTokens)}</dd>
        </div>
      </dl>

      {s.failedReads > 0 && (
        <p className="warn-text">
          {s.failedReads} of {s.tasksSampled} attempt reads failed, so their
          spend is in none of these figures.{' '}
          {/* ONE MESSAGE IS ONE FAILURE'S. The rollup keeps only the first
              error it saw (api.ts, loadSpend), so attaching that sentence to
              all N of them would present a 404, a 429 and a 500 as three
              instances of whichever resolved first -- and send the operator
              after the wrong cause twice. It is attributed to the one read it
              came from, and the others are named as unexplained rather than
              explained wrongly. */}
          {s.failedReads === 1 ? (
            <>It failed with: {s.failedDetail ?? 'no message was recorded'}</>
          ) : (
            <>
              One of them failed with: {s.failedDetail ?? 'no message was recorded'} — the
              other {s.failedReads - 1} may have failed for other reasons, which this
              rollup does not carry. Open an agent to see its own attempts.
            </>
          )}
        </p>
      )}

      <p className="provenance">
        {s.attempts} attempts across the {s.tasksSampled} most recently created
        tasks that have run
        {s.tasksWithAttempts > s.tasksSampled && (
          <> — {s.tasksWithAttempts} such tasks are on the page</>
        )}
        {s.from && s.to && (
          <>
            {' '}
            · {new Date(s.from).toLocaleString()} → {new Date(s.to).toLocaleString()}
          </>
        )}
      </p>
      {/* THE AGE OF THE SUM, not of the tasks it covers. The line above is the
          span of `created_at` across the sample, which moves with the sample
          and not with the read; this is when the fan-out actually ran. They
          differ by however long ago someone last pressed refresh, because this
          is the one panel on the screen that does not re-poll. */}
      <p className="provenance">
        summed {timeAgo(state.fetchedAt)} · this figure does not re-poll — it
        moves only when you press refresh, unlike the capacity, task, lease,
        provider and account reads, which re-run every 20 seconds
      </p>
      <p className="provenance">
        {/* Both halves matter. The first says the sum is incomplete; the second
            says the whole category does not exist at all. */}
        {unmeasured > 0
          ? `${unmeasured} attempt${unmeasured === 1 ? ' carries' : 's carry'} no cost figure and ${unmeasured === 1 ? 'is' : 'are'} counted as unmeasured, not as zero`
          : 'every attempt in the sample carried a cost figure'}{' '}
        · GCP infrastructure cost is not recorded anywhere on this platform
      </p>
    </>
  )
}

/** A number of tokens, or an em dash. Never 0 for an absent measurement. */
function tokens(v: number | null): ReactNode {
  if (v === null) return <span className="ctl-em">—</span>
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
 * too stale to trust is excluded from the figure and counted in the line
 * underneath -- excluded rather than treated as full, because "we do not know"
 * is not "there is room".
 *
 * AND A WINDOW THAT HAS ALREADY RESET IS ONE OF THE THINGS WE DO NOT KNOW.
 * `bindingWindow` scores a `reset: true` window as FULL -- deliberately, so a
 * window that refilled cannot be the binding one while another still has room
 * -- which means it comes back as binding precisely when every window has
 * reset, or on a tie. Its `utilization` then describes the window BEFORE the
 * reset. Reading it raw drew "10 % left" in the red under-15% tone for an
 * account whose five-hour window had just cleared, while the Accounts screen
 * said "cleared" for the same account and the same window. `readingOf` is the
 * one place that distinction is encoded, so it is asked rather than
 * re-derived: only a `live` reading is a figure.
 */
/**
 * EXPORTED so the arithmetic below can be asserted.
 *
 * A verifier found on 2026-09-22 that replacing
 * `accounts.length - unread - projected - notServing` with plain
 * `accounts.length` left the entire suite green. The tile would then have read
 * "best of 3 usable accounts" while its own foot line named two of the three
 * as having no reading -- a tile contradicting itself, which is the exact
 * defect class this function was rewritten to remove.
 *
 * Nothing could catch it because nothing could call this. That is the whole
 * lesson: a derivation a test cannot import is a derivation nothing guards.
 */
export function accountHeadroom(state: Result<AccountsPage>): {
  pct: number | null
  sub: string
  foot: string | undefined
  reading: boolean
  absent: string | null
} {
  if (state.status === 'loading') {
    return {
      pct: null,
      sub: 'reading the subscription pool',
      foot: undefined,
      reading: true,
      absent: null,
    }
  }
  if (state.status === 'error') {
    return {
      pct: null,
      sub: 'the subscription pool could not be read',
      foot: undefined,
      reading: false,
      absent: null,
    }
  }
  if (state.status === 'empty') {
    return {
      pct: null,
      sub: 'no account is registered, so the subscription pool supplies nothing',
      foot: 'read succeeded · a real zero',
      reading: false,
      absent: 'no accounts',
    }
  }

  const accounts = state.data.accounts
  // THE SCOPE IS PART OF THE FIGURE. `/v1/accounts` returns the accounts this
  // TENANT owns or has been lent, never the platform's -- so `accounts.length`
  // is not the number registered, and a sentence here that says "registered"
  // makes a platform-wide claim out of a tenant-wide list. On 2026-09-22 the
  // tile read "every registered account has a current reading" for tenant
  // `eng`, which owned the one account that had one, while two accounts in
  // `u-bogdan` had never been polled at all.
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
  // nothing has been spending it. Counting one made this tile answer "can
  // anything run?" with the headroom of the one account that cannot.
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
    // The two ways a reading can be missing, told apart the same way the pool
    // panel's rows tell them apart: nobody has ever polled this account, or
    // the poll landed and reported no window. Both are unknown headroom; NEITHER
    // is 100%. `usagepoll.py` fails safe by recording nothing when it cannot
    // read an account's token, so this is the shape the live platform produces.
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
    }
  }

  return {
    pct: best.pct,
    // THE AGE IS PART OF THE FIGURE, and it was missing. Every other tile on
    // this row carries the age of the read behind it; this one showed a
    // percentage with nothing saying whether it was measured a minute or four
    // days ago. A reading from four days ago is not a current one.
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
  if (state.status === 'loading') return <Reading />
  if (state.status === 'error') {
    const b = blindness(state.error)
    return (
      <Nothing kind={b.admin ? 'admin' : 'failed'} heading="The account pool could not be read">
        {b.why} This says nothing about whether the accounts have room.
      </Nothing>
    )
  }
  if (state.status === 'empty') {
    return (
      <Nothing kind="zero" heading="No account is registered">
        The read succeeded and returned nothing. Agents on a subscription
        profile have no account to run against until one is added.
      </Nothing>
    )
  }

  const total = state.data.accounts.length
  const accounts = [...state.data.accounts]
    .map((a) => ({ a, w: bindingWindow(a) }))
    // Worst first, and "needs a person" IS the worst. Sorting on room alone
    // sank the one account that had stopped working, because a REAUTH_REQUIRED
    // account's windows have usually reset and it therefore looks the emptiest.
    .sort((x, y) => rank(x.a, x.w) - rank(y.a, y.w))
    .slice(0, ACCOUNT_ROWS)

  return (
    <>
      <div className="ov-card">
        {accounts.map(({ a, w }) => {
          // FIVE KINDS OF READING, AND ONLY ONE OF THEM IS A CURRENT FIGURE.
          // `readingOf` is the same function the Accounts screen uses, so the
          // two screens cannot disagree about the same account: an account
          // whose binding window has reset reads "~90% · cleared" here and
          // "cleared" there, rather than a confident red 90% on one screen and
          // "cleared" on the other.
          // `bindingWindow` returns null when no window carried a usable
          // figure, and the two reasons for that are different sentences:
          // nobody has ever polled this account, or the poll landed and
          // reported no window. `readingOf` makes the same split, so the
          // no-window case is spelled the same way here.
          const r: AccountReading =
            a.observed_at === null
              ? { kind: 'never' }
              : w === null
                ? { kind: 'absent' }
                : readingOf(a, w.key)
          // `null`, never 0: the width of a bar nobody measured is not zero,
          // it does not exist. Narrowing on `pct !== null` below is also what
          // keeps `r.pct` reachable without a cast.
          const pct =
            r.kind === 'live' || r.kind === 'stale' || r.kind === 'reset' ? r.pct : null
          const projected = isProjected(r)
          const windowName = w?.key.replace(/_/g, '-') ?? null
          return (
            <div className="ctl-util" key={a.account_id}>
              <span className="ctl-util-name" title={a.account_id}>
                <b>{a.label}</b> · {a.assigned} assigned
              </span>
              {/* Never observed, or no reading for the binding window: hatched
                  with no fill. A plain empty track here would claim 0% used,
                  which for an account nobody has polled is a number nobody
                  measured. A PROJECTED reading does get a bar -- the figure is
                  real -- but a grey one, never the red or amber that says a
                  ceiling is being approached now. */}
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
                    {/* The same mark `cs status` and the Accounts screen use
                        for a figure that is real but not current. */}
                    {projected && <span className="ov-tilde">~</span>}
                    {Math.round(pct)}%
                  </>
                ) : (
                  <span className="ctl-em">—</span>
                )}
              </span>
              {/* A CURRENT READING CARRIES ITS AGE TOO. `stale` and `cleared`
                  have said how old they are for as long as this panel has
                  existed; a `live` reading printed only the window name, so
                  the one row on the panel with a confident figure was the one
                  row that did not say when it was measured. Thirty minutes is
                  the staleness window, so "live" spans a range wide enough to
                  matter. */}
              <span className="ctl-util-by">
                {needsAHuman(a)
                  ? 'sign in again'
                  : // Before every reading word, because it outranks all of
                    // them: whatever this row's figure says, the pool will not
                    // hand this account to this tenant while the report stands.
                    unreadableFor(a, state.data.tenant_id)
                    ? 'pool is skipping it here'
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
      <p className="provenance">
        {countOf(total, 'account')}
        {total > accounts.length && (
          <> · showing the {accounts.length} that most need looking at</>
        )}{' '}
        · utilisation is of the BINDING window, never an average of the two · ~
        marks a figure that is real but not current
        {state.data.tenant_id ? ` · scope ${state.data.tenant_id}` : ' · every tenant'}
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
// `apps/swarm-ui/test/checks.test.mjs` drives it directly -- a stalled
// workflow, parked steps, and the healthy case that has to stay quiet. What is
// left here draws what it returns.

/** How many problems are open by default. The rest are one click away, here. */
const ATTENTION_ROWS = 4

/**
 * The panel.
 *
 * Problems first, worst first. Then, ALWAYS, the line that says what could not
 * be checked -- it is not a footnote, it is the reason a short list is or is
 * not good news.
 */
function AttentionBody({ checks }: { checks: Check[] }) {
  const problems = checks
    .flatMap((c) => (c.status === 'found' ? c.problems : []))
    .sort((a, b) => (a.severity === b.severity ? b.n - a.n : a.severity === 'bad' ? -1 : 1))
  const blind = checks.filter((c): c is Extract<Check, { status: 'blind' }> => c.status === 'blind')
  const reading = checks.filter((c) => c.status === 'reading')
  const clear = checks.filter((c): c is Extract<Check, { status: 'clear' }> => c.status === 'clear')

  return (
    <>
      {problems.length === 0 &&
        reading.length === 0 &&
        (blind.length === 0 ? (
          <div className="ctl-empty ov-tight">
            <h3>Nothing is wrong that this platform records</h3>
            <p>
              All {clear.length} checks ran and all {clear.length} came back
              clear. This is an all-clear from successful reads, not from
              silence.
            </p>
          </div>
        ) : (
          // NOT an all-clear, and it must not be drawn as one. Some of what
          // could be wrong was never looked at.
          <div
            className={`ctl-empty ov-tight ${blind.every((c) => c.admin) ? 'is-admin' : 'is-partial'}`}
          >
            <h3>
              Nothing wrong in the {clear.length} check
              {clear.length === 1 ? '' : 's'} that ran
            </h3>
            <p>
              {blind.length} of {clear.length + blind.length} could not run, so
              this is not an all-clear — it is a partial one, and what it did
              not look at is listed below.
            </p>
          </div>
        ))}

      {problems.length > 0 && (
        <ul className="ov-list">
          {problems.slice(0, ATTENTION_ROWS).map((p, i) => (
            <ProblemRow key={`${p.headline}-${i}`} problem={p} />
          ))}
        </ul>
      )}
      {/* Cut, never dropped — and the rest open HERE. This used to link to a
          trouble board, which no longer exists and should not: there is no
          problem section at any level, so the overflow cannot be somebody
          else's problem. A <details> keeps the landing screen short without
          the fifth problem being a link to nowhere, and it needs no state of
          its own. */}
      {problems.length > ATTENTION_ROWS && (
        <details className="ov-more">
          <summary>
            {problems.length - ATTENTION_ROWS} more, worst first
          </summary>
          <ul className="ov-list">
            {problems.slice(ATTENTION_ROWS).map((p, i) => (
              <ProblemRow key={`${p.headline}-more-${i}`} problem={p} />
            ))}
          </ul>
        </details>
      )}

      {/* NEVER OPTIONAL. A short problem list over four blind checks and a
          short problem list over six clear ones look identical, and only one of
          them is good news. */}
      <p className="provenance ov-checks">
        {clear.length > 0 && (
          <>
            clear: {clear.map((c) => c.label.toLowerCase()).join(', ')}
            {' — '}
            {clear.map((c) => c.note).join(' · ')}
          </>
        )}
      </p>
      {reading.length > 0 && (
        <p className="provenance">
          still reading: {reading.map((c) => c.label.toLowerCase()).join(', ')}
        </p>
      )}
      {blind.length > 0 && (
        <p className={blind.every((c) => c.admin) ? 'provenance ov-info' : 'warn-text'}>
          {blind.length} check{blind.length === 1 ? '' : 's'} could not run, so
          this list is incomplete:{' '}
          {blind.map((c) => `${c.label.toLowerCase()} — ${c.why}`).join(' · ')}
        </p>
      )}
    </>
  )
}

/** One problem, with the link it goes out by. */
function ProblemRow({ problem: p }: { problem: Problem }) {
  return (
    <li className="ov-item">
      <span className={`ctl-chip ${p.severity === 'bad' ? 'is-bad' : 'is-warn'}`}>
        <i aria-hidden />
        {p.severity === 'bad' ? 'act' : 'watch'}
      </span>
      <p>
        <b>{p.headline}</b> — {p.detail}
      </p>
      <a className="ov-link" href={p.href}>
        {p.linkLabel ?? 'open'} →
      </a>
    </li>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

/**
 * WHY THIS IS HERE AND NOT IN styles.css.
 *
 * styles.css belongs to the lane that built the `ctl-` primitives, and a
 * second author appending to it during the same change is how two blocks of
 * CSS end up disagreeing about the same selector. Everything this screen needs
 * that the primitives do not already provide is below, every selector is
 * `ov-`-prefixed so it can reach nothing outside this file, and it uses only
 * tokens that already exist -- no new colour, no new spacing step. It should
 * be folded into the primitive sheet the next time that file is opened; until
 * then it is scoped and it is here.
 *
 * THE LAYOUT USED TO BE TRACK-COUNT-AGNOSTIC and is not any more, which is a
 * deliberate trade and worth stating here rather than only at the rule. An
 * auto-fitting grid needs no breakpoints to maintain, and in exchange nothing
 * -- not the stylesheet, not this file -- can know how many tracks it produced.
 * That is exactly what the rules below need in order to stop the page ending
 * with a blank right column under a column that is still going. Three stated
 * breakpoints cost three lines; a page that wastes a third of a 1600px screen
 * costs it every time anyone opens the product.
 */
const OVERVIEW_CSS = `
.ov-topline {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--ctl-s3);
  flex-wrap: wrap;
  margin-bottom: var(--ctl-s3);
}
.ov-topline .head { margin-bottom: 0; }
.ov-topline .sub { margin: 0; }

/* THE COLUMN COUNT IS EXPLICIT, and it used to be fitted automatically.
   The old declaration was correct about widths
   and unknowable about COUNT, and the count is what the rules below need: the
   only way to stop a grid ending with a blank right column is to know which
   panel lands in the last row, and an nth-child selector cannot ask a browser
   how many tracks were fitted. One column, then two, then three, each stated. */
.ov-cols {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--ctl-s3);
  align-items: start;
}
@media (min-width: 720px) {
  .ov-cols { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (min-width: 1400px) {
  .ov-cols { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

/* The alarm spans every track when it has something to say. In CSS rather than
   an inline style so the parity rules below can account for the two or three
   tracks it consumes. */
.ov-cols.has-alarm > .ov-alarm { grid-column: 1 / -1; }

/* NO PAGE MAY END WITH A BLANK RIGHT COLUMN WHILE THE LEFT CONTINUES.
   This grid holds five panels. In two tracks with no alarm that is 2+2+1, and
   the odd one out leaves half a row of empty page under a column that is still
   going -- on a console whose brief is "graphs and diagrams everywhere
   possible", empty space is the most expensive thing on screen.

   The last panel therefore widens to fill its row. WHICH panel that is depends
   on the track count and on whether the alarm consumed a whole row first, so
   every combination is stated. With T tracks and N panels the last row is
   already full when N (no alarm) or N-1 (alarm) divides by T; otherwise the
   last panel widens by the shortfall. Written as parity selectors so a sixth
   panel added later is handled without anyone remembering this comment. */
@media (min-width: 720px) and (max-width: 1399px) {
  .ov-cols:not(.has-alarm) > section:last-child:nth-child(odd) { grid-column: 1 / -1; }
  .ov-cols.has-alarm > section:last-child:nth-child(even) { grid-column: 1 / -1; }
}
@media (min-width: 1400px) {
  .ov-cols:not(.has-alarm) > section:last-child:nth-child(3n + 1) { grid-column: 1 / -1; }
  .ov-cols:not(.has-alarm) > section:last-child:nth-child(3n + 2) { grid-column: span 2; }
  .ov-cols.has-alarm > section:last-child:nth-child(3n + 2) { grid-column: 1 / -1; }
  .ov-cols.has-alarm > section:last-child:nth-child(3n) { grid-column: span 2; }
}

/* .section carries a 28px bottom margin for a stacked page; inside a grid that
   is dead space between rows that the gap already provides. */
.ov-cols > section { margin-bottom: 0; }

.ov-h {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--ctl-s3);
  margin: 0 0 var(--ctl-s2);
}
.ov-h > h2 {
  display: flex;
  align-items: baseline;
  gap: var(--ctl-s2);
  margin: 0;
  /* §B4.1: the uppercase small-caps treatment that came OFF .section > h2
     lands here, on a panel LABEL, which is the level it belongs to. --t-meta
     is the step that owns 600/uppercase/tracking. (No backticks in this
     comment: it lives inside a JS template literal, where one ends the CSS.) */
  font: 600 var(--t-meta)/var(--lh-meta) var(--font);
  text-transform: uppercase;
  letter-spacing: .07em;
  color: var(--text-faint);
}
.ov-count {
  padding: 0 7px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--bad) 16%, transparent);
  color: var(--bad);
  font: 600 var(--t-meta)/var(--lh-meta) var(--mono);
  letter-spacing: 0;
}

.ov-link {
  color: var(--info);
  font: var(--t-micro)/var(--lh-micro) var(--mono);
  text-decoration: none;
  border-bottom: 1px solid transparent;
  white-space: nowrap;
}
.ov-link:hover { border-bottom-color: currentColor; }
.ov-link:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; border-radius: 3px; }

/* The whole tile is the doorway. The answer to every figure on the strip is on
   another screen, and making the figure itself the link removes a step. */
a.ov-tile {
  display: block;
  text-decoration: none;
  color: inherit;
  transition: border-color .15s ease, transform .15s ease;
}
a.ov-tile:hover { border-color: color-mix(in srgb, var(--info) 55%, var(--line)); }
a.ov-tile:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; }
@media (prefers-reduced-motion: no-preference) {
  a.ov-tile:hover { transform: translateY(-1px); }
}

.ov-card {
  padding: var(--ctl-s2) var(--ctl-s3);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
}
/* The utilisation primitive is sized for a full-width screen; in a half-width
   panel its four fixed columns leave the name nothing. Narrowed here only --
   scoped to this file's cards, so no other screen's bars move.

   ABOVE 640px ONLY. The primitive deliberately drops the track and the "set
   by" column on a phone and keeps the name and the figure; an unscoped
   override here would out-specify that and put four columns back into 358px.
   It is the primitive's decision to make, not this screen's. */
@media (min-width: 641px) {
  .ov-card .ctl-util {
    grid-template-columns: minmax(0, 1fr) minmax(56px, 96px) 54px 104px;
    gap: var(--ctl-s2);
    padding: 4px 0;
  }
}

/* NOT --t-lead. A screen gets one lead paragraph; Overview draws one of these
   per PANEL, and five leads on a page is no lead at all. --t-body. */
.ov-lead {
  margin: 0 0 var(--ctl-s2);
  font-size: var(--t-body);
  line-height: var(--lh-body);
  color: var(--text-dim);
}
.ov-lead b { color: var(--text); font-weight: 600; }

/* "nothing more can start" is the one phrase on this screen that changes what
   someone does next, so it is not left as ordinary grey text. */
.ov-stop { color: var(--bad); font-weight: 500; }

.ov-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--ctl-s2); }
.ov-item {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr) max-content;
  gap: var(--ctl-s3);
  align-items: baseline;
}
.ov-item > p { margin: 0; font-size: var(--t-body); line-height: var(--lh-body); color: var(--text-dim); }
.ov-item > p > b { color: var(--text); font-weight: 600; }

.ov-kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(88px, 1fr)); gap: var(--ctl-s2); margin: 0; }
.ov-kv > div { min-width: 0; }
.ov-kv dt { font: 600 var(--t-meta)/var(--lh-meta) var(--mono); letter-spacing: .05em; text-transform: uppercase; color: var(--text-faint); }
.ov-kv dd { margin: 2px 0 0; font: var(--t-body)/var(--lh-body) var(--mono); font-variant-numeric: tabular-nums; color: var(--text); }
/* The one figure in the pair gets --t-figure: this IS the number the panel is
   for, and the 19px it used to take was a step nothing else in the sheet had. */
.ov-kv dd.ov-figure { font-size: var(--t-figure); line-height: var(--lh-figure); font-weight: 600; }

/* An empty state INSIDE a panel, rather than as the page. The panel's own
   heading has already said what this is about, so the padding comes down. */
.ov-tight { padding: var(--ctl-s3); }
.ov-skel { height: 76px; border-radius: var(--radius); }

/* READING. The third absence, and it must not look like either of the other
   two: .ctl-metric.is-absent is dashed and grey and says the platform has no
   such figure, .ctl-metric.is-unread is dashed and amber and says the read
   failed. This one is neither — the request is still out — so the tile keeps
   its ordinary solid border and the value slot holds a bar that is visibly
   still moving. */
.ov-tile.ov-reading .ctl-metric-value {
  display: flex;
  align-items: center;
  gap: var(--ctl-s2);
  /* A word, not a number, so --t-body -- the same rule .ctl-metric.is-absent
     follows. Nothing but a measured figure takes --t-figure. */
  font-size: var(--t-body);
  font-weight: 500;
  color: var(--text-faint);
  letter-spacing: 0;
}
.ov-bar { flex: 0 0 auto; width: 46px; height: 12px; }
.ov-reading-word { font: var(--t-micro)/var(--lh-micro) var(--mono); }

/* A figure that is REAL but not CURRENT: its window reset, or the poll is past
   the staleness window. Grey, never the red or amber that says a ceiling is
   being approached now, and the tilde beside it is the same mark that cs
   status and the Accounts screen use for the same two cases. */
.ov-card .ctl-util-fill.ov-projected { background: var(--ctl-absent); }
.ov-tilde { color: var(--ctl-absent); margin-right: 1px; }

/* The problems that did not fit. A disclosure rather than a link out, because
   there is no board to link to. */
.ov-more { margin-top: var(--ctl-s2); }
.ov-more > summary {
  cursor: pointer;
  /* --t-meta, not --t-micro: this is a CONTROL, and the micro step is for
     things you read, not things you click. */
  font: var(--t-meta)/var(--lh-meta) var(--mono);
  color: var(--info);
  list-style: none;
}
.ov-more > summary::-webkit-details-marker { display: none; }
.ov-more > summary::before { content: '▸ '; }
.ov-more[open] > summary::before { content: '▾ '; }
.ov-more > summary:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; border-radius: 3px; }
.ov-more > .ov-list { margin-top: var(--ctl-s2); }

.ov-broken { color: var(--bad); }
/* An admin gate is information, not breakage -- so the line that reports only
   admin gates is blue and calm, never the amber used for a real failure. */
.ov-info { color: var(--info); }
.ov-checks:empty { display: none; }
`
