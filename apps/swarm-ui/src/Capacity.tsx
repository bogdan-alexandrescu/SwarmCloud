import { useEffect } from 'react'
import { loadCapacity, loadLeases, type ResourceClasses } from './api'
import { classUnits, useResourceClasses } from './Blockers'
import { isPaused, type Result } from './fetch'
import type { TopicId } from './help'
import { HelpLinks } from './HelpCard'
import { POOLS_POLL_MS, poolHref, useLinkedPool } from './capacityPoll'
import { leaseCoverage } from './Holders'
import { Screen } from './Shell'
import './styles/capacity.css'
import { Segmented, ToneMark, UsageTrack } from './components'
import {
  FAMILY_TITLE,
  POOL_FAMILY_ORDER,
  POOL_LIMIT_UNSET,
  blockerCeiling,
  ceilingCopy,
  needsAPerson,
  overCeiling,
  poolGroup,
  poolKind,
  poolLabel,
  poolScope,
  setBy,
  type Capacity,
  type LeasePage,
  type Pool,
} from './types'

/**
 * Screen B -- the capacity board.
 *
 * Where an operator goes when something is not being admitted and they need to
 * know which ceiling is the binding one.
 *
 * NOTE ON THE SPEC: docs/web-ui/02-cluster-state.md defines this screen in
 * §4.1 and then stops -- the source text is cut off mid-sentence and §§4.2-9
 * were never written. So the grouping order and the scope declarations below
 * come from the spec; the columns come from the five data traps in §1.1 and
 * from what pool_to_api actually sends. Anything beyond that is not sourced
 * and is not pretended to be.
 *
 * ---------------------------------------------------------------------------
 * THE PER-PROFILE "COULD START / HELD BACK BY" PANEL IS GONE (owner's
 * decision, 2026-10-01, with the rebrand). It answered "how many more of each
 * runner profile could start" from the CALLING tenant's pools; the Profiles
 * screen's matrix (`ProfileMatrix`) answers the same question per profile and
 * pool, so this screen is the ceilings alone.
 *
 *   THE SCOPE -- "these are YOUR tenant's pools, not the platform's" -- is
 *   declared on every row of every family, under the pool's name, which is
 *   where two figures of different scope could otherwise be compared (Trap E).
 *   It was a column of its own until the #503 audit; the picked frame has
 *   none, and the column wrapped the names beside it. The families used to
 *   carry one note each, read off their FIRST row, so an admin's Tenants
 *   family said "this tenant" above four tenants' pools (CP-2, #85).
 *
 * The arguments are at `#help/pools-all-at-once`, `#help/tenant-scope` and
 * `#help/absent-vs-zero`, which is where they were already written down.
 */
export function CapacityScreen() {
  return (
    <Screen
      // "Pools", not "Capacity". The tab that leads here says Pools, the
      // section says Pools, the table below is a list of pools -- and a
      // heading reading "Capacity" made the one screen look like two places,
      // so a runbook step saying "go to Pools" named nothing on the screen it
      // landed you on. The nav's own rule (App.tsx: sections are named after
      // OBJECTS, not the question they answer) decides which side gives way.
      title="Pools"
      load={loadPoolsBoard}
      // Decided 2026-10-01 (#117): Pools re-reads every 30s, and `Screen`
      // pauses the timer while the tab is hidden.
      pollMs={POOLS_POLL_MS}
      summary={(d) => <PoolsSummary capacity={d} />}
      /* A REAL ZERO. Pools are created at provisioning time, so an environment
         with none has not been fully applied -- which is an absence the mark
         names in two words instead of two clauses.

         THE MARK IS `Screen`'s, drawn in the heading by the shared empty state
         (#145). A hand-drawn `.ctl-mark is-zero` span here said `real zero` a
         second time, with no sentence behind it. */
      empty={{
        heading: 'No pools exist',
        body: <>none provisioned in this environment</>,
      }}
    >
      {(d) => (
        <>
          <CapSeg view="ceilings" />
          {/* CEILINGS, AS DECIDED ON 2026-10-01 (capacity.html, Variant 1, #125):
              one table per family, with a "Needs action" group ahead of them
              holding every pool a person has to act on (`needsAction`). A
              pool appears once: the top group takes it out of its family.
              The Cards view was the alternative set aside and is gone. */}
          <CeilingTables pools={d.pools} viewer={viewerOf(d)} holders={d.holders} />

          <HelpLinks topics={CAPACITY_TOPICS} />
        </>
      )}
    </Screen>
  )
}

