import { loadCapacity, type ResourceClasses } from './api'
import { classUnits, useResourceClasses } from './Blockers'
import { isPaused } from './fetch'
import type { TopicId } from './help'
import { HelpLinks } from './HelpCard'
import { POOLS_POLL_MS } from './capacityPoll'
import { UtilTrack } from './primitives'
import { Screen } from './Shell'
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
 *   THE SCOPE -- "these are YOUR tenant's pools, not the platform's" -- is a
 *   `Scope` column on every row of every family, which is where two figures of
 *   different scope could otherwise be compared (Trap E). The families used to
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
      load={loadCapacity}
      // Decided 2026-10-01 (#117): Pools re-reads every 30s, and `Screen`
      // pauses the timer while the tab is hidden.
      pollMs={POOLS_POLL_MS}
      summary={(d) => {
        const paused = d.pools.filter(isPaused).length
        const over = d.pools.filter(overCeiling).length
        return (
          <>
            {d.pools.length} pools
            {paused > 0 && ` · ${paused} paused`}
            {over > 0 && ` · ${over} over ceiling`}
          </>
        )
      }}
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
          {/* CEILINGS, AS DECIDED ON 2026-10-01 (capacity.html, Variant 1, #125):
              one table per family, with a "Needs action" group ahead of them
              holding every pool a person has to act on (`needsAction`). A
              pool appears once: the top group takes it out of its family.
              The Cards view was the alternative set aside and is gone. */}
          <CeilingTables pools={d.pools} viewer={viewerOf(d)} />

          <HelpLinks topics={CAPACITY_TOPICS} />
        </>
      )}
    </Screen>
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
 * A pool's Scope cell, in words (CP-2, #85): `platform`, `this tenant`, or
 * `tenant X` for another tenant's pool.
 *
 * PER ROW, BECAUSE A FAMILY IS NOT ONE SCOPE. An admin sees every tenant's
 * pools (service.py filters them for non-admins only), so the Tenants family
 * holds several tenants and the Providers family holds both the shared
 * `provider:X` pool and every tenant's `provider:X:tenant:Y` slice. The note
 * each family used to carry was read off its first row and was wrong for the
 * rest. The owner's decision is this column, and the family note is gone.
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
function CeilingTables({ pools, viewer }: { pools: Pool[]; viewer: string | undefined }) {
  const classes = useResourceClasses()
  const byName = (a: Pool, b: Pool) => a.name.localeCompare(b.name)
  const act = pools.filter((p) => needsAction(p, classes)).sort(byName)
  const rest = pools.filter((p) => !needsAction(p, classes))
  return (
    <div className="cap-families">
      {act.length > 0 && (
        <Family title={`Needs action · ${act.length}`} className="cap-needs" pools={act} viewer={viewer} />
      )}
      {POOL_FAMILY_ORDER.map((kind) => {
        const family = rest.filter((p) => poolKind(p.name) === kind).sort(byName)
        if (family.length === 0) return null
        return <Family key={kind} title={FAMILY_TITLE[kind]} pools={family} viewer={viewer} />
      })}
    </div>
  )
}

function Family({
  title,
  className,
  pools,
  viewer,
}: {
  title: string
  className?: string
  pools: Pool[]
  /** The tenant this read is scoped to, for the rows' `this tenant`. */
  viewer: string | undefined
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
        <PoolTable pools={pools} viewer={viewer} />
      </div>
    </section>
  )
}

const LEASED = 'Leased (units)'
const CEILING = 'Ceiling (units)'

function PoolTable({ pools, viewer }: { pools: Pool[]; viewer: string | undefined }) {
  return (
    <div className="ctl-table is-scroll">
      <table role="table">
        <thead role="rowgroup">
          <tr role="row">
            <th role="columnheader" scope="col">Pool</th>
            {/* THE SCOPE, PER ROW (CP-2). Beside the name it qualifies, and
                ahead of the figures, because it says which other figures on
                this screen a row's numbers may be compared with. */}
            <th role="columnheader" scope="col">Scope</th>
            {/* "units", never "agents". Trap A: admission increments by the
                resource class's units (1, 2 or 4), so 8 leased may be two
                large agents or eight standard ones. The caveat is in the
                column name, where it cannot be scrolled away from the
                figures it governs. "Leased" because concurrency counts from
                LEASED (invariant 3), not from RUNNING. */}
            <th role="columnheader" scope="col" className="is-num">{LEASED}</th>
            {/* THE CEILING IS UNITS TOO, and it says so (CP-24). */}
            <th role="columnheader" scope="col" className="is-num">{CEILING}</th>
            <th role="columnheader" scope="col">Use</th>
            <th role="columnheader" scope="col">State</th>
            <th role="columnheader" scope="col">Set by</th>
            <th role="columnheader" scope="col" aria-label="Links" />
          </tr>
        </thead>
        <tbody role="rowgroup">
          {pools.map((p) => (
            <PoolRow key={p.name} pool={p} scope={scopeWord(p.name, viewer)} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PoolRow({ pool, scope }: { pool: Pool; scope: string }) {
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
    <tr role="row" className={marks.row}>
      <th role="rowheader" scope="row" title={pool.name}>
        {poolLabel(pool.name)}
        <span className="ctl-sub">{pool.name}</span>
      </th>
      <td role="cell" data-label="Scope">{scope}</td>
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
          <UtilTrack
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
        <a className="ctl-link" href="#capacity/holders">holders</a>
        {' · '}
        <a className="ctl-link" href="#admin/limits">limit</a>
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
        <span key={c.word} className={`ctl-chip ${c.cls}`} title={c.title}>
          <i aria-hidden="true" />
          {c.word}
        </span>
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
