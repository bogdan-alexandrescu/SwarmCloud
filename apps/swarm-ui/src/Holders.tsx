import type { ReactNode } from 'react'
import { loadHolders, type HoldersBoard } from './api'
import { HelpCard } from './HelpCard'
import { Mark, UtilRow } from './primitives'
import { Screen } from './Shell'
import { poolLabel, type LeasePage, type LeaseRow } from './types'

/**
 * WHETHER THESE ROWS ARE EVERY LIVE LEASE, in the three answers there are.
 *
 * `/v1/admin/leases` serves the newest `limit` live leases, and a list cut at
 * 200 and a list that is genuinely 200 long are the same picture unless
 * something says which. The route says it, and this is where it is read:
 *
 *   complete    `active_beyond_window` is 0: no live lease, under the same
 *               tenant filter, is outside these rows. Only then is a delta
 *               between the leases and the pool counters evidence of anything
 *               (store.py `LeaseScan`, docs/audits/2026-09-20 section 1).
 *   cut         live leases exist that are not in these rows. `beyond` is how
 *               many, or null when the API said the window was `truncated`
 *               without the count.
 *   unreported  neither field arrived -- an API older than the one that serves
 *               them. That is NOT complete: absent is not zero, and a list
 *               that cannot say whether it was cut must not look whole.
 *
 * `active_beyond_window` is the field to read; `truncated` is kept for a pager
 * (on a history read it is true for ever, because lease documents are never
 * deleted). This screen asks for `active_only=true`, where the two come from
 * one snapshot and cannot disagree, so a `truncated` alone is still a cut.
 *
 * READ DEFENSIVELY ALTHOUGH types.ts DECLARES THEM REQUIRED. The type says
 * what this API version serves, and the contract test holds the two together;
 * the check below is for the console served beside an older API, which is
 * the one time the type is wrong and the one time the answer matters.
 */
export type LeaseCoverage =
  | { kind: 'complete' }
  | { kind: 'cut'; beyond: number | null }
  | { kind: 'unreported' }

export function leaseCoverage(page: LeasePage): LeaseCoverage {
  const raw: unknown = page.active_beyond_window
  const beyond = typeof raw === 'number' && Number.isFinite(raw) && raw >= 0 ? raw : null
  if (beyond !== null && beyond > 0) return { kind: 'cut', beyond }
  if (page.truncated === true && page.active_only !== false) {
    // Cut, and the count either did not arrive or contradicts the flag. The
    // flag is the one that says "there is more", so it wins; the count it
    // cannot give is not invented.
    return { kind: 'cut', beyond: null }
  }
  if (beyond !== null) return { kind: 'complete' }
  return { kind: 'unreported' }
}

/** "3 of 5", or "3+" when more exist and nobody said how many. */
function ofTotal(rows: number, c: LeaseCoverage): string {
  if (c.kind !== 'cut') return `${rows}`
  return c.beyond === null ? `${rows}+` : `${rows} of ${rows + c.beyond}`
}

