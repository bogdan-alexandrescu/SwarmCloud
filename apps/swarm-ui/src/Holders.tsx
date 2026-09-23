import { loadHolders, type HoldersBoard } from './api'
import { HelpCard } from './HelpCard'
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
 *
 * ---------------------------------------------------------------------------
 * B4.4: WHAT THE WORDS BECAME.
 *
 * Every claim above is still made on this screen. None of it is made in a
 * sentence any more, because the encodings say it more precisely than the
 * sentences did:
 *
 *   "computed over a truncated page"  -> `.ctl-card-foot`, which names the row
 *       count the comparison ran over. Provenance belongs to the card, once,
 *       not to each figure.
 *   "every pool agrees"               -> `.ctl-figure` 0 beside
 *       `.ctl-mark.is-zero`. A measured zero, drawn as one. The paragraph that
 *       said this could sit beside a figure it did not describe; the mark
 *       cannot.
 *   "the counters could not be read"  -> `.ctl-mark.is-unread` and NO figure.
 *       A dash, never a 0.
 *   "units are weighted, not agents"  -> the column is named `UNITS (WEIGHTED)`.
 *       §8.4(3): the caveat attaches to the column, not to a footnote.
 *
 * The arguments themselves are at `#help/lease-and-pool-are-two-records` and
 * `#help/units-not-agents`, where they were already written.
 */
