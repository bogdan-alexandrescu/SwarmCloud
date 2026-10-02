import { Fragment, useState } from 'react'
import {
  FAMILY_TITLE,
  POOL_FAMILY_ORDER,
  headroomFor,
  poolKind,
  poolLabelAmong,
  type Capacity,
  type Pool,
  type PoolKind,
  type RunnerProfile,
} from './types'

/**
 * Pools › By runner profile: the profile-by-pool matrix (capacity.html §B,
 * decided 2026-10-01, #124).
 *
 * One row per runner profile, one column per pool family. Each cell is the
 * units free in that family's pool, with leased/ceiling under it. A task
 * clears every pool in its row in one transaction or none of them (invariant
 * 2), so the profile starts only when every cell has room; THE OUTLINED CELL
 * is the pool that runs out first -- the server's `admission.binding` list,
 * never a client re-derivation, so two pools tied at the minimum are both
 * outlined and raising one of them is not mistaken for the fix (CP-4).
 *
 * Every figure is the CALLING tenant's (Trap D): a profile's pool list is
 * built by `pool_names_for(tenant_id=ctx.tenant_id)`, admin included.
 */

/** One cell of the matrix, worked out from the read and nothing else. */
export interface MatrixCell {
  family: PoolKind
  /** The pool this cell draws, or null when the profile has none of this family. */
  pool: string | null
  /** The pool's row from the read, or null when it was not read or not capped. */
  row: Pool | null
  /** True when the server names this pool as one the next task runs out on. */
  binding: boolean
  /** Why there is no figure, when there is none. */
  absent: 'none' | 'unread' | 'uncapped' | null
}

/**
 * Every pool the server says the next task of this profile would run out on:
 * `admission.binding`, or `headroomFor`'s single name when the list is empty
 * (the first blocker), so the matrix never outlines FEWER pools than the
 * profile's own detail tags.
 */
export function bindingOf(profile: RunnerProfile): string[] {
  const listed = profile.admission?.binding ?? []
  if (listed.length > 0) return listed
  const one = headroomFor(profile).binding
  return one === null ? [] : [one]
}

/**
 * The profile's row: one cell per family, in the order Ceilings draws the
 * families. Where a profile clears two pools of one family (a provider's
 * shared pool and the tenant's slice of it), the cell draws the binding one,
 * else the one with the least room -- the one that matters to this row.
 */
export function matrixRow(profile: RunnerProfile, byName: ReadonlyMap<string, Pool>): MatrixCell[] {
  const binding = new Set(bindingOf(profile))
  const unread = new Set(profile.admission?.unread ?? [])
  return POOL_FAMILY_ORDER.map((family) => {
    const names = profile.pools.filter((p) => poolKind(p) === family)
    if (names.length === 0) return { family, pool: null, row: null, binding: false, absent: 'none' }
    const pick =
      names.find((n) => binding.has(n)) ??
      names.find((n) => unread.has(n)) ??
      // A pool with no limit set (#374) has no `available`, and it is the
      // one that matters most (admission refuses on it): it sorts first.
      [...names].sort((a, b) => {
        const rank = (n: string) => (byName.has(n) ? (byName.get(n)!.available ?? -Infinity) : Infinity)
        const [ra, rb] = [rank(a), rank(b)]
        return ra === rb ? 0 : ra < rb ? -1 : 1
      })[0]!
    const row = byName.get(pick) ?? null
    return {
      family,
      pool: pick,
      row,
      binding: binding.has(pick),
      absent: row !== null ? null : unread.has(pick) ? 'unread' : 'uncapped',
    }
  })
}

/** "+3 if anthropic lifted", per binding pool the server priced, or ''. */
export function liftedLine(profile: RunnerProfile): string {
  const head = headroomFor(profile)
  const among = [...profile.pools, ...bindingOf(profile)]
  const said = bindingOf(profile)
    .map((pool) => head.counterfactual.find((c) => c.pool === pool))
    .filter((c): c is NonNullable<typeof c> => c !== undefined)
    .map((c) => {
      const what = poolLabelAmong(c.pool, among)
      const verb = c.action === 'resume' ? 'resumed' : 'lifted'
      return c.delta === null ? `${what} ${verb}: not measurable` : `+${c.delta} if ${what} ${verb}`
    })
  return said.join('; ')
}