/** The sentence a coverage mark carries, with this read's own numbers in it. */
function coverageSay(rows: number, c: LeaseCoverage, what: string): string {
  if (c.kind === 'cut') {
    const missing =
      c.beyond === null
        ? 'more live leases exist than these rows, and the API did not say how many'
        : `${c.beyond} live lease${c.beyond === 1 ? ' is' : 's are'} not among them`
    return `${what} is over the ${rows} lease${rows === 1 ? '' : 's'} listed, and ${missing}. It is not a statement about the whole fleet.`
  }
  return `The API did not report whether any live lease was left out of these ${rows} row${rows === 1 ? '' : 's'}, so ${what.toLowerCase()} may be over a partial set.`
}

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
 * drafting run surfaced: the lease list is a WINDOW, and a delta computed from
 * a window that left live leases out is not evidence. The route used to give
 * no way to tell (it took the newest `limit` documents and dropped released
 * ones afterwards, so a short page and a small fleet looked identical). It now
 * reads the live set itself and says how many live leases the window left out
 * -- `active_beyond_window` -- and `leaseCoverage` above is where this screen
 * reads it. Both numbers are still shown and neither is called correct; what
 * changed is that a comparison over a cut window is MARKED as one, and a real
 * zero is only drawn over every live lease.
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
 *   "these rows are not every lease"  -> `3 of 5` wherever a count of rows is
 *       drawn, and `.ctl-mark.is-partial` (or `.is-absent`, when the API did
 *       not say) on the three things computed from them. The sentence, with
 *       this read's numbers in it, is the mark's accessible name.
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
      summary={(b) => {
        const coverage = leaseCoverage(b.page)
        const rows = b.page.leases.length
        return (
          <>
            {/* THE COUNT SAYS WHAT IT IS OUT OF. "200 unreleased leases" over a
                window cut at 200 is the platform's size misreported as the
                page's; "200 of 214" is both. A count the API could not vouch
                for says "listed", because that is all it is. */}
            {ofTotal(rows, coverage)} unreleased lease
            {rows === 1 && coverage.kind !== 'cut' ? '' : 's'}
            {coverage.kind === 'unreported' ? ' listed' : ''} ·{' '}
            {/* "units", never "agents": admission counts weighted units, so 6
                units may be six standard leases or one large plus one browser.
                And held by THESE rows when the rows are not all of them:
                `units_held` is summed over the page the route returned. */}
            {b.page.units_held} units held
            {coverage.kind === 'complete' ? '' : ' by these'}
            {b.page.tenant_id === null ? ' · every tenant' : ` · ${b.page.tenant_id} only`}
          </>
        )
      }}
      /* A REAL ZERO, DRAWN AS ONE. The read succeeded, no live lease exists
         and no pool counter holds a unit -- `loadHolders` calls it empty only
         with both measured (CP-7). The mark is what says which kind of nothing
         this is, and it says it in two words instead of two sentences.

         WHOSE LEASES, AND WHERE NEXT (CP-21, visual QA 2026-09-25). The
         populated summary says `every tenant`; this said nothing about scope,
         so "no unreleased leases" read as a claim about whoever was looking.
         The read names no tenant, so it IS every tenant. And §6.9's empty
         state ends in a way out: the counters this zero was checked against
         are on Pools. The link sits in the body until `Screen`'s `empty`
         prop has a slot for it (CH-10, in the shell lane's PR). */
      empty={{
        heading: 'No unreleased leases',
        // THE MARK IS `Screen`'s, in the heading (#145). A hand-drawn
        // `.ctl-mark is-zero` span here said `real zero` a second time.
        body: (
          <>
            no lease or counter holds capacity · every tenant ·{' '}
            <a className="ctl-link" href="#capacity/pools">
              Pools
            </a>
          </>
        ),
      }}
    >
      {(board) => {
        // READ ONCE, PASSED TO ALL THREE. The drift check, the class mix and
        // the table are three computations over the same rows, and a coverage
        // decided three times is three chances for one of them to look whole.
        const coverage = leaseCoverage(board.page)
        return (
          <>
            <div className="ctl-cards hold-top">
              <Drift board={board} coverage={coverage} />
              <ClassMix rows={board.page.leases} coverage={coverage} />
            </div>
            <HolderTable rows={board.page.leases} coverage={coverage} />
          </>
        )
      }}
    </Screen>
  )
}

/**
 * Units held per pool, from the leases, beside that pool's own counter.
 *
 * Over a CUT or unreported window, only pools that at least one loaded lease
 * names are compared: a pool no loaded lease mentions may be named by a lease
 * the window left out, and showing it with a lease side of 0 would manufacture
 * a delta out of an absence. Over EVERY live lease that argument is gone, and
 * every pool is compared (CP-7) -- see `Drift`.
 */
/**
 * THE DRIFT CARD'S HEAD, WRITTEN ONCE, AND THIS SCREEN'S ONLY `?` (B7.4).
 *
 * The two branches below -- counters unread, and counters compared -- each drew
 * their own copy of this head, so the same heading and the same help glyph were
 * authored twice and a change to one of them reached one branch. That is the
 * two-implementations failure `SectionQuestion` already cost this app once, in
 * miniature.
 *
 * WHY THIS ONE `?` SURVIVED THE DENSITY PASS AND THE OTHER THREE DID NOT. A
 * delta between the lease documents and the pool counters is not a fault and is
 * not a reconciliation error: they are two records of one fact, written at
 * different moments, and a reader who does not know that reads any non-zero
 * figure on this card as corruption. That is a platform behaviour, not a
 * property of a column, so no heading or unit can carry it -- which is exactly
 * the test for what may keep a glyph.
 */
