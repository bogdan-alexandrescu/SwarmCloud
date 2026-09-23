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
 * THE ARITHMETIC THIS SCREEN HAS TO MAKE OBVIOUS, AND HOW IT NOW DOES IT.
 * A task takes EVERY pool in its runner profile's list, so its capacity is the
 * MINIMUM across them. Raising one pool changes nothing if another still binds
 * -- which is exactly what happened when five pools went to 40 and
 * `provider:anthropic:tenant:u-bogdan` stayed at 5, holding the real ceiling
 * at five.
 *
 * That used to be a sentence: "a task must clear EVERY pool its profile lists,
 * so its ceiling is the MINIMUM across them". A sentence asserting an
 * arithmetic rule is the weakest way to show one, because the reader has to
 * carry it to the figures and apply it themselves. SO THE OPERANDS ARE DRAWN
 * INSTEAD: every profile card lists each of its pools with that pool's own
 * agent ceiling, and the smallest is marked as the one that binds. `min()` is
 * not explained; it is shown with its inputs beside its output, which is the
 * form in which nobody has to be told what `min` means.
 *
 * The sentence itself lives at `#help/pools-all-at-once`.
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
      // ("Admin") instead of naming the thing on the screen.
      title="Pool limits"
      load={loadCapacity}
      // A count, not a promise. "changes take effect immediately" was a
      // rationale in the one slot on this screen a reader cannot skip.
      summary={(d) =>
        `${d.pools.length} pools · ${Object.keys(d.runner_profiles).length} profiles`
      }
      empty={{
        heading: 'No pools exist',
        body: 'Pools are created at provisioning time.',
      }}
    >
      {(d) => <Body capacity={d} onChanged={() => setNonce((n) => n + 1)} />}
    </Screen>
  )
}

/**
 * One profile's ceiling, with the numbers it was taken over.
 *
 * `agents` is the pool's own effective limit divided by the profile's weight,
 * because a pool counts weighted units and this column counts agents. `null`
 * means the pool the profile names is not in the response at all, which is an
 * absence rather than a zero and is drawn as one.
 */
interface Operand {
  pool: string
  agents: number | null
}

function arithmetic(
  pools: string[],
  units: number,
  byName: Map<string, Pool>,
): { operands: Operand[]; ceiling: number | null; binding: string | null } {
  const w = units > 0 ? units : 1
  const operands: Operand[] = pools.map((pool) => {
    const p = byName.get(pool)
    return { pool, agents: p ? Math.floor(p.effective_limit / w) : null }
  })

  let ceiling: number | null = null
  let binding: string | null = null
  for (const o of operands) {
    if (o.agents === null) continue
    if (ceiling === null || o.agents < ceiling) {
      ceiling = o.agents
      binding = o.pool
    }
  }
  return { operands, ceiling, binding }
}

function Body({ capacity, onChanged }: { capacity: Capacity; onChanged: () => void }) {
  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const profiles = Object.entries(capacity.runner_profiles).sort(([a], [b]) =>
    a.localeCompare(b),
  )

  return (
    <>
      {profiles.length > 0 && (
        <section className="section">
          {/* One word where "What actually binds, per profile" used to be. */}
          <span className="ctl-eyebrow has-q">
            Binding
            <HelpCard topic="pools-all-at-once" />
          </span>
          <div className="ctl-cards">
            {profiles.map(([name, prof]) => (
              <ProfileCard
                key={name}
                name={name}
                units={prof.units}
                {...arithmetic(prof.pools, prof.units, byName)}
              />
            ))}
          </div>
        </section>
      )}

      <PoolEditor pools={capacity.pools} onChanged={onChanged} />
    </>
  )
}

