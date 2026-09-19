import { useCallback, useEffect, useState } from 'react'
import { loadCapacity, type Loaded } from './api'
import { limitedBy, poolKind, poolLabel, type Capacity, type Pool } from './types'

const GROUPS: { kind: ReturnType<typeof poolKind>; title: string; note?: string }[] = [
  { kind: 'global', title: 'Global' },
  { kind: 'tenant', title: 'Tenants' },
  { kind: 'provider', title: 'Providers' },
  { kind: 'runner', title: 'Runner profiles' },
  { kind: 'resource', title: 'Resource classes' },
  { kind: 'backend', title: 'Backends' },
]

export function CapacityScreen() {
  const [state, setState] = useState<Loaded<Capacity>>({ status: 'loading' })

  const refresh = useCallback(() => {
    let live = true
    setState({ status: 'loading' })
    loadCapacity().then((next) => {
      if (live) setState(next)
    })
    return () => {
      live = false
    }
  }, [])

  useEffect(() => refresh(), [refresh])

  return (
    <div className="app">
      <div className="head">
        <h1>Capacity</h1>
        <span className="env">dev</span>
      </div>
      <p className="sub">
        {state.status === 'ok' ? (
          <>
            {state.data.pools.length} pools · read {timeAgo(state.fetchedAt)}{' '}
            <button onClick={refresh}>refresh</button>
          </>
        ) : state.status === 'loading' ? (
          'Reading pools…'
        ) : (
          'Could not read pools.'
        )}
      </p>

      {state.status === 'loading' && <LoadingGrid />}

      {/* A failed read gets its own shape, never an empty grid. The whole
          point: "the query failed" must not look like "nothing is running". */}
      {state.status === 'failed' && (
        <div className="state failed">
          <h3>Could not read capacity</h3>
          <p>{state.detail}</p>
          <p style={{ marginTop: 8 }}>
            This is a failure to <em>read</em> the platform. It says nothing about whether
            agents are running.
          </p>
          {state.hint && <pre>{state.hint}</pre>}
          <button className="retry" onClick={refresh}>
            Try again
          </button>
        </div>
      )}

      {state.status === 'ok' &&
        (state.data.pools.length === 0 ? (
          <div className="state">
            <h3>No pools exist</h3>
            <p>
              The read succeeded and returned nothing. Pools are created at provisioning
              time, so an environment with none has not been fully applied — this is a real
              absence, not a failed lookup.
            </p>
          </div>
        ) : (
          GROUPS.map(({ kind, title }) => {
            const pools = state.data.pools
              .filter((p) => poolKind(p.name) === kind)
              .sort((a, b) => a.name.localeCompare(b.name))
            if (pools.length === 0) return null
            return (
              <section className="section" key={kind}>
                <h2>{title}</h2>
                <div className="grid">
                  {pools.map((p) => (
                    <PoolCard key={p.name} pool={p} />
                  ))}
                </div>
              </section>
            )
          })
        ))}
    </div>
  )
}

function PoolCard({ pool }: { pool: Pool }) {
  const limit = pool.effective_limit
  const ratio = limit > 0 ? Math.min(1, pool.active / limit) : 0
  const full = limit > 0 && pool.active >= limit
  const capped = limitedBy(pool)

  return (
    <div className={`pool${!pool.enabled ? ' paused' : ''}${full ? ' full' : ''}`}>
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
          className={full ? 'bad' : ratio > 0.8 ? 'warn' : ''}
          style={{ width: `${Math.round(ratio * 100)}%` }}
        />
      </div>

      <div className="tags">
        {!pool.enabled && <span className="tag paused">paused</span>}
        {full && <span className="tag full">full</span>}
        {capped && (
          <span
            className="tag capped"
            title={
              capped === 'quota'
                ? `Held at ${limit} by provider quota; configured limit is ${pool.hard_limit}.`
                : `Held at ${limit} by AIMD back-off; configured limit is ${pool.hard_limit}.`
            }
          >
            {capped === 'quota' ? 'quota capped' : 'backed off'}
          </span>
        )}
      </div>
    </div>
  )
}

function LoadingGrid() {
  return (
    <section className="section">
      <h2>
        <span className="skeleton" style={{ display: 'inline-block', width: 90, height: 10 }} />
      </h2>
      <div className="grid">
        {Array.from({ length: 8 }, (_, i) => (
          <div className="pool" key={i} aria-hidden>
            <div className="top">
              <span className="skeleton" style={{ width: '58%', height: 13 }} />
              <span className="skeleton" style={{ width: 34, height: 13 }} />
            </div>
            <div className="skeleton" style={{ height: 5, borderRadius: 999 }} />
            <div className="tags" />
          </div>
        ))}
      </div>
    </section>
  )
}

function timeAgo(when: Date): string {
  const s = Math.max(0, Math.round((Date.now() - when.getTime()) / 1000))
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  return `${Math.round(s / 60)}m ago`
}
