import { loadHolders, type HoldersBoard } from './api'
import { Screen } from './Shell'
import { poolLabel, type LeaseRow } from './types'

/**
 * Screen C -- what is using the platform right now.
 *
 * Distinct from the lease panels on Trouble, which answer "is something
 * wrong". This answers "what is holding capacity, and do the two records of
 * that agree".
 *
 * ON SOURCING: docs/web-ui/02-cluster-state.md is cut off mid-sentence in
 * §4.1 and has no Screen C section. The four parts below come from
 * README.md's one-line description -- lease list, in-flight units, class mix,
 * accounting-drift check -- and everything else from the data traps in §1.1
 * and from what the routes actually send. Nothing here is presented as spec
 * that is not.
 *
 * THE DRIFT CHECK IS THE POINT. Admission increments every pool in a lease's
 * list by the lease's `units`, in the same transaction that writes the lease.
 * So the units held by unreleased leases and the pool's own `active` counter
 * are two records of one fact, and a disagreement is not rounding.
 *
 * It is deliberately NOT presented as proof of a leak, for a reason the
 * drafting run surfaced: list_leases takes the newest `limit` documents and
 * drops released ones AFTERWARDS, so a short page and a genuinely small set
 * of holders look identical. A delta computed from a truncated page is not
 * evidence. Both numbers are shown, neither is called correct, and the lease
 * side says what it was computed over.
 */
export function HoldersScreen() {
  return (
    <Screen
      title="Capacity holders"
      load={loadHolders}
      summary={(b) => (
        <>
          {b.page.leases.length} unreleased lease
          {b.page.leases.length === 1 ? '' : 's'} ·{' '}
          {/* "units", never "agents": admission counts weighted units, so 6
              units may be six standard leases or one large plus one browser. */}
          {b.page.units_held} units held
          {b.page.tenant_id === null ? ' · every tenant' : ` · ${b.page.tenant_id} only`}
        </>
      )}
      empty={{
        heading: 'No unreleased leases',
        body: 'The read succeeded and returned nothing — no lease is holding capacity. This is a real zero, not a failed query.',
      }}
    >
      {(board) => (
        <>
          <Drift board={board} />
          <ClassMix rows={board.page.leases} />
          <HolderTable rows={board.page.leases} />
        </>
      )}
    </Screen>
  )
}

/**
 * Units held per pool, from the leases, beside that pool's own counter.
 *
 * Only pools that at least one loaded lease names are compared. A pool no
 * lease mentions has nothing to compare against, and showing it with a lease
 * side of 0 would manufacture a delta out of an absence.
 */
