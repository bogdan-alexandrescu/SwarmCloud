import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { usePhoneTables, tableMode } from './capacityPoll'
import { HatchDef, useHatchId, VALUE_LABEL_GAP_PX } from './charts/parts'
import { linearScale, ValueAxis } from './charts/TimeSeries'
import { Button } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { bucketName, tickLabel, unreadWords, viewerZone, type LedgerBucketSize } from './outcomes'
import {
  BREAKDOWNS,
  TIME_BUCKETS,
  TOKEN_KINDS,
  breakdownOf,
  groupingOf,
  isRead,
  loadPlatformTime,
  platformTimeQuery,
  readEstimate,
  spanDays,
  viewKey,
  type Breakdown,
  type PlatformTime,
  type TimeBucket,
  type TokenSum,
  type UsageBucket,
  type UsageFigures,
  type UsageGroupRow,
  type UsageGrouping,
  type UsageSeries,
} from './platformTimeApi'
import { BROWSER_UNUSED_ADDRESS } from './agentlist'
import { addressToPath } from './paths'
import { HELP } from './help'
import { Mark } from './primitives'
import { timeAgo } from './types'
import { AGE_TICK_MS, useNow } from './useNow'
import './styles/overtime.css'

/**
 * ADMIN › PLATFORM COUNTS, OVER TIME (COUNTS-TIME-VIEW, #2149): how many
 * agents ran per day, week or month, of which type, for how long, on how many
 * tokens, at what estimated cost. Built to BGD's pick, Counts proposal A, the
 * Analyst dashboard (docs/web-ui/mockups/counts-a.html, frames F2 to F6). Not
 * mounted here: COUNTS-PAGE mounts it on the page.
 *
 * BGD'S DECISIONS, 2026-10-23, AND WHERE EACH IS KEPT:
 *
 *   * THE UNIT IS AGENTS, RETRIES APART. The read is `usage=submitted`
 *     (platformTimeApi.ts): an agent sits in the bucket of the day it was
 *     submitted, once, and its attempts after the first are `retries` in the
 *     same bucket -- drawn as their own neutral bars (`--series-5`) on
 *     their own scale, under the agents', never stacked into them.
 *   * THE FOUR BREAKDOWNS are the route's `usage.breakdowns`, picked from a
 *     menu that says what each groups by and what it does with an agent that
 *     has no value yet. Every one comes with each read (BGD's answer 3,
 *     2026-10-24), so picking one draws at once and never costs a read.
 *   * A BREAKDOWN STACKS THE AGENTS CHART (answer 1, mock-up A's F3 and F4):
 *     the route's five busiest keys plus "all others", each a layer, with the
 *     split table beside it; retries stay apart, as totals on their own lane.
 *     A key that says a value was not measured is never a coloured layer: it
 *     is one hatched layer on top, and its own rows in the tables. "holds no
 *     account" (answer 4) is a fact, so it stacks.
 *   * COST IS "Estimated cost (list price, not a bill)", the route's own
 *     `cost_label`, printed as served. Absent cost is "not reported", never
 *     $0; a partial one is "at least $X" with "k of n agents reported"; the
 *     stopped-run estimate is included in the sum and shown apart as "n
 *     estimated from a stopped run ($X)" (answer 2).
 *
 * NULL IS NOT 0 (charts/README.md). An unread bucket is a hatched band with no
 * bar and no digit. A sum nothing reported is "not reported". An attempt with
 * no end is not measured, so run hours over a partial count are a floor.
 *
 * NOTHING IS READ BEFORE RUN. The read is billed by the days it covers, so it
 * is a priced button, as Platform counts' own count is, and nothing here runs
 * on a timer or on a change of bucket or breakdown. A change of bucket draws
 * the read already paid for when there is one and otherwise shows what Run
 * will read and what it costs. `run` is the page's own Run (COUNTS-PAGE's one
 * shared button): each new value is one press, and mounting is none.
 *
 * ONE RUN ON THE PAGE (COUNTS-ONE-RUN; BGD, CTV-5, 2026-10-24, and
 * 2026-10-25). When the page passes `run`, this view draws no Run of its own:
 * two primary buttons each priced half of what the page reads. The page's Run
 * prices both reads summed, so this view hands it its own half through
 * `onPrice`, again whenever the bucket (and so the span) changes. Without
 * `run` the view keeps its own priced Run, as the dev preview mounts it.
 */
export interface OverTimePrice {
  /** The read's price over every workspace, or null while the roster is not known. */
  total: number | null
  /** One workspace's share, for the price while `total` is null. */
  perWorkspace: number
}

/**
 * THE SESSION'S READS, kept for the tab's lifetime as Platform counts keeps
 * its last run (#135): a read is billed, and leaving the page and coming back
 * should not throw one away. Keyed by `viewKey`, the bucket size: each keeps
 * its own answer with its own age, and every breakdown is in it.
 */
let kept: Record<string, Result<PlatformTime>> = {}

/** Forgets the session's reads, for a test that starts a new session. */
export function forgetOverTime(): void {
  kept = {}
}

const UNIT: Record<TimeBucket, string> = { day: 'day', week: 'week', month: 'month' }

