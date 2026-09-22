import { useState } from 'react'
import { loadCapacity } from './api'
import { isPaused } from './fetch'
import type { TopicId } from './help'
import { HelpLinks } from './HelpCard'
import { Screen } from './Shell'
import { Counterfactuals, IncompleteNote, headroomFigure } from './Blockers'
import {
  POOL_FAMILY_ORDER,
  headroomFor,
  overCeiling,
  poolKind,
  poolLabel,
  poolScope,
  setBy,
  type Capacity,
  type Headroom,
  type Pool,
  type PoolKind,
} from './types'

const FAMILY_TITLE: Record<PoolKind, string> = {
  global: 'Global',
  tenant: 'Tenants',
  resource: 'Resource classes',
  runner: 'Runner profiles',
  backend: 'Backends',
  provider: 'Providers',
}

/**
 * Screen B -- the capacity board.
 *
 * Where an operator goes when something is not being admitted and they need to
 * know which ceiling is the binding one.
 *
 * NOTE ON THE SPEC: docs/web-ui/02-cluster-state.md defines this screen in
 * §4.1 and then stops -- the source text is cut off mid-sentence and §§4.2-9
 * were never written. So the grouping order and the conjunction header below
 * come from the spec; the columns come from the five data traps in §1.1 and
 * from what pool_to_api actually sends. Anything beyond that is not sourced
 * and is not pretended to be.
 */
export function CapacityScreen() {
  const [asTable, setAsTable] = useState(true)

  return (
    <Screen
      // "Pools", not "Capacity". The tab that leads here says Pools, the
      // section says Pools, the table below is a list of pools -- and a
      // heading reading "Capacity" made the one screen look like two places,
      // so a runbook step saying "go to Pools" named nothing on the screen it
      // landed you on. The nav's own rule (App.tsx: sections are named after
      // OBJECTS, not the question they answer) decides which side gives way:
      // "Capacity" is the question, and it still leads the screen -- as the
      // section's question line, printed under the tabs.
      title="Pools"
      load={loadCapacity}
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
      empty={{
        heading: 'No pools exist',
        body: 'The read succeeded and returned nothing. Pools are created at provisioning time, so an environment with none has not been fully applied — this is a real absence, not a failed lookup.',
      }}
    >
      {(d) => (
        <>
          <Conjunction />
          <Headroom capacity={d} />
          <div className="view-toggle">
            <button className={asTable ? 'on' : ''} onClick={() => setAsTable(true)}>
              Table
            </button>
            <button className={!asTable ? 'on' : ''} onClick={() => setAsTable(false)}>
              Cards
            </button>
          </div>
          {POOL_FAMILY_ORDER.map((kind) => {
            const pools = d.pools
              .filter((p) => poolKind(p.name) === kind)
              .sort((a, b) => a.name.localeCompare(b.name))
            if (pools.length === 0) return null
            return (
              <Family key={kind} kind={kind} pools={pools} asTable={asTable} />
            )
          })}
          <HelpLinks topics={CAPACITY_TOPICS} />
        </>
      )}
    </Screen>
  )
}

/**
 * Per-runner-profile headroom: how many more agents of each kind could start
 * right now, and which pool is the one stopping more.
 *
 * The number is the minimum across the profile's pools divided by its weight,
 * because a task must clear all of them at once and each is incremented by
 * `units` rather than by one.
 *
 * It says "for tenant X" in words, every time. The pool list comes from
 * pool_names_for for the CALLING tenant -- including for an admin -- so an
 * admin reading these as the platform's capacity is the single most plausible
 * misreading here. A platform-wide figure does not exist today and is not
 * faked by substituting another tenant's pools.
 */
