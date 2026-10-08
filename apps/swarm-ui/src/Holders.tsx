import { useState, type ReactNode } from 'react'
import { loadHolders, type HoldersBoard } from './api'
import { Segmented, ToneMark } from './components'
import { HelpCard } from './HelpCard'
import { StateMark, WarnMark } from './marks'
import { Mark } from './primitives'
import { HOLDERS_POLL_MS, capacityPoll, paneHref, tableMode, useLinkedParam, useLinkedPool, usePhoneTables } from './capacityPoll'
import { Screen } from './Shell'
import { LeaseRef, TaskRef } from './TaskRef'
import './styles/capacity.css'
import { formatDuration, leaseLiveliness, poolLabel, type LeasePage, type LeaseRow } from './types'
import { AGE_TICK_MS, useNow } from './useNow'

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
 * §4.1 and has no Screen C section. The parts below came from README.md's
 * one-line description -- lease list, in-flight units, class mix,
 * accounting-drift check -- and everything else from the data traps in §1.1
 * and from what the routes actually send. Nothing here is presented as spec
 * that is not. Since the #503 audit the layout is capacity.html frame 6's:
 * the lease table first, drift per pool under it, and no class-mix card.
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
      // Decided 2026-10-01 (#117): every 30s, paused while the tab is hidden.
      pollMs={capacityPoll(HOLDERS_POLL_MS)}
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
         are on Pools. It is `empty.link`, the slot #145 gave `Screen` for
         exactly this; it sat in the body until then, and the primitive draws
         it now, closing the sentence (the `·` stays the sentence's). */
      empty={{
        heading: 'No unreleased leases',
        // THE MARK IS `Screen`'s, in the heading (#145). A hand-drawn
        // `.ctl-mark is-zero` span here said `real zero` a second time.
        body: <>no lease or counter holds capacity · every tenant ·</>,
        link: { href: '#capacity/pools', label: 'Pools' },
      }}
    >
      {(board) => {
        // READ ONCE, PASSED TO BOTH. The drift check and the table are two
        // computations over the same rows, and a coverage decided twice is
        // two chances for one of them to look whole.
        const coverage = leaseCoverage(board.page)
        const drift = driftRows(board, coverage)
        return (
          <>
            {/* THE TABLE FIRST, DRIFT UNDER IT (capacity.html frame 6, the
                #503 audit). The question this page is opened with is "what is
                holding capacity"; whether the two records of that agree is the
                check under it. A disagreement is still the first thing on the
                page -- as one callout naming the pool, above the table -- so
                moving the card down hides nothing. The Class mix card is gone
                with the summary card: the pick has neither, and the Units
                column and each lease's pools say what it said. */}
            <DriftCallout rows={drift} coverage={coverage} rowsRead={board.page.leases.length} />
            <HolderTable rows={board.page.leases} coverage={coverage} page={board.page} />
            <Drift board={board} coverage={coverage} rows={drift} />
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

/** One pool's two records of the units held: the leases' sum and its counter. */
interface DriftRow {
  name: string
  held: number
  /** The pool's own counter, or null when no counter for it was read. */
  active: number | null
}

/**
 * Units held per pool, from the leases, beside that pool's own counter --
 * every pool the comparison may honestly make, agreeing ones included
 * (capacity.html frame 6 draws them all: From leases, Counter, Delta).
 *
 * Over a CUT or unreported window, only pools that at least one loaded lease
 * names are compared: a pool no loaded lease mentions may be named by a lease
 * the window left out, and showing it with a lease side of 0 would manufacture
 * a delta out of an absence. null when the counters were not read.
 */
function driftRows(board: HoldersBoard, coverage: LeaseCoverage): DriftRow[] | null {
  if (board.pools === null) return null
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
  // above that is the leak this card exists to find.
  if (coverage.kind === 'complete' && board.page.tenant_id === null) {
    for (const p of board.pools) if (!byPool.has(p.name)) byPool.set(p.name, 0)
  }
  const pools = board.pools
  return Array.from(byPool.entries())
    .map(([name, held]) => {
      const pool = pools.find((p) => p.name === name)
      return { name, held, active: pool ? pool.active : null }
    })
    .sort((a, b) => b.held - a.held || a.name.localeCompare(b.name))
}

function disagrees(r: DriftRow): boolean {
  return r.active !== null && r.active !== r.held
}

/**
 * THE DISAGREEMENT, NAMED ABOVE THE TABLE (capacity.html frame 6's callout).
 * One line per pool whose counter differs from its leases. Over every live
 * lease that is evidence; over a cut or unreported window it may be the
 * leases the window left out, and the line says so rather than calling it a
 * leak. Nothing when every compared pool agrees, or the counters were unread.
 */
function DriftCallout({
  rows,
  coverage,
  rowsRead,
}: {
  rows: DriftRow[] | null
  coverage: LeaseCoverage
  rowsRead: number
}) {
  const off = (rows ?? []).filter(disagrees)
  if (off.length === 0) return null
  return (
    <div className="hold-callout" role="status">
      <WarnMark />
      <div>
        {off.map((r) => {
          const d = (r.active as number) - r.held
          return (
            <p key={r.name}>
              <b className="mono">{r.name}</b> counts {Math.abs(d)} {d > 0 ? 'more' : 'fewer'} than its leases.
            </p>
          )
        })}
        {/* Over a cut or unreported window the leases left out may be the
            difference, which is said once rather than called a leak. */}
        {coverage.kind !== 'complete' && <p>{coverageSay(rowsRead, coverage, 'This')}</p>}
      </div>
    </div>
  )
}

function Drift({ board, coverage, rows }: { board: HoldersBoard; coverage: LeaseCoverage; rows: DriftRow[] | null }) {
  const rowsRead = board.page.leases.length

  /* THE COUNTERS ARE GONE, SO THERE IS NO FIGURE AT ALL.
     Not a zero, not an empty table: `.ctl-mark.is-unread` plus a dash in the
     figure slot. The detail the server gave is the card's note, which is one
     line and does not wrap -- the long form is the help topic. */
  if (rows === null) {
    return (
      <section className="ctl-card hold-drift">
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

  const disagreeing = rows.filter(disagrees)
  const compared = rows.filter((r) => r.active !== null).length
  // A DELTA IS EVIDENCE ONLY OVER EVERY LIVE LEASE. Over a cut window the
  // lease side is short by whatever the window left out, so a counter above it
  // may be those leases rather than a leak -- and an agreement may be an
  // accident of which rows made the page.
  const mark = coverageMark(rowsRead, coverage, 'This comparison')

  return (
    <section className="ctl-card hold-drift">
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
      {rows.length > 0 && (
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
                {rows.map((r) => (
                  <tr role="row" key={r.name} className={disagrees(r) ? 'is-warn' : undefined}>
                    <th role="rowheader" scope="row" title={r.name}>
                      {poolLabel(r.name)}
                      <span className="ctl-sub">{r.name}</span>
                    </th>
                    <td role="cell" data-label="From leases" className="is-num">{r.held}</td>
                    <td role="cell" data-label="Counter" className="is-num">
                      {r.active === null ? <span className="ctl-em" title="No counter for this pool was read">—</span> : r.active}
                    </td>
                    <td role="cell" data-label="Delta" className="is-num">
                      {r.active === null ? (
                        <span className="ctl-em" title="No counter to compare with">—</span>
                      ) : disagrees(r) ? (
                        <span className="hold-delta">
                          <WarnMark />
                          {r.active > r.held ? '+' : ''}
                          {r.active - r.held}
                        </span>
                      ) : (
                        '0'
                      )}
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
          is what says which this is. AGREEMENT OVER EVERY LIVE LEASE is a
          measured zero, and the mark says so; over a partial set the same
          agreement is `partial` (or `not measured`), never `real zero`. */}
      <div className="ctl-card-foot">
        {mark ?? (disagreeing.length === 0 && <span className="ctl-mark is-zero">real zero</span>)}{' '}
        over {ofTotal(rowsRead, coverage)} row{rowsRead === 1 && coverage.kind !== 'cut' ? '' : 's'}
        {coverage.kind === 'cut' && coverage.beyond !== null && ` · ${coverage.beyond} live not listed`}
        {' · '}
        <code>make pool-check</code>
      </div>
    </section>
  )
}

function HolderTable({
  rows,
  coverage,
  page,
}: {
  rows: LeaseRow[]
  coverage: LeaseCoverage
  page: Pick<LeasePage, 'thresholds' | 'evaluated_at'>
}) {
  // FILTERABLE BY TENANT (capacity.html §C, decided 2026-10-01), over the rows
  // this read loaded. The filter narrows what is drawn; the note beside the
  // heading still says what the loaded rows are out of, so a filtered list is
  // never read as the platform's whole.
  // IN THE ADDRESS (QA G5-23): `?tenant=`, so a filtered list can be linked
  // to and survives a reload, beside the `?pool=` a Pools row links with.
  // A linked tenant with no row here filters nothing: `All` is drawn chosen,
  // never an empty table under a tenant chip that is not on screen.
  const linkedTenant = useLinkedParam('tenant')
  // AND BY POOL, WHEN A LINK NAMED ONE (#125): a Pools row's name links here
  // as `?pool=<name>`, and the table draws the leases whose `pools` list names
  // it. `All pools` drops it; the address is moved too, so a reload does not
  // bring it back, and `cleared` holds the choice until the router has.
  const linked = useLinkedPool()
  const [cleared, setCleared] = useState<string | null>(null)
  const pool = linked !== null && linked !== cleared ? linked : null
  const now = useNow(AGE_TICK_MS)
  const inPool = pool === null ? rows : rows.filter((l) => Array.isArray(l.pools) && l.pools.includes(pool))
  const tenants = [...new Set(inPool.map((l) => l.tenant_id))].sort()
  const tenant = linkedTenant !== null && tenants.includes(linkedTenant) ? linkedTenant : null
  const shown = tenant === null ? inPool : inPool.filter((l) => l.tenant_id === tenant)
  const sorted = [...shown].sort((a, b) => b.units - a.units)
  const clearPool = (name: string) => {
    setCleared(name)
    if (typeof window !== 'undefined') window.location.hash = paneHref('capacity/holders', { tenant })
  }
  const setTenant = (next: string | null) => {
    if (typeof window !== 'undefined') window.location.hash = paneHref('capacity/holders', { pool, tenant: next })
  }
  const phone = usePhoneTables()
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
        {pool !== null && (
          <span className="hold-pool">
            pool{' '}
            <span className="mono" title={pool}>
              {pool}
            </span>{' '}
            · {inPool.length} of {rows.length}{' '}
            <button
              type="button"
              className="hold-pool-clear"
              onClick={() => clearPool(pool)}
            >
              All pools
            </button>
          </span>
        )}
        {/* ALWAYS, AS THE PICK DRAWS IT (capacity.html frame 6, #503): All and
            each tenant in the rows. It was drawn only for two tenants or more,
            so on a board one tenant holds the table had no filter at all and
            nothing said whose rows they were; `All | eng` says it. */}
        {tenants.length > 0 && (
          // All's key is '', which no tenant id is.
          <Segmented
            className="hold-tenants"
            label="Tenant"
            value={tenant ?? ''}
            options={[{ key: '', label: 'All' }, ...tenants.map((t) => ({ key: t, label: t }))]}
            onChange={(k) => setTenant(k === '' ? null : k)}
          />
        )}
        <span className="ctl-card-note is-end">
          {coverage.kind !== 'complete' && <>{coverageMark(rows.length, coverage, 'This list')} </>}
          {ofTotal(rows.length, coverage)} row{rows.length === 1 && coverage.kind !== 'cut' ? '' : 's'}
          {coverage.kind === 'cut' && coverage.beyond !== null && ` · ${coverage.beyond} not shown`}
          {coverage.kind === 'cut' && coverage.beyond === null && ' · more not shown'}
          {coverage.kind === 'unreported' && ' · completeness unreported'}
        </span>
      </div>
      <div className={`ctl-table ${tableMode(phone)}`}>
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
                <th role="columnheader" scope="col" className="is-num">Held for</th>
                {/* LIVENESS (#92). Overview's silent-workers item links here,
                    and this table could not say which worker was silent. */}
                <th role="columnheader" scope="col">Heartbeat</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {/* A POOL NOBODY HOLDS SAYS SO (G5-14, QA 2026-10-07). The
                  filter drew a header row and nothing else, with "0 of 3" in
                  the toolbar the only clue. One row: which pool, which kind of
                  nothing -- `real zero` only over every live lease, the
                  coverage mark otherwise -- and the way back. */}
              {pool !== null && inPool.length === 0 && (
                <tr role="row" className="hold-empty">
                  <td role="cell" colSpan={7}>
                    No lease holds <span className="mono">{pool}</span> right now ·{' '}
                    {coverageMark(rows.length, coverage, 'This list') ?? <span className="ctl-mark is-zero">real zero</span>}
                    {' · '}
                    <button type="button" className="hold-pool-clear" onClick={() => clearPool(pool)}>
                      All pools
                    </button>
                  </td>
                </tr>
              )}
              {sorted.map((l) => (
                <tr role="row" key={l.lease_id}>
                  <th role="rowheader" scope="row">
                    {/* ONE TASK REFERENCE (QA G5-15): `task_…` and the last
                        eight, as Accounts prints the same task, and the lease
                        under it says it is a lease. */}
                    <TaskRef id={l.task_id} />
                    <LeaseRef id={l.lease_id} />
                  </th>
                  <td role="cell" data-label="Tenant">{l.tenant_id}</td>
                  <td role="cell" data-label="Units (weighted)" className="is-num">{l.units}</td>
                  <td role="cell" data-label="Dispatch">
                    {/* BRAND §3'S HOLDING-CAPACITY MARK (#503). LEASED and
                        DISPATCHED both hold a slot and are not yet working, so
                        both are the half-filled teal-green disc -- the mark is
                        what says "this costs capacity" (invariant 1). The word
                        says which: awaiting a backend, or asked of one. It was
                        a grey dot chip, the healthy-and-free picture. */}
                    <StateMark
                      state={l.dispatch_state === 'LEASED' ? 'LEASED' : 'DISPATCHED'}
                      label={l.dispatch_state === 'LEASED' ? 'awaiting' : 'dispatched'}
                    />
                  </td>
                  <td role="cell" data-label="Gen" className="is-num">{l.generation}</td>
                  <td role="cell" data-label="Held for" className="is-num">
                    <HeldFor createdAt={l.created_at} now={now} />
                  </td>
                  <td role="cell" data-label="Heartbeat">
                    <Heartbeat lease={l} page={page} now={now} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
      </div>
    </section>
  )
}

/**
 * How long a lease has held its units, from the lease's own `created_at` on
 * the shared age tick. An unparseable instant is an em dash -- not measured --
 * never a 0s that would read as "just leased".
 */
function HeldFor({ createdAt, now }: { createdAt: string | null | undefined; now: number }) {
  const at = createdAt ? Date.parse(createdAt) : NaN
  if (!Number.isFinite(at)) return <span title="The lease carries no readable creation time">—</span>
  return <time dateTime={createdAt ?? undefined} title={createdAt ?? undefined}>{formatDuration(now - at)}</time>
}

/**
 * How long this lease's worker has been quiet, with the verdict the reconciler
 * acts on (#92). The verdict is `leaseLiveliness` over the page's own
 * thresholds -- the function Overview's silent-workers item counts with -- so
 * a lease is "silent" here exactly when it is one of that item's.
 *
 * THE AGE TICKS ON THE ROW'S CLOCK (G5-06, QA 2026-10-07). It was
 * `silent_seconds` as of the read while Held for, beside it, moved on
 * `useNow`, so one row said "Held for 3m 29s" and "never beat, 2m 38s". A
 * lease that has beaten ages by `silent_seconds` plus the time since the page
 * was evaluated; one never beaten ages from its creation, which is what Held
 * for shows, so the two figures are the same figure.
 *
 * A LEASE NEVER BEATEN IS STARTING, NOT SILENT, until its dispatch deadline
 * passes (G5-01): neutral, with how long it has booted and how long it has
 * left.
 */
function Heartbeat({
  lease,
  page,
  now,
}: {
  lease: LeaseRow
  page: Pick<LeasePage, 'thresholds' | 'evaluated_at'>
  now: number
}) {
  const ageMs = heartbeatAgeMs(lease, page.evaluated_at, now)
  const age = formatDuration(ageMs)
  const since = lease.heartbeat_ever ? age : `no beat yet, ${age}`
  // A page that arrived without its thresholds cannot be judged, and a local
  // grace would colour at a threshold the reconciler does not act on: the age
  // alone, with no verdict.
  if (page.thresholds === undefined) return <span title="No heartbeat thresholds arrived with this page">{since}</span>
  const { kind, copy } = leaseLiveliness({ ...lease, silent_seconds: ageMs / 1000 }, page.thresholds)
  if (kind === 'starting') {
    const deadline = Date.parse(lease.dispatch_deadline)
    const left = Number.isFinite(deadline) ? deadline - now : NaN
    const due = !Number.isFinite(left) ? null : left > 0 ? `deadline in ${formatDuration(left)}` : 'deadline due'
    return (
      <ToneMark tone="info" title={copy}>
        starting · {since}
        {due !== null && <> · {due}</>}
      </ToneMark>
    )
  }
  const tone = kind === 'presumed-dead' ? 'is-bad' : kind === 'silent' ? 'is-warn' : 'is-ok'
  const word = kind === 'presumed-dead' ? 'presumed dead' : kind === 'silent' ? 'silent' : 'beating'
  return (
    <ToneMark tone={tone} title={copy}>{word} · {since}</ToneMark>
  )
}

/**
 * How long a lease's worker has been quiet, now. Never beaten: since the
 * lease's `created_at`. Beaten: the server's `silent_seconds` at
 * `evaluated_at`, plus what has passed since. Either instant unreadable, the
 * server's figure as it came -- an age that does not move, never an invented
 * one.
 */
function heartbeatAgeMs(lease: LeaseRow, evaluatedAt: string | undefined, now: number): number {
  const served = lease.silent_seconds * 1000
  if (!lease.heartbeat_ever) {
    const created = Date.parse(lease.created_at)
    return Number.isFinite(created) ? Math.max(0, now - created) : served
  }
  const read = evaluatedAt ? Date.parse(evaluatedAt) : NaN
  return Number.isFinite(read) ? served + Math.max(0, now - read) : served
}