export function PlatformOverTime({
  workspaces = null,
  run: pressed,
  onPrice,
}: {
  workspaces?: number | null
  run?: number
  onPrice?: (price: OverTimePrice) => void
}) {
  const [bucket, setBucket] = useState<TimeBucket>('week')
  const [breakdown, setBreakdown] = useState<Breakdown>('none')
  const [reads, setReads] = useState<Record<string, Result<PlatformTime>>>(() => ({ ...kept }))
  const [busy, setBusy] = useState(false)
  const reading = useRef(false)
  const now = useNow(AGE_TICK_MS)
  const tz = useMemo(() => viewerZone(), [])

  const key = viewKey(bucket)
  const run = reads[key] ?? null
  const data = run !== null && (run.status === 'ok' || run.status === 'stale') ? run.data : null

  // HOW MANY WORKSPACES THE PRICE IS FOR: the page's when it says, else the
  // newest read's own scope. Neither: the price is per workspace.
  const known =
    workspaces ??
    Object.values(reads)
      .map((r) => (r.status === 'ok' || r.status === 'stale') && r.data.scope.kind === 'platform' ? r.data.scope.tenants.length : null)
      .find((n) => n !== null) ??
    null
  const days = spanDays(bucket, now, tz)
  const price = readEstimate(days, known)
  const priceWords = price.total === null ? `~${int(price.perWorkspace)} reads a workspace` : `~${int(price.total)} reads`
  const spanWords = TIME_BUCKETS.find((b) => b.key === bucket)!.span
  const shared = pressed !== undefined

  // THE PAGE PRICES ITS ONE RUN WITH THIS HALF: told again only when the
  // figure moves, so a clock tick that keeps the span is no re-render above.
  useEffect(() => {
    onPrice?.({ total: price.total, perWorkspace: price.perWorkspace })
  }, [price.total, price.perWorkspace])

  // A REF, NOT `busy`: two presses inside one render both read `busy` as it
  // was before the first, and each press bills a read.
  const go = () => {
    if (reading.current) return
    reading.current = true
    setBusy(true)
    const at = key
    loadPlatformTime(platformTimeQuery(bucket, tz)).then((r) => {
      reading.current = false
      setBusy(false)
      setReads((was) => {
        const next = { ...was, [at]: r }
        // Only an answer is kept for the session; a failure is this visit's news.
        if (r.status === 'ok' || r.status === 'stale') kept = { ...kept, [at]: r }
        return next
      })
    })
  }

  // THE PAGE'S RUN: a new value is a press; the value it mounted with is not.
  const seen = useRef(pressed)
  useEffect(() => {
    if (pressed === undefined || pressed === seen.current) return
    seen.current = pressed
    go()
    // `go` reads the bucket of this render, which is the one the press is for.
  }, [pressed])

  const provenance = busy
    ? 'reading now…'
    : run === null
      ? 'not read yet'
      : data === null
        ? 'last read failed'
        : `read ${timeAgo(data.generated_at, now)} · ${int(data.reads)} reads${data.cached ? ', cached' : ''}`

  return (
    <section className="pot" aria-labelledby="pot-title">
      <div className="pot-head">
        <h2 className="pot-title" id="pot-title">Over time</h2>
        <p className="pot-sub">Agents counted on the day they were submitted. Sealed days answer from the rollup; the newest are derived at Run.</p>
      </div>
      <div className="pot-bar">
        <div className="c-seg" role="radiogroup" aria-label="Bucket">
          {TIME_BUCKETS.map((b) => (
            <button key={b.key} type="button" role="radio" aria-checked={b.key === bucket} onClick={() => setBucket(b.key)}>
              {b.label} <em className="pot-seg-span">{b.span}</em>
            </button>
          ))}
        </div>
        <BreakdownMenu value={breakdown} onPick={setBreakdown} />
        <span className="pot-run">
          <span className="pot-prov">{provenance}</span>
          {/* NO SECOND RUN when the page has one: its Run reads this too. */}
          {!shared && (
            <Button kind={run === null ? 'primary' : 'secondary'} onClick={go} aria-disabled={busy || undefined}>
              {run === null ? 'Run' : 'Run again'}
              {' · '}
              <span className="pot-price">{priceWords}</span>
            </Button>
          )}
        </span>
      </div>
      <p className="pot-note">
        {/* PART OF THE PAGE'S PRICE, NOT THE PRICE (visual QA pass 1, admin-shell-15): beside the page's
            Run this half read as the whole, ~538 under a button that says ~562. Right now's note adds the two up. */}
        {shared && 'The page’s Run reads this too, with the snapshot below. '}
        {price.total === null
          ? `${shared ? 'This view’s part' : 'The price'} is ~${int(price.perWorkspace)} reads for each workspace: one stored day for each of ${days} days, and the newest derived live. `
          : `${shared ? 'This view’s part' : 'The price'} is ~${int(price.total)} reads for ${spanWords} of stored days over ${known} workspaces, and the newest derived live. `}
        A breakdown costs no read: it groups the same days. Nothing here refreshes on its own. Times in your zone, {tz}. Weeks start Monday.
      </p>

      <div className="pot-body" aria-busy={busy || undefined}>
        {run === null ? (
          <NotRun bucket={bucket} breakdown={breakdown} spanWords={spanWords} priceWords={priceWords} busy={busy} />
        ) : run.status === 'error' ? (
          <Failed error={run.error} />
        ) : run.status === 'empty' || data === null ? (
          <Failed error={null} />
        ) : (
          <Answer data={data} breakdown={breakdown} tz={tz} />
        )}
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// The controls
// ---------------------------------------------------------------------------

/**
 * THE BREAKDOWN PICKER (F6): a menu whose every option says what it groups by
 * and what it does with an agent that has no value yet. Picking reads nothing.
 */
function BreakdownMenu({ value, onPick }: { value: Breakdown; onPick: (b: Breakdown) => void }) {
  const [open, setOpen] = useState(false)
  const host = useRef<HTMLSpanElement | null>(null)
  return (
    <span
      className="c-menu-host pot-menu-host"
      ref={host}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.stopPropagation()
          setOpen(false)
          host.current?.querySelector<HTMLButtonElement>('button')?.focus()
        }
      }}
      onBlur={(e) => {
        if (open && host.current !== null && !host.current.contains(e.relatedTarget as Node | null)) setOpen(false)
      }}
    >
      <Button aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        Breakdown <b className="pot-pick">{breakdownOf(value).label}</b>
      </Button>
      {open && (
        <div className="pot-menu" role="menu" aria-label="Break agents down by">
          <p className="pot-menu-head" aria-hidden>Break agents down by</p>
          {BREAKDOWNS.map((b) => (
            <button
              key={b.key}
              type="button"
              role="menuitemradio"
              aria-checked={b.key === value}
              className="pot-menu-item"
              onClick={() => {
                onPick(b.key)
                setOpen(false)
              }}
            >
              <span className="pot-menu-name">{b.label}</span>
              <span className="pot-menu-says">{b.says}</span>
            </button>
          ))}
          <p className="pot-menu-foot">Picking one reads nothing: every breakdown comes with the read.</p>
        </div>
      )}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Before Run, and a failed run
// ---------------------------------------------------------------------------

const TILE_NAMES = ['Agents', 'Retries', 'Run hours', 'Tokens, input + output', 'Estimated cost'] as const

/**
 * NOTHING HAS BEEN READ (F1). Every tile keeps its place with a dash and the
 * neutral not-measured mark, and the charts are one card that names what Run
 * will read and what it costs: no empty axes, so an unread page is never
 * mistaken for a quiet platform.
 */
function NotRun({ bucket, breakdown, spanWords, priceWords, busy }: { bucket: TimeBucket; breakdown: Breakdown; spanWords: string; priceWords: string; busy: boolean }) {
  const grouping = breakdown === 'none' ? 'no breakdown' : `by ${breakdownOf(breakdown).label.toLowerCase()}`
  return (
    <>
      <div className="pot-tiles">
        {TILE_NAMES.map((name) => (
          <div className={`pot-tile${name === 'Estimated cost' ? ' is-cost' : ''}`} key={name}>
            <p className="pot-tile-label">{name}</p>
            <p className="pot-tile-value is-absent">
              <span aria-hidden>—</span>{' '}
              {busy ? (
                <Mark kind="pending" say="Reading now. Nothing has come back yet, so there is no figure to show." />
              ) : (
                <Mark kind="absent" say="Not run yet. Nothing has been read, so there is no figure to show." />
              )}
            </p>
            <p className="pot-tile-foot">{busy ? 'reading now' : 'not run yet, so nothing has been read'}</p>
          </div>
        ))}
      </div>
      <div className="ctl-card pot-card pot-notrun">
        <p>
          <b>{busy ? 'Reading now.' : 'Not run yet.'}</b> Nothing has been read, so there is nothing to draw, and this says nothing about the platform.
          {busy ? ' This run reads' : ' Run reads'} {spanWords} of agents ({UNIT[bucket]} buckets, {grouping}) across every workspace: {priceWords}.
        </p>
        <ul className="pot-notrun-list">
          <li><b>Agents over time</b>: agents per {UNIT[bucket]}, retries apart.</li>
          <li><b>Estimated cost (list price, not a bill)</b>: cost per {UNIT[bucket]} with its coverage, and cost per agent.</li>
          <li><b>Breakdown</b>: every figure as a table.</li>
        </ul>
      </div>
    </>
  )
}

function Failed({ error }: { error: ApiError | null }) {
  return (
    <div className="ctl-empty is-failed">
      <span className="ctl-mark is-unread" aria-label="No figures are shown, because none arrived. This says nothing about the platform.">
        not read
      </span>
      <h3>{error === null ? 'Nothing came back' : errorHeading(error)}</h3>
      <p>{error === null ? 'Treat this as a failed read.' : `${error.code ?? 'error'} — ${error.message}`}</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Formatting: every figure beside how much of it was measured
// ---------------------------------------------------------------------------

function int(n: number): string {
  return Math.round(n).toLocaleString('en-US')
}

/** Three significant figures, so 1.35 B and 47.4 M and 978 k read alike. */
function sig(v: number): string {
  return v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : v.toFixed(0)
}

function tokens(n: number): string {
  if (n >= 1e9) return `${sig(n / 1e9)} B`
  if (n >= 1e6) return `${sig(n / 1e6)} M`
  if (n >= 1e4) return `${sig(n / 1e3)} k`
  return int(n)
}

function hours(s: number): string {
  const h = s / 3600
  if (h === 0) return '0 h'
  if (h < 0.1) return 'under 0.1 h'
  return h < 10 ? `${h.toFixed(1)} h` : `${int(h)} h`
}

/** Dollars as a person reads them; `$0.00` only for a measured zero, which the caller has already told from an absence. */
export function usd(n: number): string {
  if (n === 0) return '$0.00'
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`
  return Math.abs(n) < 100 ? `$${n.toFixed(2)}` : `$${int(n)}`
}

function pct(part: number, whole: number): string {
  const p = (100 * part) / whole
  return p < 10 ? `${p.toFixed(1)}%` : `${Math.round(p)}%`
}

/** A figure, whether it is a floor, and what it covers. `absent` has no number at all. */
interface Cell {
  text: string
  tone: 'plain' | 'floor' | 'absent'
  cover: string[]
  /**
   * The cover's short form, for the readout laid over the chart only, where a
   * column is about 136px: a cell whose full cover is wider than that has one
   * (re-review of PR 2348, 2026-10-26). The full words stay what a screen
   * reader hears, and what the readout shows under the chart.
   */
  brief?: string[]
}

/**
 * A figure no agent here could have yet: every agent in the row is still
 * open (or ended after the span), so nothing about it was measured. Not "not
 * reported", which says an ended agent left it out.
 */
const NONE_ENDED = 'none ended'

/**
 * RETRIES ARE COUNTED FROM ENDED AGENTS (`_Usage.add`): an open agent adds
 * itself to `agents` and nothing else. So a row in which none ended has no
 * retry count, not a count of 0.
 */
function retriesCell(f: UsageFigures): Cell {
  if (f.agents_ended === 0 && f.agents > 0) return { text: NONE_ENDED, tone: 'absent', cover: [] }
  return { text: int(f.retries), tone: 'plain', cover: [] }
}

function runCell(f: UsageFigures): Cell {
  const r = f.run_s
  if (r.sum === null) return { text: f.agents_ended === 0 ? NONE_ENDED : 'not reported', tone: 'absent', cover: r.attempts > 0 ? [`0 of ${int(r.attempts)} measured`] : [] }
  const floor = r.measured < r.attempts
  return { text: `${floor ? 'at least ' : ''}${hours(r.sum)}`, tone: floor ? 'floor' : 'plain', cover: [`${int(r.measured)} of ${int(r.attempts)} measured`] }
}

function tokenCell(t: TokenSum, ended: number): Cell {
  if (t.sum === null) return { text: ended === 0 ? NONE_ENDED : 'not reported', tone: 'absent', cover: t.attempts > 0 ? [`0 of ${int(t.attempts)} reported`] : [] }
  const floor = t.reporting < t.attempts
  return { text: `${floor ? 'at least ' : ''}${tokens(t.sum)}`, tone: floor ? 'floor' : 'plain', cover: [`${int(t.reporting)} of ${int(t.attempts)} reported`] }
}

function costCell(f: UsageFigures): Cell {
  const c = f.cost
  const cover = [`${int(c.reporting)} of ${int(c.agents)} agents reported`]
  // BGD's answer 2: the count of agents beside the dollars, as F2's cost tile.
  if (c.estimated > 0) cover.push(`${int(c.estimated)} estimated from a stopped run${c.estimated_usd === null ? '' : ` (${usd(c.estimated_usd)})`}`)
  // BGD: a null cost reads "not reported", whatever the reason; a row where
  // none ended says why on the line under it.
  if (c.sum_usd === null) return { text: 'not reported', tone: 'absent', cover: f.agents_ended === 0 && f.agents > 0 ? ['none ended yet', ...cover] : cover }
  const floor = c.reporting < c.agents
  return { text: `${floor ? 'at least ' : ''}${usd(c.sum_usd)}`, tone: floor ? 'floor' : 'plain', cover }
}

/**
 * UNUSED BROWSER TIME (BROWSER-USE-UI, #2322): the run hours of the ended
 * browser-runtime agents the worker found never used their browser
 * (`usage.*.browser`, BROWSER-USE-FLAG). A DASH WITH ITS REASON, never 0 h,
 * when the read carries no `browser` (an API older than the record) or no
 * browser agent in the row has a measured run. A FLOOR when some browser
 * agents have no verdict: theirs may be unused too, and they are counted in
 * neither verdict.
 */
export function unusedBrowserCell(f: Pick<UsageFigures, 'browser'>): Cell {
  const b = f.browser
  if (b === undefined || b === null) return { text: '—', tone: 'absent', cover: ['not in this read: the API serving it does not record browser use yet'], brief: ['not in this read'] }
  if (b.unused_s === null) {
    return b.agents === 0
      ? { text: '—', tone: 'absent', cover: ['no agent ended on a browser runtime'], brief: ['no browser agent'] }
      : { text: '—', tone: 'absent', cover: [`none of ${int(b.agents)} browser agents has a measured run`], brief: [`none of ${int(b.agents)} measured`] }
  }
  const floor = b.unknown_agents > 0
  const brief = [`${int(b.unused_agents)} of ${int(b.agents)} agents`]
  const cover = [`${int(b.unused_agents)} of ${int(b.agents)} browser agents did not use it`]
  if (b.run_s !== null) cover.push(`of ${hours(b.run_s)} browser run time`)
  if (floor) cover.push(`${int(b.unknown_agents)} with no verdict, counted in neither`)
  return { text: `${floor ? 'at least ' : ''}${hours(b.unused_s)}`, tone: floor ? 'floor' : 'plain', cover, brief }
}

/** Input plus output, one figure: a floor when either is, absent only when both are. */
function inOutCell(f: UsageFigures): Cell {
  const i = f.tokens.input_tokens
  const o = f.tokens.output_tokens
  if (i.sum === null && o.sum === null) return tokenCell(i, f.agents_ended)
  const floor = i.sum === null || o.sum === null || i.reporting < i.attempts || o.reporting < o.attempts
  const both = (i.sum ?? 0) + (o.sum ?? 0)
  const k = `${int(Math.min(i.reporting, o.reporting))} of ${int(i.attempts)} attempts`
  // The brief is the table's "k of n attempts", without the trailing verb.
  return { text: `${floor ? 'at least ' : ''}${tokens(both)}`, tone: floor ? 'floor' : 'plain', cover: [`${k} reported`], brief: [k] }
}

/** The words that make a figure a floor; drawn as a small prefix, so the figure keeps its column. */
const FLOOR = 'at least '

/**
 * A figure and its coverage. `partialOnly`: the table's run and token cells
 * print "k of n" only under a figure that is not whole -- a floor, or none
 * reported -- because a line of "488 of 488" under every cell pushed the cost
 * column off a 1440 page. The table's note says a figure with no "k of n" was
 * reported by every attempt. Cost always carries its "k of n agents reported"
 * (BGD, 2026-10-23).
 */
function CellView({ cell, partialOnly = false, brief = false }: { cell: Cell; partialOnly?: boolean; brief?: boolean }) {
  // In the table the head note says what "k of n" counts, so the cell drops
  // the trailing verb: "547 of 549 reported" was the widest thing in a row.
  const cover = partialOnly ? (cell.tone === 'plain' ? [] : cell.cover.map((c) => c.replace(/ (reported|measured)$/, ''))) : cell.cover
  // ONE ELEMENT: on a phone the record's cell is a two-column grid (the key,
  // then the value), and a figure and its coverage as two children put the
  // coverage on a row of its own, beside the next key (review, 2026-10-24).
  return (
    <span className="pot-cell">
      <span className={`pot-fig is-${cell.tone}`}>
        {cell.text.startsWith(FLOOR) ? (
          <>
            <span className="pot-atleast">{FLOOR}</span>
            {cell.text.slice(FLOOR.length)}
          </>
        ) : (
          cell.text
        )}
      </span>
      {brief && cell.brief !== undefined ? (
        <>
          {/* BOTH FORMS, AND overtime.css PICKS: over the chart the short one
              shows and the full one is clipped to nothing but still read;
              under it (a phone, or a readout too tall to lay over the chart)
              only the full one is drawn. */}
          {cover.map((c) => <span className="pot-cov is-full" key={c}>{c}</span>)}
          {cell.brief.map((c) => <span className="pot-cov is-brief" aria-hidden key={`brief:${c}`} title={cover.join('; ')}>{c}</span>)}
        </>
      ) : (
        cover.map((c) => <span className="pot-cov" key={c}>{c}</span>)
      )}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The answer
// ---------------------------------------------------------------------------

type ReadBucket = UsageBucket & UsageFigures

function Answer({ data, breakdown, tz }: { data: PlatformTime; breakdown: Breakdown; tz: string }) {
  const [picked, setPicked] = useState<number | null>(null)
  // THE HOVERED BUCKET (F6): its readout follows the pointer until a bucket is
  // picked, and a picked one stays put under a passing pointer.
  const [hovered, setHovered] = useState<number | null>(null)
  const u = data.usage
  const size: LedgerBucketSize = data.bucket
  const unread = u.buckets.filter((b) => !isRead(b)).length
  const unit = UNIT[size as TimeBucket] ?? size
  const grouping = groupingOf(u, breakdown)
  const layers = grouping === null ? null : layersOf(grouping)
  const label = breakdownOf(breakdown).label.toLowerCase()
  const shown = picked ?? hovered
  return (
    <>
      <Tiles data={data} unread={unread} unit={unit} />
      <section className="ctl-card pot-card pot-agents" aria-labelledby="pot-agents-title">
        <div className="pot-card-head">
          <h3 className="pot-card-title" id="pot-agents-title">Agents over time{layers !== null && `, by ${label}`}</h3>
          <p className="pot-card-sub">
            Agents per {unit}, placed on the day each was submitted. Retries sit apart, on their own scale.
            {layers !== null && layers.unknown.length > 0 && ' What is not known yet is hatched on top, never a colour of its own.'}
          </p>
        </div>
        <div className={layers === null ? 'pot-agents-body' : 'pot-agents-body is-split'}>
          {/* THE PLOT: the chart and its readout, which overtime.css lays over
              the chart where the plot is wide (F6) and keeps under it on a
              phone (F7). */}
          <div className="pot-plot">
            <AgentsFigure buckets={u.buckets} layers={layers} size={size} tz={tz} picked={picked} onPick={setPicked} onHover={setHovered} />
            {shown !== null && u.buckets[shown] !== undefined && (
              <Readout
                bucket={u.buckets[shown]!}
                index={shown}
                count={u.buckets.length}
                hover={picked === null}
                layers={layers}
                by={grouping?.by ?? null}
                size={size}
                tz={tz}
                onClose={() => setPicked(null)}
              />
            )}
          </div>
          {layers !== null && <SplitTable layers={layers} by={grouping!.by} />}
        </div>
        <Legend
          keys={[
            ...(layers === null ? [['pot-key-agents', 'agents'] as const] : []),
            ['pot-key-retries', 'retries, own scale'],
            ['pot-key-unread', 'not read: no figure, not zero'],
            ['pot-key-wash', 'in progress: the bucket is still filling'],
          ]}
        />
      </section>
      <CostCard data={data} size={size} tz={tz} picked={picked} />
      <Table data={data} grouping={grouping} breakdown={breakdown} tz={tz} />
    </>
  )
}

// ---------------------------------------------------------------------------
// The layers of a breakdown (answer 1)
// ---------------------------------------------------------------------------

/**
 * A GROUPING AS LAYERS. `stacked` is the route's measured series in its order
 * -- the busiest first, so at the bottom -- then "all others"; `unknown` is
 * every series that is not a measurement, drawn as ONE hatched layer on top
 * (charts README: an unknown is never a stacked colour) and listed apart.
 */
interface Layers {
  stacked: UsageSeries[]
  unknown: UsageSeries[]
}

function layersOf(g: UsageGrouping): Layers {
  return { stacked: g.series.filter((s) => s.measured), unknown: g.series.filter((s) => !s.measured) }
}

/** The swatch class of a stacked layer: the five series colours, then a neutral for "all others". */
function layerClass(s: UsageSeries, i: number): string {
  return s.other ? 'pot-layer-other' : `pot-layer-${Math.min(i, 4)}`
}

/**
 * THE WORDS FOR A KEY. Under pool account, the unknowns say what is unknown:
 * "not ended" is F4's "account not known yet" (it is read off the agent's
 * events when it ends), apart from "holds no account" (answer 4).
 */
function keyName(by: string | null, s: Pick<UsageSeries, 'key' | 'other' | 'keys'>): string {
  if (s.other) return `${s.key} (${int(s.keys)})`
  if (by === 'pool_account') {
    if (s.key === 'not ended') return 'account not known yet'
    if (s.key === 'not recorded') return 'account not recorded'
    if (s.key === 'unread') return 'account not read'
  }
  return s.key
}

function sumOf(v: ReadonlyArray<number | null>): number {
  return v.reduce<number>((n, x) => n + (x ?? 0), 0)
}

/**
 * THE SPLIT TABLE BESIDE THE STACK (F3, F4): top of the stack first, so it
 * reads down the chart beside it -- the hatched unknowns, then "all others",
 * then the named layers -- each with its agents over the read buckets and its
 * share, and their total, which is the Agents tile's.
 */
function SplitTable({ layers, by }: { layers: Layers; by: string }) {
  const rows = [
    ...layers.unknown.map((s) => ({ s, cls: 'pot-key-unread' })),
    ...layers.stacked.map((s, i) => ({ s, cls: layerClass(s, i) })).reverse(),
  ]
  const total = rows.reduce((n, r) => n + sumOf(r.s.agents), 0)
  return (
    <div className="pot-split">
      <table aria-label="Layers, top of the stack first">
        <thead>
          <tr>
            <th scope="col">Layer, top of the stack first</th>
            <th scope="col" className="is-num">Agents</th>
            <th scope="col" className="is-num">Share</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ s, cls }) => {
            const n = sumOf(s.agents)
            return (
              <tr key={s.key} data-key={s.key}>
                <th scope="row">
                  <span className="pot-split-name">
                    <i className={`pot-key ${cls}`} aria-hidden />
                    <span>{keyName(by, s)}</span>
                  </span>
                </th>
                <td className="is-num">{int(n)}</td>
                <td className="is-num">{total === 0 ? '—' : pct(n, total)}</td>
              </tr>
            )
          })}
          <tr data-key="__total" className="pot-row-total">
            <th scope="row">All agents</th>
            <td className="is-num">{int(total)}</td>
            <td />
          </tr>
        </tbody>
      </table>
    </div>
  )
}

/**
 * THE CHANGE ON A TILE: the last full bucket against the one before, never the
 * span against the previous span (that would double the read). An in-progress
 * bucket is never the "last full" one, and an unread one breaks the pair.
 */
function change(
  buckets: readonly UsageBucket[],
  value: (b: ReadBucket) => number | null,
  floor: (b: ReadBucket) => boolean,
  unit: string,
): string | null {
  const full = buckets.filter((b) => !b.in_progress)
  const last = full[full.length - 1]
  const before = full[full.length - 2]
  if (last === undefined || before === undefined || !isRead(last) || !isRead(before)) return null
  const a = value(last)
  const b = value(before)
  if (a === null || b === null || b === 0) return null
  const d = (100 * (a - b)) / b
  const arrow = d > 0 ? '▲' : d < 0 ? '▼' : '='
  // F5's words for a day view whose newest bucket is today, still filling.
  const which = unit === 'day' && buckets[buckets.length - 1]?.in_progress && full.length === buckets.length - 1 ? 'yesterday vs the day before' : `last full ${unit} vs the one before`
  // A change between two floors is a change between two lower bounds: said, never hidden.
  const floors = floor(last) && floor(before) ? ' (both floors)' : floor(last) || floor(before) ? ' (one a floor)' : ''
  return `${arrow} ${Math.abs(Math.round(d))}% ${which}${floors}`
}

/** The tile's name, and the label Help and the tests find it by. */
export const UNUSED_BROWSER = 'Unused browser time'

/**
 * A tile's layout class. On a phone the tiles are two columns, and the two
 * long figures, tokens and cost, take a row each, so six tiles fill the grid
 * with no hole (styles/overtime.css).
 */
const TILE_CLASS: Readonly<Record<string, string>> = { 'Estimated cost': ' is-cost', 'Tokens, input + output': ' is-tokens' }

function Tiles({ data, unread, unit }: { data: PlatformTime; unread: number; unit: string }) {
  const u = data.usage
  const t = u.totals
  const n = u.buckets.length
  const runs = runCell(t)
  const unusedBrowser = unusedBrowserCell(t)
  const inout = inOutCell(t)
  const cost = costCell(t)
  const cacheRead = tokenCell(t.tokens.cache_read_input_tokens, t.agents_ended)
  const cacheWrite = tokenCell(t.tokens.cache_creation_input_tokens, t.agents_ended)
  const io = (b: ReadBucket) => {
    const s = (b.tokens.input_tokens.sum ?? 0) + (b.tokens.output_tokens.sum ?? 0)
    return b.tokens.input_tokens.sum === null && b.tokens.output_tokens.sum === null ? null : s
  }
  const readNote = unread > 0 ? `; ${n - unread} of ${n} ${unit}s read` : ''
  const never = () => false
  const tiles: Array<{ name: string; value: ReactNode; series: (b: ReadBucket) => number | null; floor: (b: ReadBucket) => boolean; foot: string[]; tone: Cell['tone']; links?: Array<{ href: string; text: string; title: string }> }> = [
    { name: 'Agents', value: int(t.agents), tone: 'plain', series: (b) => b.agents, floor: never, foot: [`submitted in the span, each counted once${readNote}`] },
    {
      name: 'Retries',
      value: int(t.retries),
      tone: 'plain',
      series: (b) => b.retries,
      floor: never,
      foot: [`attempts after an agent’s first${t.attempts > 0 ? ` · ${pct(t.retries, t.attempts)} of ${int(t.attempts)} attempts` : ''}`],
    },
    {
      name: 'Run hours',
      value: runs.text,
      tone: runs.tone,
      series: (b) => (b.run_s.sum === null ? null : b.run_s.sum / 3600),
      floor: (b) => runCell(b).tone === 'floor',
      foot: [
        'summed over measured attempts',
        t.run_s.measured < t.run_s.attempts
          ? `${int(t.run_s.measured)} of ${int(t.run_s.attempts)} attempts measured; the rest are open or never wrote an end, not 0 h`
          : `all ${int(t.run_s.attempts)} attempts measured`,
      ],
    },
    // BESIDE THE RUN HOURS IT IS A PART OF (BROWSER-USE-UI, #2322): browser
    // runtime hours whose agent never used the browser, with the agents they
    // were, linked to the Agents list of exactly those (`?browser=unused`).
    {
      name: UNUSED_BROWSER,
      value: unusedBrowser.text,
      tone: unusedBrowser.tone,
      series: (b) => (b.browser === undefined || b.browser === null || b.browser.unused_s === null ? null : b.browser.unused_s / 3600),
      floor: (b) => unusedBrowserCell(b).tone === 'floor',
      foot: unusedBrowser.cover,
      // WHAT "NOT USED" MEANS IS A LINK HERE, NOT A `?` (B7.4: the console's
      // glyph budget is spent), and only once the figure is drawn: before Run
      // the page holds to its word budget.
      links: [
        ...(t.browser === undefined || t.browser === null
          ? []
          : [
              {
                href: addressToPath(BROWSER_UNUSED_ADDRESS),
                text: 'Agents that did not use it ›',
                title: 'The Agents list, Recent, filtered to browser agents whose browser was not used. It lists the agents loaded there, not this span.',
              },
            ]),
        { href: `#${HELP['browser-not-used'].anchor}`, text: 'What “not used” means', title: HELP['browser-not-used'].title },
      ],
    },
    {
      name: 'Tokens, input + output',
      value: inout.text,
      tone: inout.tone,
      series: io,
      floor: (b) => inOutCell(b).tone === 'floor',
      foot: [`cache read ${cacheRead.text} · cache write ${cacheWrite.text}`, `input and output: ${inout.cover[0] ?? 'none reported'}`],
    },
    { name: 'Estimated cost', value: cost.text, tone: cost.tone, series: (b) => b.cost.sum_usd, floor: (b) => costCell(b).tone === 'floor', foot: ['list price, not a bill', cost.cover.join(' · ')] },
  ]
  return (
    <div className={`pot-tiles${tiles.length === 6 ? ' is-six' : ''}`}>
      {tiles.map((tile) => {
        const moved = change(u.buckets, tile.series, tile.floor, unit)
        return (
          <div className={`pot-tile${TILE_CLASS[tile.name] ?? ''}`} key={tile.name} data-tile={tile.name}>
            <p className="pot-tile-label">{tile.name}</p>
            {typeof tile.value === 'string' && tile.value.startsWith(FLOOR) ? (
              <p className={`pot-tile-value is-${tile.tone}`}>
                <span className="pot-tile-floor">{FLOOR}</span>
                {tile.value.slice(FLOOR.length)}
              </p>
            ) : (
              <p className={`pot-tile-value is-${tile.tone}`}>{tile.value}</p>
            )}
            <Spark buckets={u.buckets} value={tile.series} />
            {moved !== null && <p className="pot-tile-move">{moved}</p>}
            {tile.foot.map((f) => (
              <p className="pot-tile-foot" key={f}>{f}</p>
            ))}
            {tile.links?.map((l) => (
              <p className="pot-tile-foot" key={l.href}>
                <a className="ctl-link pot-tile-link" href={l.href} title={l.title}>
                  {l.text}
                </a>
              </p>
            ))}
          </div>
        )
      })}
    </div>
  )
}