function Headroom({ capacity }: { capacity: Capacity }) {
  const profiles = Object.entries(capacity.runner_profiles)
  if (profiles.length === 0) return null

  // `/v1/capacity` names the tenant itself -- service.capacity() has always
  // sent `tenant_id`, and this file used to guess it back out of the pool
  // names. The regex stays as the fallback for an API that predates the
  // field, because a blank here silently drops the scope from every figure.
  const tenant =
    capacity.tenant_id ??
    capacity.pools
      .map((p) => /(?:^|:)tenant:([^:]+)/.exec(p.name)?.[1])
      .find((t): t is string => Boolean(t))

  const rows = profiles
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, profile]) => ({ name, profile, h: headroomFor(profile) }))

  return (
    <section className="section">
      <h2>
        Headroom
        <span className="scope tenant">
          {tenant ? `for tenant ${tenant}` : 'for your tenant'}
        </span>
      </h2>
      <div className="table-wrap">
        <table className="pools">
          <thead>
            <tr>
              <th scope="col">Runner profile</th>
              <th scope="col" className="n">Could start</th>
              <th scope="col" className="n">Weight</th>
              {/* Plural on purpose. A task must clear EVERY pool at once, so
                  more than one can refuse at the same moment -- and while this
                  column named a single one, an operator would raise it and
                  nothing would move. */}
              <th scope="col">Held back by</th>
              <th scope="col">Backend</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ name, profile, h }) => {
              const figure = headroomFigure(h)
              return (
                <tr
                  key={name}
                  className={
                    h.agents === 0 ? 'over' : h.agents === null ? 'unmeasured' : undefined
                  }
                >
                  <th scope="row">{name}</th>
                  <td className="n" title={figure.title}>
                    {figure.text}
                  </td>
                  <td className="n">{profile.units}u</td>
                  <td>
                    <HeldBackBy h={h} />
                  </td>
                  <td>{profile.backend}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="muted small">
        How many more agents of each profile could be admitted right now, for
        this tenant. A task must clear every pool in its list at once, so this
        is the minimum across them divided by the profile&apos;s weight &mdash; not
        a platform figure, and not a sum. An em dash is not a zero: it means the
        number was not measured, and the cell says why.
      </p>
      {rows.map(({ name, profile, h }) =>
        h.blockers.length > 0 || !h.complete || h.counterfactual.length > 0 ? (
          <details key={name} className="admission-details">
            <summary>
              <span className="mono">{name}</span>
              {h.blockers.length > 0 ? (
                <>
                  {' '}
                  &mdash; {h.blockers.length} pool
                  {h.blockers.length === 1 ? '' : 's'} refusing it
                </>
              ) : !h.complete ? (
                <> &mdash; not fully measured</>
              ) : (
                <> &mdash; what lifting each ceiling would buy</>
              )}
            </summary>
            <IncompleteNote h={h} />
            <Counterfactuals h={h} generatedAt={capacity.generated_at} />
            <p className="provenance">
              {profile.pools.length} pools in its list &middot; {profile.units} unit
              {profile.units === 1 ? '' : 's'} added to each on admission
            </p>
          </details>
        ) : null,
      )}
    </section>
  )
}

/**
 * EVERY pool refusing this profile, not the tightest one.
 *
 * The bug this replaces: `headroomFor` kept a running minimum and overwrote
 * `binding` on each new one, so with `resource:large` and `provider:anthropic`
 * both at their ceilings the cell named one of them. An operator raised it,
 * nothing changed, and the pool still refusing never appeared anywhere.
 *
 * A pause and a full pool are drawn differently because the remedies are
 * opposite -- resume it, versus wait or raise it -- and a paused pool can read
 * 0 of 8 units in use while admitting nothing at all, which is the case that
 * looks healthiest and is not.
 */
function HeldBackBy({ h }: { h: Headroom }) {
  if (h.blockers.length === 0) {
    return (
      <>
        <span
          className="ctl-em"
          title={
            h.complete
              ? 'No pool is refusing this profile.'
              : 'At least one required pool could not be read, so nothing can be said about what refuses this.'
          }
        >
          &mdash;
        </span>
        {!h.complete && (
          <span className="client-side"> &middot; {h.unread.length} unread</span>
        )}
        {h.missing.length > 0 && (
          <span className="client-side"> &middot; {h.missing.length} uncapped</span>
        )}
      </>
    )
  }
  return (
    <>
      <span className="tags">
        {h.blockers.map((b) => (
          <span
            key={b.pool}
            className={`tag ${b.reason === 'MANUAL_PAUSE' ? 'paused' : 'full'}`}
            title={`${b.pool} — ${b.reason}, ${b.active} of ${b.limit} units in use`}
          >
            {poolLabel(b.pool)}
            {b.reason === 'MANUAL_PAUSE' ? ' paused' : ` ${b.active}/${b.limit}`}
          </span>
        ))}
      </span>
      {!h.complete && (
        <span className="client-side"> &middot; and {h.unread.length} unread</span>
      )}
      {h.missing.length > 0 && (
        <span className="client-side"> &middot; {h.missing.length} uncapped</span>
      )}
    </>
  )
}

/**
 * Stated once, above everything. The single most common misreading of this
 * screen is adding two pools together.
 */
function Conjunction() {
  return (
    <p className="conjunction">
      A task must clear <strong>every</strong> pool in its list at the same
      moment. Capacity is the <strong>minimum</strong> across them, never a sum.
    </p>
  )
}