/**
 * THE IN-PAGE VIEW STRIP (capacity.html C1, frames 0 and 3): Pools is one page
 * with two views, Ceilings and By runner profile, and the strip under the
 * heading is how a reader on one reaches the other without the panel. Both
 * views head the page "Pools"; the strip and the crumb name the view.
 *
 * Links, not a tablist: each view is its own address (`/capacity/pools`,
 * `/capacity/profiles`), so the strip is navigation and the current one is
 * `aria-current="page"`. A local stand-in for components.html A's segmented
 * control, named for this section until the shared one lands.
 */
export function CapSeg({ view }: { view: 'ceilings' | 'profiles' }) {
  // The canonical segmented control's link form (components.html A).
  return (
    <div className="cap-seg">
      <Segmented
        label="Pools views"
        value={view}
        options={[
          // JSX labels: test_nav_headings_agree.py reads `>By runner profile<`
          // out of this file as the in-page strip's text.
          { key: 'ceilings', label: <>Ceilings</>, href: '#capacity/pools' },
          { key: 'profiles', label: <>By runner profile</>, href: '#capacity/profiles' },
        ]}
      />
    </div>
  )
}

/**
 * How many unreleased leases name each pool, or why that was not counted.
 *
 * The frame's `4 holders` link (capacity.html C1) is a count of LEASES, which
 * the capacity read does not carry: a pool counts units, and 4 units may be
 * one large lease or four standard ones. So it comes from the lease read
 * Holders makes, and it is only a count when that read can vouch for every
 * live lease (`leaseCoverage` complete). A cut window, an admin-only refusal
 * or a failed read is not a count of zero: the link then says `holders`, with
 * the reason in its title.
 */
export type HolderCounts = { byPool: ReadonlyMap<string, number> } | { unknown: string }

/** The Ceilings read: the capacity board, and the holder counts beside it. */
export type PoolsBoard = Capacity & { holders: HolderCounts }

export function holderCounts(leases: Result<LeasePage>): HolderCounts {
  if (leases.status === 'error') return { unknown: `Holders were not counted: ${leases.error.message}` }
  if (leases.status === 'loading') return { unknown: 'Holders were not counted: the lease read did not complete.' }
  // An older page kept from before a failed re-read is not a count of now.
  if (leases.status === 'stale') return { unknown: `Holders were not counted: ${leases.error.message}` }
  if (leases.status === 'empty') return { byPool: new Map() }
  const page = leases.data
  if (leaseCoverage(page).kind !== 'complete') {
    return { unknown: 'Holders were not counted: the lease read did not return every live lease.' }
  }
  const byPool = new Map<string, number>()
  for (const l of page.leases) {
    if (!Array.isArray(l.pools)) continue
    for (const p of l.pools) byPool.set(p, (byPool.get(p) ?? 0) + 1)
  }
  return { byPool }
}

/**
 * The capacity read and the lease read, together, on Pools' one cadence.
 *
 * The capacity read decides the page: its failure is the page's failure, and
 * its empty is the page's empty. The lease read only ever decides the holders
 * count -- it is admin-only, so for most readers it answers 403 and the links
 * simply carry no count.
 */
export async function loadPoolsBoard(): Promise<Result<PoolsBoard>> {
  const [capacity, leases] = await Promise.all([loadCapacity(), loadLeases()])
  if (capacity.status !== 'ok' && capacity.status !== 'stale') return capacity
  return { ...capacity, data: { ...capacity.data, holders: holderCounts(leases) } }
}

/**
 * The line under the heading (capacity.html C1: "9 families · 2 full · 1
 * lowered · tenant eng"). Every figure is a count of rows this read returned.
 * `lowered` is a pool whose effective ceiling sits under its configured hard
 * limit -- AIMD or a provider quota took it down -- which is the row a reader
 * otherwise has to find by its `/30` suffix.
 */
