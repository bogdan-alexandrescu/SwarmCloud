import { PHONE_PAGE_LIMIT, TASK_PAGE_LIMIT } from './pageLimits'
import { hhmmUtc, stateView, type OverviewTaskPage } from './overviewReads'
import { errorHeading, isPaused, type ApiError, type Result } from './fetch'
import {
  CONCURRENCY_STATES,
  PARK_CLEARS_ITSELF,
  PARK_NEEDS_A_PERSON,
  PARK_WAITS_ON_A_STEP,
  TERMINAL_STATES,
  clearsIn,
  formatDuration,
  leaseLiveliness,
  needsAHuman,
  overCeiling,
  poolLabel,
  providerTone,
  reasonCopy,
  timeAgo,
  type AccountsPage,
  type Capacity,
  type LeasePage,
  type ProvidersPage,
  type Stats,
  type Task,
  type TaskPage,
  type Workflow,
  type WorkflowPage,
  pluralise,
} from './types'
import { waitingShort } from './waitingShort'

/**
 * WHAT IS WRONG, derived from real state -- the whole of it, and none of the
 * drawing.
 *
 * This module was the bottom third of Overview.tsx. It moved out for one
 * reason: it is the only part of that screen whose failure is SILENCE, and
 * silence cannot be reviewed by reading. A check that never fires looks exactly
 * like a platform that is healthy, so this layer has to be exercised -- and it
 * can be, from `node --test`, only because nothing in here imports React, the
 * DOM, or a clock it was not handed. `apps/swarm-ui/test/checks.test.mjs` runs
 * it; `Overview.tsx` keeps `AttentionBody` and `ProblemRow`, which draw what
 * this returns.
 *
 * THERE IS NO ALERT MODEL ON THIS PLATFORM. Nothing stores, routes or
 * acknowledges an alert, and nothing has a threshold anyone configured. So this
 * is not an inbox and does not pretend to be one: it is a set of queries over
 * live state, re-derived on every read, with no memory and no acknowledgement.
 * The upside is that it cannot go stale; the cost is that a check whose read
 * failed knows nothing, and SAYING SO is the entire difference between this
 * panel and a reassuring one.
 */

export interface Problem {
  severity: 'bad' | 'warn'
  /** The count this problem covers, so the panel can lead with a figure. */
  n: number
  headline: string
  detail: string
  href: string
  /**
   * What the link says, when "open" would land somewhere the reader may not be
   * able to use.
   *
   * The quota problems are the case this exists for. `quotaCheck` reads
   * `/v1/providers`, which every caller can read, precisely so the check does
   * not render as "admin only" on the screen most people land on -- and then
   * the only screen that goes deeper is `/v1/admin/quota`. Sending a non-admin
   * to a blue admin-only panel behind a bare "open →" is the dead end this
   * panel exists to avoid, so the link says what it is BEFORE the click, which
   * is the same rule the section nav follows for its admin tabs. The problem's
   * own detail already carries the full quota document, so nobody depends on
   * the click.
   */
  linkLabel?: string
  /**
   * The workflows the headline names, each with its own address (visual QA
   * N11, 2026-10-21). The card is one link to `href`, so an id inside it
   * could not be a link of its own -- a link inside a link is invalid -- and
   * run 4 reached wf_5e5ad3… through Work › Workflows instead. A problem that
   * carries this list is drawn with each id as a link beside the card's own.
   */
  workflows?: { id: string; href: string }[]
}

export type Check =
  | { label: string; status: 'reading' }
  | { label: string; status: 'blind'; why: string; admin: boolean }
  | { label: string; status: 'clear'; note: string }
  | { label: string; status: 'found'; problems: Problem[] }

/**
 * Why a check has nothing, split by CAUSE.
 *
 * `admin` is carried separately rather than folded into the sentence because
 * the two render completely differently -- blue and calm versus amber and
 * wrong -- and a caller that has to read the copy to tell them apart will
 * eventually get it backwards.
 */
export function blindness(error: ApiError): { why: string; admin: boolean } {
  if (error.kind === 'admin_required') {
    return { why: 'Admin only. Nothing failed.', admin: true }
  }
  return { why: `${errorHeading(error)} — ${error.message}`, admin: false }
}

/**
 * One row of the reconciler's workflow stall check (#616), as
 * `GET /v1/workflows` serves it under `stalled_workflows` (swarm_api/stalls.py;
 * the row is `WorkflowStall.entry` in reconciler/detect.py). Declared here
 * rather than in types.ts because this check is its only reader.
 */
export interface StalledWorkflow {
  workflow_id: string
  tenant_id: string | null
  step_id: string | null
  task_id: string | null
  /** no_progress · start_overdue · unreadable · dependencies_met · state_drift */
  kind: string
  /** The reconciler's grade, so this screen does not restate which kinds are worse. */
  severity: 'bad' | 'warn' | 'note'
  age_seconds: number | null
  reason: string
  repaired: boolean
  repair: string | null
}

/**
 * The caller's rows from the reconciler's newest pass. `count` is NULL, never
 * zero, when the result is not known -- no pass, a blind check, a failed read --
 * and `check_error` says which.
 */
export interface StalledWorkflows {
  count: number | null
  /** Worst first, in the reconciler's order. */
  workflows: StalledWorkflow[]
  truncated: boolean
  scan_truncated: boolean
  pass_at: string | null
  check_error: string | null
}