export function HoldersScreen() {
  // "Holders", matching the tab, and the two are not allowed to differ:
  // test_nav_headings_agree.py asserts every screen's heading IS the label of
  // the tab that opens it. It was "Capacity holders", and it had to be while
  // the section was called Pools -- one tab away from "Accounts", "Holders"
  // alone reads as holders of accounts. The section is called Capacity now, so
  // the parent supplies the noun this heading was carrying for it.
  return (
    <Screen
      title="Holders"
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
      /* A REAL ZERO, DRAWN AS ONE. The read succeeded and no lease holds
         capacity. The mark is what says which kind of nothing this is, and it
         says it in two words instead of two sentences. */
      empty={{
        heading: 'No unreleased leases',
        body: (
          <>
            <span className="ctl-mark is-zero">real zero</span> no lease holds capacity
          </>
        ),
      }}
    >
      {(board) => (
        <>
          <div className="ctl-cards hold-top">
            <Drift board={board} />
            <ClassMix rows={board.page.leases} />
          </div>
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
  const rowsRead = board.page.leases.length

  /* THE COUNTERS ARE GONE, SO THERE IS NO FIGURE AT ALL.
     Not a zero, not an empty table: `.ctl-mark.is-unread` plus a dash in the
     figure slot. The detail the server gave is the card's note, which is one
     line and does not wrap -- the long form is the help topic. */
  if (board.pools === null) {
    return (
      <section className="ctl-card">
        <div className="ctl-card-head">
          <h2 className="ctl-card-title">
            Accounting drift
            <HelpCard topic="lease-and-pool-are-two-records" />
          </h2>
          <span className="ctl-card-note">not compared</span>
        </div>
        <div className="ctl-card-body">
          <b
            className="ctl-figure is-absent"
            role="img"
            aria-label="The pool counters could not be read, so no comparison was made. This is not a delta of zero."
          >
            <span className="ctl-em">—</span>
          </b>
          <div className="hold-mark">
            <span className="ctl-mark is-unread">not read</span>
          </div>
        </div>
        <div className="ctl-card-foot">
          leases read · counters unread · {board.poolsDetail ?? 'the pool read did not complete'}
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
  const compared = rows.filter((r) => r.active !== null).length

  return (
    <section className="ctl-card">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">
          Accounting drift
          <HelpCard topic="lease-and-pool-are-two-records" />
        </h2>
        {/* §8.4(2): the coverage qualifier, one line, mono, right-aligned.
            This is the sentence "computed over the N rows returned" as an
            attribute of the card rather than a paragraph under it. */}
        <span className="ctl-card-note">
          {compared} of {rows.length} pools
        </span>
      </div>

      {disagreeing.length === 0 ? (
        /* A MEASURED ZERO. The figure is a digit, because the comparison ran
           and its answer is nought -- and the mark beside it is what keeps
           that apart from the unread case above, which has no digit at all. */
        <div className="ctl-card-body">
          <b className="ctl-figure">0</b>
          <div className="hold-mark">
            <span className="ctl-mark is-zero">real zero</span>
          </div>
        </div>
      ) : (
        <div className="ctl-card-body is-flush">
          <div className="ctl-table is-stacked">
            <table role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Pool</th>
                  <th role="columnheader" scope="col" className="is-num">From leases</th>
                  <th role="columnheader" scope="col" className="is-num">Counter</th>
                  <th role="columnheader" scope="col" className="is-num">Delta</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {disagreeing.map((r) => (
                  <tr role="row" key={r.name} className="is-warn">
                    <th role="rowheader" scope="row">
                      {poolLabel(r.name)}
                      <span className="ctl-sub">{r.name}</span>
                    </th>
                    <td role="cell" data-label="From leases" className="is-num">{r.held}</td>
                    <td role="cell" data-label="Counter" className="is-num">{r.active}</td>
                    <td role="cell" data-label="Delta" className="is-num">
                      {r.active !== null ? (r.active > r.held ? '+' : '') : ''}
                      {r.active !== null ? r.active - r.held : <span className="ctl-em">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* THE SAMPLE SIZE IS THE MARKER, and the tool that settles a
          disagreement is an ACTION, so both stay -- as provenance and as a
          command, not as two sentences. A comparison computed over a truncated
          page is not the same claim as one computed over the fleet, and the
          row count is what says which this is. */}
      <div className="ctl-card-foot">
        over {rowsRead} row{rowsRead === 1 ? '' : 's'} · <code>make pool-check</code>
      </div>
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
    <section className="ctl-card">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">
          Class mix
          <HelpCard topic="units-not-agents" />
        </h2>
        {/* A LEASE THAT NAMES NO CLASS IS COUNTED IN NO CLASS, and the note is
            where that is said -- not folded into `standard`, which would
            understate the rest. It was a two-clause sentence; it is now the
            qualifier on the card whose total it qualifies. */}
        {unclassified > 0 && (
          <span className="ctl-card-note">{unclassified} unclassified</span>
        )}
      </div>
      <div className="ctl-card-body">
        {entries.length === 0 ? (
          <div className="hold-mark">
            <span className="ctl-mark is-zero">real zero</span>
          </div>
        ) : (
          entries.map(([cls, e]) => (
            <div className="ctl-util" key={cls}>
              <span className="ctl-util-name">
                <b>{cls}</b>
              </span>
              <span className="ctl-util-track">
                <i className="ctl-util-fill" style={{ width: `${(e.units / max) * 100}%` }} />
              </span>
              <span className="ctl-util-figure">{e.units}u</span>
              <span className="ctl-util-by">
                {e.leases} lease{e.leases === 1 ? '' : 's'}
              </span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}

function HolderTable({ rows }: { rows: LeaseRow[] }) {
  const sorted = [...rows].sort((a, b) => b.units - a.units)
  return (
    /* §B6.1: the screen's one full-width table is the one box on it. It was
       a card wrapping a card-body wrapping a `.ctl-table`, which drew two
       rounded edges 16px apart around five columns. */
    <section className="section hold-all">
      <div className="ctl-toolbar">
        <h2 className="ctl-card-title">Every holder</h2>
        <span className="ctl-card-note is-end">
          {rows.length} row{rows.length === 1 ? '' : 's'}
        </span>
      </div>
      <div className="ctl-table is-stacked">
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Task</th>
                <th role="columnheader" scope="col">Tenant</th>
                {/* §8.4(3): THE CAVEAT ATTACHES TO THE COLUMN. "units are
                    weighted, not agent counts" was a footnote under the table
                    that a reader had to carry back up to the column it was
                    about. In the heading it cannot be missed and cannot be
                    applied to the wrong column. */}
                <th role="columnheader" scope="col" className="is-num">
                  Units (weighted)
                  <HelpCard topic="units-not-agents" />
                </th>
                <th role="columnheader" scope="col">Dispatch</th>
                <th role="columnheader" scope="col" className="is-num">Gen</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {sorted.map((l) => (
                <tr role="row" key={l.lease_id}>
                  <th role="rowheader" scope="row">
                    {l.task_id.slice(-10)}
                    <span className="ctl-sub">{l.lease_id.slice(-10)}</span>
                  </th>
                  <td role="cell" data-label="Tenant">{l.tenant_id}</td>
                  <td role="cell" data-label="Units (weighted)" className="is-num">{l.units}</td>
                  <td role="cell" data-label="Dispatch">
                    {/* The state, as a word AND as a shape -- `.ctl-dot`
                        carries the silhouette so the column survives
                        greyscale and a colour-blind reader. */}
                    <span className={`ctl-chip ${l.dispatch_state === 'LEASED' ? 'is-warn' : 'is-ok'}`}>
                      <i aria-hidden="true" />
                      {l.dispatch_state === 'LEASED' ? 'awaiting' : 'dispatched'}
                    </span>
                  </td>
                  <td role="cell" data-label="Gen" className="is-num">{l.generation}</td>
                </tr>
              ))}
            </tbody>
          </table>
      </div>
    </section>
  )
}