function PoolsSummary({ capacity }: { capacity: Capacity }) {
  const pools = capacity.pools
  const families = new Set(pools.map((p) => poolKind(p.name))).size
  const full = pools.filter((p) => !overCeiling(p) && p.effective_limit !== null && p.effective_limit > 0 && p.active >= p.effective_limit).length
  const lowered = pools.filter((p) => p.effective_limit !== null && p.hard_limit !== null && p.effective_limit < p.hard_limit).length
  const paused = pools.filter(isPaused).length
  const over = pools.filter(overCeiling).length
  const viewer = viewerOf(capacity)
  return (
    <>
      {pools.length} pools in {families} famil{families === 1 ? 'y' : 'ies'}
      {full > 0 && ` · ${full} full`}
      {lowered > 0 && ` · ${lowered} lowered`}
      {paused > 0 && ` · ${paused} paused`}
      {over > 0 && ` · ${over} over ceiling`}
      {viewer !== undefined && ` · tenant ${viewer}`}
    </>
  )
}

/**
 * The tenant this read is scoped to: whose pool a row's `this tenant` means.
 *
 * `/v1/capacity` names the tenant itself -- service.capacity() has always
 * sent `tenant_id`, and this file used to guess it back out of the pool
 * names. The regex stays as the fallback for an API that predates the
 * field, because a blank here silently drops the scope from every figure.
 */
function viewerOf(capacity: Capacity): string | undefined {
  return (
    capacity.tenant_id ??
    capacity.pools
      .map((p) => /(?:^|:)tenant:([^:]+)/.exec(p.name)?.[1])
      .find((t): t is string => Boolean(t))
  )
}

/**
 * A pool's scope, in words (CP-2, #85): `platform`, `this tenant`, or
 * `tenant X` for another tenant's pool.
 *
 * PER ROW, BECAUSE A FAMILY IS NOT ONE SCOPE. An admin sees every tenant's
 * pools (service.py filters them for non-admins only), so the Tenants family
 * holds several tenants and the Providers family holds both the shared
 * `provider:X` pool and every tenant's `provider:X:tenant:Y` slice. The note
 * each family used to carry was read off its first row and was wrong for the
 * rest. The owner's decision is a scope on every row (under the name since
 * #503), and the family note is gone.
 */
function scopeWord(name: string, viewer: string | undefined): string {
  if (poolScope(name) === 'platform') return 'platform'
  // `tenant:Y` or `provider:X:tenant:Y`: the tenant is the last segment.
  const owner = /(?:^|:)tenant:([^:]+)$/.exec(name)?.[1]
  if (owner === undefined || owner === viewer) return 'this tenant'
  return `tenant ${owner}`
}

/**
 * True when a pool belongs in "Needs action" (owner decision 2026-10-01):
 * `poolGroup` (types.ts) files it there by `blockerGroup`, the rule the
 * Agents list's Waiting split uses -- paused, set to zero by a person, no
 * limit set, over its ceiling (drift), or below one task of its class. A `resource:` pool is weighed
 * against its class's `units` from the catalogue; no other pool has one class.
 *
 * It was "Needs a look", decided by this screen's own classification, which
 * also took in a pool that was merely full or lowered by AIMD: two screens,
 * two rules, and a busy pool filed beside one no wait will reopen. Those keep
 * their marks in their family's table.
 */
export function needsAction(pool: Pool, classes: ResourceClasses | null): boolean {
  const units = poolKind(pool.name) === 'resource' ? classUnits(classes, pool.name.slice('resource:'.length)) : null
  return poolGroup(pool, units) === 'needs_action'
}

/**
 * The Ceilings tab: "Needs action" first, then one table per family, each
 * holding only the pools the top group did not take.
 */