function Family({
  kind,
  pools,
  asTable,
}: {
  kind: PoolKind
  pools: Pool[]
  asTable: boolean
}) {
  // Trap E: a number may only sit beside another number of the same scope, so
  // the scope is declared on the group rather than left to be inferred.
  const first = pools[0]
  if (!first) return null
  const scope = poolScope(first.name)

  return (
    <section className="section">
      <h2>
        {FAMILY_TITLE[kind]}
        <span className={`scope ${scope}`}>
          {scope === 'platform' ? 'platform-wide' : 'this tenant'}
        </span>
      </h2>
      {asTable ? <PoolTable pools={pools} /> : (
        <div className="grid">
          {pools.map((p) => (
            <PoolCard key={p.name} pool={p} />
          ))}
        </div>
      )}
    </section>
  )
}

function PoolTable({ pools }: { pools: Pool[] }) {
  return (
    <div className="table-wrap">
      <table className="pools">
        <thead>
          <tr>
            <th scope="col">Pool</th>
            {/* "units", never "agents". Trap A: admission increments by the
                resource class's units (1, 2 or 4), so active: 8 may be two
                large agents or eight standard ones. */}
            <th scope="col" className="n">Units in use</th>
            <th scope="col" className="n">Ceiling</th>
            <th scope="col" className="n">Headroom</th>
            <th scope="col">Set by</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {pools.map((p) => (
            <PoolRow key={p.name} pool={p} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PoolRow({ pool }: { pool: Pool }) {
  const paused = isPaused(pool)
  const over = overCeiling(pool)
  const full = !over && pool.effective_limit > 0 && pool.active >= pool.effective_limit
  const by = setBy(pool)

  return (
    <tr className={over ? 'over' : paused ? 'paused' : full ? 'full' : undefined}>
      <th scope="row" className="pool-name" title={pool.name}>
        {poolLabel(pool.name)}
        <span className="raw">{pool.name}</span>
      </th>
      <td className="n">{pool.active}</td>
      <td className="n">
        {pool.effective_limit}
        {pool.effective_limit < pool.hard_limit && (
          <span className="was" title={`Configured hard limit is ${pool.hard_limit}`}>
            of {pool.hard_limit}
          </span>
        )}
      </td>
      <td className="n">{pool.available}</td>
      <td title={by.detail}>{by.term}</td>
      <td>
        <span className="tags">
          {paused && (
            <span className="tag paused" title="An operator paused this pool. It admits nothing until resumed.">
              paused
            </span>
          )}
          {over && (
            <span
              className="tag full"
              title={`${pool.active} units are held against a ceiling of ${pool.effective_limit}. Admission cannot produce this, so it is drift: a limit lowered under running work, or a slot never released. 'make pool-check' finds these.`}
            >
              over ceiling
            </span>
          )}
          {full && <span className="tag capped">full</span>}
          {!paused && !over && !full && <span className="tag ok">ok</span>}
        </span>
      </td>
    </tr>
  )
}

function PoolCard({ pool }: { pool: Pool }) {
  const limit = pool.effective_limit
  const ratio = limit > 0 ? Math.min(1, pool.active / limit) : 0
  const paused = isPaused(pool)
  const over = overCeiling(pool)
  const full = !over && limit > 0 && pool.active >= limit
  const by = setBy(pool)

  return (
    <div className={`pool${paused ? ' paused' : ''}${full || over ? ' full' : ''}`}>
      <div className="top">
        <span className="name" title={pool.name}>
          {poolLabel(pool.name)}
        </span>
        <span className="count">
          {pool.active}
          <span className="of"> / {limit}</span>
        </span>
      </div>

      {/* THE ONE TRACK (§B4.3 move 2), and it is no longer `.bar`: that
          selector is also the header banner further down styles.css, which
          won on order and gave this 5px pill 9px of padding and a border.
          `.is-unknown` when no ceiling was read -- an empty plain track is a
          claim that nothing is in use, and "no ceiling" is not a zero. */}
      <div
        className={`ctl-track${limit > 0 ? '' : ' is-unknown'}`}
        role="meter"
        aria-valuenow={pool.active}
        aria-valuemin={0}
        aria-valuemax={limit}
        /* "units", not "agents": admission counts weighted units, so 8 may be
           two large agents or eight standard ones. */
        aria-label={`${poolLabel(pool.name)}: ${pool.active} of ${limit} units in use`}
      >
        <i
          className={over || full ? 'bad' : ratio > 0.8 ? 'warn' : ''}
          style={{ width: `${Math.round(ratio * 100)}%` }}
        />
      </div>

      <div className="tags">
        {paused && <span className="tag paused">paused</span>}
        {over && <span className="tag full">over ceiling</span>}
        {full && <span className="tag full">full</span>}
        {pool.effective_limit < pool.hard_limit && (
          <span className="tag capped" title={by.detail}>
            {by.term}
          </span>
        )}
      </div>
    </div>
  )
}

/**
 * The two things about this data that are counter-intuitive enough to need
 * saying on the screen rather than in a doc nobody opens.
 */
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