/**
 * A TILE'S GLANCE: the same series as the big charts, in miniature, with no
 * axis and no digit (so no text to shrink). Broken at an unread bucket or a
 * missing sum; the in-progress bucket is a hollow point off the line, because
 * a bucket still filling is not comparable to the ones before it.
 */
function Spark({ buckets, value }: { buckets: readonly UsageBucket[]; value: (b: ReadBucket) => number | null }) {
  const W = 120
  const H = 26
  const vals = buckets.map((b) => (isRead(b) ? value(b) : null))
  const max = Math.max(0, ...vals.map((v) => v ?? 0))
  if (max === 0) return <svg className="pot-spark" viewBox={`0 0 ${W} ${H}`} aria-hidden />
  const x = (i: number) => (buckets.length === 1 ? W / 2 : 3 + (i * (W - 6)) / (buckets.length - 1))
  const y = (v: number) => H - 3 - (v / max) * (H - 6)
  const runs: string[] = []
  let cur: string[] = []
  vals.forEach((v, i) => {
    const open = buckets[i]!.in_progress
    if (v === null || open) {
      if (cur.length > 1) runs.push(cur.join(' '))
      cur = []
      return
    }
    cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`)
  })
  if (cur.length > 1) runs.push(cur.join(' '))
  const lastIndex = buckets.length - 1
  const last = vals[lastIndex]
  return (
    <svg className="pot-spark" viewBox={`0 0 ${W} ${H}`} aria-hidden>
      {runs.map((p) => (
        <polyline key={p} points={p} />
      ))}
      {last !== null && last !== undefined && buckets[lastIndex]!.in_progress && <circle className="is-open" cx={x(lastIndex)} cy={y(last)} r={2.5} />}
    </svg>
  )
}

function Legend({ keys }: { keys: ReadonlyArray<readonly [string, string]> }) {
  return (
    <ul className="pot-legend">
      {keys.map(([cls, words]) => (
        <li key={cls}>
          <i className={`pot-key ${cls}`} aria-hidden />
          {words}
        </li>
      ))}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// The charts: each drawn at its own widths, never scaled (charts/README.md)
// ---------------------------------------------------------------------------

/**
 * THE AUTHORED WIDTHS. 640 and 300 are the inspector charts' pair (`DRAWN`,
 * parts.tsx); the agents chart also has the outcome ledger's 1080-wide
 * drawing, because on a 1440 page it sits in a card about 1,030 wide and the
 * 640 drawing would be scaled up to text 19px tall. overtime.css shows the
 * widest drawing its container holds, so none is ever scaled down.
 */
const AGENT_DRAWN = [
  { key: 'xl', w: 1040, h: 300, ticks: 4 },
  { key: 'wide', w: 640, h: 260, ticks: 3 },
  { key: 'narrow', w: 300, h: 240, ticks: 2 },
] as const
const COST_DRAWN = [
  { key: 'wide', w: 640, h: 210, ticks: 3 },
  { key: 'narrow', w: 300, h: 190, ticks: 2 },
] as const

type Drawing = { key: string; w: number; h: number; ticks: number }

/**
 * Room between two value labels on this view's axes. The shared gap is for
 * drawings shown at their own size; these grow up to a fifth past theirs, and
 * at the shared gap the cost axis printed "$443" over "$400" with nothing
 * between them (review, 2026-10-25).
 */
const AXIS_GAP_PX = Math.max(VALUE_LABEL_GAP_PX, 24)

const GUTTER = 48
/** Room past the last column for the newest x label, which is centred on it and anchored there. */
const RIGHT = 26

/** Which buckets carry an x label: about one per `minPx`, anchored on the newest. */
function labelled(n: number, colPx: number, minPx: number): (i: number) => boolean {
  const stride = Math.max(1, Math.ceil(minPx / Math.max(1, colPx)))
  return (i) => (n - 1 - i) % stride === 0
}

function xLabelPx(size: LedgerBucketSize): number {
  return size === 'month' ? 44 : 56
}

/** Keyboard on a chart root: arrows move the readout a bucket, Home/End to the ends, Esc closes it. */
function moveKeys(n: number, picked: number | null, onPick: (i: number | null) => void) {
  return (e: KeyboardEvent<HTMLElement>) => {
    const at = picked ?? n - 1
    const next = e.key === 'ArrowLeft' ? at - 1 : e.key === 'ArrowRight' ? at + 1 : e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : null
    if (e.key === 'Escape') {
      onPick(null)
      return
    }
    if (next === null) return
    e.preventDefault()
    onPick(Math.max(0, Math.min(n - 1, picked === null ? at : next)))
  }
}

function AgentsFigure({ buckets, layers, size, tz, picked, onPick, onHover }: { buckets: readonly UsageBucket[]; layers: Layers | null; size: LedgerBucketSize; tz: string; picked: number | null; onPick: (i: number | null) => void; onHover: (i: number | null) => void }) {
  const hatch = useHatchId()
  const read = buckets.filter(isRead)
  const maxAgents = Math.max(0, ...read.map((b) => b.agents))
  const maxRetries = Math.max(0, ...read.map((b) => b.retries))
  return (
    <div
      className="pot-chart"
      role="group"
      tabIndex={0}
      aria-label={`Agents and retries per ${size}. Arrow keys move the readout one ${size}; Escape closes it.`}
      onKeyDown={moveKeys(buckets.length, picked, onPick)}
    >
      {AGENT_DRAWN.map((d) => (
        <AgentsDrawing key={d.key} d={d} hatch={`${hatch}-${d.key}`} buckets={buckets} layers={layers} size={size} tz={tz} maxAgents={maxAgents} maxRetries={maxRetries} picked={picked} onPick={onPick} onHover={onHover} />
      ))}
    </div>
  )
}

function AgentsDrawing({
  d, hatch, buckets, layers, size, tz, maxAgents, maxRetries, picked, onPick, onHover,
}: {
  d: Drawing
  hatch: string
  buckets: readonly UsageBucket[]
  layers: Layers | null
  size: LedgerBucketSize
  tz: string
  maxAgents: number
  maxRetries: number
  picked: number | null
  onPick: (i: number | null) => void
  onHover: (i: number | null) => void
}) {
  const n = buckets.length
  const plotW = d.w - GUTTER - RIGHT
  const col = plotW / Math.max(1, n)
  const barW = Math.max(1, Math.min(col * 0.72, 40))
  const top = 22
  const retriesH = Math.round(d.h * 0.2)
  const axisH = 22
  const gap = 26
  const agentsH = d.h - top - gap - retriesH - axisH
  const agentsBase = top + agentsH
  const retriesTop = agentsBase + gap
  const retriesBase = retriesTop + retriesH
  const aScale = linearScale({ lo: 0, hi: maxAgents, degenerate: maxAgents === 0 }, [agentsBase, top])
  const rScale = linearScale({ lo: 0, hi: maxRetries, degenerate: maxRetries === 0 }, [retriesBase, retriesTop])
  const showLabel = labelled(n, col, xLabelPx(size))
  const x0 = (i: number) => GUTTER + i * col
  const barX = (i: number) => x0(i) + (col - barW) / 2
  const height = (scale: typeof aScale, base: number, v: number, max: number) => (max === 0 || v === 0 ? 0 : Math.max(1, base - scale(v)))
  return (
    <svg className={`pot-svg is-${d.key}`} viewBox={`0 0 ${d.w} ${d.h}`} width={d.w} height={d.h} role="group" aria-label={`Agents per ${size}, drawn ${d.w} wide`} onMouseLeave={() => onHover(null)}>
      <HatchDef id={hatch} />
      <text className="pot-lane" x={GUTTER} y={top - 8}>agents per {size}</text>
      {/* The narrow drawing's label is short: the long one ran the width of the
          plot and the picked bucket's ring struck through it (review, 2026-10-25). */}
      <text className="pot-lane" x={GUTTER} y={retriesTop - 8}>{d.key === 'narrow' ? 'retries, own scale' : `retries per ${size}, own scale`}</text>
      {/* PER LANE, so the gap between the lanes -- where the retries lane's
          label is printed -- never carries a hatch: text never sits on one
          (design-system.md §1.5). */}
      {buckets.map((b, i) =>
        [[top, agentsBase], [retriesTop, retriesBase]].map(([y0, y1], lane) =>
          !isRead(b) ? (
            <rect key={`u${i}-${lane}`} data-unread="" x={x0(i) + 1} y={y0} width={Math.max(1, col - 2)} height={y1! - y0!} fill={`url(#${hatch})`} className="pot-unread" />
          ) : b.in_progress ? (
            <rect key={`w${i}-${lane}`} x={x0(i) + 1} y={y0} width={Math.max(1, col - 2)} height={y1! - y0!} className="pot-wash" />
          ) : null,
        ),
      )}
      {maxAgents > 0 && (
        <ValueAxis side="left" scale={aScale} extent={{ lo: 0, hi: maxAgents, degenerate: false }} format={(v) => int(v)} left={GUTTER} ticks={d.ticks} minGapPx={AXIS_GAP_PX} keep={[0]} />
      )}
      {maxRetries > 0 && (
        <ValueAxis side="left" scale={rScale} extent={{ lo: 0, hi: maxRetries, degenerate: false }} format={(v) => int(v)} left={GUTTER} ticks={1} minGapPx={AXIS_GAP_PX} keep={[0]} />
      )}
      <line className="pot-base" x1={GUTTER} x2={d.w - RIGHT} y1={agentsBase} y2={agentsBase} />
      <line className="pot-base" x1={GUTTER} x2={d.w - RIGHT} y1={retriesBase} y2={retriesBase} />
      {layers !== null && (
        <Stack layers={layers} buckets={buckets} hatch={hatch} cx={(i) => x0(i) + col / 2} half={barW / 2} y={(v) => (maxAgents === 0 ? agentsBase : aScale(v))} />
      )}
      {buckets.map((b, i) => {
        if (!isRead(b)) return null
        const ah = height(aScale, agentsBase, b.agents, maxAgents)
        const rh = height(rScale, retriesBase, b.retries, maxRetries)
        return (
          <g key={`b${i}`}>
            {layers === null && <rect data-bar="agents" data-value={b.agents} className="pot-bar-agents" x={barX(i)} y={agentsBase - ah} width={barW} height={ah} />}
            <rect data-bar="retries" data-value={b.retries} className="pot-bar-retries" x={barX(i)} y={retriesBase - rh} width={barW} height={rh} />
          </g>
        )
      })}
      {buckets.map((b, i) =>
        showLabel(i) ? (
          <text key={`x${i}`} className="pot-x" x={x0(i) + col / 2} y={d.h - 6} textAnchor="middle">
            {tickLabel(b.start, size, tz)}
          </text>
        ) : null,
      )}
      {buckets[n - 1]?.in_progress && (
        <text className="pot-lane" x={d.w - RIGHT} y={top - 8} textAnchor="end">so far</text>
      )}
      {picked !== null &&
        picked < n &&
        [[top, agentsBase], [retriesTop, retriesBase]].map(([y0, y1], lane) => (
          // Per lane, as the hatch is: one ring across the gap struck through the retries label.
          <rect key={`p${lane}`} className="pot-picked" x={x0(picked) + 0.5} y={y0! - 2} width={Math.max(1, col - 1)} height={y1! - y0! + 4} />
        ))}
      {buckets.map((b, i) => (
        <rect
          key={`h${i}`}
          className="pot-hit"
          data-stack-top={layers !== null && isRead(b) ? stackTop(layers, i) : undefined}
          x={x0(i)}
          y={top}
          width={col}
          height={retriesBase - top}
          onClick={() => onPick(picked === i ? null : i)}
          onMouseEnter={() => onHover(i)}
        >
          <title>{bucketName(b.start, size, tz)}</title>
        </rect>
      ))}
    </svg>
  )
}