function CeilingTables({
  pools,
  viewer,
  holders,
}: {
  pools: Pool[]
  viewer: string | undefined
  holders: HolderCounts
}) {
  const classes = useResourceClasses()
  const act = pools.filter((p) => needsAction(p, classes)).sort(byUrgency)
  const rest = pools.filter((p) => !needsAction(p, classes))
  // THE ROW A LINK NAMED (#128): Provider quota's Feeds pool lands here, on
  // its pool's row, outlined the way Pool limits outlines its linked row.
  const target = useLinkedPool()
  const landed = target !== null && pools.some((p) => p.name === target)
  useEffect(() => {
    if (!landed) return
    const row = document.getElementById(poolRowId(target))
    // jsdom implements no `scrollIntoView`, hence the guard.
    if (row !== null && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'center' })
  }, [landed, target])
  return (
    <div className="cap-families">
      {act.length > 0 && (
        <Family
          title={`Needs action · ${act.length}`}
          className="cap-needs"
          pools={act}
          viewer={viewer}
          target={target}
          holders={holders}
        />
      )}
      {POOL_FAMILY_ORDER.map((kind) => {
        const family = rest.filter((p) => poolKind(p.name) === kind).sort(byUrgency)
        if (family.length === 0) return null
        return (
          <Family key={kind} title={FAMILY_TITLE[kind]} pools={family} viewer={viewer} target={target} holders={holders} />
        )
      })}
    </div>
  )
}

/** The id of a pool's row on Pools, which a `?pool=` link scrolls to. */
function poolRowId(pool: string): string {
  return `pool-${pool}`
}

/**
 * ABNORMAL ROWS FIRST, THEN THE FULLEST (#125). A family used to be sorted by
 * name, so at 3am the one full pool among twenty sat wherever its name put
 * it. Now a family reads worst first: over ceiling, paused, no limit set or
 * limit 0, full -- the order `classifyPool` draws its marks in -- then every
 * other row by used/ceiling, highest first, and by name only to break a tie.
 */
export function byUrgency(a: Pool, b: Pool): number {
  const rank = (p: Pool): number => {
    const limit = p.effective_limit
    if (overCeiling(p)) return 0
    if (isPaused(p)) return 1
    if (limit === null || limit === 0) return 2
    if (p.active >= limit) return 3
    return 4
  }
  const used = (p: Pool): number => (p.effective_limit !== null && p.effective_limit > 0 ? p.active / p.effective_limit : 0)
  return rank(a) - rank(b) || used(b) - used(a) || a.name.localeCompare(b.name)
}

function Family({
  title,
  className,
  pools,
  viewer,
  target,
  holders,
}: {
  title: string
  className?: string
  pools: Pool[]
  /** The tenant this read is scoped to, for the rows' `this tenant`. */
  viewer: string | undefined
  /** The pool a `?pool=` link named, or null. */
  target: string | null
  holders: HolderCounts
}) {
  // Trap E: a number may only sit beside another number of the same scope, so
  // the scope is declared rather than left to be inferred -- ON EVERY ROW
  // (CP-2, #85). This head used to carry one `.ctl-card-note` computed from
  // the family's first row, and a family is not one scope: Tenants holds
  // every tenant's pool for an admin, and Providers holds the shared pool
  // beside per-tenant slices. The note is removed, as the owner decided.
  return (
    <section className={className === undefined ? 'ctl-card' : `ctl-card ${className}`}>
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">{title}</h2>
      </div>
      <div className="ctl-card-body is-flush">
        <PoolTable pools={pools} viewer={viewer} target={target} holders={holders} />
      </div>
    </section>
  )
}

/*
 * "units", never "agents" (Trap A): admission increments by the resource
 * class's units (1, 2 or 4), so 8 leased may be two large agents or eight
 * standard ones, and the caveat is in the column name, where it cannot be
 * scrolled away from the figures it governs (CP-24). "Leased" because
 * concurrency counts from LEASED (invariant 3), not from RUNNING.
 * The frame draws the one word; CP-24 is the owner's earlier ruling that the
 * unit is on the head, so the head keeps it -- on one line, in a column wide
 * enough for it (#503: the head used to wrap onto two lines).
 */
const LEASED = 'Leased (units)'
const CEILING = 'Ceiling (units)'
const UNITS_TITLE = 'Weighted units, not agents: a task holds its resource class’s units in every pool it clears.'