/** The reads every check is derived from. One per panel-independent route. */
export interface CheckInputs {
  capacity: Result<Capacity>
  /** The Overview's page, with its state reads when the page is not every task. */
  tasks: Result<OverviewTaskPage>
  leases: Result<LeasePage>
  providers: Result<ProvidersPage>
  accounts: Result<AccountsPage>
  stats: Result<Stats>
  workflows: Result<WorkflowPage>
  /**
   * Whether `tasks` is the phone's page (`PHONE_PAGE_LIMIT` rows) rather than
   * the api's full one (`TASK_PAGE_LIMIT`). Absent reads as the full page.
   */
  phonePage?: boolean
}

/**
 * THE TWO WINDOWS, SAID (owner QA R12, 2026-10-04). At the same moment a phone
 * said "8 failed among the 50 most recent" and the desktop "26 among the 200":
 * each counts the page its own agent list reads (OV-10), and a phone reads 50
 * rows because every row carries its input and output on a 20s poll (§2.5).
 * Both stay; each figure now says that the other width reads another window.
 */
export function windowNote(phonePage: boolean, phoneRows: number, wideRows: number): string {
  // ONE WINDOW AT EVERY WIDTH (live QA CH-4): nothing to say about another.
  if (phoneRows === wideRows) return ''
  return phonePage
    ? ` · a phone reads the newest ${phoneRows}, a wider screen the newest ${wideRows}`
    : ` · a phone reads only the newest ${phoneRows}`
}

/**
 * EIGHT CHECKS, each derived from state the platform genuinely records.
 *
 * Each has four outcomes and they render differently: reading, blind (with
 * whether it was an admin gate), clear (with what was actually examined), and
 * found. "Clear" always names the population it cleared, because "no overdue
 * leases" over zero leases read and over forty leases read are different
 * sentences.
 *
 * `now` IS A PARAMETER, not a `Date.now()` inside the checks. Two of them
 * measure an age and one of those decides whether a workflow is stalled; a
 * function that reads the clock itself can be tested only against the clock,
 * which means the stall threshold -- the one number on this screen chosen from
 * a measurement -- could not be exercised at all.
 *
 * WHY WORKFLOWS ARE HERE AT ALL. Until this change the word "workflow" did not
 * appear anywhere on the landing screen. With a three-step workflow in the
 * tenant holding one READY step and two PARKED ones, the panel said "Nothing
 * is running -- no task on the 7 most recently created is in LEASED,
 * DISPATCHED, STARTING or RUNNING. The state counts agree: zero." Every
 * sentence was true and the conclusion a reader drew from them was false: the
 * work had stopped, and the screen whose entire job is to say what to look at
 * first said there was nothing to look at. Six checks covered dispatch,
 * leases, provider quota, accounts, pools and failures. None of them can see a
 * workflow that has stopped moving, because a stalled workflow breaks no
 * lease, holds no capacity and fails no task.
 */
export function deriveChecks(s: CheckInputs, now: number): Check[] {
  return [
    dispatchCheck(s.stats),
    workflowCheck(s.workflows, now),
    leaseCheck(s.leases),
    parkedCheck(s.tasks, now),
    quotaCheck(s.providers, now),
    accountCheck(s.accounts),
    poolCheck(s.capacity),
    failureCheck(s.tasks, s.stats, now, s.phonePage ?? false),
  ]
}