export function ProfileMatrix({ capacity }: { capacity: Capacity }) {
  const [open, setOpen] = useState<string | null>(null)
  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const entries = Object.entries(capacity.runner_profiles).sort(([a], [b]) => a.localeCompare(b))
  // Only the families some profile clears get a column.
  const families = POOL_FAMILY_ORDER.filter((f) =>
    entries.some(([, p]) => p.pools.some((n) => poolKind(n) === f)),
  )
  const span = families.length + 3

  return (
    <div className="cap-mx">
      <div className="ctl-table is-scroll">
        <table role="table">
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">Runner profile</th>
              {families.map((f) => (
                <th key={f} role="columnheader" scope="col" className="is-num">
                  {FAMILY_TITLE[f]}
                </th>
              ))}
              <th role="columnheader" scope="col" className="is-num">Can start</th>
              <th role="columnheader" scope="col">If lifted</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {entries.map(([name, profile]) => {
              const cells = matrixRow(profile, byName).filter((c) => families.includes(c.family))
              const head = headroomFor(profile)
              const off = profile.available === false
              const isOpen = open === name
              const binds = bindingOf(profile)
              const among = [...profile.pools, ...binds]
              const lifted = off ? '' : liftedLine(profile)
              return (
                <Fragment key={name}>
                  <tr role="row" className={isOpen ? 'is-open' : undefined} data-profile={name}>
                    <th role="rowheader" scope="row">
                      <button
                        type="button"
                        className="cap-mx-toggle"
                        aria-expanded={isOpen}
                        onClick={() => setOpen(isOpen ? null : name)}
                      >
                        <span aria-hidden="true">{isOpen ? '▾' : '▸'}</span> <span className="mono">{name}</span>
                      </button>
                    </th>
                    {cells.map((c) => (
                      <MatrixCellView key={c.family} cell={c} />
                    ))}
                    <td role="cell" data-label="Can start" className="is-num">
                      {off ? (
                        <span className="ctl-chip is-bad">
                          <i aria-hidden="true" />
                          disabled
                        </span>
                      ) : (
                        <b className={head.agents === 0 ? 'cap-mx-zero' : undefined}>
                          {head.agents === null ? '—' : head.agents}
                        </b>
                      )}
                    </td>
                    <td role="cell" data-label="If lifted" className="cap-mx-lift">
                      {lifted}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr role="row" className="cap-mx-exp">
                      <td role="cell" colSpan={span}>
                        {off
                          ? `${name} is disabled: ${profile.disabled_reason || 'refused by the platform'}.`
                          : binds.length === 0
                            ? `${name} needs all ${profile.pools.length} pools at once, and none of them was named as running out first.`
                            : `${name} needs all ${profile.pools.length} pools at once; ${binds
                                .map((b) => poolLabelAmong(b, among))
                                .join(' and ')} ${binds.length === 1 ? 'runs' : 'run'} out first${
                                lifted === '' ? '' : ` (${lifted})`
                              }, so raising any other pool changes nothing.`}{' '}
                        <a className="ctl-link" href="#admin/limits">Pool limits</a>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="cap-mx-note">
        Each cell is the units free in that pool, leased/ceiling under it. The outlined cell runs out first.
      </p>
    </div>
  )
}

function MatrixCellView({ cell }: { cell: MatrixCell }) {
  const label = FAMILY_TITLE[cell.family]
  if (cell.absent === 'none') {
    return (
      <td role="cell" data-label={label} className="is-num cap-mx-na" title="This profile clears no pool of this family">
        ·
      </td>
    )
  }
  if (cell.row === null) {
    // An em dash only for a figure nobody measured (CP-1); a pool nothing
    // caps is a measured answer and says so in words.
    return (
      <td
        role="cell"
        data-label={label}
        data-pool={cell.pool ?? undefined}
        className={cell.binding ? 'is-num is-binding' : 'is-num'}
        title={cell.pool ?? undefined}
      >
        {cell.absent === 'unread' ? '—' : 'uncapped'}
      </td>
    )
  }
  const r = cell.row
  if (r.available === null || r.effective_limit === null) {
    // No limit set (#374): unknown, never a 0 -- admission refuses on it and
    // somebody has to set one.
    return (
      <td
        role="cell"
        data-label={label}
        data-pool={cell.pool ?? undefined}
        className={cell.binding ? 'is-num is-binding' : 'is-num'}
        title={cell.pool ?? undefined}
      >
        <b className="cap-mx-zero">no limit set</b>
        <small>{`${r.active}/—`}</small>
      </td>
    )
  }
  return (
    <td
      role="cell"
      data-label={label}
      data-pool={cell.pool ?? undefined}
      className={cell.binding ? 'is-num is-binding' : 'is-num'}
      title={cell.pool ?? undefined}
    >
      <b className={r.available <= 0 ? 'cap-mx-zero' : undefined}>{r.available}</b>
      <small>{`${r.active}/${r.effective_limit}`}</small>
    </td>
  )
}