/**
 * ONE COLGROUP FOR EVERY FAMILY TABLE (#503). The tables are fixed-layout so
 * that each column starts at the same x in every family (CP-18); the widths
 * are set here on purpose, in styles/capacity.css, so the Use track and its
 * figure have room and nothing paints into the State column beside it.
 */
function PoolCols() {
  return (
    <colgroup>
      <col className="cap-c-pool" />
      <col className="cap-c-num" />
      <col className="cap-c-num" />
      <col className="cap-c-use" />
      <col className="cap-c-state" />
      <col className="cap-c-by" />
      <col className="cap-c-links" />
    </colgroup>
  )
}

function PoolTable({
  pools,
  viewer,
  target,
  holders,
}: {
  pools: Pool[]
  viewer: string | undefined
  target: string | null
  holders: HolderCounts
}) {
  return (
    <div className="ctl-table is-scroll cap-pools">
      <table role="table">
        <PoolCols />
        <thead role="rowgroup">
          <tr role="row">
            <th role="columnheader" scope="col">Pool</th>
            <th role="columnheader" scope="col" className="is-num" title={UNITS_TITLE}>{LEASED}</th>
            {/* THE CEILING IS UNITS TOO, and it says so (CP-24). */}
            <th role="columnheader" scope="col" className="is-num" title={UNITS_TITLE}>{CEILING}</th>
            <th role="columnheader" scope="col">Use</th>
            <th role="columnheader" scope="col">State</th>
            <th role="columnheader" scope="col">Set by</th>
            <th role="columnheader" scope="col" aria-label="Links" />
          </tr>
        </thead>
        <tbody role="rowgroup">
          {pools.map((p) => (
            <PoolRow
              key={p.name}
              pool={p}
              scope={scopeWord(p.name, viewer)}
              target={p.name === target}
              holders={holders}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The holders link (capacity.html C1: `4 holders · limit`). The count is the
 * leases that name this pool, when `holderCounts` could vouch for every live
 * lease; otherwise the word alone, and the title says why -- never a 0 for a
 * count nobody made.
 */
function HoldersLink({ pool, holders }: { pool: string; holders: HolderCounts }) {
  const href = poolHref('capacity/holders', pool)
  if ('unknown' in holders) {
    return (
      <a className="ctl-link" href={href} title={holders.unknown}>
        holders
      </a>
    )
  }
  const n = holders.byPool.get(pool) ?? 0
  return (
    <a className="ctl-link" href={href} title="Unreleased leases that name this pool">
      {n} holder{n === 1 ? '' : 's'}
    </a>
  )
}

function PoolRow({
  pool,
  scope,
  target,
  holders,
}: {
  pool: Pool
  scope: string
  target: boolean
  holders: HolderCounts
}) {
  const marks = classifyPool(pool)
  const by = setBy(pool)
  const limit = pool.effective_limit
  // UNROUNDED for the track: 1 unit of 300 is 0.3%, and rounding it to 0
  // would hand the track a measured zero for a pool that holds something. A
  // ceiling of 0 leaves no room at all, so the track is full in the
  // classification's tone (#159 review of CP-14), never the not-measured hatch.
  // No limit set (#374): the ceiling was never read, so the track is the
  // not-measured hatch (null) and the cell says so, never `no room` or 0%.
  const pct = limit === null ? null : limit > 0 ? Math.min(100, (pool.active / limit) * 100) : 100
  const shown = limit === null ? 'no limit set' : limit > 0 ? `${Math.round((pool.active / limit) * 100)}%` : 'no room'

  return (
    <tr
      role="row"
      id={poolRowId(pool.name)}
      className={[marks.row, target ? 'is-target' : ''].filter(Boolean).join(' ') || undefined}
    >
      <th role="rowheader" scope="row" title={pool.name}>
        {/* THE NAME IS THE WAY TO WHO HOLDS IT (#125): Holders, filtered to
            the leases that name this pool. */}
        <a className="ctl-link" href={poolHref('capacity/holders', pool.name)}>
          {poolLabel(pool.name)}
        </a>
        {/* THE SCOPE, PER ROW (CP-2), under the name it qualifies: it says
            which other figures on this screen a row's numbers may be compared
            with. It was a column of its own, which the frame does not have
            and which wrapped the names beside it (#503). */}
        <span className="cap-sub">
          <span className="ctl-sub">{pool.name}</span> · <span className="cap-scope">{scope}</span>
        </span>
      </th>
      <td role="cell" data-label={LEASED} className="is-num">{pool.active}</td>
      <td role="cell" data-label={CEILING} className="is-num">
        {limit === null || pool.hard_limit === null ? (
          // No limit set (#374): unknown, so no number -- not even a 0.
          <i className="ctl-em" title={by.detail}>no limit set</i>
        ) : (
          limit
        )}
        {limit !== null && pool.hard_limit !== null && limit < pool.hard_limit && (
          <span className="cap-was" title={`Configured hard limit is ${pool.hard_limit}`}>
            /{pool.hard_limit}
          </span>
        )}
      </td>
      <td role="cell" data-label="Use">
        <span className="cap-use">
          <UsageTrack
            pct={pct}
            tone={marks.track}
            meter={
              limit === null
                ? { label: `${poolLabel(pool.name)}: ${pool.active} units leased, no limit set`, now: pool.active, max: 0 }
                : {
                    label: `${poolLabel(pool.name)}: ${pool.active} of ${limit} units leased`,
                    now: pool.active,
                    max: limit,
                  }
            }
          />
          <span className="cap-use-pct">{shown}</span>
        </span>
      </td>
      <td role="cell" data-label="State">
        <span className="cap-marks">
          <PoolMarks marks={marks} />
        </span>
      </td>
      {/* SET BY ONLY WHEN IT IS NOT "configured" (#125): the configured limit
          is what applies to almost every row, and a column repeating it hid
          the two rows where AIMD or quota had lowered it. */}
      <td role="cell" data-label="Set by" title={by.detail}>
        {by.term === 'configured' ? '' : by.term}
      </td>
      <td role="cell" data-label="Links" className="cap-links">
        {/* Both name the pool (#125): Holders filtered to it, and its own row
            on Pool limits (#134), where an admin edits it and anyone else
            reads it with Edit locked. */}
        <HoldersLink pool={pool.name} holders={holders} />
        {' · '}
        <a className="ctl-link" href={poolHref('admin/limits', pool.name)}>limit</a>
      </td>
    </tr>
  )
}

/** What a pool's state is drawn as, in the table and on its card alike. */
interface PoolClass {
  /** The marks, in the order they are drawn. Never empty: healthy is `ok`. */
  chips: { cls: string; word: string; title: string }[]
  /** The row's tone class (and its legacy word), or none. */
  row: string | undefined
  /** The card track's fill tone, from the same classification. */
  track: 'is-bad' | 'is-paused' | 'is-warn' | undefined
}

/**
 * ONE CLASSIFICATION FOR A POOL, READ BY THE TABLE AND THE CARDS ALIKE (CP-14,
 * #85): paused, over ceiling, limit 0, full, ok.
 *
 * The two views used to decide for themselves, and disagreed: the table drew
 * a healthy pool `ok` and the Cards view drew nothing; the card coloured a
 * full pool's track `is-bad`, and past 80% `is-warn`, while its chip and the
 * table's row said warn. Now the chip, the row and the track are one answer:
 * a full pool is warn in all three.
 *
 * `LIMIT 0` IS NEW ON POOLS, and it is the mark Held back by and Profile
 * headroom draw for the same pool: a pool that is not paused with a ceiling of
 * 0 admits nothing, and it was drawn `ok` because nothing was in use.
 * `blockerCeiling` decides whose zero it is -- a pool only a person writes is
 * `set-to-zero` (paused tone: a person has to act), a provider pool zeroed by
 * its quota is `zero` (bad tone) -- so the three screens cannot disagree.
 *
 * `paused` and `over ceiling` can both be true, and both are drawn: the drift
 * is not hidden by the pause. The row and the track take the worse of them.
 */
function classifyPool(pool: Pool): PoolClass {
  const paused = isPaused(pool)
  const over = overCeiling(pool)
  const limit = pool.effective_limit
  // NO LIMIT SET (#374) is neither `limit 0` nor `ok`: the ceiling was never
  // read, admission refuses on it, and a person has to set one. The verdict is
  // the blockers' own `limit-unset`, so the screens cannot disagree.
  const unset = !paused && limit === null
  const zero = !paused && !over && limit === 0
  const full = !over && limit !== null && limit > 0 && pool.active >= limit
  const ceiling = zero
    ? blockerCeiling({ reason: '', pool: pool.name, limit: 0 })
    : unset
      ? blockerCeiling({ reason: POOL_LIMIT_UNSET, pool: pool.name, limit: null })
      : null
  const zeroTone = ceiling !== null && needsAPerson(ceiling) ? 'is-paused' : 'is-bad'

  const chips: PoolClass['chips'] = []
  if (paused) {
    chips.push({ cls: 'is-paused', word: 'paused', title: 'An operator paused this pool. It admits nothing until resumed.' })
  }
  if (over) {
    chips.push({
      cls: 'is-bad',
      word: 'over ceiling',
      title: `${pool.active} units are held against a ceiling of ${limit}. Admission cannot produce this, so it is drift: a limit lowered under running work, or a slot never released. 'make pool-check' finds these.`,
    })
  }
  if (unset) {
    chips.push({
      cls: zeroTone,
      word: 'no limit set',
      title:
        ceilingCopy({ reason: POOL_LIMIT_UNSET, pool: pool.name, limit: null, active: pool.active }, 'This pool') ??
        'This pool has no limit set.',
    })
  }
  if (zero) {
    chips.push({
      cls: zeroTone,
      word: 'limit 0',
      title: ceilingCopy({ reason: '', pool: pool.name, limit: 0, active: pool.active }, 'This pool') ?? 'This pool is at limit 0.',
    })
  }
  if (full) {
    chips.push({ cls: 'is-warn', word: 'full', title: `At its ceiling: ${pool.active} of ${limit} units in use.` })
  }
  // §6.6 AND THE CP-14 RULING: a healthy pool gets the ok mark -- the filled
  // disc and the word -- and the mark is a text grey, not a hue (`--text-faint`
  // since CH-17 set the chip primitive's grey). Healthy carries no hue; a quiet
  // grey screen is what a healthy platform looks like.
  if (chips.length === 0) chips.push({ cls: 'is-ok', word: 'ok', title: 'Read, capped, not paused, and not at its ceiling.' })

  const track = over ? 'is-bad' : paused ? 'is-paused' : zero || unset ? zeroTone : full ? 'is-warn' : undefined
  const row =
    track === undefined
      ? undefined
      : over
        ? 'is-bad over'
        : paused
          ? 'is-paused paused'
          : zero
            ? `${zeroTone} limit-0`
            : unset
              ? `${zeroTone} limit-unset`
              : 'is-warn full'
  return { chips, row, track }
}

/**
 * The marks `classifyPool` decided, drawn once for the table and the cards.
 * The caller supplies the `.cap-marks` strip, so a card can lead it with the
 * pool's scope.
 */
function PoolMarks({ marks }: { marks: PoolClass }) {
  return (
    <>
      {marks.chips.map((c) => (
        <ToneMark key={c.word} tone={c.cls} title={c.title}>
          {c.word}
        </ToneMark>
      ))}
    </>
  )
}

/**
 * The legend this screen used to end on, as links.
 *
 * The middle entry carried "standard 1, browser 2, large 4" -- three platform
 * figures typed into a paragraph, which §5 of the prose migration table lists
 * as a restatement nothing checks. `units-not-agents` states the RULE and
 * names no figure, and the weights themselves are on the sizing table under
 * Runtimes, where they are read from the catalogue.
 */
const CAPACITY_TOPICS: readonly TopicId[] = [
  'units-not-agents',
  'pools-all-at-once',
  'pool-freshness',
  'tenant-scope',
  'absent-vs-zero',
]
