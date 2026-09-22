import { useState } from 'react'
import { loadCapacity, setPoolLimit } from './api'
import { errorHeading, isPaused, type ApiError } from './fetch'
import { HelpCard } from './HelpCard'
import { Screen } from './Shell'
import { poolKind, poolLabel, setBy, type Capacity, type Pool } from './types'

/**
 * Pool limits: the concurrency ceilings, editable.
 *
 * WHY THIS SCREEN EXISTS RATHER THAN A TFVARS EDIT. `pool_limits` in an
 * environment's tfvars is the ceiling a NEW environment is born with, and
 * nothing else: terraform/modules/firestore/bootstrap.tf carries
 * `ignore_changes = [fields]` on every pool document, deliberately, because
 * `active` is mutated by the admission transaction on every lease and an
 * apply that rewrote those documents would reset live counters to zero and
 * instantly oversubscribe every pool.
 *
 * The consequence was found the hard way: pool_limits was raised from
 * 20/10/5 to 40/20/15, committed with a carefully argued comment beside the
 * values, applied -- and the running platform stayed at 20/10/5. The apply
 * moved a terraform output and nothing else.
 *
 * THE ARITHMETIC THIS SCREEN HAS TO MAKE OBVIOUS. A task takes EVERY pool in
 * its runner profile's list, so its capacity is the MINIMUM across them.
 * Raising one pool changes nothing if another still binds -- which is exactly
 * what happened when five pools went to 40 and
 * `provider:anthropic:tenant:u-bogdan` stayed at 5, holding the real ceiling
 * at five. So every profile's binding pool is named at the top, before any
 * input box.
 */
export function AdminSettingsScreen() {
  const [nonce, setNonce] = useState(0)
  return (
    <Screen
      key={nonce}
      // "Pool limits", not "Admin settings". The tab says Pool limits and it
      // is the accurate one twice over: this screen edits concurrency ceilings
      // and nothing else, so "Admin settings" over-claimed a settings page
      // that does not exist, and it restated the section it already sits under
      // ("Admin") instead of naming the thing on the screen. Someone arriving
      // from Admin > Pool limits and reading "Admin settings" cannot tell
      // whether the other admin tab is inside this page or beside it.
      title="Pool limits"
      load={loadCapacity}
      summary={(d) => `${d.pools.length} pools · changes take effect immediately`}
      empty={{
        heading: 'No pools exist',
        body: 'The read succeeded and returned nothing. Pools are created at provisioning time, so an environment with none has not been fully applied.',
      }}
    >
      {(d) => <Body capacity={d} onChanged={() => setNonce((n) => n + 1)} />}
    </Screen>
  )
}

function Body({ capacity, onChanged }: { capacity: Capacity; onChanged: () => void }) {
  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const profiles = Object.entries(capacity.runner_profiles)

  return (
    <>
      <p className="conjunction">
        A task must clear <strong>every</strong> pool its profile lists, so its
        ceiling is the <strong>minimum</strong> across them.
        <HelpCard topic="pools-all-at-once" />
      </p>

      {profiles.length > 0 && (
        <section className="section panel">
          <h2>What actually binds, per profile</h2>
          <div className="table-wrap">
            <table className="pools">
              <thead>
                <tr>
                  <th scope="col">Runner profile</th>
                  <th scope="col" className="n">Ceiling</th>
                  <th scope="col">Set by</th>
                </tr>
              </thead>
              <tbody>
                {profiles
                  .sort(([a], [b]) => a.localeCompare(b))
                  .map(([name, prof]) => {
                    // The minimum across the profile's pools, in agent terms:
                    // a pool's headroom is in weighted units, so it is divided
                    // by the profile's weight.
                    let ceiling = Infinity
                    let binding = '—'
                    for (const pn of prof.pools) {
                      const pool = byName.get(pn)
                      if (!pool) continue
                      const units = prof.units > 0 ? prof.units : 1
                      const agents = Math.floor(pool.effective_limit / units)
                      if (agents < ceiling) {
                        ceiling = agents
                        binding = pn
                      }
                    }
                    return (
                      <tr key={name}>
                        <th scope="row">{name}</th>
                        <td className="n">{Number.isFinite(ceiling) ? ceiling : '—'}</td>
                        <td title={binding}>{binding === '—' ? '—' : poolLabel(binding)}</td>
                      </tr>
                    )
                  })}
              </tbody>
            </table>
          </div>
          {/* THE UNIT OF THE COLUMN STAYS ON THE SURFACE -- agents, not units,
              and the two are different numbers. The WEIGHTS that used to be
              typed out here are platform figures nothing checked (§5); the
              sizing table under Runtimes reads them from the catalogue. */}
          <p className="muted small">
            &ldquo;Ceiling&rdquo; is how many <strong>agents</strong> of that
            profile could run at once, not units.
            <HelpCard topic="units-not-agents" />
          </p>
        </section>
      )}

      <PoolEditor pools={capacity.pools} onChanged={onChanged} />
    </>
  )
}

