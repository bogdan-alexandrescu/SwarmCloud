import { Fragment, useState } from 'react'
import { isPaused } from './fetch'
import { WarnMark } from './marks'
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
 * The `Runs out first` column names each of those pools with its Fits -- how
 * many more of the profile that pool alone has room for -- and an opened row
 * is the per-pool Fits table with `+N if lifted` (#124), which is what the
 * retired Profile headroom tab drew.
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

/**
 * How many more tasks of a profile ONE pool has room for: its free units over
 * the profile's units, rounded down (#124). Per task, never per unit -- a
 * browser task weighs 2, so 3 free units fit 1. NULL when the pool was not
 * read or has no limit set: not measured, never a 0. A PAUSED pool fits 0:
 * `available` stays positive while it is paused (it is ceiling minus active),
 * but admission refuses it, and the server prices it with `resume`.
 */
export function fitsIn(row: Pool | null, units: number): number | null {
  if (row === null || row.available === null || row.effective_limit === null) return null
  if (isPaused(row)) return 0
  if (!Number.isFinite(units) || units <= 0) return null
  return Math.max(0, Math.floor(row.available / units))
}

/**
 * The `+N if lifted` cell for one pool (#124): what the server priced lifting
 * (or resuming) it at, from `admission.counterfactual`. The server prices the
 * binding pools only; any other pool is not the one running out, so lifting
 * it changes nothing and the cell says so rather than inventing a figure.
 */
export function liftedFor(profile: RunnerProfile, pool: string): string {
  const c = headroomFor(profile).counterfactual.find((x) => x.pool === pool)
  if (c !== undefined) {
    if (c.delta === null) return 'not measurable'
    return c.action === 'resume' ? `+${c.delta} if resumed` : `+${c.delta}`
  }
  return bindingOf(profile).includes(pool) ? 'not priced' : 'no change'
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
              {/* THE ONE CEILING TO RAISE, ON THE ROW (#124): every pool the
                  server names as running out first, each with how many more
                  of this profile it fits. The +N a lift buys is in the opened
                  row's table, one figure per pool, not a sentence here. */}
              <th role="columnheader" scope="col">Runs out first</th>
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
                        // A CONDITION, NOT A STATE (brand §3, #503): the red
                        // diamond is FAILED's, a task that ended in error. A
                        // disabled profile is a standing refusal by the
                        // platform, which is the amber warning triangle.
                        <span title={profile.disabled_reason || 'refused by the platform'}>
                          <WarnMark label="disabled" />
                        </span>
                      ) : (
                        <b className={head.agents === 0 ? 'cap-mx-zero' : undefined}>
                          {head.agents === null ? '—' : head.agents}
                        </b>
                      )}
                    </td>
                    <td role="cell" data-label="Runs out first" className="cap-mx-first">
                      {!off &&
                        binds.map((b) => {
                          const fits = fitsIn(byName.get(b) ?? null, profile.units)
                          return (
                            <span key={b} className="cap-mx-bind" data-pool={b} title={b}>
                              {poolLabelAmong(b, among)}{' '}
                              <small>{fits === null ? 'fits —' : `fits ${fits}`}</small>
                            </span>
                          )
                        })}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr role="row" className="cap-mx-exp">
                      <td role="cell" colSpan={span}>
                        {off ? (
                          <>{`${name} is disabled: ${profile.disabled_reason || 'refused by the platform'}.`}</>
                        ) : (
                          <FitsTable profile={profile} byName={byName} among={among} binds={binds} />
                        )}{' '}
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

/**
 * The opened row (#124): one line per pool the profile clears, with its Fits
 * and what lifting it buys -- the per-pool table Profile headroom drew, in
 * place of the counterfactual sentences. The binding rows carry the matrix's
 * own outline mark. A task clears every row at once (invariant 2), so the
 * profile's figure is the least Fits here, never a sum.
 */
function FitsTable({
  profile,
  byName,
  among,
  binds,
}: {
  profile: RunnerProfile
  byName: ReadonlyMap<string, Pool>
  among: readonly string[]
  binds: readonly string[]
}) {
  return (
    <div className="ctl-table is-scroll cap-mx-fits">
      <table role="table">
        <thead role="rowgroup">
          <tr role="row">
            <th role="columnheader" scope="col">Pool</th>
            <th role="columnheader" scope="col" className="is-num">Leased/ceiling (units)</th>
            <th role="columnheader" scope="col" className="is-num">Fits</th>
            <th role="columnheader" scope="col" className="is-num">+N if lifted</th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          {profile.pools.map((name) => {
            const row = byName.get(name) ?? null
            const fits = fitsIn(row, profile.units)
            const binding = binds.includes(name)
            return (
              <tr role="row" key={name} data-pool={name} className={binding ? 'is-binding' : undefined}>
                <th role="rowheader" scope="row" title={name}>
                  {poolLabelAmong(name, among)}
                </th>
                <td role="cell" data-label="Leased/ceiling (units)" className="is-num">
                  {row === null ? '—' : `${row.active}/${row.effective_limit === null ? '—' : row.effective_limit}`}
                </td>
                <td role="cell" data-label="Fits" className="is-num">
                  {fits === null ? '—' : String(fits)}
                </td>
                <td role="cell" data-label="+N if lifted" className="is-num">
                  {liftedFor(profile, name)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
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