function Drift({ board }: { board: HoldersBoard }) {
  if (board.pools === null) {
    return (
      <section className="section panel">
        <h2>Accounting drift</h2>
        <div className="state partial" role="status">
          <h3>Pool counters could not be read</h3>
          <p>
            The leases loaded, so the table below is trustworthy. The
            comparison is not available: {board.poolsDetail}
          </p>
        </div>
      </section>
    )
  }

  const byPool = new Map<string, number>()
  for (const l of board.page.leases) {
    if (!Array.isArray(l.pools)) continue
    const units = typeof l.units === 'number' && Number.isFinite(l.units) ? l.units : null
    if (units === null) continue
    for (const p of l.pools) byPool.set(p, (byPool.get(p) ?? 0) + units)
  }

  const rows = Array.from(byPool.entries())
    .map(([name, held]) => {
      const pool = board.pools?.find((p) => p.name === name)
      return { name, held, active: pool ? pool.active : null }
    })
    .sort((a, b) => b.held - a.held)

  const disagreeing = rows.filter((r) => r.active !== null && r.active !== r.held)

  return (
    <section className="section panel">
      <h2>
        Accounting drift
        {disagreeing.length > 0 && <span className="count-chip">{disagreeing.length}</span>}
      </h2>

      {disagreeing.length === 0 ? (
        <p className="muted">
          Every pool a loaded lease names holds exactly the units those leases
          account for.
        </p>
      ) : (
        <div className="table-wrap">
          <table className="pools">
            <thead>
              <tr>
                <th scope="col">Pool</th>
                <th scope="col" className="n">From leases</th>
                <th scope="col" className="n">Pool counter</th>
                <th scope="col" className="n">Delta</th>
              </tr>
            </thead>
            <tbody>
              {disagreeing.map((r) => (
                <tr key={r.name} className="over">
                  <th scope="row" className="pool-name">
                    {poolLabel(r.name)}
                    <span className="raw">{r.name}</span>
                  </th>
                  <td className="n">{r.held}</td>
                  <td className="n">{r.active}</td>
                  <td className="n">
                    {r.active !== null ? (r.active > r.held ? '+' : '') : ''}
                    {r.active !== null ? r.active - r.held : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="muted small">
        Admission writes the lease and increments every pool in its list in one
        transaction, so these are two records of the same fact and a
        disagreement is never rounding. It is <em>not</em> proof of a leak on
        its own: the lease side is computed over the{' '}
        {board.page.leases.length} rows returned, and the route takes the
        newest rows before dropping released ones — so a truncated page
        produces a delta that looks the same. A counter above the lease sum
        means units reserved that no loaded lease accounts for;{' '}
        <code>make pool-check</code> is the tool that resolves which.
      </p>
    </section>
  )
}

/**
 * Class mix, from each lease's own `resource:<class>` pool.
 *
 * NOT inferred by inverting `units`. That works today because 1, 2 and 4 are
 * distinct, and silently picks the wrong class the first time two classes
 * share a weight.
 *
 * vCPU and memory are deliberately NOT shown. They live in RESOURCE_CLASSES
 * in the frozen contract, this screen has no route that exposes them, and
 * hand-copying them here is the restatement drift check-contract-parity.sh
 * exists to catch. Its TypeScript section would now catch such a copy, which
 * is not a reason to make one: the fix is to serve the numbers, as
 * /v1/resource-classes does for the run-detail screen. See
 * docs/audits/2026-09-20/data-gaps-found-by-fanout.md.
 */
function ClassMix({ rows }: { rows: LeaseRow[] }) {
  const byClass = new Map<string, { leases: number; units: number }>()
  let unclassified = 0

  for (const l of rows) {
    const pool = Array.isArray(l.pools) ? l.pools.find((p) => p.startsWith('resource:')) : undefined
    if (!pool) {
      unclassified++
      continue
    }
    const cls = pool.slice('resource:'.length)
    const e = byClass.get(cls) ?? { leases: 0, units: 0 }
    e.leases++
    if (typeof l.units === 'number' && Number.isFinite(l.units)) e.units += l.units
    byClass.set(cls, e)
  }

  const entries = Array.from(byClass.entries()).sort((a, b) => b[1].units - a[1].units)
  const max = Math.max(1, ...entries.map(([, e]) => e.units))

  return (
    <section className="section panel">
      <h2>Class mix</h2>
      {entries.length === 0 ? (
        <p className="muted">No loaded lease names a resource class.</p>
      ) : (
        entries.map(([cls, e]) => (
          <div className="split-row" key={cls}>
            <span className="sr-name">{cls}</span>
            <span className="sr-bar">
              <i style={{ width: `${(e.units / max) * 100}%` }} />
            </span>
            <span className="sr-n">{e.units}u</span>
            <span className="sr-by">
              {e.leases} lease{e.leases === 1 ? '' : 's'}
            </span>
          </div>
        ))
      )}
      {unclassified > 0 && (
        <p className="warn-text">
          {unclassified} lease{unclassified === 1 ? '' : 's'} name no resource
          pool and {unclassified === 1 ? 'is' : 'are'} counted in no class —
          not folded into <code>standard</code>, which would understate the
          rest.
        </p>
      )}
    </section>
  )
}

function HolderTable({ rows }: { rows: LeaseRow[] }) {
  const sorted = [...rows].sort((a, b) => b.units - a.units)
  return (
    <section className="section panel">
      <h2>Every holder</h2>
      <div className="table-wrap">
        <table className="pools">
          <thead>
            <tr>
              <th scope="col">Task</th>
              <th scope="col">Tenant</th>
              <th scope="col" className="n">Units</th>
              <th scope="col">Dispatch</th>
              <th scope="col" className="n">Gen</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((l) => (
              <tr key={l.lease_id}>
                <th scope="row" className="pool-name">
                  {l.task_id.slice(-10)}
                  <span className="raw">{l.lease_id.slice(-10)}</span>
                </th>
                <td>{l.tenant_id}</td>
                <td className="n">{l.units}</td>
                <td>{l.dispatch_state === 'LEASED' ? 'awaiting dispatch' : 'dispatched'}</td>
                <td className="n">{l.generation}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="provenance">
        {rows.length} rows returned · units are weighted, not agent counts
      </p>
    </section>
  )
}