/** A read bucket's stack height: every layer's agents in it, the hatched ones too. */
function stackTop(layers: Layers, i: number): number {
  return [...layers.stacked, ...layers.unknown].reduce((n, s) => n + (s.agents[i] ?? 0), 0)
}

/**
 * THE STACKED AREA (F3, F4). One path a layer, each layer's band laid on the
 * ones below it, in the route's order; the unknowns as one hatched band on top.
 * A run of read buckets is one shape through the column centres; an unread
 * bucket breaks it, so the area never bridges a bucket with no figure. A run
 * of one bucket is a bar's width, so it is not a line nothing can see.
 */
function Stack({
  layers, buckets, hatch, cx, half, y,
}: {
  layers: Layers
  buckets: readonly UsageBucket[]
  hatch: string
  cx: (i: number) => number
  half: number
  y: (v: number) => number
}) {
  const runs: number[][] = []
  let cur: number[] = []
  buckets.forEach((b, i) => {
    if (isRead(b)) {
      cur.push(i)
      return
    }
    if (cur.length > 0) runs.push(cur)
    cur = []
  })
  if (cur.length > 0) runs.push(cur)
  const below = buckets.map(() => 0)
  const band = (s: UsageSeries): string =>
    runs
      .map((run) => {
        const xs = run.length === 1 ? [cx(run[0]!) - half, cx(run[0]!) + half] : run.map(cx)
        const at = run.length === 1 ? [run[0]!, run[0]!] : run
        const upper = at.map((i, k) => `${xs[k]!.toFixed(1)},${y(below[i]! + (s.agents[i] ?? 0)).toFixed(1)}`)
        const lower = at.map((i, k) => `${xs[k]!.toFixed(1)},${y(below[i]!).toFixed(1)}`).reverse()
        return `M${[...upper, ...lower].join(' L')} Z`
      })
      .join(' ')
  const lift = (list: readonly UsageSeries[]) => buckets.forEach((_, i) => {
    below[i]! += list.reduce((n, s) => n + (s.agents[i] ?? 0), 0)
  })
  const stacked = layers.stacked.map((s, k) => {
    const path = <path key={s.key} data-layer={s.key} className={`pot-layer ${layerClass(s, k)}`} d={band(s)} />
    lift([s])
    return path
  })
  // The unknowns as one band: their sum per bucket, above every stacked layer.
  const unknown: UsageSeries = {
    key: 'unknown',
    measured: false,
    other: false,
    keys: layers.unknown.length,
    agents: buckets.map((_, i) => layers.unknown.reduce((n, s) => n + (s.agents[i] ?? 0), 0)),
    retries: [],
  }
  return (
    <g>
      {stacked}
      {layers.unknown.length > 0 && <path data-unknown="" className="pot-layer-unknown" d={band(unknown)} fill={`url(#${hatch})`} />}
    </g>
  )
}

