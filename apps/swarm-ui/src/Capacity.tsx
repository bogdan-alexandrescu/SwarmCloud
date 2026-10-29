import { Fragment, useEffect, useId } from 'react'
import { loadCapacity, loadLeases, type ResourceClasses } from './api'
import { classUnits, useResourceClasses } from './Blockers'
import { isPaused, type Result } from './fetch'
import type { TopicId } from './help'
import {
  POOLS_POLL_MS,
  capacityPoll,
  comparePools,
  loweredCeiling,
  nearCeiling,
  poolHref,
  poolProblemRank,
  poolViewer,
  tableMode,
  useLinkedPool,
  usePhoneTables,
} from './capacityPoll'
import { leaseCoverage } from './Holders'
import { Absent } from './primitives'
import { Screen, SkeletonRows, staleFoot, type ScreenReading } from './Shell'
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
import { AGE_TICK_MS, useNow } from './useNow'

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
      // THE PAGE'S ONE `?` OPENS THE SCREEN'S TOPICS (VQ-033, BGD 2026-10-26:
      // "Move the topics behind the page's one ?"). They were a `Reading this
      // screen:` footer under the families; the `?` takes the section
      // question's place by the title, so the page still draws one glyph.
      help={CAPACITY_TOPICS}
      load={loadPoolsView}
      // Decided 2026-10-01 (#117): Pools re-reads every 30s, and `Screen`
      // pauses the timer while the tab is hidden.
      pollMs={capacityPoll(POOLS_POLL_MS)}
      skeleton={<PoolsSkeleton view="ceilings" />}
      summary={(d) => d.pools === 'some' && <PoolsSummary capacity={d.board} />}
    >
      {(d, reading) => (
        <>
          <CapSeg view="ceilings" />
          {/* CEILINGS, AS DECIDED ON 2026-10-01 (capacity.html, Variant 1, #125):
              one table per family, with a "Needs a look" group ahead of them
              holding every pool that is full, lowered, paused or near its
              ceiling (`needsALook`, BGD 2026-10-26). A pool appears once: the
              top group takes it out of its family. The Cards view was the
              alternative set aside and is gone. */}
          {d.pools === 'none' ? (
            <NoPools reading={reading} />
          ) : (
            <CeilingTables pools={d.board.pools} viewer={poolViewer(d.board)} holders={d.board.holders} />
          )}
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
 * A Pools read whose empty answer stays an answer (visual QA V199, part of
 * #1427): `some` carries the board, `none` is a capacity read that succeeded
 * with an empty pool list.
 *
 * WHY NOT `Screen`'s `empty`. `Screen` draws its empty panel in place of
 * `children`, so it drew it without the view strip, and each view worded the
 * one absence its own way ("No pools exist" on Ceilings, "The capacity read
 * returned no pools" on By runner profile). Kept as data, the empty read
 * reaches `children`, which draws the strip and then `NoPools`, on both
 * views. `Screen` has no slot above its empty panel to put the strip in.
 */
export type PoolsView<T> = { pools: 'some'; board: T } | { pools: 'none' }

/** `load`, with its `empty` answer turned into `{ pools: 'none' }` and nothing else changed. */
export function keepNoPools<T>(load: () => Promise<Result<T>>): () => Promise<Result<PoolsView<T>>> {
  return async () => {
    const r = await load()
    if (r.status === 'ok' || r.status === 'stale') return { ...r, data: { pools: 'some', board: r.data } }
    if (r.status === 'empty') {
      return {
        status: 'ok',
        data: { pools: 'none' },
        fetchedAt: r.fetchedAt,
        ...(r.serverAt !== undefined ? { serverAt: r.serverAt } : {}),
      }
    }
    return r
  }
}

const loadPoolsView = keepNoPools(loadPoolsBoard)

/**
 * THE ONE EMPTY POOLS STATE, FOR BOTH VIEWS (V199). A REAL ZERO: pools are
 * created at provisioning time, so an environment with none has not been fully
 * applied. The `real zero` mark is `Absent`'s, in the heading (#145); the body
 * does not repeat it. Its foot says the read's age only once it is stale, as
 * `Screen`'s own empty panel does (#98).
 */
export function NoPools({ reading }: { reading: ScreenReading }) {
  const now = useNow(AGE_TICK_MS)
  return (
    <Absent
      kind="zero"
      heading="No pools exist"
      say="The capacity read succeeded and returned no pools."
      foot={staleFoot(reading.fetchedAt, now) ?? undefined}
    >
      The capacity read succeeded with an empty pool list: pools are created at provisioning time, and this environment has
      not been given any yet.
    </Absent>
  )
}

/**
 * WHAT A POOLS VIEW DRAWS BEFORE ITS FIRST READ LANDS (visual QA V063, the
 * capacity half): the count note's line, the view strip, then the generic
 * rows -- the order the loaded page draws them in -- so the strip is where it
 * will stay and nothing above the body moves when the data lands. The strip is
 * live: it needs no data, and a reader can change view while the read is out.
 */
export function PoolsSkeleton({ view }: { view: 'ceilings' | 'profiles' }) {
  return (
    <>
      <p className="c-count-note" aria-hidden="true">
        <span className="skeleton" style={{ display: 'inline-block', width: '24ch', height: '1em', verticalAlign: 'middle' }} />
      </p>
      <CapSeg view={view} />
      <SkeletonRows />
    </>
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

/** A pool's marks, worst first: the order the row's tone takes them in. */
const WORST_FIRST = ['over ceiling', 'paused', 'limit 0', 'no limit set', 'too small', 'full', 'near ceiling', 'lowered'] as const

/** The head's clauses, in the order the line has always read them. */
const HEAD_ORDER = ['full', 'near ceiling', 'lowered', 'paused', 'limit 0', 'no limit set', 'too small', 'over ceiling'] as const

/**
 * The line under the heading (capacity.html C1: "9 families · 2 full · 1
 * lowered · tenant eng"). Every figure is a count of rows this read returned.
 * `lowered` is a pool whose effective ceiling sits under its configured hard
 * limit -- AIMD or a provider quota took it down -- which is the row a reader
 * otherwise has to find by its `/30` suffix.
 */
function PoolsSummary({ capacity }: { capacity: Capacity }) {
  const pools = capacity.pools
  // `too small` IS COUNTED BY THE ROW'S OWN RULE (visual QA run 6,
  // 6:capacity-011, part of #1680). The top group listed resource:large as
  // `too small` while this line read `1 full · 1 lowered · 1 paused`, so the
  // summary named every state but the one a person had to act on. `isTooSmall`
  // is what `classifyPool` draws the chip from, weighed against the same
  // catalogue, so the count and the marks cannot disagree.
  const classes = useResourceClasses()
  const families = new Set(pools.map((p) => poolKind(p.name))).size
  // EACH POOL IS COUNTED ONCE, BY ITS WORST STATE (QA2-FIX-14, capacity-11).
  // Every clause had its own filter, so `eng` -- paused, and lowered from 10
  // -- was counted twice, and the head read `1 full · 2 lowered · 1 paused ·
  // 1 too small` (5) over the 4 pools Needs a look holds. The state is now the
  // worst of the marks `classifyPool` draws on the row (`WORST_FIRST`), so the
  // clauses add up to the pools that carry one, and `limit 0` and `no limit
  // set`, which the row marks, are counted rather than read as `lowered`.
  const worst = pools.map((p) => {
    const words = classifyPool(p, poolWeight(p, classes)).chips.map((c) => c.word)
    return WORST_FIRST.find((w) => words.includes(w)) ?? null
  })
  const count = (w: string) => worst.filter((x) => x === w).length
  const viewer = poolViewer(capacity)
  // EACH CLAUSE IS ONE UNBREAKABLE ITEM (QA-054, 2026-10-20): at 390 the line
  // wrapped inside the tenant id, at its hyphen -- `tenant u-` / `bogdan`. The
  // line now wraps only BETWEEN clauses, at a ` · `, as Accounts' foot does.
  const clauses = [
    `${pools.length} pools in ${families} famil${families === 1 ? 'y' : 'ies'}`,
    ...HEAD_ORDER.map((w) => (count(w) > 0 ? `${count(w)} ${w}` : null)),
    viewer !== undefined ? `tenant ${viewer}` : null,
  ].filter((c): c is string => c !== null)
  return (
    <>
      {clauses.map((c, i) => (
        <Fragment key={c}>
          {i > 0 && ' · '}
          <span className="cap-clause">{c}</span>
        </Fragment>
      ))}
    </>
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
 * True when a pool is one a person has to act on: `poolGroup` (types.ts)
 * files it under `needs_action` by `blockerGroup`, the rule the Agents list's
 * Waiting split uses -- paused, set to zero by a person, no limit set, over
 * its ceiling (drift), or below one task of its class. A `resource:` pool is
 * weighed against its class's `units` from the catalogue; no other pool has
 * one class. Every such pool is also in `needsALook`, so nothing Agents calls
 * needs-action is left down in a family here.
 */
export function needsAction(pool: Pool, classes: ResourceClasses | null): boolean {
  const units = poolKind(pool.name) === 'resource' ? classUnits(classes, pool.name.slice('resource:'.length)) : null
  return poolGroup(pool, units) === 'needs_action'
}

/**
 * True when a pool belongs in Pools' top group, "Needs a look": full,
 * lowered, paused or at `POOL_NEAR_CEILING` (80%) of its ceiling or more, the
 * group capacity.html A draws -- and every pool `needsAction` takes, so a
 * pool at 0, with no limit, over its ceiling or too small for its class leads
 * as well. `poolProblemRank` is the rule the marks and the family order
 * already use: anything it ranks above `ok` is something to look at.
 *
 * BGD'S ANSWER, 2026-10-26 08:20-08:30Z (VQ-031, task_06d63267fd684e27a419):
 * "Needs a look, as the mock-up draws it". It REVERSES the 2026-10-01 note
 * that made this group "Needs action", filed by the Agents list's Waiting
 * split, which kept a full or AIMD-lowered pool in its family because
 * waiting clears it. That split answers a different question -- which of a
 * TASK's refusals a person must clear -- and Agents keeps it. Pools answers
 * "which pools should I look at": the 46-of-50 pool read `ok` at the foot of
 * Providers under that rule (pass 1), and the mock-up's group of full and
 * lowered pools is what the owner picked.
 */
/** `poolProblemRank`'s rank for a pool with nothing to look at (capacityPoll.ts). */
const POOL_RANK_OK = 6

export function needsALook(pool: Pool, classes: ResourceClasses | null): boolean {
  return needsAction(pool, classes) || poolProblemRank(pool) < POOL_RANK_OK
}

/**
 * The Ceilings tab: "Needs a look" first, then one table per family, each
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
  // ONE POOL ORDER (QA G5-22): `comparePools`, the order Pool limits draws
  // the same pools in -- family, problems first, this tenant, name.
  const order = comparePools(viewer)
  const look = pools.filter((p) => needsALook(p, classes)).sort(order)
  const rest = pools.filter((p) => !needsALook(p, classes))
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
      {/* A LINK THAT NAMES NO POOL IS SAID (live QA CAP-12): `?pool=runner:nope`
          drew the page with no highlight and nothing to say the link missed. */}
      {target !== null && !landed && (
        <p className="warn-text cap-missing" role="status">
          No pool is named <code>{target}</code> in this read.
        </p>
      )}
      <HoldersUncounted holders={holders} />
      {look.length > 0 && (
        <Family
          title={`Needs a look · ${look.length}`}
          className="cap-needs"
          pools={look}
          viewer={viewer}
          target={target}
          holders={holders}
          classes={classes}
        />
      )}
      {POOL_FAMILY_ORDER.map((kind) => {
        const family = rest.filter((p) => poolKind(p.name) === kind).sort(order)
        if (family.length === 0) return null
        return (
          <Family
            key={kind}
            title={FAMILY_TITLE[kind]}
            pools={family}
            viewer={viewer}
            target={target}
            holders={holders}
            classes={classes}
          />
        )
      })}
    </div>
  )
}

/**
 * WHY NO LINK CARRIES A COUNT, SAID ON THE PAGE (visual QA V094, 2026-10-11).
 * When the lease read failed, was refused or was cut, every row's Open column
 * read a bare `holders` and the reason was only in the link's title -- a
 * hover nobody makes on a phone, and 29 identical links that look like a
 * count someone forgot to print. The reason is said once, here, above the
 * families, with the `not counted` mark; each link names this line as its
 * description, so a screen reader hears it on the link too. One line and not
 * one per row: the reason is the same for every pool.
 */
const HOLDERS_NOTE_ID = 'cap-holders-uncounted'

function HoldersUncounted({ holders }: { holders: HolderCounts }) {
  if (!('unknown' in holders)) return null
  return (
    <p className="cap-holders-note" id={HOLDERS_NOTE_ID}>
      <span className="ctl-mark is-unread">not counted</span>
      <span>{holders.unknown}</span>
    </p>
  )
}

/** The id of a pool's row on Pools, which a `?pool=` link scrolls to. */
function poolRowId(pool: string): string {
  return `pool-${pool}`
}

function Family({
  title,
  className,
  pools,
  viewer,
  target,
  holders,
  classes,
}: {
  title: string
  className?: string
  pools: Pool[]
  /** The tenant this read is scoped to, for the rows' `this tenant`. */
  viewer: string | undefined
  /** The pool a `?pool=` link named, or null. */
  target: string | null
  holders: HolderCounts
  /** The resource-class catalogue, for a `resource:` pool's `too small`; null unread. */
  classes: ResourceClasses | null
}) {
  // Trap E: a number may only sit beside another number of the same scope, so
  // the scope is declared rather than left to be inferred -- ON EVERY ROW
  // (CP-2, #85). This head used to carry one `.ctl-card-note` computed from
  // the family's first row, and a family is not one scope: Tenants holds
  // every tenant's pool for an admin, and Providers holds the shared pool
  // beside per-tenant slices. The note is removed, as the owner decided.
  // THE TABLE IS NAMED BY ITS HEADING (visual QA run 3, a11y-keyboard-003):
  // a screen reader's table list read six unnamed tables, all one schema.
  const titleId = useId()
  return (
    <section className={className === undefined ? 'ctl-card' : `ctl-card ${className}`}>
      <div className="ctl-card-head">
        <h2 className="ctl-card-title" id={titleId}>{title}</h2>
      </div>
      <div className="ctl-card-body is-flush">
        <PoolTable pools={pools} viewer={viewer} target={target} holders={holders} classes={classes} labelledBy={titleId} />
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
const UNITS_TITLE = 'Weighted units, not agents: each agent holds its resource class’s units in every pool it clears.'

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
  classes,
  labelledBy,
}: {
  pools: Pool[]
  viewer: string | undefined
  target: string | null
  holders: HolderCounts
  classes: ResourceClasses | null
  /** The id of the family's heading, which names the table. */
  labelledBy: string
}) {
  const phone = usePhoneTables()
  return (
    <div className={`ctl-table ${tableMode(phone)} cap-pools`}>
      <table role="table" aria-labelledby={labelledBy}>
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
            {/* A HEAD YOU CAN SEE (D32): blank, its links read as Set by's. */}
            <th role="columnheader" scope="col">Open</th>
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
              classes={classes}
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
function HoldersLink({ pool, active, holders }: { pool: string; active: number; holders: HolderCounts }) {
  const href = poolHref('capacity/holders', pool)
  if ('unknown' in holders) {
    // The reason is on the page once, above the families (`HoldersUncounted`),
    // and each link is described by it as well as titled with it.
    return (
      <a className="ctl-link" href={href} title={holders.unknown} aria-describedby={HOLDERS_NOTE_ID}>
        holders
      </a>
    )
  }
  const n = holders.byPool.get(pool) ?? 0
  // UNITS ON THE COUNTER AND NO LEASE ROW IS NOT `0 holders` (QA2-FIX-14,
  // capacity-02). `standard` at 5 of 20 read `0 holders` beside its 5 leased
  // units: the counter holds them and no unreleased lease names the pool,
  // which is the accounting drift Holders measures. The link says the leases
  // are counter only, and its title says what that means.
  if (n === 0 && active > 0) {
    return (
      <a
        className="ctl-link"
        href={href}
        title={`The pool's counter holds ${active} unit${active === 1 ? '' : 's'}, and no unreleased lease names this pool: the units are counter only. Holders compares the two under Accounting drift.`}
      >
        no lease rows
      </a>
    )
  }
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
  classes,
}: {
  pool: Pool
  scope: string
  target: boolean
  holders: HolderCounts
  classes: ResourceClasses | null
}) {
  // The weight `needsAction` weighed this pool against, so the row draws the
  // reason it was filed under Needs a look (QA-052).
  const marks = classifyPool(pool, poolWeight(pool, classes))
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
        {/* THE NAME IS THIS ROW'S OWN ADDRESS (QA G5-23). It linked to
            Holders filtered by the pool, the same target as `N holders` two
            columns along; it is now the link to this row on this screen --
            the one a reader copies to point someone at a pool -- and who holds
            it stays one click away, in Open. */}
        <a className="ctl-link" href={poolHref('capacity/pools', pool.name)}>
          {poolLabel(pool.name)}
        </a>
        {/* THE SCOPE, PER ROW (CP-2), under the name it qualifies: it says
            which other figures on this screen a row's numbers may be compared
            with. It was a column of its own, which the frame does not have
            and which wrapped the names beside it (#503). */}
        <span className="cap-sub">
          <span className="ctl-sub">{pool.name}</span>
          {/* THE KEY IS NEVER THE PART THAT IS CUT (QA G5-11): at 1440
              `provider:anthropic:tenant:smoke · tenant …` lost the scope and
              `provider:mock-provider:tenant:eng · this t…` nearly lost the key.
              The scope stays on every row (CP-2) but gives way first -- it is
              the part a key with a `tenant:` segment already repeats. */}
          <span className="cap-sub-scope">
            {' · '}
            <span className="cap-scope">{scope}</span>
          </span>
        </span>
      </th>
      <td role="cell" data-label={LEASED} className="is-num">{pool.active}</td>
      <td role="cell" data-label={CEILING} className="is-num">
        {/* ONE ELEMENT, NOT A TEXT NODE AND A SPAN (visual QA V012): stacked,
            each child is a grid item, and `6` then `/10` were two of them --
            the `/10` a row below its figure. */}
        <span className="cap-ceil">
          {limit === null || pool.hard_limit === null ? (
            // No limit set (#374): unknown, so no number -- not even a 0.
            <i className="ctl-em" title={by.detail}>no limit set</i>
          ) : (
            limit
          )}
          {limit !== null && pool.hard_limit !== null && limit < pool.hard_limit && (
            // THE SLASH IS ITS OWN ELEMENT (QA2-FIX-14, capacity-12): the
            // compact phone row read `0 /6 /10`, three bare figures. It draws
            // `0 of 6 (lowered from 10)` instead (styles/capacity.css), which
            // needs the `/` to be something it can hide; the wide table keeps
            // the frame's `6/10`.
            <span className="cap-was" title={`Configured hard limit is ${pool.hard_limit}`}>
              <span className="cap-was-sl">/</span>
              {pool.hard_limit}
            </span>
          )}
        </span>
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
      {/* NEVER A BLANK CELL (browser QA D32, 2026-10-04), AND NEVER THE WORD
          IN INK ON EVERY ROW (QA G5-19; capacity.html §G). 28 of 29 rows said
          `configured` and drowned the one that said `provider quota`, so the
          configured case is a faint `·`, an image named `configured`, with
          the cell's title saying where it was set. THE DOT HERE, THE WORD ON
          POOL LIMITS ONLY: owner decision 2026-10-22 (Console Design Calls,
          c14), reversing PR 1853, which had printed the word here too after
          QA-099. Pool limits keeps `configured` in the dim ink (AdmSetBy);
          do not flip this one back to match it. */}
      {/* `cap-by-cfg` LETS THE PHONE ROW DROP IT (VQ-032): the compact row
          keeps Set by only when something other than the configured limit
          sets the ceiling, which is when it says anything. */}
      <td role="cell" data-label="Set by" title={by.detail} className={by.term === 'configured' ? 'cap-by-cfg' : undefined}>
        {by.term === 'configured' ? (
          <span className="cap-setby-cfg" role="img" aria-label="configured">·</span>
        ) : (
          by.term
        )}
      </td>
      <td role="cell" data-label="Links" className="cap-links">
        {/* Both name the pool (#125): Holders filtered to it, and its own row
            on Pool limits (#134), where an admin edits it and anyone else
            reads it with Edit locked. */}
        {/* ONE ELEMENT (visual QA V012): the bare ` · ` between the links
            was an anonymous grid item when stacked, a keyless row of its own. */}
        <span className="cap-open">
          <HoldersLink pool={pool.name} active={pool.active} holders={holders} />
          {' · '}
          <a className="ctl-link" href={poolHref('admin/limits', pool.name)}>limit</a>
        </span>
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
 * #85): paused, over ceiling, limit 0, full, near ceiling, lowered, ok.
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
/**
 * The class a `resource:` pool is weighed against, and its `units` from the
 * catalogue (null unread). No other pool has one class, so it has no weight.
 */
function poolWeight(pool: Pool, classes: ResourceClasses | null): { name: string; units: number | null } | null {
  if (poolKind(pool.name) !== 'resource') return null
  const name = pool.name.slice('resource:'.length)
  return { name, units: classUnits(classes, name) }
}

/**
 * TOO SMALL FOR ONE TASK OF ITS CLASS: a positive limit below `units`, on a
 * pool neither paused nor over its ceiling (those marks say more). The row's
 * chip and the summary's count both read this.
 */
function isTooSmall(pool: Pool, units: number | null): boolean {
  const limit = pool.effective_limit
  return (
    !isPaused(pool) &&
    !overCeiling(pool) &&
    limit !== null &&
    limit > 0 &&
    blockerCeiling({ reason: '', pool: pool.name, limit }, units) === 'below-units'
  )
}

function classifyPool(pool: Pool, weight: { name: string; units: number | null } | null = null): PoolClass {
  const paused = isPaused(pool)
  const over = overCeiling(pool)
  const limit = pool.effective_limit
  // TOO SMALL FOR ONE TASK OF ITS CLASS (QA-052, 2026-10-20). `needsAction`
  // files a `resource:` pool whose positive limit is below its class's units
  // under Needs a look -- admission refuses when `active + units > limit`, so
  // it admits nothing at 0 in use -- and the row said `ok` with `0 / 2`, and
  // nothing on it said why it was there. `blockerCeiling` decides it, so the
  // group and the mark are one rule.
  const units = weight?.units ?? null
  const small = isTooSmall(pool, units)
  // NO LIMIT SET (#374) is neither `limit 0` nor `ok`: the ceiling was never
  // read, admission refuses on it, and a person has to set one. The verdict is
  // the blockers' own `limit-unset`, so the screens cannot disagree.
  const unset = !paused && limit === null
  const zero = !paused && !over && limit === 0
  const full = !over && limit !== null && limit > 0 && pool.active >= limit
  // NEAR ITS CEILING, AND LOWERED (VQ-031, visual QA pass 1). `anthropic · eng`
  // at 46 of a lowered 50 read `ok` in the grey an idle pool takes, and the
  // head's `4 lowered` named four rows nothing on them marked. Near is the
  // full mark's amber, in the chip, the row and the track alike (CP-14: one
  // answer); lowered is a neutral fact about the ceiling -- not a warning on
  // its own -- with what lowered it in its title. Both file the pool under
  // Needs a look (`needsALook`, BGD 2026-10-26), which `poolProblemRank`
  // orders.
  const near = nearCeiling(pool)
  const lowered = !paused && !over && loweredCeiling(pool)
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
  if (small) {
    chips.push({
      cls: 'is-paused',
      word: 'too small',
      title: `This pool's limit is ${limit} unit${limit === 1 ? '' : 's'}, below the ${units} units one ${weight?.name} agent weighs, so it admits no ${weight?.name} agent at this limit. Waiting cannot clear it: somebody has to raise the limit to at least ${units}.`,
    })
  }
  if (full) {
    chips.push({ cls: 'is-warn', word: 'full', title: `At its ceiling: ${pool.active} of ${limit} units in use.` })
  }
  if (near) {
    chips.push({
      cls: 'is-warn',
      word: 'near ceiling',
      title: `${pool.active} of ${limit} units in use (${Math.round((pool.active / (limit as number)) * 100)}%): little room is left for the next agent.`,
    })
  }
  // Lowered says what `ok` would have hidden; beside another mark it would only
  // repeat the `/30` and the Set by the row already prints.
  if (lowered && chips.length === 0) {
    const by = setBy(pool)
    chips.push({
      cls: 'is-info',
      word: 'lowered',
      title: by.term === 'configured' ? `Lowered to ${limit} from its configured ${pool.hard_limit}.` : by.detail,
    })
  }
  // §6.6 AND THE CP-14 RULING: a healthy pool gets the ok mark -- the filled
  // disc and the word -- and the mark is a text grey, not a hue (`--text-faint`
  // since CH-17 set the chip primitive's grey). Healthy carries no hue; a quiet
  // grey screen is what a healthy platform looks like.
  if (chips.length === 0) chips.push({ cls: 'is-ok', word: 'ok', title: 'Read, capped, not paused, not lowered, and not near its ceiling.' })

  const track = over ? 'is-bad' : paused ? 'is-paused' : zero || unset ? zeroTone : small ? 'is-paused' : full || near ? 'is-warn' : undefined
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
              : small
                ? 'is-paused below-units'
                : full
                  ? 'is-warn full'
                  : 'is-warn near'
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
 * The legend this screen used to end on, as links -- and then as a footer
 * index, until VQ-033 moved it behind the title's `?` (`help` above).
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