function DriftHead({ note }: { note: ReactNode }) {
  return (
    <div className="ctl-card-head">
      <h2 className="ctl-card-title">
        Accounting drift
        <HelpCard topic="lease-and-pool-are-two-records" />
      </h2>
      <span className="ctl-card-note">{note}</span>
    </div>
  )
}

/**
 * The mark a comparison over these rows wears when the rows are not all of
 * them. null when they are: a complete read needs no qualifier.
 *
 * `partial` for a cut -- some of it arrived and the rest is known to exist --
 * and `absent` when the API did not say, because "whether anything was left
 * out" is then a figure nobody measured. Neither is `real zero`, which is the
 * one claim a comparison over a partial set cannot make.
 */
function coverageMark(rows: number, c: LeaseCoverage, what: string): ReactNode {
  if (c.kind === 'complete') return null
  return <Mark kind={c.kind === 'cut' ? 'partial' : 'absent'} say={coverageSay(rows, c, what)} />
}

function Drift({ board, coverage }: { board: HoldersBoard; coverage: LeaseCoverage }) {
  const rowsRead = board.page.leases.length

  /* THE COUNTERS ARE GONE, SO THERE IS NO FIGURE AT ALL.
     Not a zero, not an empty table: `.ctl-mark.is-unread` plus a dash in the
     figure slot. The detail the server gave is the card's note, which is one
     line and does not wrap -- the long form is the help topic. */
  if (board.pools === null) {
    return (
      <section className="ctl-card">
        <DriftHead note="not compared" />
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
  // OVER EVERY LIVE LEASE, EVERY POOL IS COMPARED (CP-7, visual QA 2026-09-25).
  // A pool no loaded lease names used to be skipped, on the grounds that its
  // lease side of 0 would be an absence dressed as a measurement -- which is
  // true of a CUT window and false of a complete one. When `leaseCoverage`
  // says no live lease was left out, and the rows are not filtered to one
  // tenant (a tenant's leases say nothing about the platform's `global`
  // counter), a pool no lease names holds 0 units BY MEASUREMENT. A counter
  // above that is the leak this card exists to find, and it was the one leak
  // the card could not see.
  if (coverage.kind === 'complete' && board.page.tenant_id === null) {
    for (const p of board.pools) if (!byPool.has(p.name)) byPool.set(p.name, 0)
  }

  const rows = Array.from(byPool.entries())
    .map(([name, held]) => {
      const pool = board.pools?.find((p) => p.name === name)
      return { name, held, active: pool ? pool.active : null }
    })
    .sort((a, b) => b.held - a.held)

  const disagreeing = rows.filter((r) => r.active !== null && r.active !== r.held)
  const compared = rows.filter((r) => r.active !== null).length
  // A DELTA IS EVIDENCE ONLY OVER EVERY LIVE LEASE. Over a cut window the
  // lease side is short by whatever the window left out, so a counter above it
  // may be those leases rather than a leak -- and an agreement may be an
  // accident of which rows made the page.
  const mark = coverageMark(rowsRead, coverage, 'This comparison')

  return (
    <section className="ctl-card">
      {/* §8.4(2): the coverage qualifier, one line, mono, right-aligned. This
          is the sentence "computed over the N rows returned" as an attribute of
          the card rather than a paragraph under it -- and when the rows are not
          every live lease, it says what they are out of. */}
      <DriftHead
        note={
          <>
            {compared} of {rows.length} pools
            {coverage.kind === 'cut' && <> · {ofTotal(rowsRead, coverage)} leases</>}
            {coverage.kind === 'unreported' && <> · coverage unreported</>}
          </>
        }
      />

      {disagreeing.length === 0 ? (
        /* A MEASURED ZERO -- WHEN IT IS ONE. The figure is a digit, because the
           comparison ran and its answer is nought, and the mark beside it is
           what keeps that apart from the unread case above, which has no digit
           at all. Over a partial set of leases the same nought is not a real
           zero across the fleet, so the mark says `partial` instead. */
        <div className="ctl-card-body">
          <b className="ctl-figure">0</b>
          <div className="hold-mark">
            {mark ?? <span className="ctl-mark is-zero">real zero</span>}
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
          row count -- out of the total, when the route said there was more --
          is what says which this is. When a table of deltas is on the card the
          coverage mark rides here, beside the count it qualifies; with no table
          it is already beside the 0. */}
      <div className="ctl-card-foot">
        {disagreeing.length > 0 && mark}
        over {ofTotal(rowsRead, coverage)} row{rowsRead === 1 && coverage.kind !== 'cut' ? '' : 's'}
        {coverage.kind === 'cut' && coverage.beyond !== null && ` · ${coverage.beyond} live not listed`}
        {' · '}
        <code>make pool-check</code>
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
function ClassMix({ rows, coverage }: { rows: LeaseRow[]; coverage: LeaseCoverage }) {
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
        {/* NO `?` (B7.4). Every figure in this card is already labelled in
            units -- the bars are units, the table column below says
            `Units (weighted)` -- so `units-not-agents` here was an argument
            for a convention the card follows in front of the reader. It is one
            click away in the rail's Help section and is linked from the three
            screens whose figures it actually disambiguates. */}
        <h2 className="ctl-card-title">Class mix</h2>
        {/* A LEASE THAT NAMES NO CLASS IS COUNTED IN NO CLASS, and the note is
            where that is said -- not folded into `standard`, which would
            understate the rest. It was a two-clause sentence; it is now the
            qualifier on the card whose total it qualifies. */}
        {/* AND A MIX OVER A CUT WINDOW IS THE MIX OF THE ROWS, not of the
            fleet: the leases the route left out are in no class here, so the
            note says what the bars are out of. */}
        {(unclassified > 0 || coverage.kind !== 'complete') && (
          <span className="ctl-card-note">
            {unclassified > 0 && `${unclassified} unclassified`}
            {unclassified > 0 && coverage.kind !== 'complete' && ' · '}
            {coverage.kind === 'cut' && `${ofTotal(rows.length, coverage)} leases`}
            {coverage.kind === 'unreported' && 'coverage unreported'}
          </span>
        )}
      </div>
      <div className="ctl-card-body">
        {entries.length === 0 ? (
          <div className="hold-mark">
            {coverageMark(rows.length, coverage, 'This mix') ?? (
              <span className="ctl-mark is-zero">real zero</span>
            )}
          </div>
        ) : (
          entries.map(([cls, e]) => (
            // THE SHARED ROW AND TRACK (./primitives.tsx). This card drew its
            // own track by hand; a class whose leases carried no finite unit
            // count now draws the measured-zero tick rather than a zero-width
            // fill, which was indistinguishable from a bar that failed to paint.
            <UtilRow
              key={cls}
              name={<b>{cls}</b>}
              track={{ pct: (e.units / max) * 100 }}
              figure={`${e.units}u`}
              by={`${e.leases} lease${e.leases === 1 ? '' : 's'}`}
            />
          ))
        )}
      </div>
    </section>
  )
}

function HolderTable({ rows, coverage }: { rows: LeaseRow[]; coverage: LeaseCoverage }) {
  const sorted = [...rows].sort((a, b) => b.units - a.units)
  return (
    /* §B6.1: the screen's one full-width table is the one box on it. It was
       a card wrapping a card-body wrapping a `.ctl-table`, which drew two
       rounded edges 16px apart around five columns. */
    <section className="section hold-all">
      <div className="ctl-toolbar">
        {/* "EVERY HOLDER" IS A CLAIM, and over a cut window it is false. The
            heading stays -- it names what the table is FOR -- and the note
            beside it says what the rows are out of, with the mark that makes
            a cut list look cut: the same list at 200 of 200 and at 200 of 214
            would otherwise be the same picture. */}
        <h2 className="ctl-card-title">Every holder</h2>
        <span className="ctl-card-note is-end">
          {coverage.kind !== 'complete' && <>{coverageMark(rows.length, coverage, 'This list')} </>}
          {ofTotal(rows.length, coverage)} row{rows.length === 1 && coverage.kind !== 'cut' ? '' : 's'}
          {coverage.kind === 'cut' && coverage.beyond !== null && ` · ${coverage.beyond} not shown`}
          {coverage.kind === 'cut' && coverage.beyond === null && ' · more not shown'}
          {coverage.kind === 'unreported' && ' · completeness unreported'}
        </span>
      </div>
      <div className="ctl-table is-scroll">
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
                <th role="columnheader" scope="col" className="is-num">Units (weighted)</th>
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