/**
 * THE READOUT ON ONE BUCKET (F6): the picked bucket's, else the hovered one's.
 * Every figure with its coverage; a live region, so a keyboard reader hears it
 * as the arrows move it.
 *
 * OVER THE CHART, ON THE SIDE AWAY FROM ITS BUCKET (COUNTS-ONE-RUN), as
 * counts-a.html's F6 draws it: below the chart it pushed the cost card down
 * the page by its own height on every pick. `data-side` is that side, and
 * overtime.css lays it over the plot only where the plot is wide; on a phone
 * (F7) it stays under the chart. It never covers its own bucket, so the ring
 * on the picked column stays in sight.
 *
 * IT FITS, OR IT GOES UNDER THE CHART. Laid over the chart it is held between
 * the agents axis's top label and the x labels, and clips rather than scrolls:
 * a hover's readout takes no pointer, so a scrolled-off figure could never be
 * reached (review, 2026-10-25: at 1440 the costs and the last coverage line
 * were sliced off, and with a breakdown most of the figures). So after every
 * draw, and on every resize, it is measured where the overlay put it; when
 * its figures are taller than the room, `data-place="under"` puts it back in
 * the flow under the chart, where every figure shows.
 */
function Readout({
  bucket, index, count, hover, layers, by, size, tz, onClose,
}: {
  bucket: UsageBucket
  index: number
  /** How many buckets the chart draws, to say which half `index` is in. */
  count: number
  /** Shown for the pointer, not picked: it takes no pointer itself, and has nothing to close. */
  hover: boolean
  layers: Layers | null
  by: string | null
  size: LedgerBucketSize
  tz: string
  onClose: () => void
}) {
  const name = bucketName(bucket.start, size, tz)
  const box = useRef<HTMLDivElement>(null)
  // On every render, not on a key: what fits depends on the bucket, the
  // layers and the plot's width, and the measure is one read of one box.
  useLayoutEffect(() => {
    const el = box.current
    if (el === null) return
    const fit = () => {
      el.removeAttribute('data-place')
      if (el.scrollHeight > el.clientHeight + 1) el.setAttribute('data-place', 'under')
    }
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  })
  return (
    <div ref={box} className="pot-readout" role="status" aria-live="polite" data-side={index >= count / 2 ? 'left' : 'right'} data-hover={hover ? '' : undefined}>
      <p className="pot-readout-head">
        <b>{name}</b>
        <span>{!isRead(bucket) ? unreadWords(bucket.unread_reason) : bucket.in_progress ? 'in progress, so far' : 'sealed'}</span>
        {!hover && <button type="button" className="pot-readout-close" aria-label="Close the readout" onClick={onClose}>×</button>}
      </p>
      {!isRead(bucket) ? (
        <p className="pot-readout-none">No figure, not zero: this {size} was not read. The next Run reads it again.</p>
      ) : (
        <>
          {layers !== null && (
            <ul className="pot-readout-layers" aria-label="Layers in this bucket, top of the stack first">
              {[...layers.unknown.map((s) => ({ s, cls: 'pot-key-unread' })), ...layers.stacked.map((s, i) => ({ s, cls: layerClass(s, i) })).reverse()].map(({ s, cls }) => (
                <li key={s.key}>
                  <i className={`pot-key ${cls}`} aria-hidden />
                  <span>{keyName(by, s)}</span>
                  <b>{int(s.agents[index] ?? 0)}</b>
                </li>
              ))}
            </ul>
          )}
          <dl className="pot-readout-list">
            <ReadoutRow name="Agents" cell={{ text: int(bucket.agents), tone: 'plain', cover: [`${int(bucket.agents_ended)} ended`] }} />
            <ReadoutRow name="Retries" cell={{ text: int(bucket.retries), tone: 'plain', cover: [] }} />
            <ReadoutRow name="Run hours" cell={runCell(bucket)} />
            <ReadoutRow name="Unused browser" cell={unusedBrowserCell(bucket)} />
            <ReadoutRow name="Tokens in + out" cell={inOutCell(bucket)} />
            <ReadoutRow name="Cache read" cell={tokenCell(bucket.tokens.cache_read_input_tokens, bucket.agents_ended)} />
            <ReadoutRow name="Cache write" cell={tokenCell(bucket.tokens.cache_creation_input_tokens, bucket.agents_ended)} />
            <ReadoutRow name="Estimated cost" cell={costCell(bucket)} wide />
          </dl>
        </>
      )}
    </div>
  )
}

