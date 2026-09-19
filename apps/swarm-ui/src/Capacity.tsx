import { useState } from 'react'
import { loadCapacity } from './api'
import { isPaused } from './fetch'
import { Screen } from './Shell'
import {
  POOL_FAMILY_ORDER,
  headroomFor,
  overCeiling,
  poolKind,
  poolLabel,
  poolScope,
  setBy,
  type Capacity,
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
      title="Capacity"
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
          <Legend capacity={d} />
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

  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  // Any tenant-scoped pool tells us whose view this is. There is no tenant id
  // on /v1/capacity itself, so it is read off the pool names rather than
  // assumed or left blank.
  const tenant = capacity.pools
    .map((p) => /(?:^|:)tenant:([^:]+)/.exec(p.name)?.[1])
    .find((t): t is string => Boolean(t))

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
              <th scope="col">Held back by</th>
              <th scope="col">Backend</th>
            </tr>
          </thead>
          <tbody>
            {profiles
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([name, profile]) => {
                const h = headroomFor(profile, byName)
                return (
                  <tr key={name} className={h.agents === 0 ? 'over' : undefined}>
                    <th scope="row">{name}</th>
                    <td className="n">{h.agents}</td>
                    <td className="n">{profile.units}u</td>
                    <td title={h.binding ?? undefined}>
                      {h.agents === 0 && h.binding
                        ? poolLabel(h.binding)
                        : h.binding
                          ? poolLabel(h.binding)
                          : '\u2014'}
                      {h.missing.length > 0 && (
                        <span className="client-side"> · {h.missing.length} uncapped</span>
                      )}
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
        is the minimum across them divided by the profile&apos;s weight — not a
        platform figure, and not a sum.
      </p>
    </section>
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

      <div
        className="bar"
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
function Legend({ capacity }: { capacity: Capacity }) {
  return (
    <section className="section legend">
      <h2>Reading this screen</h2>
      <dl>
        <dt>Units, not agents</dt>
        <dd>
          Admission counts weighted units — standard 1, browser 2, large 4 — so
          8 units in use may be two large agents or eight standard ones. Agent
          counts live on the Agents screen and are a different number.
        </dd>
        <dt>Freshness is this page's own read time</dt>
        <dd>
          A pool's <code>updated_at</code> is not a change time: pools bootstrapped
          by Terraform carry none and the API fills it with "now" on every
          request, and the quota broker rewrites it on every provider pool each
          pass whether or not anything changed. So it is never shown here as
          "last changed".
        </dd>
        {Object.keys(capacity.runner_profiles).length > 0 && (
          <>
            <dt>Headroom is for your tenant</dt>
            <dd>
              Runner-profile pool lists come from <code>pool_names_for</code> for
              the calling tenant — including for an admin. They describe what a
              task <em>you</em> submit must clear, not a platform figure.
            </dd>
          </>
        )}
      </dl>
    </section>
  )
}
