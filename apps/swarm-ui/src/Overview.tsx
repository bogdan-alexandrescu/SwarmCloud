import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  loadAccountPool,
  loadCapacity,
  loadLeases,
  loadProviders,
  loadSpend,
  loadStats,
  loadTasks,
  type SpendRollup,
} from './api'
import { errorHeading, isPaused, type ApiError, type Result } from './fetch'
import { timeAgo } from './Shell'
import {
  CONCURRENCY_STATES,
  bindingWindow,
  clearsIn,
  elapsed,
  headroomFor,
  leaseLiveliness,
  needsAHuman,
  overCeiling,
  poolLabel,
  providerTone,
  stateGlyph,
  stateTone,
  whyAgent,
  type AccountsPage,
  type Capacity,
  type LeasePage,
  type Pool,
  type ProvidersPage,
  type Stats,
  type Task,
  type TaskPage,
  type TaskState,
} from './types'

/**
 * OVERVIEW -> NOW. The landing screen, and the one-stop shop.
 *
 * It answers five questions on one laptop screen, and every one of them ends
 * in a link to the screen that goes deeper. Nothing here is a dead end and
 * nothing here is the last word: this screen's job is to tell you which of the
 * other four sections to open, in the first three seconds.
 *
 *   1. CAPACITY -- used against available, and WHICH pool binds each runner
 *      profile. The conjunction is the whole trap: a task must clear every
 *      pool in its list at the same moment, so its ceiling is the MINIMUM
 *      across them. Raising the pool that is not binding changes nothing, and
 *      an operator who cannot see which one binds raises the wrong one.
 *   2. WHAT IS RUNNING, with runtime so far.
 *   3. WHAT IS WRONG, derived from real state -- never from an alert model,
 *      because this platform has none.
 *   4. SPEND, in tokens and dollars of token cost, with the scope named.
 *   5. THE SUBSCRIPTION POOL's headroom, which is the ceiling that binds
 *      first in practice and used to be buried three clicks down.
 *
 * SIX READS, INDEPENDENTLY. One failing must not blank the page and must not
 * leave the page looking complete -- so each panel owns its own state and the
 * header counts what did not arrive. This is the shape Trouble already uses;
 * `Screen` is deliberately not used here because it has exactly one load and
 * one failure, and this screen has six of each.
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
  // bucket on figures that change slowly. Both carry their own read age in the
  // tile foot so a stale one is visible rather than merely old.
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
  const stats = useRead(loadStats, heavy)

  const spend = useSpend(dataOf(tasks), heavy)

  // Runtimes tick on their own rather than only when a read lands. An agent
  // that has been running for four minutes should say so a second later.
  const now = useNow()

  // Typed as `Result<unknown>` because this list only ever asks about a read's
  // OUTCOME, never its payload. Leaving it to inference makes it a union of six
  // differently-parameterised Results that matches no single `Result<T>`.
  const reads: Result<unknown>[] = [capacity, tasks, leases, providers, accounts, stats, spend]

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

  // An admin gate is not a failure and must not be counted as one. The lease
  // read is the only admin route on this screen and a non-admin legitimately
  // cannot make it.
  const broken = reads.filter(
    (r) => r.status === 'error' && r.error.kind !== 'admin_required',
  ).length

  const checks = useMemo(
    () => deriveChecks({ capacity, tasks, leases, providers, accounts, stats }),
    [capacity, tasks, leases, providers, accounts, stats],
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
          <h1>Now</h1>
          <span className="env">dev</span>
        </div>
        <p className="sub">
          <Scope tasks={tasks} /> ·{' '}
          {broken > 0 ? (
            <strong className="ov-broken">
              {broken} of {reads.length} reads failed
            </strong>
          ) : (
            <>all {reads.length} reads landed</>
          )}{' '}
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

      <div className="ov-cols">
        {/* THE ALARM TAKES THE TOP WHEN IT IS RINGING. With something to say it
            spans both columns and is the first panel under the tiles; when
            every check came back clear it collapses into one column and gets
            out of the way. A control plane whose problem list is the same size
            whether or not there are problems teaches you to stop looking. */}
        <section className="section panel" style={found > 0 ? { gridColumn: '1 / -1' } : undefined}>
          <PanelHead
            title="Needs attention"
            count={found}
            href="#overview/attention"
            cta="full trouble board"
          />
          <AttentionBody checks={checks} />
        </section>

        <section className="section panel">
          <PanelHead
            title="Capacity, by what binds it"
            href="#capacity/profiles"
            cta="all pools"
          />
          <CapacityBody state={capacity} />
        </section>

        <section className="section panel">
          <PanelHead title="Running now" href="#agents/running" cta="all agents" />
          <RunningBody tasks={tasks} stats={stats} now={now} />
        </section>

        <section className="section panel">
          <PanelHead title="Spend" href="#activity/timeline" cta="activity" />
          <SpendBody state={spend} tasks={tasks} />
        </section>

        <section className="section panel">
          <PanelHead title="Subscription pool" href="#capacity/accounts" cta="all accounts" />
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
 * So the page is read through a ref, and the effect fires on exactly two
 * things: the first page arriving, and a person pressing refresh.
 */