function PoolEditor({ pools, onChanged }: { pools: Pool[]; onChanged: () => void }) {
  return (
    <section className="section panel">
      <h2>Pool ceilings</h2>
      <div className="table-wrap">
        <table className="pools">
          <thead>
            <tr>
              <th scope="col">Pool</th>
              <th scope="col" className="n">In use</th>
              <th scope="col" className="n">Ceiling</th>
              <th scope="col">Set by</th>
              <th scope="col">Change</th>
            </tr>
          </thead>
          <tbody>
            {[...pools]
              .sort((a, b) => a.name.localeCompare(b.name))
              .map((p) => (
                <PoolRow key={p.name} pool={p} onChanged={onChanged} />
              ))}
          </tbody>
        </table>
      </div>
      {/* THE CONSEQUENCE OF THE CONTROL STAYS BESIDE THE CONTROL (§6): an
          operator lowering a ceiling has to know, before pressing it, that
          nothing is evicted. */}
      <p className="muted small">
        <strong>Lowering a ceiling evicts nothing.</strong> Running work keeps
        its slots; the pool admits nothing new until it drains.
        <HelpCard topic="ceiling-change-evicts-nothing" />
      </p>
    </section>
  )
}

function PoolRow({ pool, onChanged }: { pool: Pool; onChanged: () => void }) {
  const [value, setValue] = useState(String(pool.hard_limit))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [done, setDone] = useState(false)

  const dirty = value.trim() !== String(pool.hard_limit)
  const parsed = Number(value)
  const valid = Number.isInteger(parsed) && parsed >= 0 && parsed <= 100_000
  const by = setBy(pool)

  const save = () => {
    if (!valid || busy) return
    setBusy(true)
    setError(null)
    setDone(false)
    setPoolLimit(pool.name, parsed).then((r) => {
      setBusy(false)
      if (r.status === 'ok' || r.status === 'stale') {
        setDone(true)
        onChanged()
      } else if (r.status === 'error') {
        setError(r.error)
      }
    })
  }

  return (
    <tr className={isPaused(pool) ? 'paused' : undefined}>
      <th scope="row" className="pool-name">
        {poolLabel(pool.name)}
        {/* The raw name, because it is what you paste into pool-limit.sh and a
            prettified label is not. */}
        <span className="raw">{pool.name}</span>
      </th>
      <td className="n">{pool.active}</td>
      <td className="n">{pool.effective_limit}</td>
      <td title={by.detail}>{by.term}</td>
      <td>
        <span className="limit-edit">
          <input
            type="number"
            min={0}
            max={100000}
            value={value}
            disabled={busy || !isEditable(pool)}
            onChange={(e) => {
              setValue(e.target.value)
              setDone(false)
            }}
            aria-label={`Hard limit for ${pool.name}`}
          />
          <button onClick={save} disabled={!dirty || !valid || busy || !isEditable(pool)}>
            {busy ? 'saving…' : 'save'}
          </button>
          {!isEditable(pool) && (
            <span className="client-side">no route for this pool kind</span>
          )}
          {dirty && !valid && <span className="warn-text">0–100000</span>}
          {done && <span className="tag ok">saved</span>}
          {error && (
            <span className="warn-text" title={error.message}>
              {errorHeading(error)}
            </span>
          )}
        </span>
      </td>
    </tr>
  )
}

/**
 * Whether a route exists for this pool kind.
 *
 * Rendering an enabled input for a pool nothing can write would be a control
 * that silently does nothing — worse than no control. Every kind below has a
 * PUT under /v1/admin/limits/, including the two added on 2026-09-20:
 * backend, and the per-tenant slice of a provider.
 */
function isEditable(pool: Pool): boolean {
  const kind = poolKind(pool.name)
  if (kind === 'global' || kind === 'tenant' || kind === 'resource') return true
  if (kind === 'runner' || kind === 'backend') return true
  if (kind === 'provider') return true
  return false
}
