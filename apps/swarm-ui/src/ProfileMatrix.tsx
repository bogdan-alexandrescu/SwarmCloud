import { Fragment, useState } from 'react'
import { isPaused } from './fetch'
import { tableMode, usePhoneTables } from './capacityPoll'
import { WarnMark } from './marks'
import './styles/profile-matrix.css'
import {
  FAMILY_TITLE,
  POOL_FAMILY_ORDER,
  headroomFor,
  poolKind,
  poolLabelAmong,
  unitsIn,
  type Capacity,
  type Pool,
  type PoolKind,
  type RunnerProfile,
  type Weight,
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
  // A DISABLED PROFILE HAS NOTHING THAT RUNS OUT FIRST (live QA CAP-10): it
  // cannot start at all, its Runs out first says `none · disabled`, and an
  // outlined cell beside that contradicted it and the legend.
  const binding = new Set(profile.available === false ? [] : bindingOf(profile))
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
 * large task weighs 4, so 7 free units fit 1. The units are what one agent
 * takes in THIS pool (`unitsIn`, contract request 129, #2950): pass the
 * profile, whose served `pool_units` puts a provider pool at 1, so 7 free
 * provider slots fit 7 large agents, not 1. NULL when the pool was not
 * read or has no limit set: not measured, never a 0. A PAUSED pool fits 0:
 * `available` stays positive while it is paused (it is ceiling minus active),
 * but admission refuses it, and the server prices it with `resume`.
 */
export function fitsIn(row: Pool | null, units: Weight): number | null {
  if (row === null || row.available === null || row.effective_limit === null) return null
  if (isPaused(row)) return 0
  const share = unitsIn(units, row.name)
  if (share === null || !Number.isFinite(share) || share <= 0) return null
  return Math.max(0, Math.floor(row.available / share))
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

/**
 * The opened row of a disabled profile, said once (QA G5-18). The server's
 * `disabled_reason` often already reads `<name> is disabled ...` and ends in
 * a full stop, and prefixing `<name> is disabled:` and appending `.` drew
 * "codex is disabled: codex is disabled on this platform. ... claude-code..".
 * A reason whose first word is the profile's name is the sentence as it
 * stands; any other is prefixed. Either way it ends in exactly one stop.
 */
export function disabledSentence(name: string, reason: string | null | undefined): string {
  const said = (reason ?? '').trim().replace(/\.+$/, '')
  const body = said === '' ? 'refused by the platform' : said
  const first = body.split(/\s/, 1)[0]
  return `${first === name ? body : `${name} is disabled: ${body}`}.`
}

export function ProfileMatrix({ capacity }: { capacity: Capacity }) {
  const [open, setOpen] = useState<string | null>(null)
  // ONE CARD PER PROFILE ON A PHONE (QA G5-08). The matrix was 923px in a
  // 352px box, and Can start and Runs out first -- the answer -- sat 570px
  // off-screen behind the family cells. On a phone each profile is a record:
  // its name, `Can start N`, and the pool it runs out on with leased/ceiling;
  // the per-family figures are in the opened row, whose Fits table names
  // every pool the profile clears.
  const phone = usePhoneTables()
  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const entries = Object.entries(capacity.runner_profiles).sort(([a], [b]) => a.localeCompare(b))
  // Only the families some profile clears get a column -- and none on a phone.
  const families = phone
    ? []
    : POOL_FAMILY_ORDER.filter((f) => entries.some(([, p]) => p.pools.some((n) => poolKind(n) === f)))
  const span = families.length + 3
  // A TIE OUTLINES SEVERAL CELLS ON ONE ROW (QA-056, 2026-10-20): mock's row
  // outlined Global, Resource classes and Backends, all 15 free, under a
  // caption naming one outlined cell. The caption follows what is drawn.
  const tied = entries.some(
    ([, p]) => matrixRow(p, byName).filter((c) => families.includes(c.family) && c.binding).length > 1,
  )

  return (
    <div className="cap-mx">
      <div className={`ctl-table ${tableMode(phone)}`}>
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
              const binds = off ? [] : bindingOf(profile)
              const among = [...profile.pools, ...binds]
              return (
                <Fragment key={name}>
                  <tr role="row" className={isOpen ? 'is-open' : undefined} data-profile={name}>
                    <th role="rowheader" scope="row">
                      {/* THE NAME IS CUT, NOT THE TABLE (visual QA V031): one
                          long profile name widened this no-wrap column and
                          pushed Can start and Runs out first -- the answer --
                          off the right edge at 1440. The button is held to a
                          width (styles/capacity.css) and the name ellipsizes;
                          the title has it whole. */}
                      <button
                        type="button"
                        className="cap-mx-toggle"
                        aria-expanded={isOpen}
                        title={name}
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
                      {/* NEVER AN EMPTY FIELD (visual QA V033). A disabled
                          profile, or one whose headroom was not measured, drew
                          the key and nothing after it -- on a phone a label
                          with no value reads as a value that failed to load.
                          Disabled says why there is no pool to name; unmeasured
                          is the console's em dash, as Can start beside it is. */}
                      {off ? (
                        <i className="ctl-em cap-mx-none">none · disabled</i>
                      ) : binds.length === 0 ? (
                        head.agents === null ? (
                          <span className="ctl-em" aria-label="not measured">—</span>
                        ) : (
                          <i className="ctl-em cap-mx-none">no pool runs out</i>
                        )
                      ) : (
                        binds.map((b) => {
                          const row = byName.get(b) ?? null
                          const fits = fitsIn(row, profile)
                          // LEASED/CEILING BESIDE THE NAME (QA G5-08): `tenant
                          // eng (37/40)` says how near the pool is without the
                          // family cell, which a phone card does not draw.
                          const use =
                            row === null ? null : `(${row.active}/${row.effective_limit === null ? '—' : row.effective_limit})`
                          // ONE LINE AT ONE WIDTH (visual QA V090/V195,
                          // 2026-10-27): beside a 70-character profile name the
                          // entry wrapped inside itself, `(5/5) fits ⏎0`. The
                          // name is its own span, so where the table is
                          // cramped the sheet wraps the NAME, whole, and
                          // breaks the entry only between its pieces, never
                          // inside the figures (capacity-15,
                          // styles/profile-matrix.css).
                          return (
                            <span key={b} className="cap-mx-bind" data-pool={b} title={b}>
                              <span className="cap-mx-bind-pool">{poolLabelAmong(b, among)}</span>
                              {use !== null && <span className="cap-mx-bind-use"> {use}</span>}{' '}
                              <small>{fits === null ? 'fits —' : `fits ${fits}`}</small>
                            </span>
                          )
                        })
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr role="row" className="cap-mx-exp">
                      <td role="cell" colSpan={span}>
                        {off ? (
                          <>{disabledSentence(name, profile.disabled_reason)}</>
                        ) : (
                          <FitsTable
                            profile={profile}
                            byName={byName}
                            among={among}
                            binds={binds}
                            complete={capacity.pools_complete !== false}
                          />
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
      {/* THE NOTE DESCRIBES WHAT IS DRAWN (visual QA V033). A phone draws no
          per-pool cell, so "each cell" and "the outlined cell" named nothing
          on its screen; there it says where the per-pool figures went. */}
      <p className="cap-mx-note">
        {phone
          ? 'Runs out first names the pool with leased/ceiling units. Open a profile for every pool it clears.'
          : tied
            ? 'Each cell is the units free in that pool, leased/ceiling under it. The outlined cells run out first; several on one row run out together.'
            : 'Each cell is the units free in that pool, leased/ceiling under it. The outlined cell runs out first.'}
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
  complete,
}: {
  profile: RunnerProfile
  byName: ReadonlyMap<string, Pool>
  among: readonly string[]
  binds: readonly string[]
  /** `pools_complete`: false means a pool missing from the read may exist. */
  complete: boolean
}) {
  // A POOL WITH NO DOCUMENT IS UNCAPPED, NOT UNMEASURED (QA G5-04). A profile's
  // list is `pool_names_for`, which names a per-tenant provider pool whether
  // or not anyone created it, and admission skips a missing pool as unlimited
  // (`evaluate_capacity`). The server says which ones in `admission.uncapped`;
  // without an admission block, absence from a complete read means the same.
  // The em dash stays for a pool that was not read -- the matrix's own split.
  const uncapped = new Set(
    profile.admission?.uncapped ?? (complete ? profile.pools.filter((n) => !byName.has(n)) : []),
  )
  // Four columns: a record on a phone, like every table here (QA G5-08).
  const phone = usePhoneTables()
  return (
    <div className={`ctl-table ${tableMode(phone)} cap-mx-fits`}>
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
            const fits = fitsIn(row, profile)
            const binding = binds.includes(name)
            return (
              <tr role="row" key={name} data-pool={name} className={binding ? 'is-binding' : undefined}>
                <th role="rowheader" scope="row" title={name}>
                  {poolLabelAmong(name, among)}
                </th>
                {row === null && uncapped.has(name) ? (
                  // One statement across both figure columns: there is no
                  // ceiling to lease against and so no count it fits.
                  <td role="cell" data-label="Leased/ceiling (units)" className="is-num" colSpan={2}>
                    no pool · uncapped
                  </td>
                ) : (
                  <>
                    <td role="cell" data-label="Leased/ceiling (units)" className="is-num">
                      {row === null ? '—' : `${row.active}/${row.effective_limit === null ? '—' : row.effective_limit}`}
                    </td>
                    <td role="cell" data-label="Fits" className="is-num">
                      {fits === null ? '—' : String(fits)}
                    </td>
                  </>
                )}
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

/**
 * One family cell. ITS FIGURES ARE ONE BOX, `.cap-mx-v` (visual QA V090): the
 * binding outline was drawn on the cell, so it was as wide as its column --
 * `Resource classes` drew a wider box than `Global` -- and 4px from the next
 * row's. Drawn on a box of one minimum width, right-aligned in the cell, every
 * outline is the same size and the cell's own padding separates two rows.
 */
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
        <span className="cap-mx-v">{cell.absent === 'unread' ? '—' : 'uncapped'}</span>
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
        <span className="cap-mx-v">
          <b className="cap-mx-zero">no limit set</b>
          <small>{`${r.active}/—`}</small>
        </span>
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
      <span className="cap-mx-v">
        <b className={r.available <= 0 ? 'cap-mx-zero' : undefined}>{r.available}</b>
        <small>{`${r.active}/${r.effective_limit}`}</small>
      </span>
    </td>
  )
}