/**
 * `wide`: Estimated cost, whose covers ("N of N agents reported", "k
 * estimated from a stopped run ($x)") are BGD's words and longer than one
 * overlay column. overtime.css gives it two columns over the chart, so each
 * stays on one line. Every other row is one column, and a cover too long for
 * it has its `brief` there instead: eight figures in three rows of three is
 * the height that fits over the chart at 1440 (re-review of PR 2348).
 */
function ReadoutRow({ name, cell, wide = false }: { name: string; cell: Cell; wide?: boolean }) {
  return (
    <div className="pot-readout-row" data-wide={wide ? '' : undefined}>
      <dt>{name}</dt>
      <dd>
        <CellView cell={cell} brief />
      </dd>
    </div>
  )
}

/**
 * ESTIMATED COST (LIST PRICE, NOT A BILL): per bucket with its coverage lane,
 * and per agent that reported. Never broken down here: the table splits it.
 * A dot is solid when every agent in the bucket reported a cost and hollow
 * when some did not (the figure is then a floor); a bucket where none
 * reported has no dot, and an unread one is hatched.
 */
function CostCard({ data, size, tz, picked }: { data: PlatformTime; size: LedgerBucketSize; tz: string; picked: number | null }) {
  const u = data.usage
  const cost = costCell(u.totals)
  const hatch = useHatchId()
  return (
    <section className="ctl-card pot-card pot-cost" aria-labelledby="pot-cost-title">
      <div className="pot-card-head">
        <h3 className="pot-card-title" id="pot-cost-title">{u.cost_label}</h3>
        <p className="pot-card-sub">{cost.cover.join(' · ')}. Never broken down here: the table splits it.</p>
      </div>
      <div className="pot-cost-grid">
        <figure className="pot-cost-fig">
          <figcaption className="pot-fig-title">Estimated cost per {size}</figcaption>
          <div className="pot-chart is-cost" role="group" aria-label={`Estimated cost per ${size}, with which buckets every agent reported`}>
            {COST_DRAWN.map((d) => (
              <CostDrawing key={d.key} d={d} hatch={`${hatch}-c${d.key}`} buckets={u.buckets} size={size} tz={tz} picked={picked} value={(b) => b.cost.sum_usd} lane />
            ))}
          </div>
        </figure>
        <figure className="pot-cost-fig">
          <figcaption className="pot-fig-title">Cost per agent that reported</figcaption>
          <div className="pot-chart is-cost" role="group" aria-label={`Estimated cost per agent that reported, per ${size}`}>
            {COST_DRAWN.map((d) => (
              <CostDrawing
                key={d.key}
                d={d}
                hatch={`${hatch}-p${d.key}`}
                buckets={u.buckets}
                size={size}
                tz={tz}
                picked={picked}
                value={(b) => (b.cost.sum_usd === null || b.cost.reporting === 0 ? null : b.cost.sum_usd / b.cost.reporting)}
                lane={false}
              />
            ))}
          </div>
        </figure>
      </div>
      <Legend
        keys={[
          ['pot-key-dot', 'solid: every agent in the bucket reported a cost'],
          ['pot-key-dot is-open', 'open: some did not, so the figure is a floor'],
          ['pot-key-unread', 'not read, or nothing reported: no figure'],
        ]}
      />
    </section>
  )
}