function useSpend(page: TaskPage | null, heavy: number): Result<SpendRollup> {
  const [state, setState] = useState<Result<SpendRollup>>({
    status: 'loading',
    since: Date.now(),
  })
  const latest = useRef<TaskPage | null>(null)
  latest.current = page
  const ready = page !== null

  useEffect(() => {
    const p = latest.current
    if (!p) return
    let live = true
    loadSpend(p).then((r) => {
      if (live) setState(r)
    })
    return () => {
      live = false
    }
  }, [heavy, ready])

  return state
}

/** A clock that ticks, so "running for 4m 12s" is true a second later. */
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
 * Why a panel has nothing, split by CAUSE.
 *
 * `admin` is carried separately rather than folded into the sentence because
 * the two render completely differently -- blue and calm versus amber and
 * wrong -- and a caller that has to read the copy to tell them apart will
 * eventually get it backwards.
 */
function blindness(error: ApiError): { why: string; admin: boolean } {
  if (error.kind === 'admin_required') {
    return { why: 'Admin only. Nothing failed.', admin: true }
  }
  return { why: `${errorHeading(error)} — ${error.message}`, admin: false }
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

// ---------------------------------------------------------------------------
// The metric strip
// ---------------------------------------------------------------------------

/**
 * Five tiles, five doorways. Each is an anchor, because the answer to every
 * one of these numbers is on another screen and making the number itself the
 * link removes a step.
 *
 * Every tile has three renderings and they must not converge: a figure, "not
 * recorded" (the platform does not have it) and "unreadable" (the platform may
 * well have it, we did not get it). The primitive encodes the difference --
 * `.is-absent` versus `.is-unread` -- so no tile has to remember.
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
        unread={stats.status === 'error' ? errorHeading(stats.error) : null}
        tone={inFlight !== null && inFlight > 0 ? 'good' : undefined}
      />

      <Tile
        href="#capacity/pools"
        label="Units held"
        value={global ? global.active : null}
        unit={global ? `of ${global.effective_limit}` : undefined}
        // "units", never "agents": admission increments by the resource
        // class's weight, so 8 may be two large agents or eight standard ones.
        sub="weighted units on the platform-wide global pool — not a count of agents"
        foot={footFor(capacity, 'read')}
        unread={capacity.status === 'error' ? errorHeading(capacity.error) : null}
        absent={
          capacity.status !== 'error' && cap !== null && global === null
            ? 'no global pool'
            : null
        }
        tone={global && overCeiling(global) ? 'alert' : undefined}
      />

      <Tile
        href="#overview/attention"
        label="Needs attention"
        value={ran === 0 ? null : found}
        unit={found === 1 ? 'thing' : 'things'}
        sub={
          found > 0
            ? 'derived from lease, quota, account, pool and failure state'
            : 'nothing in lease, quota, account, pool or failure state'
        }
        // THE FOOT IS THE HONEST PART. A "0" with two checks blind is a
        // reassurance nobody earned, so the count of checks that could not run
        // sits under the figure every time rather than only when it is
        // convenient.
        foot={
          ran === 0
            ? 'no check could run'
            : `${ran} of ${checks.length} checks ran${blind > 0 ? ` · ${blind} could not` : ''}`
        }
        unread={ran === 0 ? 'no check ran' : null}
        tone={found > 0 ? 'alert' : blind === 0 ? 'good' : undefined}
      />

      <Tile
        href="#activity/timeline"
        label="Token spend"
        value={sp && sp.costUsd !== null ? money(sp.costUsd) : null}
        sub={
          sp
            ? `${sp.attemptsWithCost} of ${sp.attempts} attempts, over the ${sp.tasksSampled} most recent tasks that ran`
            : 'summed from the attempts of the most recent tasks that ran'
        }
        // Said on the tile, not only in the panel: this is the number someone
        // screenshots, and "spend" with no qualifier reads as the bill.
        foot="token cost only · infra cost is not recorded"
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
        href="#capacity/accounts"
        label="Subscription headroom"
        value={pool.pct === null ? null : Math.round(pool.pct)}
        unit={pool.pct === null ? undefined : '% left'}
        sub={pool.sub}
        foot={pool.foot}
        unread={accounts.status === 'error' ? errorHeading(accounts.error) : null}
        absent={pool.absent}
        tone={pool.pct !== null && pool.pct < 15 ? 'alert' : undefined}
      />
    </div>
  )
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
  unread,
  absent,
  tone,
}: {
  href: string
  label: string
  /** The figure. null means there is none, and `unread`/`absent` say which. */
  value: number | string | null
  unit?: string | undefined
  sub: string
  foot?: string | undefined
  /** Set when the READ failed: the platform may have this, we did not get it. */
  unread?: string | null
  /** Set when the platform genuinely has no such figure. */
  absent?: string | null
  tone?: 'alert' | 'good' | undefined
}) {
  // Order matters. A failed read outranks everything: it must never fall
  // through to a figure from an earlier state or to a reassuring absence.
  const state = unread ? 'unread' : value === null ? 'absent' : 'value'
  const cls =
    state === 'unread'
      ? 'is-unread'
      : state === 'absent'
        ? 'is-absent'
        : tone === 'alert'
          ? 'is-alert'
          : tone === 'good'
            ? 'is-good'
            : ''

  return (
    <a className={`ctl-metric ov-tile ${cls}`} href={href}>
      <span className="ctl-metric-label">{label}</span>
      <span className="ctl-metric-value">
        {state === 'unread' ? (
          unread
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
    </a>
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
  href: string
  cta: string
}) {
  return (
    <div className="ov-h">
      <h2>
        {title}
        {typeof count === 'number' && count > 0 && <span className="ov-count">{count}</span>}
      </h2>
      <a className="ov-link" href={href}>
        {cta} →
      </a>
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
  const h = headroomFor(profile, byName)
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
        {h.agents === 0 ? (
          <span className="ov-stop">· none can start</span>
        ) : (
          <>· {h.agents} can start</>
        )}
      </span>

      {/* An unfilled plain track reads as "0% used", which is a claim. When no
          pool in the profile's list is configured there is no ceiling to draw
          against, so the track is hatched and carries no fill at all. */}
      <span className={`ctl-util-track${known ? '' : ' is-unknown'}`}>
        {known && (
          <>
            <i
              className={`ctl-util-fill ${fillClass}`}
              style={{ width: `${Math.min(100, ratio * 100)}%` }}
            />
            {/* Held above the ceiling: hatched rather than clipped at 100%,
                because a bar pinned full hides the one thing worth seeing. */}
            {over && <i className="ctl-util-over" style={{ width: '14%' }} />}
          </>
        )}
      </span>

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

      <span className="ctl-util-by" title={h.binding ?? undefined}>
        {paused
          ? 'paused'
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
  now,
}: {
  tasks: Result<TaskPage>
  stats: Result<Stats>
  now: number
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
            <RunningRow key={t.id} task={t} now={now} />
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

function RunningRow({ task, now }: { task: Task; now: number }) {
  const e = elapsed(task, now)
  const why = whyAgent(task)
  return (
    <tr>
      <th scope="row">
        <a className="ov-link" href={`#agents/task/${encodeURIComponent(task.id)}`}>
          {task.runner_profile}
        </a>
        <span className="ctl-sub">
          {task.id}
          {why ? ` · ${why}` : ''}
        </span>
      </th>
      <td>
        {/* The word is mandatory; the dot is decoration. Roughly 8% of male
            viewers cannot separate this panel's amber from its red. */}
        <span className={`ctl-chip ${chipTone(task.state)}`}>
          <i aria-hidden />
          {stateGlyph(task.state)} {task.state}
        </span>
      </td>
      {/* A LEASED task has no started_at -- lifecycle writes it on
          DISPATCHED -> STARTING -- so `elapsed` says "queued 4m", never "0s". */}
      <td className="is-num">{e.text}</td>
    </tr>
  )
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
    // The rollup cannot start until the task page lands, so a failed task read
    // leaves this permanently loading unless it is named.
    if (tasks.status === 'error') {
      return (
        <Nothing kind="failed" heading="Spend could not be assembled">
          It is summed from the attempts of the most recent tasks, and the task
          list itself could not be read: {tasks.error.message}
        </Nothing>
      )
    }
    return <Reading />
  }
  if (state.status === 'error') {
    return (
      <Nothing kind="failed" heading="No attempt read completed">
        {errorHeading(state.error)} — {state.error.message} No figure is shown,
        because a partial sum here would be indistinguishable from a small bill.
      </Nothing>
    )
  }
  if (state.status === 'empty') {
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
          spend is in none of these figures: {s.failedDetail}
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
 */
function accountHeadroom(state: Result<AccountsPage>): {
  pct: number | null
  sub: string
  foot: string | undefined
  absent: string | null
} {
  if (state.status === 'loading') {
    return { pct: null, sub: 'reading the subscription pool', foot: undefined, absent: 'reading…' }
  }
  if (state.status === 'error') {
    return { pct: null, sub: 'the subscription pool could not be read', foot: undefined, absent: null }
  }
  if (state.status === 'empty') {
    return {
      pct: null,
      sub: 'no account is registered, so the subscription pool supplies nothing',
      foot: 'read succeeded · a real zero',
      absent: 'no accounts',
    }
  }

  const accounts = state.data.accounts
  let best: { pct: number; key: string; resetsAt: string } | null = null
  let usable = 0

  for (const a of accounts) {
    // PAUSED and DRAINING are deliberate operator states, not faults -- but
    // they do not serve, so they are not counted as headroom either.
    if (a.state !== 'AVAILABLE') continue
    if (a.observed_at === null || a.stale) continue
    const w = bindingWindow(a)
    if (!w) continue
    usable++
    const left = Math.max(0, Math.min(100, (1 - w.window.utilization) * 100))
    if (best === null || left > best.pct) {
      best = { pct: left, key: w.key, resetsAt: w.window.resets_at }
    }
  }

  const unusable = accounts.length - usable
  if (best === null) {
    return {
      pct: null,
      sub: `${accounts.length} accounts registered, none with a current reading`,
      foot: 'a missing reading is not 0% used',
      absent: 'nothing measured',
    }
  }

  return {
    pct: best.pct,
    sub: `best of ${usable} usable account${usable === 1 ? '' : 's'} · its ${best.key.replace('_', '-')} window binds`,
    foot:
      unusable > 0
        ? `${unusable} account${unusable === 1 ? '' : 's'} excluded — paused, stale or needing sign-in`
        : 'every registered account has a current reading',
    absent: null,
  }
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
          const trusted = a.observed_at !== null && !a.stale && w !== null
          const pct = trusted && w ? Math.min(100, Math.max(0, w.window.utilization * 100)) : 0
          return (
            <div className="ctl-util" key={a.account_id}>
              <span className="ctl-util-name" title={a.account_id}>
                <b>{a.label}</b> · {a.assigned} assigned
              </span>
              {/* Never observed, or too old to trust: hatched with no fill.
                  A plain empty track here would claim 0% used, which for an
                  account nobody has polled is a number nobody measured. */}
              <span className={`ctl-util-track${trusted ? '' : ' is-unknown'}`}>
                {trusted && (
                  <i
                    className={`ctl-util-fill ${
                      a.state !== 'AVAILABLE'
                        ? 'is-paused'
                        : pct > 90
                          ? 'is-bad'
                          : pct > 75
                            ? 'is-warn'
                            : ''
                    }`}
                    style={{ width: `${pct}%` }}
                  />
                )}
              </span>
              <span className="ctl-util-figure">
                {trusted ? (
                  `${Math.round(pct)}%`
                ) : (
                  <span className="ctl-em">—</span>
                )}
              </span>
              <span className="ctl-util-by">
                {needsAHuman(a)
                  ? 'sign in again'
                  : a.observed_at === null
                    ? 'never polled'
                    : a.stale
                      ? `stale ${timeAgo(a.observed_at)}`
                      : a.state !== 'AVAILABLE'
                        ? a.state.toLowerCase()
                        : (w?.key.replace('_', '-') ?? '—')}
              </span>
            </div>
          )
        })}
      </div>
      <p className="provenance">
        {total} accounts
        {total > accounts.length && (
          <> · showing the {accounts.length} that most need looking at</>
        )}{' '}
        · utilisation is of the BINDING window, never an average of the two
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

interface Problem {
  severity: 'bad' | 'warn'
  /** The count this problem covers, so the panel can lead with a figure. */
  n: number
  headline: string
  detail: string
  href: string
}

type Check =
  | { label: string; status: 'reading' }
  | { label: string; status: 'blind'; why: string; admin: boolean }
  | { label: string; status: 'clear'; note: string }
  | { label: string; status: 'found'; problems: Problem[] }

/**
 * SIX CHECKS, each derived from state the platform genuinely records.
 *
 * There is no alert model on this platform -- nothing stores, routes or
 * acknowledges an alert, and nothing has a threshold anyone configured. So
 * this is not an inbox and does not pretend to be one: it is six queries over
 * live state, re-derived on every read, with no memory and no acknowledgement.
 * The upside of that is that it cannot go stale; the cost is that a check
 * whose read failed knows nothing, and SAYING SO is the entire difference
 * between this panel and a reassuring one.
 *
 * Each check has four outcomes and they render differently: reading, blind
 * (with whether it was an admin gate), clear (with what was actually
 * examined), and found. "Clear" always names the population it cleared, because
 * "no overdue leases" over zero leases read and over forty leases read are
 * different sentences.
 */
function deriveChecks(s: {
  capacity: Result<Capacity>
  tasks: Result<TaskPage>
  leases: Result<LeasePage>
  providers: Result<ProvidersPage>
  accounts: Result<AccountsPage>
  stats: Result<Stats>
}): Check[] {
  return [
    dispatchCheck(s.stats),
    leaseCheck(s.leases),
    quotaCheck(s.providers),
    accountCheck(s.accounts),
    poolCheck(s.capacity),
    failureCheck(s.tasks),
  ]
}

/** The loudest possible state: nothing is being admitted, platform-wide. */
function dispatchCheck(stats: Result<Stats>): Check {
  const label = 'Dispatch'
  if (stats.status === 'loading') return { label, status: 'reading' }
  if (stats.status === 'error') return { label, status: 'blind', ...blindness(stats.error) }
  const st = dataOf(stats)
  if (st === null) return { label, status: 'clear', note: 'nothing to read' }
  if (st.dispatch_paused) {
    return {
      label,
      status: 'found',
      problems: [
        {
          severity: 'bad',
          n: 1,
          headline: 'Dispatch is paused platform-wide',
          detail:
            'Admission still runs and leases are still taken, but nothing is handed to a backend. Every agent submitted from now on waits.',
          href: '#admin/limits',
        },
      ],
    }
  }
  return { label, status: 'clear', note: 'the platform is dispatching' }
}

/**
 * The freshest failure signal there is: a lease sees a quiet worker up to five
 * minutes before the reconciler acts on it.
 *
 * THE THRESHOLDS ARRIVE WITH THE DATA. 90 and 120 belong to the reconciler and
 * the grace derives from the heartbeat interval, so a local constant here
 * would mark a row overdue at a threshold the reconciler does not act on.
 */
function leaseCheck(leases: Result<LeasePage>): Check {
  const label = 'Leases'
  if (leases.status === 'loading') return { label, status: 'reading' }
  if (leases.status === 'error') return { label, status: 'blind', ...blindness(leases.error) }
  if (leases.status === 'empty') {
    return { label, status: 'clear', note: 'no lease is holding capacity' }
  }
  const page = leases.data
  const rows = page.leases
  const overdue = rows.filter((l) => l.dispatch_state === 'LEASED' && l.dispatch_overdue)
  const dead = rows.filter((l) => leaseLiveliness(l, page.thresholds).kind === 'presumed-dead')
  const silent = rows.filter(
    (l) => !dead.includes(l) && leaseLiveliness(l, page.thresholds).kind === 'silent',
  )

  const problems: Problem[] = []
  if (dead.length > 0) {
    problems.push({
      severity: 'bad',
      n: dead.length,
      headline: `${dead.length} lease${dead.length === 1 ? ' is' : 's are'} past the TTL`,
      detail:
        'The lease timeout has run out as well as the heartbeat going quiet. Capacity is held by something that is almost certainly gone.',
      href: '#capacity/holders',
    })
  }
  if (silent.length > 0) {
    problems.push({
      severity: 'warn',
      n: silent.length,
      headline: `${silent.length} worker${silent.length === 1 ? '' : 's'} silent past the grace period`,
      detail: `No heartbeat for ${page.thresholds.heartbeat_grace_seconds}s or more. This is already the reconciler's trigger, and its next pass is up to five minutes away.`,
      href: '#capacity/holders',
    })
  }
  if (overdue.length > 0) {
    problems.push({
      severity: 'bad',
      n: overdue.length,
      headline: `${overdue.length} lease${overdue.length === 1 ? ' was' : 's were'} admitted but never dispatched`,
      detail:
        'Capacity was reserved and the backend was never handed the work. These hold units while doing nothing.',
      href: '#capacity/holders',
    })
  }

  return problems.length > 0
    ? { label, status: 'found', problems }
    : {
        label,
        status: 'clear',
        note: `${rows.length} unreleased lease${rows.length === 1 ? '' : 's'}, none overdue, silent or expired`,
      }
}

/**
 * Provider quota, for THIS tenant.
 *
 * Read from `/v1/providers` rather than `/v1/admin/quota` on purpose: quota is
 * per provider per tenant because tenants bring their own keys, so one
 * tenant's 429 is not a platform outage. The tenant-scoped route carries the
 * same quota document and every caller can read it, whereas the admin route
 * would render this check as "admin only" for most people who open the landing
 * screen -- which teaches nothing. The platform-wide roll-up is one click
 * away under Capacity.
 */
function quotaCheck(providers: Result<ProvidersPage>): Check {
  const label = 'Provider quota'
  if (providers.status === 'loading') return { label, status: 'reading' }
  if (providers.status === 'error') {
    return { label, status: 'blind', ...blindness(providers.error) }
  }
  if (providers.status === 'empty') {
    return { label, status: 'clear', note: 'no provider is configured for this tenant' }
  }

  const rows = providers.data.providers
  const problems: Problem[] = []
  for (const p of rows) {
    const q = p.quota
    // No document is NOT a problem. One is written the first time a worker
    // reports on a provider, so its absence means "never used", not "broken".
    if (!q) continue
    const tone = providerTone(q.state)
    if (tone === 'bad') {
      problems.push({
        severity: 'bad',
        n: 1,
        headline: `${p.provider} is ${q.state}`,
        detail:
          q.state === 'DISABLED'
            ? 'Disabled for this tenant. Its effective limit is 0 and nothing using it will be admitted.'
            // `clearsIn`, not `timeAgo`: a reset is in the FUTURE and timeAgo
            // floors at zero, so it would render every pending reset as
            // "just now" -- the opposite of what it says.
            : `Quota is spent. Effective limit is ${q.effective_limit}${q.reset_at ? `, resetting in ${clearsIn(q.reset_at, Date.now())}` : ''}. Work on this provider parks rather than fails.`,
        href: '#capacity/quota',
      })
    } else if (tone === 'wait') {
      problems.push({
        severity: 'warn',
        n: 1,
        headline: `${p.provider} is ${q.state}`,
        detail: `${q.rate_limit_count} rate-limit responses so far; the ceiling this derives is ${q.effective_limit}${q.last_429_at ? `, last 429 ${timeAgo(q.last_429_at)}` : ''}. Throughput is reduced, not stopped.`,
        href: '#capacity/quota',
      })
    }
  }

  const measured = rows.filter((p) => p.quota !== null).length
  return problems.length > 0
    ? { label, status: 'found', problems }
    : {
        label,
        status: 'clear',
        note:
          measured === 0
            ? `${rows.length} providers, none with a quota document yet — never used, not healthy`
            : `${measured} of ${rows.length} providers have a quota document; none is throttled, exhausted or disabled`,
      }
}

/** The subscription pool's own faults. REAUTH_REQUIRED is the one only a person can clear. */
function accountCheck(accounts: Result<AccountsPage>): Check {
  const label = 'Accounts'
  if (accounts.status === 'loading') return { label, status: 'reading' }
  if (accounts.status === 'error') {
    return { label, status: 'blind', ...blindness(accounts.error) }
  }
  if (accounts.status === 'empty') {
    return { label, status: 'clear', note: 'no account is registered' }
  }

  const rows = accounts.data.accounts
  const reauth = rows.filter(needsAHuman)
  const never = rows.filter((a) => !needsAHuman(a) && a.observed_at === null)
  const stale = rows.filter((a) => !needsAHuman(a) && a.observed_at !== null && a.stale)

  const problems: Problem[] = []
  if (reauth.length > 0) {
    problems.push({
      severity: 'bad',
      n: reauth.length,
      headline: `${reauth.length} account${reauth.length === 1 ? ' needs' : 's need'} signing in again`,
      detail: `${reauth.map((a) => a.label).join(', ')} — the broker has stopped trying, so this removes capacity until a person acts. It will not clear on its own.`,
      href: '#capacity/accounts',
    })
  }
  if (never.length > 0) {
    problems.push({
      severity: 'warn',
      n: never.length,
      headline: `${never.length} account${never.length === 1 ? ' has' : 's have'} never been polled`,
      detail: `${never.map((a) => a.label).join(', ')} — no reading has ever arrived, so their utilisation is unknown rather than zero and they cannot be counted as headroom.`,
      href: '#capacity/accounts',
    })
  }
  if (stale.length > 0) {
    problems.push({
      severity: 'warn',
      n: stale.length,
      headline: `${stale.length} account reading${stale.length === 1 ? ' is' : 's are'} too old to trust`,
      detail: `${stale.map((a) => a.label).join(', ')} — the last reading is past the broker's staleness window, so the figures are real but describe an earlier moment.`,
      href: '#capacity/accounts',
    })
  }

  return problems.length > 0
    ? { label, status: 'found', problems }
    : {
        label,
        status: 'clear',
        note: `${rows.length} accounts, all with a current reading and none needing sign-in`,
      }
}

/**
 * Pools that are not admitting.
 *
 * Oversubscribed is its own problem and the loudest of the two: admission
 * cannot produce a pool holding more than its own ceiling, so it means a limit
 * was lowered under running work or a slot was never released. Folding it into
 * "full" hides it, because both have available == 0.
 */
function poolCheck(capacity: Result<Capacity>): Check {
  const label = 'Pools'
  if (capacity.status === 'loading') return { label, status: 'reading' }
  if (capacity.status === 'error') {
    return { label, status: 'blind', ...blindness(capacity.error) }
  }
  if (capacity.status === 'empty') {
    return { label, status: 'clear', note: 'no pool exists to be over or paused' }
  }

  const pools = capacity.data.pools
  const over = pools.filter(overCeiling)
  const paused = pools.filter(isPaused)

  const problems: Problem[] = []
  if (over.length > 0) {
    problems.push({
      severity: 'bad',
      n: over.length,
      headline: `${over.length} pool${over.length === 1 ? '' : 's'} holding more than the ceiling allows`,
      detail: `${over.map((p) => poolLabel(p.name)).join(', ')} — admission cannot produce this, so it is a ceiling lowered under running work or a slot never released. Running "make pool-check" resolves which.`,
      href: '#capacity/pools',
    })
  }
  if (paused.length > 0) {
    problems.push({
      severity: 'warn',
      n: paused.length,
      headline: `${paused.length} pool${paused.length === 1 ? '' : 's'} paused by an operator`,
      detail: `${paused.map((p) => poolLabel(p.name)).join(', ')} — deliberate, and it admits nothing until resumed. Anything whose profile lists one of these can start no agents at all.`,
      href: '#admin/limits',
    })
  }

  return problems.length > 0
    ? { label, status: 'found', problems }
    : { label, status: 'clear', note: `${pools.length} pools, none over ceiling or paused` }
}

/**
 * Failures among the tasks this page can see.
 *
 * SCOPED, and the scope is stated: `/v1/tasks?limit=200` is the 200 most
 * recently CREATED tasks, so this is "recent failures" in the only sense the
 * API can serve cheaply. It is not the tenant's total and does not claim to
 * be; the exact per-state count lives on Platform counts.
 */
function failureCheck(tasks: Result<TaskPage>): Check {
  const label = 'Failures'
  if (tasks.status === 'loading') return { label, status: 'reading' }
  if (tasks.status === 'error') return { label, status: 'blind', ...blindness(tasks.error) }
  if (tasks.status === 'empty') {
    return { label, status: 'clear', note: 'no task has ever been submitted' }
  }

  const rows = tasks.data.tasks
  const failed = rows.filter((t) => t.state === 'FAILED')
  const exhausted = failed.filter((t) => t.attempt_count >= t.max_attempts)

  if (failed.length === 0) {
    return {
      label,
      status: 'clear',
      note: `none of the ${rows.length} most recently created tasks is FAILED`,
    }
  }

  return {
    label,
    status: 'found',
    problems: [
      {
        severity: 'bad',
        n: failed.length,
        headline: `${failed.length} failed task${failed.length === 1 ? '' : 's'} among the ${rows.length} most recent`,
        detail:
          exhausted.length > 0
            ? `${exhausted.length} of them have used every attempt, so nothing will retry them. Newest: ${failed[0]?.last_error ?? 'no error was recorded'}`
            : `All still have attempts left and may retry. Newest: ${failed[0]?.last_error ?? 'no error was recorded'}`,
        href: '#overview/attention',
      },
    ],
  }
}

/** How many problems the landing screen lists before it links to the rest. */
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
            <li className="ov-item" key={`${p.headline}-${i}`}>
              <span className={`ctl-chip ${p.severity === 'bad' ? 'is-bad' : 'is-warn'}`}>
                <i aria-hidden />
                {p.severity === 'bad' ? 'act' : 'watch'}
              </span>
              <p>
                <b>{p.headline}</b> — {p.detail}
              </p>
              <a className="ov-link" href={p.href}>
                open →
              </a>
            </li>
          ))}
        </ul>
      )}
      {/* Cut, never dropped. A list silently truncated to the top four is a
          list that hides the fifth problem. */}
      {problems.length > ATTENTION_ROWS && (
        <p className="provenance">
          {problems.length - ATTENTION_ROWS} more, worst first —{' '}
          <a className="ov-link" href="#overview/attention">
            the full trouble board →
          </a>
        </p>
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
 * The layout itself is `auto-fit` rather than a media query, so the two
 * columns become one below roughly 700px with no breakpoint to maintain and
 * no phone-specific copy of the grid to keep in step.
 */
const OVERVIEW_CSS = `
.ov-topline {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--ctl-s3);
  flex-wrap: wrap;
  margin-bottom: var(--ctl-s4);
}
.ov-topline .head { margin-bottom: 0; }
.ov-topline .sub { margin: 0; }

.ov-cols {
  display: grid;
  /* min(400px, 100%), not a bare 400px. A minmax floor wider than the
     container does not collapse -- auto-fit drops to one column and that one
     column is still 400px wide, which is 26px of horizontal page scroll at
     390pt. The min() clamps the floor to the container. */
  grid-template-columns: repeat(auto-fit, minmax(min(400px, 100%), 1fr));
  gap: var(--ctl-s3);
  align-items: start;
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
  font: 600 12px/1.4 var(--font);
  text-transform: uppercase;
  letter-spacing: .07em;
  color: var(--text-faint);
}
.ov-count {
  padding: 0 7px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--bad) 16%, transparent);
  color: var(--bad);
  font: 600 11px var(--mono);
  letter-spacing: 0;
}

.ov-link {
  color: var(--info);
  font: 11px var(--mono);
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

.ov-lead {
  margin: 0 0 var(--ctl-s2);
  font-size: 12.5px;
  line-height: 1.5;
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
.ov-item > p { margin: 0; font-size: 12.5px; line-height: 1.55; color: var(--text-dim); }
.ov-item > p > b { color: var(--text); font-weight: 600; }

.ov-kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(88px, 1fr)); gap: var(--ctl-s2); margin: 0; }
.ov-kv > div { min-width: 0; }
.ov-kv dt { font: 600 10.5px var(--mono); letter-spacing: .05em; text-transform: uppercase; color: var(--text-faint); }
.ov-kv dd { margin: 2px 0 0; font: 15px var(--mono); font-variant-numeric: tabular-nums; color: var(--text); }
.ov-kv dd.ov-figure { font-size: 19px; font-weight: 600; }

/* An empty state INSIDE a panel, rather than as the page. The panel's own
   heading has already said what this is about, so the padding comes down. */
.ov-tight { padding: var(--ctl-s3); }
.ov-skel { height: 76px; border-radius: var(--radius); }

.ov-broken { color: var(--bad); }
/* An admin gate is information, not breakage -- so the line that reports only
   admin gates is blue and calm, never the amber used for a real failure. */
.ov-info { color: var(--info); }
.ov-checks:empty { display: none; }
`