function ProfileCard({
  name,
  units,
  operands,
  ceiling,
  binding,
}: {
  name: string
  units: number
  operands: Operand[]
  ceiling: number | null
  binding: string | null
}) {
  // The unit that used to be a footnote -- "'Ceiling' is how many AGENTS of
  // that profile could run at once, not units" -- is now fused to the figure
  // (`4 agents`) and to the weight in the card note (`1 unit each`), which is
  // the other half of the same fact. §8.4.1: a well-chosen unit is the
  // explanation.
  const measured = ceiling !== null

  return (
    <section className="ctl-card">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">{name}</h2>
        <span className="ctl-card-note">
          {units} unit{units === 1 ? '' : 's'} each
        </span>
      </div>
      <div className="ctl-card-body">
        <b
          className={`ctl-figure${measured ? '' : ' is-absent'}`}
          aria-label={
            measured
              ? `${ceiling} agents of ${name} can run at once. That is the smallest ceiling across the ${operands.length} pools this profile takes, and ${binding === null ? 'none' : poolLabel(binding)} is the pool that binds it.`
              : `No ceiling can be computed for ${name}: none of the pools it takes are in this response, so the figure is not measured rather than zero.`
          }
        >
          {measured ? ceiling : <i className="ctl-em">—</i>}
          {measured && <span className="ctl-figure-unit">agents</span>}
        </b>

        {/* THE OPERANDS. Each pool's own ceiling in agents, the smallest one
            marked. This is the whole reason the paragraph could go: the
            reader sees three numbers and the marked one is the smallest, so
            "the minimum across them" is a thing they read off the card
            rather than a rule they were asked to remember. */}
        {/* §B6.4: a key COLUMN. Eight pool names and eight ceilings in a
            wrapping strip read as one run-on line of alternating word and
            digit; the smallest of them is the whole point of the card and it
            was the hardest thing on it to find. */}
        <ul className="ctl-facts is-rows adm-operands">
          {operands.map((o) => (
            <li
              key={o.pool}
              className={`ctl-fact${o.agents === null ? ' is-absent' : ''}${
                o.pool === binding ? ' is-binding' : ''
              }`}
              title={o.pool}
            >
              <b>{poolLabel(o.pool)}</b>
              {o.agents === null ? <i className="ctl-em">—</i> : o.agents}
            </li>
          ))}
        </ul>
      </div>
      <p className="ctl-card-foot">
        {binding === null ? (
          <>no pool in this response</>
        ) : (
          <>binds on {poolLabel(binding)}</>
        )}
      </p>
    </section>
  )
}

function PoolEditor({ pools, onChanged }: { pools: Pool[]; onChanged: () => void }) {
  return (
    <section className="section">
      <span className="ctl-eyebrow has-q">
        Ceilings
        {/* "Lowering a ceiling evicts nothing. Running work keeps its slots;
            the pool admits nothing new until it drains." was eighteen words
            under the control. The consequence still sits beside the control,
            as the two-word qualifier below; the sentence is behind the ?. */}
        <HelpCard topic="ceiling-change-evicts-nothing" />
      </span>
      <div className="ctl-table is-stacked">
        <table role="table">
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">Pool</th>
              <th role="columnheader" scope="col" className="is-num">In use</th>
              {/* The unit rides on the column name (§8.4.3) rather than in a
                  footnote under the table. */}
              <th role="columnheader" scope="col" className="is-num">Ceiling (units)</th>
              <th role="columnheader" scope="col">Set by</th>
              <th role="columnheader" scope="col">Change</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {[...pools]
              .sort((a, b) => a.name.localeCompare(b.name))
              .map((p) => (
                <PoolRow key={p.name} pool={p} onChanged={onChanged} />
              ))}
          </tbody>
          <caption>lowering a ceiling evicts nothing</caption>
        </table>
      </div>
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
  const editable = isEditable(pool)

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
    <tr role="row" className={isPaused(pool) ? 'is-paused' : undefined}>
      <th role="rowheader" scope="row" className="pool-name">
        {poolLabel(pool.name)}
        {/* The raw name, because it is what you paste into pool-limit.sh and a
            prettified label is not. */}
        <span className="ctl-sub">{pool.name}</span>
      </th>
      <td role="cell" data-label="In use" className="is-num">{pool.active}</td>
      <td role="cell" data-label="Ceiling (units)" className="is-num">{pool.effective_limit}</td>
      <td role="cell" data-label="Set by" title={by.detail}>{by.term}</td>
      <td role="cell" data-label="Change">
        <span className="limit-edit">
          <input
            type="number"
            min={0}
            max={100000}
            value={value}
            disabled={busy || !editable}
            onChange={(e) => {
              setValue(e.target.value)
              setDone(false)
            }}
            // The sentence that used to sit beside a disabled input lives here,
            // where a keyboard reader reaching the control gets it and a
            // sighted reader gets the two-word marker instead.
            aria-label={
              editable
                ? `Hard limit for ${pool.name}`
                : `Hard limit for ${pool.name}. This pool kind has no write route, so the control is read-only.`
            }
          />
          <button onClick={save} disabled={!dirty || !valid || busy || !editable}>
            {busy ? 'saving…' : 'save'}
          </button>
          {!editable && <span className="client-side">read-only</span>}
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
