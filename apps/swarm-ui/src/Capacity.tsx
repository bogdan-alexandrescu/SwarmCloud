import { loadCapacity } from './api'
import { Screen } from './Shell'
import { limitedBy, poolKind, poolLabel, type Pool, type PoolKind } from './types'

const GROUPS: { kind: PoolKind; title: string }[] = [
  { kind: 'global', title: 'Global' },
  { kind: 'tenant', title: 'Tenants' },
  { kind: 'provider', title: 'Providers' },
  { kind: 'runner', title: 'Runner profiles' },
  { kind: 'resource', title: 'Resource classes' },
  { kind: 'backend', title: 'Backends' },
]

export function CapacityScreen() {
  return (
    <Screen
      title="Capacity"
      load={loadCapacity}
      summary={(d) => {
        const paused = d.pools.filter((p) => !p.enabled).length
        return `${d.pools.length} pools${paused ? ` · ${paused} paused` : ''}`
      }}
      empty={{
        heading: 'No pools exist',
        body: 'The read succeeded and returned nothing. Pools are created at provisioning time, so an environment with none has not been fully applied — this is a real absence, not a failed lookup.',
      }}
    >
      {(d) =>
        GROUPS.map(({ kind, title }) => {
          const pools = d.pools
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
      }
    </Screen>
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