/** The loudest possible state: nothing is being admitted, platform-wide. */
function dispatchCheck(stats: Result<Stats>): Check {
  const label = 'Dispatch'
  if (stats.status === 'loading') return { label, status: 'reading' }
  if (stats.status === 'error') return { label, status: 'blind', ...blindness(stats.error) }
  // AN UNKNOWN IS NOT A CLEAR. `loadStats` passes `() => false` as its empty
  // predicate -- a successful read always yields twelve counts -- so this is
  // unreachable today. It is still `blind` rather than `clear`, because the
  // day that predicate changes, a `clear` here would be counted in "N of 6
  // checks ran" and would feed "all N came back clear": a reassurance derived
  // from a read that returned nothing. The other five checks all route an
  // unknown this way.
  if (stats.status === 'empty') {
    return {
      label,
      status: 'blind',
      why: 'The read succeeded but carried no counts, so whether dispatch is paused was not established.',
      admin: false,
    }
  }
  const st = stats.data
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
      linkLabel: 'holders',
    })
  }
  if (silent.length > 0) {
    // THE HEADLINE NAMES THE WORKERS (#92). "2 workers silent" named nobody,
    // and at 3am the next question is always "which". The quietest first, so
    // the one closest to being reclaimed is the one that is never cut.
    const quietest = [...silent].sort((a, b) => b.silent_seconds - a.silent_seconds)
    problems.push({
      severity: 'warn',
      n: silent.length,
      headline: `${silent.length} worker${silent.length === 1 ? '' : 's'} silent past the grace period: ${namesOf(quietest.map((l) => l.task_id))}`,
      detail: `No heartbeat for ${page.thresholds.heartbeat_grace_seconds}s or more. This is already the reconciler's trigger, and its next pass is up to five minutes away.`,
      href: '#capacity/holders',
      linkLabel: 'holders',
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
      linkLabel: 'holders',
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
function quotaCheck(providers: Result<ProvidersPage>, now: number): Check {
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
            : `Quota is spent. Effective limit is ${q.effective_limit}${q.reset_at ? `, resetting in ${clearsIn(q.reset_at, now)}` : ''}. Work on this provider parks rather than fails.`,
        href: '#capacity/quota',
        linkLabel: 'quota, all tenants · admin',
      })
    } else if (tone === 'wait') {
      problems.push({
        severity: 'warn',
        n: 1,
        headline: `${p.provider} is ${q.state}`,
        detail: `${q.rate_limit_count} rate-limit responses so far; the ceiling this derives is ${q.effective_limit}${q.last_429_at ? `, last 429 ${timeAgo(q.last_429_at, now)}` : ''}. Throughput is reduced, not stopped.`,
        href: '#capacity/quota',
        linkLabel: 'quota, all tenants · admin',
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
        note: `${pluralise(rows.length, 'account')}, all with a current reading and none needing sign-in`,
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
 * recently CREATED tasks (50 at phone width, where the Overview reads the
 * agent list's phone page -- OV-10), so this is "recent failures" in the only
 * sense the API can serve cheaply, and the headline's N says which page. It is
 * not the tenant's total and does not claim to be; the exact per-state count
 * lives on Platform counts.
 *
 * A PAGE WITH MORE BEHIND IT IS NOT ASKED ALONE (live QA OV-2, 2026-10-11).
 * A phone's 50 newest held no FAILED row while 11 tasks had failed in the
 * last 24h -- created earlier, failed since -- and this check said clear:
 * "checks 8/8 · 2 found something" (the head's wording then) against 3 on the desktop. So when the
 * page is not every task the Overview also reads FAILED by state, and this
 * counts the FAILED tasks that ENDED in the last 24h, over that read. A
 * FAILED read that has more behind it makes the figure `≥N`. When that read
 * did not land, the page answers alone, and a page with no failure on it is
 * clear only if `/v1/stats` counts no FAILED task at all; otherwise it is
 * blind, never "none".
 */
function failureCheck(tasks: Result<OverviewTaskPage>, stats: Result<Stats>, now: number, phonePage: boolean): Check {
  const label = 'Failures'
  if (tasks.status === 'loading') return { label, status: 'reading' }
  if (tasks.status === 'error') return { label, status: 'blind', ...blindness(tasks.error) }
  if (tasks.status === 'empty') {
    return { label, status: 'clear', note: 'no agent has ever been submitted' }
  }

  const rows = tasks.data.tasks
  const pageWhole = (tasks.data.next_page_token ?? null) === null
  const byState = tasks.data.byState?.find((r) => r.state === 'FAILED')
  const widths = windowNote(phonePage, PHONE_PAGE_LIMIT, TASK_PAGE_LIMIT)

  if (!pageWhole && byState !== undefined) {
    const view = stateView(tasks.data, ['FAILED'])
    const failed = newestEndedFirst(view.rows.filter((t) => endedWithin(t, now, DAY_MS)))
    const whole = view.whole('FAILED')
    const reach = whole ? 'every FAILED agent read' : `FAILED agents created since ${hhmmUtc(view.since ?? '')} read`
    if (failed.length === 0) {
      return { label, status: 'clear', note: `no agent FAILED in the last 24 hours · ${reach}` }
    }
    return { label, status: 'found', problems: [failedProblem(failed, now, `${whole ? '' : '≥'}${failed.length}`, ` in the last 24 hours${whole ? '' : ` · ${reach}`}`)] }
  }

  const failed = newestEndedFirst(rows.filter((t) => t.state === 'FAILED'))

  if (failed.length === 0) {
    if (!pageWhole) {
      // THE PAGE IS NOT EVERY TASK AND THE FAILED READ DID NOT LAND. Only an
      // exact count of zero clears it.
      const st = stats.status === 'ok' || stats.status === 'stale' ? stats.data : null
      const counted = st?.tasks_by_state.FAILED
      if (counted !== 0) {
        return {
          label,
          status: 'blind',
          admin: false,
          why:
            typeof counted === 'number'
              ? `None of the ${rows.length} most recently created agents is FAILED, but the state counts say ${counted} FAILED in all and the read of FAILED agents did not land, so whether any failed recently is not established.`
              : `None of the ${rows.length} most recently created agents is FAILED, but more agents exist beyond them and neither the read of FAILED agents nor the state counts landed, so whether any failed recently is not established.`,
        }
      }
      return { label, status: 'clear', note: `the state counts say no agent has ever FAILED${widths}` }
    }
    return {
      label,
      status: 'clear',
      note: `none of the ${rows.length} most recently created agents is FAILED${widths}`,
    }
  }

  return { label, status: 'found', problems: [failedProblem(failed, now, String(failed.length), ` among the ${rows.length} most recent`, widths)] }
}

/**
 * The one failures item: how many, how fresh, from which workflow, and the
 * newest one's error.
 *
 * `failed` IS NEWEST-ENDED FIRST (live QA OV-3, 2026-10-11): the headline's
 * "newest 15m ago" is the newest END, and the detail's "Newest:" was the
 * first row in creation order -- another, older failure's error.
 */
function failedProblem(failed: Task[], now: number, count: string, scope: string, widths = ''): Problem {
  const exhausted = failed.filter((t) => t.attempt_count >= t.max_attempts)
  return {
    severity: 'bad',
    n: failed.length,
    // HOW FRESH, AND FROM WHICH WORKFLOW (#92, #90). A count with no age
    // stays open until newer tasks push the failures off the page, so
    // "21 failed" reads the same two minutes and two days later. And a
    // failed workflow was only ever a share of this count; it is named
    // here, in this item, rather than in a second item restating the same
    // failures in another voice.
    headline:
      `${count} failed agent${failed.length === 1 ? '' : 's'}${scope}` +
      newestFailureClause(failed, now) +
      workflowClause(failed) +
      widths,
    // FAILED IS TERMINAL (QA G1-04, 2026-10-07). The detail read "All
    // still have attempts left and may retry" beside an error saying the
    // task failed without a retry: attempts left is not a retry coming.
    // A retryable failure with attempts left goes back to READY
    // (agent_worker/control.py), so FAILED is written only once nothing
    // will run it again: TERMINAL_STATES holds it, and no writer moves it
    // back. The attempt count says why, never whether.
    detail: `${terminalClause(failed.length, exhausted.length)} Newest: ${failed[0]?.last_error ?? 'no error was recorded'}`,
    // A failed agent is a row in the agent list, not an entry on a board
    // of its own. THE ADDRESS OPENS EXACTLY THAT LIST (OV-10): the Recent
    // tab filtered to FAILED. The tab used to be component state the hash
    // could not carry, so this said "agents · Recent tab" -- a how-to --
    // and landed on Live whenever anything ran. The word names the
    // destination, as OV-11 set for the card heads.
    //
    // THE LIST FILTERS ITS OWN PAGE client-side. Where this item counts the
    // FAILED read rather than the page (OV-2) the list can show fewer: its
    // page is the newest-created N, and the headline says which population
    // this count is.
    //
    // THE PAGE'S OWN NAME (visual QA QA-109, 2026-10-20): "failed agents"
    // was a name the list it opens carries nowhere -- the page is titled
    // Agents and filters by state. So the word is the page, then the state
    // this item counts.
    href: '#work/running/recent/failed',
    linkLabel: 'Agents · FAILED',
    // EACH WORKFLOW THE HEADLINE NAMES OPENS ITSELF (visual QA N11): the
    // same two `workflowClause` names, in its order.
    workflows: namedWorkflows(failed).map(([id]) => ({ id, href: `#work/workflows?wf=${encodeURIComponent(id)}` })),
  }
}

const DAY_MS = 24 * 60 * 60 * 1000

function endedMs(t: Task): number | null {
  if (t.completed_at === null) return null
  const v = new Date(t.completed_at).getTime()
  return Number.isFinite(v) ? v : null
}

function endedWithin(t: Task, now: number, span: number): boolean {
  const v = endedMs(t)
  return v !== null && now - v <= span
}

/** Newest recorded end first; a task with no recorded end after every dated one. */
export function newestEndedFirst(rows: readonly Task[]): Task[] {
  return [...rows].sort((a, b) => (endedMs(b) ?? -Infinity) - (endedMs(a) ?? -Infinity))
}

/** At most three names, then how many more: a headline, not a list. */
function namesOf(ids: string[]): string {
  const shown = ids.slice(0, 3).join(', ')
  return ids.length > 3 ? `${shown} +${ids.length - 3} more` : shown
}

/**
 * Why N FAILED tasks will not run again, by what their attempts say (QA
 * G1-04): never "may retry", because FAILED is terminal whatever was left.
 */
export function terminalClause(failed: number, exhausted: number): string {
  const left = failed - exhausted
  const parts = [
    ...(exhausted > 0 ? [`${exhausted} used every attempt`] : []),
    ...(left > 0 ? [`${left} had attempts left but the cause is not retryable`] : []),
  ]
  return `Terminal: nothing will retry ${failed === 1 ? 'it' : 'them'}. ${parts.join('; ')}.`
}

/**
 * ` · newest 3m ago`, from the newest failure's END. Empty when no failed task
 * carries a recorded end: an age nobody recorded is not "just now", and the
 * document's last write is not its end (browser QA N4, 2026-10-04: a later
 * write made failures ~9h old read "58m ago").
 */
export function newestFailureClause(failed: Task[], now: number): string {
  let newest: number | null = null
  for (const t of failed) {
    const v = t.completed_at === null ? NaN : new Date(t.completed_at).getTime()
    if (Number.isFinite(v) && (newest === null || v > newest)) newest = v
  }
  return newest === null ? '' : ` · newest ${timeAgo(newest, now)}`
}

/**
 * ` · 4 in workflow wf-nightly`, for the workflows the failures belong to,
 * most failures first, two named at most.
 */
function workflowClause(failed: Task[]): string {
  const ranked = rankedWorkflows(failed)
  if (ranked.length === 0) return ''
  const named = namedWorkflows(failed).map(([id, n], i) => (i === 0 ? `${n} in workflow ${id}` : `${n} in ${id}`))
  const rest = ranked.length - named.length
  return ` · ${named.join(', ')}${rest > 0 ? ` +${pluralise(rest, 'workflow')}` : ''}`
}

/** The failures' workflows with their counts, most failures first, then by id. */
function rankedWorkflows(failed: Task[]): [string, number][] {
  const by = new Map<string, number>()
  for (const t of failed) {
    if (t.workflow_id) by.set(t.workflow_id, (by.get(t.workflow_id) ?? 0) + 1)
  }
  return [...by.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
}

/** The workflows `workflowClause` names by id: the first two of the ranking. */
function namedWorkflows(failed: Task[]): [string, number][] {
  return rankedWorkflows(failed).slice(0, 2)
}

// ---------------------------------------------------------------------------
// Workflows: is the thing a person actually submitted still moving?
// ---------------------------------------------------------------------------

/**
 * HOW LONG A WORKFLOW MAY SIT WITH NOTHING IN FLIGHT before this screen calls
 * it stalled. Twelve minutes.
 *
 * THE NUMBER IS MEASURED, NOT CHOSEN. Three figures bound it from below and
 * every one of them is a real number from this platform:
 *
 *   - DISPATCHED -> STARTING was measured at p50 122.6s and p90 159.0s across
 *     231 tasks. Anything under about 160s fires on an ordinary cold start,
 *     which would make this check's first lesson "ignore me".
 *   - `dispatch_timeout_seconds` is 480 (apps/common/swarm_common/config.py;
 *     300 until contract request 37, 2026-09-30). That is the platform's OWN
 *     deadline for a LEASED task to reach a backend, and `leaseCheck` above
 *     already reports a lease that misses it. A workflow threshold at or below
 *     480 would restate the lease check's finding in a second voice, on the
 *     same screen, five rows apart.
 *   - the scheduler notices an eligible step on a loop bounded at
 *     `max_run_seconds` 45 with an aging tick of `aging_interval_seconds` 60
 *     (apps/scheduler/scheduler/settings.py:72,79), so "eligible" and "picked
 *     up" are a pass apart even when everything is healthy.
 *
 * When the deadline was 300, 300 + 159 + a scheduler pass was a little over
 * 500s, and 600 was the first round number clear of all three. Contract
 * request 37 (#404) raised the deadline to 480: 600 stayed above it and above
 * the p90 cold start individually, the two bounds
 * tests/unit/control_plane/test_overview_workflow_checks.py holds, but was no
 * longer above their sum (about 639s) -- a workflow could then read as
 * stalled before a lease that merely missed the 480s deadline on a slow cold
 * start could have been fenced and redispatched. #404 left the number at 600
 * and called raising it a separate decision; this is that decision. 720 is
 * clear of 480 + 159 with margin, and still well inside the twenty-minute
 * stall the UI audit watched go entirely unreported, which is the failure
 * this exists to end.
 *
 * The gate below matters as much as the number: a workflow with ANY step
 * LEASED, DISPATCHED, STARTING or RUNNING is never stalled however long it has
 * been at it, because an agent that runs for forty minutes is doing its job.
 * Without that gate no threshold could be both useful and quiet.
 */
const WORKFLOW_STALL_SECONDS = 720

/** For the copy, so the sentence and the constant can never disagree. */
const WORKFLOW_STALL_MINUTES = Math.round(WORKFLOW_STALL_SECONDS / 60)

/** A count out of `rollup.counts`, which omits every key whose count is zero. */
function countOf(w: Workflow, key: string): number {
  const n = w.rollup?.counts[key]
  return typeof n === 'number' ? n : 0
}

/**
 * How many of this workflow's steps are holding capacity.
 *
 * Summed over `CONCURRENCY_STATES` rather than over a list written out here.
 * CONTRACT invariant 1 names those four and only those four, and a second
 * spelling of the set is how a screen ends up teaching a different model of
 * what costs money than the admission path uses.
 */
function stepsInFlight(w: Workflow): number {
  let n = 0
  for (const state of CONCURRENCY_STATES) n += countOf(w, state)
  return n
}

/** Whether the derived state says this workflow is over. */
function isFinished(w: Workflow): boolean {
  // `complete === true`, explicitly, for the same reason the split in
  // `workflowCheck` below is explicit. An UNKNOWN state is never terminal.
  if (w.rollup?.complete !== true) return false
  return (TERMINAL_STATES as ReadonlySet<string>).has(w.rollup.state)
}

/** Seconds since an ISO instant, or null when it will not parse. */
function secondsSince(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return null
  return Math.max(0, (now - t) / 1000)
}

/**
 * Workflows that have stopped moving.
 *
 * WHY `workflow.updated_at` AND NOT THE STEP TASKS'. A step task's
 * `updated_at` is not a progress signal: `scheduler/store.py:192-201` rewrites
 * `blocked_by` with a fresh timestamp on every pass in which a READY task was
 * not admitted, so a task wedged at the head of a full pool for an hour looks
 * like it moved a minute ago. The WORKFLOW document is the opposite --
 * `rollup._persist` refuses to write a value that already agrees, expressly so
 * that `updated_at` keeps meaning "the document changed" rather than "something
 * looked at it" (`rollup._persist`). It therefore moves exactly when the
 * derived state moves, which is what "advanced" means here.
 *
 * And while nothing is in flight, the derived state cannot move without the
 * workflow advancing: with no step holding capacity the state is whichever
 * pending state ranks highest (`swarm_rollup._PENDING_PRECEDENCE`), so PARKED -> READY, READY ->
 * anything in flight, and a step finishing all change it. A quiet `updated_at`
 * under a quiet set of steps is a real stop, not a gap in the record.
 */
function workflowCheck(workflows: Result<WorkflowPage>, now: number): Check {
  const label = 'Workflows'
  if (workflows.status === 'loading') return { label, status: 'reading' }
  if (workflows.status === 'error') {
    return { label, status: 'blind', ...blindness(workflows.error) }
  }
  if (workflows.status === 'empty') {
    return { label, status: 'clear', note: 'no workflow has ever been submitted' }
  }

  const page = workflows.data
  const rows = page.workflows

  // THE `false // true` TRAP, in its TypeScript spelling.
  //
  // CLAUDE.md records the jq form: `.enabled // true` reports a PAUSED pool as
  // open, because the alternative operator treats `false` as absent. `||` has
  // exactly that shape here -- `w.rollup?.complete || true` is `true` for every
  // input there is -- and `?? true` has the subtler half of it: it keeps
  // `false` but invents agreement for a workflow whose rollup is missing
  // altogether. Both would make this check trust a state derived from steps the
  // API could not read, which is the one thing swarm_api/rollup.py exists to
  // prevent. So: compare against the value that matters, and let the two
  // absences stay different things.
  const judged: Workflow[] = []
  const unjudged: Workflow[] = []
  for (const w of rows) {
    if (w.rollup === undefined || w.rollup.complete === false) unjudged.push(w)
    else if (secondsSince(w.updated_at, now) === null) unjudged.push(w)
    else judged.push(w)
  }

  const live = judged.filter((w) => !isFinished(w))
  // A cancel already told the platform to stop, so "has not advanced" is what
  // was asked for and reporting it would be reporting a granted request.
  const cancelling = live.filter((w) => w.cancel_requested)
  const moving = live.filter((w) => !w.cancel_requested)

  const stalled = moving.filter((w) => {
    if (stepsInFlight(w) > 0) return false
    const quiet = secondsSince(w.updated_at, now)
    return quiet !== null && quiet >= WORKFLOW_STALL_SECONDS
  })

  const problems: Problem[] = []
  if (stalled.length > 0) {
    // Worst first, so the detail names the one that has been stopped longest.
    const byQuiet = [...stalled].sort(
      (a, b) => (secondsSince(b.updated_at, now) ?? 0) - (secondsSince(a.updated_at, now) ?? 0),
    )
    const worst = byQuiet[0] as Workflow
    // A READY step is ELIGIBLE and is being picked up by nothing, which is a
    // fault. A workflow whose steps are all parked or not started is waiting on
    // something `parkedCheck` below names, and waiting is not the same as
    // broken.
    const eligible = stalled.filter((w) => countOf(w, 'READY') > 0)
    // THE AGE, NOT THE THRESHOLD, AND EVERY ONE NAMED (live QA OV-9,
    // 2026-10-11): the headline said "has not advanced in 12 minutes" -- the
    // threshold -- while the detail said "last changed 21m ago", and with two
    // stalled the detail named one and "Open" went to the unfiltered list.
    // The headline carries the longest quiet; the detail names up to three;
    // one stalled workflow opens that workflow.
    const quiet = secondsSince(worst.updated_at, now) ?? 0
    const longest = quiet < 3600 ? `${Math.floor(quiet / 60)} minutes` : formatDuration(quiet * 1000)
    const others = byQuiet.slice(1)
    problems.push({
      severity: eligible.length > 0 ? 'bad' : 'warn',
      n: stalled.length,
      headline:
        stalled.length === 1
          ? `1 workflow has not advanced for ${longest}`
          : `${stalled.length} workflows have not advanced for ${WORKFLOW_STALL_MINUTES} minutes or more, the longest ${longest}`,
      detail:
        `${worst.workflow_id} last changed ${timeAgo(worst.updated_at, now)} and has ${describeSteps(worst)}. ` +
        (others.length > 0
          ? `Also ${others.slice(0, 2).map((w) => `${w.workflow_id} (${timeAgo(w.updated_at, now)})`).join(', ')}${others.length > 2 ? ` and ${others.length - 2} more` : ''}. `
          : '') +
        `No step of any of them is LEASED, DISPATCHED, STARTING or RUNNING, so none is holding capacity and none is costing anything — ` +
        `this is a progress problem, not a full pool. ` +
        (eligible.length > 0
          ? `${eligible.length} ${eligible.length === 1 ? 'has a step' : 'have steps'} READY: eligible, and picked up by nothing.`
          : 'Every stalled step is parked or not yet started; the parked check says on what.'),
      href: stalled.length === 1 ? `#work/workflows?wf=${encodeURIComponent(worst.workflow_id)}` : '#work/workflows',
      ...(stalled.length === 1 ? { linkLabel: 'workflow' } : {}),
    })
  }

  if (unjudged.length > 0) {
    // NOT folded into the note. A workflow whose rollup did not complete is a
    // workflow this check did not look at, and a short problem list over rows
    // nobody examined is the reassuring silence this whole panel is built
    // against.
    const why = page.rollup_report?.step_read_budget_exhausted
      ? 'the route ran out of step reads for this page, so their state was never established'
      : `their rollup came back incomplete (${unjudged[0]?.rollup?.reason ?? 'no reason given'})`
    problems.push({
      severity: 'warn',
      n: unjudged.length,
      headline: `${unjudged.length} workflow${unjudged.length === 1 ? ' could' : 's could'} not be judged`,
      detail: `${unjudged.map((w) => w.workflow_id).slice(0, 3).join(', ')}${unjudged.length > 3 ? ', …' : ''} — ${why}. Whether ${unjudged.length === 1 ? 'it is' : 'they are'} moving is unknown, not fine.`,
      href: '#work/workflows',
    })
  }

  // THE RECONCILER'S CHECK (#616). Everything above judges the workflows on
  // THIS page by their `updated_at`; the reconciler judges every live
  // workflow on its pass, step by step -- a step parked on parents that
  // finished, one not started past its budget, a stored state that drifted.
  const reconciler = stalledProblem(
    (page as WorkflowPage & { stalled_workflows?: StalledWorkflows }).stalled_workflows,
    now,
  )
  if (reconciler.problem) problems.push(reconciler.problem)

  if (problems.length > 0) return { label, status: 'found', problems }

  const scope = page.rollup_report?.truncated
    ? `the first ${rows.length} workflows of more`
    : `all ${rows.length} workflow${rows.length === 1 ? '' : 's'}`
  return {
    label,
    status: 'clear',
    note:
      (live.length === 0
        ? `${scope} read, none unfinished`
        : `${live.length} unfinished of ${scope}${cancelling.length > 0 ? ` (${cancelling.length} cancelling)` : ''}, each holding a step in flight or advanced within ${WORKFLOW_STALL_MINUTES} minutes`) +
      reconciler.note,
  }
}

/** "wf_x step review no progress, 50m 0s" -- one stalled row, in words. */
function describeStall(w: StalledWorkflow): string {
  const age = w.age_seconds === null ? '' : `, ${formatDuration(w.age_seconds * 1000)}`
  return `${w.workflow_id}${w.step_id ? ` step ${w.step_id}` : ''} ${w.kind.replace(/_/g, ' ')}${age}`
}

/**
 * The reconciler's stalled workflows as at most one problem, and the clause
 * the clear note carries otherwise.
 *
 * A RESULT THAT IS NOT KNOWN IS A PROBLEM. `count: null` is the API saying the
 * check was blind or never ran, and folding it into "clear" would be this
 * panel reporting a reassurance nobody established. A field the API does not
 * send at all (a deployment older than #616) is said in the note.
 *
 * REPAIRED ROWS ARE NOT RAISED. The reconciler promoted the step or wrote the
 * state; nothing is left for a reader to do, so they are counted in the note.
 */
function stalledProblem(
  report: StalledWorkflows | undefined,
  now: number,
): { problem?: Problem; note: string } {
  if (report === undefined) return { note: ' · the API reported no reconciler stall check' }
  if (report.check_error !== null || report.count === null) {
    return {
      note: '',
      problem: {
        severity: 'warn',
        n: 1,
        headline: "The reconciler's workflow stall check is not known",
        detail: `${report.check_error ?? 'The API gave no count.'} Whether a workflow has stopped is unknown, not fine.`,
        href: '#work/workflows',
      },
    }
  }
  const open = report.workflows.filter((w) => !w.repaired)
  const fixed = report.workflows.length - open.length
  const note = fixed > 0 ? ` · the reconciler repaired ${fixed}` : ' · the reconciler found none stalled'
  if (open.length === 0) return { note }
  const worst = open[0] as StalledWorkflow
  const shown = open.slice(0, 3).map((w) => `${describeStall(w)}: ${w.reason}`)
  const more = open.length > shown.length || report.truncated ? '; and more' : ''
  const when = report.pass_at ? ` Checked ${timeAgo(report.pass_at, now)}.` : ''
  return {
    note,
    problem: {
      severity: open.some((w) => w.severity === 'bad') ? 'bad' : 'warn',
      n: open.length,
      headline: `${open.length} workflow finding${open.length === 1 ? '' : 's'} from the reconciler, worst ${describeStall(worst)}`,
      detail: `${shown.join('; ')}${more}.${when}`,
      href: '#work/workflows',
    },
  }
}

/** "2 steps parked, 1 ready, 3 done of 6" -- from `rollup.counts` alone. */
function describeSteps(w: Workflow): string {
  const parked = countOf(w, 'PARKED')
  const ready = countOf(w, 'READY')
  const unstarted = countOf(w, 'unstarted')
  const done = countOf(w, 'SUCCEEDED')
  const total = Object.values(w.rollup?.counts ?? {}).reduce((a, b) => a + b, 0)
  const parts = [
    `${done} of ${total} step${total === 1 ? '' : 's'} finished`,
    `${parked} parked`,
    `${ready} ready`,
  ]
  if (unstarted > 0) parts.push(`${unstarted} never started`)
  return parts.join(', ')
}

// ---------------------------------------------------------------------------
// Parked work: durable, free, and invisible until now
// ---------------------------------------------------------------------------

/**
 * Parked tasks, grouped by WHAT WOULD END THE PARK.
 *
 * THIS IS NOT A CAPACITY PROBLEM AND THE COPY MUST NEVER SAY IT IS. CONTRACT
 * invariant 1 gives infrastructure demand to LEASED, DISPATCHED, STARTING and
 * RUNNING only; a PARKED task holds no lease, occupies no pool slot and costs
 * nothing. Parking is how this platform declines to pay for a wait. An operator
 * sent to raise a ceiling because their work is parked would raise the wrong
 * number and the work would still not move.
 *
 * DEPENDENCY_INCOMPLETE RAISES NOTHING, and that is the load-bearing omission.
 * It is the ordinary state of a workflow step waiting its turn: a three-step
 * chain has two steps parked like that for its whole life with nothing wrong.
 * Raising it would light this panel for every healthy workflow on the platform
 * and teach a reader to stop looking. It becomes interesting only when the
 * workflow holding it has stopped, and `workflowCheck` asks that once, of the
 * workflow, instead of once per step.
 */
function parkedCheck(tasks: Result<TaskPage>, now: number): Check {
  const label = 'Parked work'
  if (tasks.status === 'loading') return { label, status: 'reading' }
  if (tasks.status === 'error') return { label, status: 'blind', ...blindness(tasks.error) }
  if (tasks.status === 'empty') {
    return { label, status: 'clear', note: 'no agent has ever been submitted' }
  }

  const rows = tasks.data.tasks
  const parked = rows.filter((t) => t.state === 'PARKED')
  if (parked.length === 0) {
    return {
      label,
      status: 'clear',
      note: `none of the ${rows.length} most recently created agents is parked`,
    }
  }

  const reasonOf = (t: Task) => (t.park_reason === null ? '' : String(t.park_reason))
  const person = parked.filter((t) => PARK_NEEDS_A_PERSON.has(reasonOf(t)))
  const clock = parked.filter((t) => PARK_CLEARS_ITSELF.has(reasonOf(t)))
  const sequencing = parked.filter((t) => PARK_WAITS_ON_A_STEP.has(reasonOf(t)))
  const unclassified = parked.filter(
    (t) =>
      !PARK_NEEDS_A_PERSON.has(reasonOf(t)) &&
      !PARK_CLEARS_ITSELF.has(reasonOf(t)) &&
      !PARK_WAITS_ON_A_STEP.has(reasonOf(t)),
  )

  // EVERY PARKED ITEM OPENS THE WAITING TAB, BY ADDRESS (OV-10): the list's
  // tab is part of the hash now, so the link lands where it says rather than
  // on whichever tab has rows, and its word names the destination.
  const problems: Problem[] = []
  if (person.length > 0) {
    problems.push({
      severity: 'bad',
      n: person.length,
      headline: `${person.length} parked ${unitFor(person)} will not resume without a person`,
      detail: `${whyEach(person)}. No timer ends any of these. They hold no capacity while they wait, so nothing is being spent -- the work is simply not happening.`,
      href: '#work/running/waiting',
      linkLabel: 'waiting agents',
    })
  }
  if (clock.length > 0) {
    problems.push({
      severity: 'warn',
      n: clock.length,
      headline: `${clock.length} parked ${unitFor(clock)} waiting on a clock`,
      detail: `${whyEach(clock)}. ${soonest(clock, now)} Parked work holds no capacity, so this costs nothing while it waits.`,
      href: '#work/running/waiting',
      linkLabel: 'waiting agents',
    })
  }
  if (unclassified.length > 0) {
    problems.push({
      severity: 'warn',
      n: unclassified.length,
      headline: `${unclassified.length} parked ${unitFor(unclassified)} with no reason this build understands`,
      detail: `${whyEach(unclassified)}. Either the platform grew a park reason newer than this bundle, or the agent was parked without one recorded. Whether it will resume by itself is unknown.`,
      href: '#work/running/waiting',
      linkLabel: 'waiting agents',
    })
  }

  if (problems.length > 0) return { label, status: 'found', problems }
  return {
    label,
    status: 'clear',
    note: `${sequencing.length} parked ${unitFor(sequencing)} waiting on an earlier step, which is what a queued step looks like and costs nothing; nothing else is parked`,
  }
}

/**
 * "workflow step" or "agent", because a parked agent need not belong to a
 * workflow and calling a standalone agent a step is a lie the reader will chase.
 */
function unitFor(rows: Task[]): string {
  const steps = rows.filter((t) => t.workflow_id !== null).length
  if (steps === rows.length && rows.length > 0) {
    return rows.length === 1 ? 'workflow step' : 'workflow steps'
  }
  return rows.length === 1 ? 'agent' : 'agents'
}

/**
 * "PROVIDER_QUOTA_EXHAUSTED ×2 — The provider quota is spent...", and then
 * EACH ITEM'S OWN REASON (WAIT-UI-LISTS, #1782): "CI_PENDING ×2 — ... —
 * CI #1779: 5 pending (merge-a); CI #1780: 1 failed (merge-b)". During the
 * 2026-10-20/21 CI stall the card counted every merge step under one
 * sentence, whether its checks were queued, failing or done; WAIT-API's
 * `waiting.summary` on each row says which, cut by `waitingShort` as the
 * Agents list cuts it. A row whose API sends no `waiting` adds nothing, so
 * an older API's card reads as it did.
 */
function whyEach(rows: Task[]): string {
  const counts = new Map<string, { n: number; items: string[] }>()
  for (const t of rows) {
    const key = t.park_reason === null ? 'no reason recorded' : String(t.park_reason)
    const g = counts.get(key) ?? { n: 0, items: [] }
    g.n += 1
    const short = t.waiting ? waitingShort(t.waiting) : null
    if (short !== null) g.items.push(`${short} (${t.step_id ?? t.id})`)
    counts.set(key, g)
  }
  return [...counts.entries()]
    .sort((a, b) => b[1].n - a[1].n)
    // NO TRAILING STOP (live QA OV-8, 2026-10-11): `reasonCopy` is a
    // sentence and ends in one, and every caller follows this with ". ",
    // so the parked item read "Holds no capacity.. The first becomes...".
    .map(
      ([reason, { n, items }]) =>
        `${reason}${n > 1 ? ` ×${n}` : ''} — ${reasonCopy(reason).replace(/\.+$/, '')}` +
        (items.length > 0 ? ` — ${items.join('; ')}` : ''),
    )
    .join(' · ')
}

/** When the first of these becomes eligible again, if any of them says. */
function soonest(rows: Task[], now: number): string {
  const times = rows
    .map((t) => (t.next_eligible_at ? new Date(t.next_eligible_at).getTime() : NaN))
    .filter((t) => Number.isFinite(t))
  if (times.length === 0) {
    return 'None of them records when it becomes eligible again, so the wait is open-ended.'
  }
  const first = Math.min(...times)
  return `The first becomes eligible in ${clearsIn(new Date(first).toISOString(), now)}.`
}