function CostDrawing({
  d, hatch, buckets, size, tz, picked, value, lane,
}: {
  d: Drawing
  hatch: string
  buckets: readonly UsageBucket[]
  size: LedgerBucketSize
  tz: string
  picked: number | null
  value: (b: ReadBucket) => number | null
  /** Draw the coverage lane under the plot: the per-bucket chart has it, the per-agent one does not need it (it divides by those that reported). */
  lane: boolean
}) {
  const n = buckets.length
  const plotW = d.w - GUTTER - RIGHT
  const col = plotW / Math.max(1, n)
  const top = 22
  const laneH = lane ? 12 : 0
  const axisH = 22
  const plotBase = d.h - axisH - (lane ? laneH + 10 : 0)
  const vals = buckets.map((b) => (isRead(b) ? value(b) : null))
  const max = Math.max(0, ...vals.map((v) => v ?? 0))
  const scale = linearScale({ lo: 0, hi: max, degenerate: max === 0 }, [plotBase, top])
  const cx = (i: number) => GUTTER + i * col + col / 2
  const showLabel = labelled(n, col, xLabelPx(size))
  const segs: string[] = []
  let cur: string[] = []
  vals.forEach((v, i) => {
    if (v === null) {
      if (cur.length > 1) segs.push(cur.join(' '))
      cur = []
      return
    }
    cur.push(`${cx(i).toFixed(1)},${(max === 0 ? plotBase : scale(v)).toFixed(1)}`)
  })
  if (cur.length > 1) segs.push(cur.join(' '))
  const laneTop = plotBase + 8
  return (
    <svg className={`pot-svg is-${d.key}`} viewBox={`0 0 ${d.w} ${d.h}`} width={d.w} height={d.h} role="group" aria-label={`drawn ${d.w} wide`}>
      <HatchDef id={hatch} />
      <text className="pot-lane" x={GUTTER} y={top - 8}>{lane ? `$ per ${size}` : '$ per agent that reported'}</text>
      {buckets.map((b, i) =>
        !isRead(b) ? (
          <rect key={`u${i}`} data-unread="" x={GUTTER + i * col + 1} y={top} width={Math.max(1, col - 2)} height={plotBase - top} fill={`url(#${hatch})`} className="pot-unread" />
        ) : b.in_progress ? (
          <rect key={`w${i}`} x={GUTTER + i * col + 1} y={top} width={Math.max(1, col - 2)} height={plotBase - top} className="pot-wash" />
        ) : null,
      )}
      {max > 0 && (
        <ValueAxis side="left" scale={scale} extent={{ lo: 0, hi: max, degenerate: false }} format={(v) => usd(v)} left={GUTTER} ticks={d.ticks} minGapPx={AXIS_GAP_PX} keep={[0]} />
      )}
      <line className="pot-base" x1={GUTTER} x2={d.w - RIGHT} y1={plotBase} y2={plotBase} />
      {segs.map((p) => (
        <polyline key={p} className="pot-cost-line" points={p} />
      ))}
      {buckets.map((b, i) => {
        const v = vals[i]
        if (v === null || v === undefined || !isRead(b)) return null
        const whole = b.cost.reporting >= b.cost.agents
        return <circle key={`d${i}`} data-dot={whole ? 'whole' : 'floor'} className={`pot-dot${whole && lane ? '' : lane ? ' is-open' : ''}`} cx={cx(i)} cy={max === 0 ? plotBase : scale(v)} r={col < 8 ? 2 : 3} />
      })}
      {lane &&
        buckets.map((b, i) => {
          const x = GUTTER + i * col + 1
          const w = Math.max(1, col - 2)
          if (!isRead(b)) return <rect key={`l${i}`} x={x} y={laneTop} width={w} height={laneH} fill={`url(#${hatch})`} className="pot-unread" />
          if (b.cost.agents === 0) return null
          const full = b.cost.reporting >= b.cost.agents
          return <rect key={`l${i}`} data-cover={full ? 'full' : 'some'} x={x} y={laneTop} width={w} height={laneH} className={full ? 'pot-cover is-full' : 'pot-cover is-some'} />
        })}
      {buckets.map((b, i) =>
        showLabel(i) ? (
          <text key={`x${i}`} className="pot-x" x={cx(i)} y={d.h - 6} textAnchor="middle">
            {tickLabel(b.start, size, tz)}
          </text>
        ) : null,
      )}
      {picked !== null && picked < n && <rect className="pot-picked" x={GUTTER + picked * col + 0.5} y={top - 2} width={Math.max(1, col - 1)} height={plotBase - top + 2} />}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// The table: every figure as text a reader can copy
// ---------------------------------------------------------------------------

/** How many bucket rows show before "Show all", newest first (F2). */
const FOLDED = 8

const KEY_SAYS: Record<string, string> = {
  'not ended': 'still open, or ended after the span; never guessed',
  'not recorded': 'nothing recorded it; never guessed',
  'holds no account': 'its profile takes no pool account, or it was never admitted',
}

function Table({ data, grouping, breakdown, tz }: { data: PlatformTime; grouping: UsageGrouping | null; breakdown: Breakdown; tz: string }) {
  const phone = usePhoneTables()
  const [all, setAll] = useState(false)
  const u = data.usage
  const size = data.bucket
  const unit = UNIT[size as TimeBucket] ?? size
  const b = breakdownOf(breakdown)
  const byGroup = grouping !== null
  const n = u.buckets.length
  const newestFirst = [...u.buckets].reverse()
  const shown = all ? newestFirst : newestFirst.slice(0, FOLDED)
  const title = byGroup ? `Breakdown by ${b.label.toLowerCase()}` : `Breakdown per ${unit}`
  const head = byGroup ? b.label : unit[0]!.toUpperCase() + unit.slice(1)
  return (
    <section className="ctl-card pot-card pot-table" aria-labelledby="pot-table-title">
      <div className="pot-card-head">
        <h3 className="pot-card-title" id="pot-table-title">{title}, over {n} {unit}s</h3>
        <p className="pot-card-sub">
          {byGroup ? 'One row per group, then the total. ' : 'One row per bucket, newest first. '}
          Reported counts, of all the agents in the row, the ones that reported a cost; an agent still open is in Agents and in no measured figure. A figure is “at least” when not everything reported, with “k of n” under it: the attempts that reported it (run hours: that wrote an end), of those that started. A figure with none under it was reported by every attempt. “Not reported” is never 0.
        </p>
      </div>
      <div className={`ctl-table ${tableMode(phone)}`}>
        <table role="table" aria-label={`${title}, over ${n} ${unit}s`}>
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">{head}</th>
              <th role="columnheader" scope="col" className="is-num">Agents</th>
              <th role="columnheader" scope="col" className="is-num">Retries</th>
              <th role="columnheader" scope="col" className="is-num">Run hours</th>
              {TOKEN_KINDS.map((t) => (
                <th role="columnheader" scope="col" className="is-num" key={t.key}>{t.label}</th>
              ))}
              <th role="columnheader" scope="col" className="is-num">{u.cost_label}</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {byGroup
              ? grouping.rows.map((r) => (
                  <FigureRow key={r.key} rowKey={r.key} name={keyName(grouping.by, { key: r.key, other: false, keys: 1 })} says={KEY_SAYS[r.key]} f={r} costLabel={u.cost_label} head={head} />
                ))
              : shown.map((bk) =>
                  isRead(bk) ? (
                    <FigureRow key={bk.start} rowKey={bk.start} name={bucketName(bk.start, size, tz)} says={bk.in_progress ? 'so far' : undefined} f={bk} costLabel={u.cost_label} head={head} />
                  ) : (
                    <tr role="row" key={bk.start} data-key={bk.start} className="pot-row-unread">
                      <th role="rowheader" scope="row" data-label={head}>{bucketName(bk.start, size, tz)}</th>
                      <td role="cell" colSpan={8} data-label="Figures">
                        <span className="ctl-mark is-unread">not read</span> {unreadWords(bk.unread_reason)}; the next Run reads it again. No figure, not zero.
                      </td>
                    </tr>
                  ),
                )}
            <FigureRow rowKey="__total" name={byGroup ? `All ${b.plural}` : `All ${n} ${unit}s`} f={u.totals} costLabel={u.cost_label} head={head} total />
          </tbody>
        </table>
      </div>
      {!byGroup && n > FOLDED && (
        <p className="pot-fold">
          <Button size="sm" onClick={() => setAll((v) => !v)} aria-expanded={all}>
            {all ? `Show the newest ${FOLDED}` : `Show all ${n} ${unit}s`}
          </Button>{' '}
          {all ? '' : `${n - FOLDED} older ${unit}s folded; newest first.`}
        </p>
      )}
      {byGroup && grouping.rows_total > grouping.rows.length && (
        <p className="pot-fold">{grouping.rows_total - grouping.rows.length} smaller groups are not listed; the total counts them.</p>
      )}
      {!u.totals.complete && (
        <p className="pot-fold">
          <span className="ctl-mark is-partial">partial</span> The total counts the {unit}s that were read; an unread {unit} adds nothing to it.
        </p>
      )}
    </section>
  )
}

function FigureRow({ rowKey, name, says, f, costLabel, head, total = false }: { rowKey: string; name: string; says?: string; f: UsageFigures | UsageGroupRow; costLabel: string; head: string; total?: boolean }) {
  return (
    <tr role="row" data-key={rowKey} className={total ? 'pot-row-total' : undefined}>
      <th role="rowheader" scope="row" data-label={head}>
        <span className="pot-name">{name}</span>
        {says !== undefined && <span className="pot-cov">{says}</span>}
      </th>
      <td role="cell" className="is-num" data-col="agents" data-label="Agents">{int(f.agents)}</td>
      <td role="cell" className="is-num" data-col="retries" data-label="Retries"><CellView cell={retriesCell(f)} /></td>
      <td role="cell" className="is-num" data-col="run" data-label="Run hours"><CellView cell={runCell(f)} partialOnly /></td>
      {TOKEN_KINDS.map((t) => (
        <td role="cell" className="is-num" data-col={t.key} data-label={t.label} key={t.key}>
          <CellView cell={tokenCell(f.tokens[t.key], f.agents_ended)} partialOnly />
        </td>
      ))}
      <td role="cell" className="is-num" data-col="cost" data-label={costLabel}><CellView cell={costCell(f)} /></td>
    </tr>
  )
}
