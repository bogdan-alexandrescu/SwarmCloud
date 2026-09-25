import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  loadMe,
  loadOutcomes,
  loadRunnerProfiles,
  loadStats,
  loadTasksInState,
  loadTenants,
} from './api'
import { IDLE_POLL_MS } from './Agents'
import { OutcomeLedger } from './charts/OutcomeLedger'
import { errorHeading, type ApiError, type Result } from './fetch'
import { helpAnchor, type TopicId } from './help'
import { HelpCard, HelpLinks } from './HelpCard'
import {
  CancelCausesCard,
  CostCard,
  FailureClassesCard,
  LatencyCard,
  LedgerTable,
  OpenWorkCard,
  ReliabilityCard,
  RetriesCard,
  WorkflowsFailedCard,
  type OpenWork,
} from './Ledger'
import {
  MAX_BUCKETS,
  OUTCOMES_CACHE_S,
  SPANS,
  VERIFY_TENANT,
  cacheable,
  dayDocWords,
  filtersSet,
  hourAllowed,
  instantLabel,
  interval,
  monthAllowed,
  nothingReadWords,
  outcomesQuery,
  parseView,
  pct,
  rangeRefusal,
  serializeView,
  spanCoverage,
  unitWord,
  viewDays,
  viewerZone,
  wallOf,
  type BucketChoice,
  type GroupBy,
  type Kind,
  type LedgerView,
  type OutcomeBucket,
  type Outcomes,
  type Span,
} from './outcomes'
import { Absent, Mark } from './primitives'
import { Id, PageHead, Screen, timeAgo } from './Shell'
import { AGE_TICK_MS, useNow } from './useNow'

/**
 * Where the Timeline's sentences live. Typed as `TopicId` rather than spelled
 * into an href, so a renamed topic is a compile error here instead of a link
 * that lands on the top of the Help page and answers nothing.
 *
 * ONE GLYPH ON THIS SCREEN (B7.4): the `?` after "Success rate", the one
 * figure the page exists for. Every other explanation goes by the label's
 * `aria-describedby` (a card's `explain`) or by the footer index below, which
 * draws links and no glyphs -- the console-wide ceiling is twenty glyphs and
 * two per screen, and this screen had none before.
 */
const RATE_HELP: TopicId = 'success-rate'
const SCOPE_HELP: TopicId = 'tenant-scope'
const READING_TOPICS: readonly TopicId[] = [
  RATE_HELP,
  'outcome-buckets',
  'failure-classes',
  'tokens-reported',
  SCOPE_HELP,
]

/** `#help/<topic>`, as an href. */
function helpHref(topic: TopicId): string {
  return `#${helpAnchor(topic)}`
}

// ---------------------------------------------------------------------------
// The remembered view (per viewer, in this browser only)
// ---------------------------------------------------------------------------

/**
 * THE LAST CHOICE IS REMEMBERED, AND THE PAGE IS CORRECT WITHOUT IT. The
 * address is the view's truth -- a pasted link reproduces the page -- and this
 * is only the view a bare `#work/timeline` opens with. Every read and write is
 * inside try/catch: storage throws in a private window and in previews, and
 * the page then opens on the defaults.
 */
const VIEW_KEY = 'swarm.timeline.view'

function rememberedView(): string | null {
  try {
    return window.localStorage.getItem(VIEW_KEY)
  } catch {
    return null
  }
}

function rememberView(query: string): void {
  try {
    if (query === '') window.localStorage.removeItem(VIEW_KEY)
    else window.localStorage.setItem(VIEW_KEY, query)
  } catch {
    // Nothing to do: the address still carries the view.
  }
}

// ---------------------------------------------------------------------------
// Screen A1 -- the Timeline (#work/timeline), as the outcome ledger
// ---------------------------------------------------------------------------

/**
 * THE TIMELINE IS AN OUTCOME LEDGER OVER A REAL SPAN (#185, owner decisions
 * 2026-09-25). It used to read the newest 200-2,000 tasks and label whatever
 * span they covered -- "bound by rows, label by the span" -- because the task
 * list could not filter by time. `GET /v1/outcomes` removes that premise, so
 * the Rows control and its rule are retired: the span is chosen (24h to 90d,
 * or a range), the server buckets it, and every bucket from `since` to `until`
 * is drawn, with the ones before the first task as the measured zeroes they
 * are.
 *
 * ONE TIME BASIS ON THE DRAWING'S OUTCOMES. Every outcome is by completed_at;
 * the throughput lane is the one series by created_at, and its label says so.
 * Open work has no completed_at and lives in "Not finished yet", read live.
 *
 * THE VIEW IS THE ADDRESS. Every filter -- span, bucket, scope, tenants,
 * profile, submitter, kind, grouping and the Table toggle -- is written to
 * the hash (`#work/timeline?span=30d&…`), so a link reproduces the page. App
 * owns the address; this screen hands it a new query through `onView`.
 */
export function ActivityScreen({
  view: hashView = null,
  onView,
}: {
  /** The hash's query, when it carries one. */
  view?: string | null
  /** Write a new view to the address. Absent, the screen keeps its own. */
  onView?: (query: string) => void
} = {}) {
  const [own, setOwn] = useState<string>(() => hashView ?? rememberedView() ?? '')
  // With an address to write to, the address is the view; without one (a
  // screen rendered on its own), the screen keeps its own.
  const query = onView === undefined ? own : (hashView ?? own)
  const view = useMemo(() => parseView(query), [query])
  const tz = useMemo(viewerZone, [])
  const now = useNow(AGE_TICK_MS)

  // A bare `#work/timeline` opens on the remembered view and says so in the
  // address; a pasted link wins over what this browser remembers.
  useEffect(() => {
    if (hashView !== null && hashView !== own) setOwn(hashView)
    else if (hashView === null && own !== '' && onView !== undefined) onView(own)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hashView])

  const setView = useCallback(
    (next: LedgerView) => {
      const q = serializeView(next)
      setOwn(q)
      rememberView(q)
      onView?.(q)
    },
    [onView],
  )

  // ---- the ledger read ---------------------------------------------------
  const key = useMemo(() => outcomesQuery(view, tz).toString(), [view, tz])
  const [nonce, setNonce] = useState(0)
  const [pending, setPending] = useState(true)
  const [latest, setLatest] = useState<{ key: string; result: Result<Outcomes> } | null>(null)
  /** The last payload that drew, kept (dimmed) while a filter change is being read. */
  const [good, setGood] = useState<{ key: string; data: Outcomes } | null>(null)
  useEffect(() => {
    let live = true
    setPending(true)
    loadOutcomes(new URLSearchParams(key)).then((r) => {
      if (!live) return
      setLatest({ key, result: r })
      if (r.status === 'ok' || r.status === 'stale') setGood({ key, data: r.data })
      setPending(false)
    })
    return () => {
      live = false
    }
  }, [key, nonce])

  // ---- who is asking, and what the filters can offer ---------------------
  const [admin, setAdmin] = useState<boolean | null>(null)
  const [myTenant, setMyTenant] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    loadMe().then((r) => {
      if (!live || (r.status !== 'ok' && r.status !== 'stale')) return
      setAdmin(r.data.principal.is_admin)
      setMyTenant(r.data.tenant.tenant_id)
    })
    return () => {
      live = false
    }
  }, [])
  const [profiles, setProfiles] = useState<string[] | 'failed' | null>(null)
  useEffect(() => {
    let live = true
    loadRunnerProfiles().then((r) => {
      if (live) setProfiles(r.status === 'ok' || r.status === 'stale' ? r.data : r.status === 'empty' ? [] : 'failed')
    })
    return () => {
      live = false
    }
  }, [])
  const [tenants, setTenants] = useState<string[] | null>(null)
  useEffect(() => {
    if (admin !== true) return
    let live = true
    loadTenants().then((r) => {
      if (live && (r.status === 'ok' || r.status === 'stale')) setTenants(r.data.tenants.map((t) => t.tenant_id).sort())
    })
    return () => {
      live = false
    }
  }, [admin])

  // ---- the one live card -------------------------------------------------
  // A LIVE READ CARRIES ITS AGE, AND IS RE-READ. "Not finished yet" is the one
  // card not drawn from the ledger's payload, so the head's `read Ns ago`
  // (the ledger's generated_at) says nothing about it. It is read on open, on
  // refresh, on every filter change -- the moment a reader is comparing it
  // with a ledger that was just re-read -- and on the Agents screen's idle
  // cadence while the page stays open and visible; the card prints the age
  // of the read it shows. Between reads the last counts stay drawn rather
  // than blanking to "reading" on every tick.
  const [open, setOpen] = useState<OpenWork>({ stats: null, parked: null })
  const [openTick, setOpenTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => {
      if (typeof document === 'undefined' || document.visibilityState !== 'hidden') setOpenTick((n) => n + 1)
    }, IDLE_POLL_MS)
    return () => clearInterval(timer)
  }, [])
  useEffect(() => {
    let live = true
    loadStats().then((r) => {
      if (!live) return
      setOpen((o) => ({
        ...o,
        stats:
          r.status === 'ok' || r.status === 'stale'
            ? { status: 'ok', data: r.data }
            : { status: 'error', message: r.status === 'error' ? r.error.message : 'the read did not complete' },
      }))
    })
    loadTasksInState('PARKED').then((r) => {
      if (!live) return
      setOpen((o) => ({
        ...o,
        parked:
          r.status === 'ok' || r.status === 'stale'
            ? { status: 'ok', data: r.data }
            : r.status === 'empty'
              ? { status: 'empty' }
              : { status: 'error', message: r.status === 'error' ? r.error.message : 'the read did not complete' },
      }))
    })
    return () => {
      live = false
    }
  }, [key, nonce, openTick])

  const [picked, setPicked] = useState<string | null>(null)
  const refresh = () => setNonce((n) => n + 1)

  const settled = latest !== null && latest.key === key ? latest.result : null
  const failure: ApiError | null = !pending && settled?.status === 'error' ? settled.error : null
  const data = good?.data ?? null
  const dim = pending || (good !== null && good.key !== key)

  const zoom = (b: OutcomeBucket) =>
    setView({ ...view, span: null, since: b.start, until: b.end, bucket: 'auto', back: serializeView({ ...view, back: null }) })
  const mine = () => setView({ ...view, platform: false, tenant: [], exclude_tenant: [], group: view.group === 'tenant_id' ? 'runner_profile' : view.group })

  return (
    <>
      <PageHead title="Timeline">
        {data === null ? (
          'reading…'
        ) : (
          <>
            {rangeWords(data)} · read {timeAgo(data.generated_at, now)}
            {data.cached && ` · from the ${OUTCOMES_CACHE_S} s cache`}
          </>
        )}{' '}
        <button type="button" onClick={refresh} disabled={pending}>
          {pending ? 'reading…' : 'refresh'}
        </button>
      </PageHead>

      <LedgerToolbar
        view={view}
        setView={setView}
        admin={admin === true}
        profiles={profiles}
        tenants={tenants}
        data={data}
      />

      {failure !== null ? (
        <LedgerFailed error={failure} onRetry={refresh} onMine={mine} />
      ) : data === null ? (
        <div className="ol-body">
          <LedgerFacts data={null} pending view={view} />
          {/* STILL READING IS NOT NOTHING REPORTED (§8.7): the moving sweep, at the chart's own geometry. */}
          <div className="ctl-pending ol-pending" aria-hidden="true" />
        </div>
      ) : (
        <div className={dim ? 'ol-body ctl-stale-body' : 'ol-body'} aria-busy={pending}>
          <LedgerFacts data={data} pending={pending} view={view} />
          <LedgerSection
            data={data}
            view={view}
            picked={picked}
            onPick={setPicked}
            onZoom={zoom}
          />
          <div className="ctl-cards ol-cards">
            <FailureClassesCard data={data} picked={picked} />
            <RetriesCard data={data} />
            <LatencyCard data={data} />
            <ReliabilityCard
              data={data}
              group={view.group}
              platform={view.platform}
              onGroup={(g: GroupBy) => setView({ ...view, group: g })}
            />
            <WorkflowsFailedCard data={data} spanLabel={view.span ?? 'this range'} />
            <CostCard data={data} picked={picked} />
            <OpenWorkCard open={open} view={view} tenant={myTenant} now={now} />
            <CancelCausesCard data={data} picked={picked} />
          </div>
          <Provenance data={data} />
        </div>
      )}

      <HelpLinks topics={READING_TOPICS} label="Reading this screen:" />
    </>
  )
}

/** `Sep 12, 00:00 → now`, in the zone the server bucketed in. */
function rangeWords(d: Outcomes): string {
  const toNow = Math.abs(Date.parse(d.until) - Date.parse(d.generated_at)) < 2_000
  return `${instantLabel(d.since, d.tz)} → ${toNow ? 'now' : instantLabel(d.until, d.tz)}`
}

/** The span a delta compares with, in words. */
function prevWords(d: Outcomes): string {
  return d.requested.span !== null ? `prev ${d.requested.span}` : 'prev period'
}

/**
 * THE DELTA IS DROPPED RATHER THAN COMPUTED OVER PART OF A SPAN (#185). A
 * previous span with a bucket unread, or with nothing decided, gives no delta
 * -- and says why, so a missing delta is not read as "no change".
 */
export function deltaWords(d: Outcomes): string | null {
  const p = d.previous
  if (p === null) return null
  // Nothing of THIS span was read: there is nothing to compare, and the
  // headline says so with the not-read mark.
  if (d.totals.buckets_read === 0) return null
  const prev = prevWords(d)
  if (!p.complete) return `${prev} partial, no delta`
  if (p.rate === null) return `${prev}: no finished work, so no delta`
  const t = d.totals.rate
  if (t === null) return `${prev} ${pct(p.rate.p)}`
  if (!d.totals.complete) return `${prev} ${pct(p.rate.p)} · this span partial, no delta`
  const pts = (t.p - p.rate.p) * 100
  return `${prev} ${pct(p.rate.p)} · ${pts >= 0 ? '+' : '−'}${Math.abs(pts).toFixed(1)} pts`
}

/**
 * The chart section: the page's ONE figure, then the ledger (or its table).
 */
function LedgerSection({
  data,
  view,
  picked,
  onPick,
  onZoom,
}: {
  data: Outcomes
  view: LedgerView
  picked: string | null
  onPick: (start: string | null) => void
  onZoom: (b: OutcomeBucket) => void
}) {
  const t = data.totals
  const delta = deltaWords(data)
  const orphans = data.coverage.terminal_without_completed_at
  const cov = spanCoverage(data)
  return (
    <section className="section ol-ledger" aria-labelledby="ol-rate-title">
      <div className="ol-head">
        <h2 className="ol-title" id="ol-rate-title">
          Success rate
        </h2>
        <HelpCard topic={RATE_HELP} />
        {/* WHAT THE FIGURE LEAVES OUT, fused to it rather than footnoted: the
            cancels are counted, drawn in their own lane, and not in the rate
            (owner decision). With nothing read there is no count to print. */}
        <span className="ctl-card-note ol-note">
          {cov.none ? 'cancels excluded' : `excludes ${t.cancelled.total} cancelled`}
        </span>
      </div>
      <p className="ol-headline">
        {cov.none ? (
          // NOTHING READ IS NOT NOTHING FINISHED. "no finished work" is a
          // measured n = 0; over a span of which no bucket was read the route's
          // zeroes are sums over nothing, so the figure slot takes the
          // not-read mark and no digit (§8.6).
          <>
            <b className="ol-figure is-phrase is-unread">
              <Mark
                kind="unread"
                say={`The success rate was not read: none of the span's ${cov.of} ${cov.unit} could be read (${cov.reasons.join('; ')}). No figure is drawn, because none was measured.`}
              />
            </b>
            <span className="ol-q">{nothingReadWords(cov)}</span>
          </>
        ) : t.rate === null ? (
          // n = 0 is measured, but a rate over nothing is undefined: a
          // phrase, never 0 %.
          <b className="ol-figure is-phrase">no finished work</b>
        ) : (
          <b
            className="ctl-figure ol-figure"
            role="img"
            aria-label={`Success rate ${pct(t.rate.p)}: ${t.rate.k} of ${t.rate.n} decided, 95 % interval ${interval(t.rate)}, excluding ${t.cancelled.total} cancelled`}
          >
            {pct(t.rate.p)}
          </b>
        )}
        {t.rate !== null && (
          <span className="ol-kofn">
            {t.rate.k} of {t.rate.n} decided
          </span>
        )}
        {/* THE INTERVAL IS PRINTED, not only named to a screen reader (#185:
            "the figure with k of n decided, its interval"). The readout also
            carries it, but under Table the readout is not drawn. */}
        {t.rate !== null && <span className="ol-interval">95 % interval {interval(t.rate)}</span>}
        {!t.complete && !cov.none && (
          <span className="ol-partial">
            <Mark
              kind="partial"
              say={`${t.buckets - t.buckets_read} of ${t.buckets} ${unitWord(data.bucket)} could not be read, so every total here covers ${t.buckets_read} of them and is a floor.`}
            />{' '}
            {t.buckets_read} of {t.buckets} {unitWord(data.bucket)}
          </span>
        )}
        {delta !== null && <span className="ol-delta">{delta}</span>}
      </p>
      {orphans === null ? (
        <p className="ctl-panel-note">
          <Mark kind="unread" say="The count of terminal tasks with no completed_at could not be read." /> terminal tasks
          without completed_at: not read
        </p>
      ) : orphans > 0 ? (
        <p
          className="ctl-panel-note"
          aria-label={`${orphans} terminal tasks carry no completed_at. Every writer that moves a task terminal sets it, so this is a data bug; these tasks cannot be placed on this axis and are in no bucket.`}
        >
          <Mark kind="partial" say={`${orphans} terminal tasks carry no completed_at and are on no bucket.`} /> {orphans}{' '}
          terminal tasks carry no <code>completed_at</code>
          <a href={helpHref('outcome-buckets')}>Why &rarr;</a>
        </p>
      ) : null}
      {view.table ? (
        <LedgerTable data={data} />
      ) : (
        <OutcomeLedger data={data} picked={picked} onPick={onPick} onZoom={onZoom} />
      )}
    </section>
  )
}

/**
 * THE FACTS LINE, under the controls: the resolved range, the zone, the basis,
 * how much is sealed, and whose figures these are. The server resolves and
 * echoes the range; nothing here re-derives it.
 */
function LedgerFacts({ data, pending, view }: { data: Outcomes | null; pending: boolean; view: LedgerView }) {
  if (data === null) {
    return (
      <ul className="ctl-facts ol-facts">
        <li className="ctl-fact">
          <Mark kind="pending" say="The outcome ledger is still being read." /> {view.span ?? 'range'}
        </li>
      </ul>
    )
  }
  const inProgress = data.buckets.some((b) => b.in_progress)
  const s = data.scope
  return (
    <ul className="ctl-facts ol-facts">
      <li className="ctl-fact">
        <b>range</b>
        {rangeWords(data)}
      </li>
      <li className="ctl-fact">
        <b>zone</b>
        {data.tz}
      </li>
      <li className="ctl-fact">
        <b>by</b>
        <code>{data.basis.outcomes}</code>
      </li>
      <li className="ctl-fact">
        {/* THE ROLLUP'S UNIT, NOT THE VIEWER'S DAYS: tenant-days on the UTC
            calendar, so a 14-day span reads "14 of 15 UTC days" and a platform
            view counts every tenant's (`dayDocWords`). */}
        <b>sealed</b>
        {data.coverage.days.sealed} of {data.coverage.days.total} {dayDocWords(data, data.coverage.days.total)}
        {inProgress ? ' · today so far' : ''}
      </li>
      {s.kind === 'tenant' ? (
        <li className="ctl-fact">
          <b>tenant</b>
          <Id>{s.tenant_id}</Id>
        </li>
      ) : (
        <li className="ctl-fact">
          <b>platform</b>
          {s.tenants.length} tenants
          {s.excluded.length > 0 && ` · ${s.excluded.join(', ')} excluded`}
          {!s.tenants_complete && (
            <>
              {' '}
              <Mark kind="partial" say="The tenant listing stopped at its limit, so tenants past it are not in these figures." />
            </>
          )}
        </li>
      )}
      {pending && (
        <li className="ctl-fact">
          <Mark kind="pending" say="A newer read is in flight; the figures below are from the last one and are dimmed." />
        </li>
      )}
    </ul>
  )
}

/**
 * THE PROVENANCE FOOT: what this payload cost and how old it is. On a cache
 * hit the request spent nothing, and `generated_at` is the original read's, so
 * the age stays honest.
 *
 * ONLY A COMPLETE PAYLOAD IS CACHED (`cacheable`): the route re-derives a
 * partial one on the next read rather than serving its gaps for a minute, so
 * the foot says which this one is. The days are the rollup's tenant-days
 * (`dayDocWords`), and a live one need not be today: yesterday stays live for
 * the seal grace after midnight UTC.
 */
function Provenance({ data }: { data: Outcomes }) {
  const c = data.coverage
  const clauses: ReactNode[] = [
    `${c.days.sealed} ${dayDocWords(data, c.days.sealed)} sealed`,
    c.days.live > 0 ? `${c.days.live} live` : null,
    data.cached ? `from the ${OUTCOMES_CACHE_S} s cache · 0 reads this request` : `${data.reads} reads`,
    cacheable(data) ? `cached ${OUTCOMES_CACHE_S} s` : 'not cached: partial, the next read continues the build',
    `generated ${new Date(data.generated_at).toLocaleTimeString(undefined, { timeZone: data.tz, hourCycle: 'h23' })}`,
    c.derived_now > 0 ? `${c.derived_now} days built by this read` : null,
    c.reopened > 0 ? `${c.reopened} re-ended tasks counted once` : null,
  ]
  return (
    <p className="ol-prov ctl-foot-run">
      {clauses
        .filter((x) => x !== null)
        .map((x, i) => (
          <span key={i}>{x}</span>
        ))}
    </p>
  )
}

/**
 * A FAILED READ DRAWS NO NUMBER ANYWHERE (§6.9). And a 403 on platform scope
 * is not a failure: a non-admin genuinely cannot ask for it, so it is the
 * blue admin state with the way back to their own tenant.
 */
function LedgerFailed({ error, onRetry, onMine }: { error: ApiError; onRetry: () => void; onMine: () => void }) {
  if (error.kind === 'admin_required') {
    return (
      <Absent
        kind="admin"
        heading="Platform scope is for administrators"
        say="The platform-wide ledger is served to administrators only. Nothing failed, and your own tenant's ledger is readable."
      >
        Your own tenant’s ledger is one click away:{' '}
        <button type="button" className="sbf-mini" onClick={onMine}>
          my tenant
        </button>
      </Absent>
    )
  }
  return (
    <Absent
      kind="failed"
      heading={errorHeading(error)}
      say={`The outcome ledger could not be read: ${error.message}. No figure is drawn, because none was read.`}
    >
      {error.message}{' '}
      <button type="button" className="sbf-mini" onClick={onRetry}>
        try again
      </button>
    </Absent>
  )
}

// ---------------------------------------------------------------------------
// The toolbar
// ---------------------------------------------------------------------------

const KIND_LABEL: Record<Kind, string> = { all: 'all', standalone: 'standalone', steps: 'workflow steps' }

/** A multi-select as a disclosure of checkboxes: every option visible, none hidden in a native control. */
function Picker({
  label,
  options,
  selected,
  onChange,
  summary,
}: {
  label: string
  options: readonly string[] | null
  selected: readonly string[]
  onChange: (next: string[]) => void
  summary: string
}) {
  const all = Array.from(new Set([...(options ?? []), ...selected])).sort()
  return (
    <details className="ol-pick">
      <summary>
        {label} <b>{summary}</b>
      </summary>
      <fieldset className="ol-pick-list">
        <legend className="ol-q">{label}</legend>
        {all.map((o) => (
          <label key={o} className="ol-pick-item">
            <input
              type="checkbox"
              checked={selected.includes(o)}
              onChange={(e) => onChange(e.target.checked ? [...selected, o] : selected.filter((s) => s !== o))}
            />{' '}
            {o}
          </label>
        ))}
        {selected.length > 0 && (
          <button type="button" className="sbf-mini" onClick={() => onChange([])}>
            clear
          </button>
        )}
      </fieldset>
    </details>
  )
}

/** A YYYY-MM-DD day, one day on: the range's inclusive "to" day is sent as an exclusive `until`. */
function nextDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + 1)
  return d.toISOString().slice(0, 10)
}

function prevDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() - 1)
  return d.toISOString().slice(0, 10)
}

const isDay = (s: string | null): s is string => s !== null && /^\d{4}-\d{2}-\d{2}$/.test(s)

function RangeInputs({ view, setView }: { view: LedgerView; setView: (v: LedgerView) => void }) {
  const [from, setFrom] = useState(isDay(view.since) ? view.since : '')
  const [to, setTo] = useState(isDay(view.until) ? prevDay(view.until) : '')
  // The route refuses a `since` in the future and a span over its limit; the
  // button says which instead of sending a 422. "Today" is the viewer's day
  // in the zone the page sends as `tz`.
  const refusal = rangeRefusal(from, to, wallOf(new Date().toISOString(), viewerZone()).date)
  const ok = refusal === null
  return (
    <span className="ol-range" role="group" aria-label="Range">
      <label>
        from <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
      </label>
      <label>
        to <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
      </label>
      <button
        type="button"
        className="sbf-mini"
        disabled={!ok}
        onClick={() => setView({ ...view, span: null, since: from, until: nextDay(to), back: null })}
      >
        apply
      </button>
      {refusal !== null && (from !== '' || to !== '') && <span className="ol-q ol-range-why">{refusal}</span>}
    </span>
  )
}

function SubmittedBy({ view, setView }: { view: LedgerView; setView: (v: LedgerView) => void }) {
  const [text, setText] = useState(view.submitted_by.join(', '))
  const apply = () => {
    const list = text
      .split(/[\s,]+/)
      .map((s) => s.trim().toLowerCase())
      .filter((s) => s.includes('@'))
    setView({ ...view, submitted_by: Array.from(new Set(list)).sort() })
  }
  return (
    <label className="ol-field">
      Submitted by{' '}
      <input
        type="search"
        inputMode="email"
        placeholder="anyone"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={apply}
        onKeyDown={(e) => {
          if (e.key === 'Enter') apply()
        }}
      />
    </label>
  )
}

/**
 * ONE TOOLBAR ABOVE EVERYTHING IT SCOPES (§6.11). The span first, as a
 * segmented control; the bucket override; the scope and tenant for admins
 * only; profile, submitter and kind; the Table toggle at the end. Every
 * control writes the address, and the server applies every filter.
 *
 * AT 390 (§7.2) the span keeps its 44px segments, with 24h and the range moved
 * into the filter sheet; everything else collapses behind `Filters · N`, and
 * the Table toggle stays in view.
 */
function LedgerToolbar({
  view,
  setView,
  admin,
  profiles,
  tenants,
  data,
}: {
  view: LedgerView
  setView: (v: LedgerView) => void
  admin: boolean
  profiles: string[] | 'failed' | null
  tenants: string[] | null
  data: Outcomes | null
}) {
  const [sheet, setSheet] = useState(false)
  const [range, setRange] = useState(view.span === null)
  const days = viewDays(view)
  const choose = (s: Span) => setView({ ...view, span: s, since: null, until: null, back: null, bucket: view.bucket === 'month' && s !== '90d' ? 'auto' : view.bucket })
  const back = view.back === null ? null : parseView(view.back)
  const chosen = data !== null && data.bucket_chosen_by === 'server' ? data.bucket : null
  const verifyServed = tenants?.includes(VERIFY_TENANT) === true
  const verifyOut = view.exclude_tenant.includes(VERIFY_TENANT)
  const n = filtersSet(view)
  const profileOptions = profiles === 'failed' || profiles === null ? null : profiles
  const tenantSummary =
    view.tenant.length > 0
      ? view.tenant.join(', ')
      : view.exclude_tenant.length > 0
        ? `all but ${view.exclude_tenant.join(', ')}`
        : tenants === null
          ? 'all'
          : `all ${tenants.length}`
  const spanSeg = (extra: boolean) => (
    <>
      {SPANS.map((s) =>
        (s === '24h') === extra ? (
          <button key={s} type="button" aria-pressed={view.span === s} onClick={() => choose(s)}>
            {s}
          </button>
        ) : null,
      )}
      {extra && (
        <button type="button" aria-pressed={view.span === null} onClick={() => setRange(!range)}>
          from–to
        </button>
      )}
    </>
  )
  return (
    <div className="ctl-toolbar ol-toolbar" role="group" aria-label="Timeline filters">
      <div className="ctl-seg ol-span" role="group" aria-label="Span">
        {SPANS.map((s) => (
          <button key={s} type="button" className={s === '24h' ? 'is-wide-only' : undefined} aria-pressed={view.span === s} onClick={() => choose(s)}>
            {s}
          </button>
        ))}
        <button type="button" className="is-wide-only" aria-pressed={view.span === null} onClick={() => setRange(!range)}>
          from–to
        </button>
      </div>
      {back !== null && (
        // A zoom is a span change, and the chip is the way back to the span it came from.
        <button type="button" className="sbf-mini ol-back" onClick={() => setView(back)}>
          ← {back.span ?? 'range'}
        </button>
      )}
      <button
        type="button"
        className="ol-sheet-toggle"
        aria-expanded={sheet}
        aria-controls="ol-sheet"
        onClick={() => setSheet(!sheet)}
      >
        Filters · {n}
      </button>
      <div className={sheet ? 'ol-sheet is-open' : 'ol-sheet'} id="ol-sheet">
        <div className="ctl-seg ol-span-more" role="group" aria-label="More spans">
          {spanSeg(true)}
        </div>
        {(range || view.span === null) && <RangeInputs view={view} setView={setView} />}
        <label className="ol-field">
          Group by{' '}
          <select
            value={view.bucket}
            onChange={(e) => setView({ ...view, bucket: e.target.value as BucketChoice })}
          >
            <option value="auto">{chosen === null ? 'auto' : `auto · ${chosen}`}</option>
            <option value="hour" disabled={!hourAllowed(view)}>
              hour{!hourAllowed(view) ? ` (over ${MAX_BUCKETS.toLocaleString('en-US')} buckets)` : ''}
            </option>
            <option value="day">day</option>
            <option value="week">week</option>
            {/* Month over a span under 60 days draws one lonely column, and the
                route refuses it: disabled with the reason, as before. */}
            <option value="month" disabled={!monthAllowed({ ...view, bucket: 'month' })}>
              month{days !== null && days < 60 ? ' (span under 60 days)' : ''}
            </option>
          </select>
        </label>
        {admin && (
          <div className="ctl-seg ol-scope" role="group" aria-label="Scope">
            <button type="button" aria-pressed={!view.platform} onClick={() => setView({ ...view, platform: false, tenant: [], exclude_tenant: [], group: view.group === 'tenant_id' ? 'runner_profile' : view.group })}>
              my tenant
            </button>
            <button type="button" aria-pressed={view.platform} onClick={() => setView({ ...view, platform: true })}>
              platform
            </button>
          </div>
        )}
        {admin && view.platform && (
          <Picker
            label="Tenant"
            options={tenants}
            selected={view.tenant}
            summary={tenantSummary}
            onChange={(next) => setView({ ...view, tenant: next, exclude_tenant: next.length > 0 ? [] : view.exclude_tenant })}
          />
        )}
        {admin && view.platform && verifyServed && view.tenant.length === 0 && (
          // ONE CLICK, AND AN EXCLUSION, NOT AN INCLUDE LIST: a tenant created
          // tomorrow is still counted (owner decision on #185).
          <button
            type="button"
            className="sbf-mini ol-verify"
            aria-pressed={verifyOut}
            onClick={() =>
              setView({
                ...view,
                exclude_tenant: verifyOut ? view.exclude_tenant.filter((t) => t !== VERIFY_TENANT) : [...view.exclude_tenant, VERIFY_TENANT].sort(),
              })
            }
          >
            {verifyOut ? `include ${VERIFY_TENANT}` : `exclude ${VERIFY_TENANT}`}
          </button>
        )}
        <Picker
          label="Profile"
          options={profileOptions}
          selected={view.profile}
          summary={
            view.profile.length > 0
              ? view.profile.join(', ')
              : profiles === 'failed'
                ? 'catalogue not read'
                : profileOptions === null
                  ? 'all'
                  : `all ${profileOptions.length}`
          }
          onChange={(next) => setView({ ...view, profile: next.sort() })}
        />
        <SubmittedBy view={view} setView={setView} />
        <div className="ctl-seg ol-kind" role="group" aria-label="Kind">
          {(['all', 'standalone', 'steps'] as const).map((k) => (
            <button key={k} type="button" aria-pressed={view.kind === k} onClick={() => setView({ ...view, kind: k })}>
              {KIND_LABEL[k]}
            </button>
          ))}
        </div>
      </div>
      <button
        type="button"
        className="ol-table-toggle is-end"
        aria-pressed={view.table}
        onClick={() => setView({ ...view, table: !view.table })}
      >
        Table
      </button>
    </div>
  )
}

/**
 * Screen A4 -- Tenants (admin only), and the platform-wide state counts.
 *
 * Both are admin-gated, and a 403 renders as information rather than a red
 * failure: a non-admin genuinely cannot read these, and styling that as an
 * error makes a working page look broken.
 */
export function TenantsScreen() {
  return (
    <Screen
      title="Tenants"
      load={loadTenants}
      summary={(d) => `${d.tenants.length} tenants`}
      // One sentence, and it is the one that separates a real zero from a
      // failed read. The provisioning argument is `docs/`.
      empty={{
        heading: 'No tenants',
        body: 'The read succeeded and returned nothing.',
      }}
    >
      {(d) => (
        <section className="section">
          {/* `is-scroll` (CH-13, design-system.md §7.3), for the same reason
              as the People table above: nine columns compared down the
              roster is a data table, so below 900px it scrolls sideways with
              the tenant column held in view; only records of four columns or
              fewer stack. It was `is-stacked`, because at 390pt everything
              from `Max active` rightwards sat behind a scrollbar this
              platform does not paint, and a tenant row whose visible part
              ends at `Principal` says nothing about whether that tenant can
              run anything at all. The held column is what answers that now:
              every value stays beside the tenant it belongs to.

              STATUS IS THE SECOND COLUMN, beside the name (AH-11). It was the
              last, and at 1440 it sat past the panel edge behind the same
              unpainted scrollbar -- pushed there by two identity columns of
              55-65 characters in `nowrap` cells. Whether a tenant can run
              anything is the first thing this roster is read for, so it
              cannot be the column that falls off, and at 390 it is the first
              column past the held name. The identities are shortened on the
              wide table instead (`.ten-ident`, styles.css). */}
          <div className="table-wrap is-scroll">
            <table className="pools" role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col" rowSpan={2}>Tenant</th>
                  <th role="columnheader" scope="col" rowSpan={2}>Status</th>
                  <th role="columnheader" scope="col" rowSpan={2}>Kind</th>
                  <th role="columnheader" scope="col" rowSpan={2}>Principal</th>
                  {/* THE CEILING ADMISSION ACTUALLY APPLIES (AH-12). The two
                      registry values were printed bare, and the figure that
                      binds -- the smaller, which every writer of the tenant
                      pool writes as its hard limit -- was nowhere. It is the
                      column; the two values it comes from sit under
                      `Configured`.

                      THE HEAD IS ITS LABEL AND NOTHING ELSE. The decided help
                      link is under the table, not a `?` in here: a glyph in a
                      `<th>` publishes its HelpNote as part of the column's
                      name, which a screen reader then reads on every cell, and
                      while this table was stacked below 900px §B6.3 hid this
                      row while leaving it in the tab order. It scrolls now
                      (CH-13), so the row shows, but the first reason stands. */}
                  <th role="columnheader" scope="col" rowSpan={2} className="n">
                    Enforced
                  </th>
                  <th role="columnheader" scope="colgroup" colSpan={2} className="n">
                    Configured
                  </th>
                  <th role="columnheader" scope="col" rowSpan={2}>Credentials</th>
                  <th role="columnheader" scope="col" rowSpan={2}>Identity</th>
                </tr>
                <tr role="row">
                  <th role="columnheader" scope="col" className="n">Max active</th>
                  <th role="columnheader" scope="col" className="n">Units</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {d.tenants.map((t) => (
                  <tr role="row" key={t.tenant_id} className={t.enabled === false ? 'paused' : undefined}>
                    <th role="rowheader" scope="row">{t.tenant_id}</th>
                    <td role="cell" data-label="Status">
                      {t.enabled === false ? (
                        <span className="tag paused">disabled</span>
                      ) : (
                        <span className="tag ok">enabled</span>
                      )}
                    </td>
                    <td role="cell" data-label="Kind">{t.kind}</td>
                    <td role="cell" data-label="Principal" className="mono">
                      <span className="ten-ident" title={t.principal}>
                        {t.principal}
                      </span>
                    </td>
                    <td role="cell" data-label="Enforced" className="n">
                      <Enforced tenant={t} />
                    </td>
                    <td role="cell" data-label="Max active" className="n">{t.max_active}</td>
                    <td role="cell" data-label="Units" className="n">{t.capacity_units}</td>
                    <td role="cell" data-label="Credentials">
                      {t.credentials.length > 0 ? (
                        // `.tags`, the wrapper every other run of tags in this
                        // console sits in: it spaces them. Bare, `anthropic`
                        // and `openai` rendered touching, as one word. They
                        // stay `.tag` and not `.ctl-chip` -- a chip is a state,
                        // and a credential name is metadata.
                        <span className="tags">
                          {t.credentials.map((c) => (
                            // These are Secret Manager NAMES and `.tag`
                            // uppercases. An uppercased secret name is one
                            // nobody can look up, so the value is wrapped:
                            // `.id` beats the ancestor by inheritance.
                            <span className="tag" key={c}>
                              <Id>{c}</Id>
                            </span>
                          ))}
                        </span>
                      ) : (
                        <span className="tag capped">none registered</span>
                      )}
                    </td>
                    <td role="cell" data-label="Identity" className="mono">
                      {/* null means NO IDENTITY, not an empty string. A blank
                          cell here reads as fine and it is the opposite. */}
                      {typeof t.service_account === 'string' ? (
                        <span className="ten-ident" title={t.service_account}>
                          {t.service_account}
                        </span>
                      ) : (
                        <span className="tag full">no service account</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* WHY A COLUMN IS ABSENT IS STILL STATED, IN ONE LINE, IN THE
              READER'S WORDS (AH-21). A table with a column quietly missing is
              a table a reader completes from memory, so this cannot simply be
              deleted. It printed the API's field name and a rationale under a
              `not measured` mark -- but a budget is a setting, not a
              measurement, and this line sits in no figure slot, so it carries
              no mark (as AG-5's plain facts do not). The account of the 422
              and of the missing cost-attribution source is one paragraph of
              the Tenants fields topic, behind `Why →`.

              AND IT IS IN PLAIN INK (`.ten-budget`). AG-5 defines a plain
              fact as no mark AND NO DIMMING; `.ctl-panel-note` is the faint
              tone of a qualifier under a figure, and this line qualifies no
              figure -- it is the fact that a column is absent. */}
          <p
            className="ctl-panel-note ten-budget"
            aria-label="No budget column, and no budget can be set: the only route that could set a budget refuses it, so none is set for any tenant, and the column is left out rather than drawn empty."
          >
            no budget column · no budget can be set
            <a href={helpHref(TENANT_HELP)}>Why &rarr;</a>
          </p>
          {/* THE HELP LINK AH-12 DECIDED FOR THE ENFORCED COLUMN: the footer
              index every migrated panel carries (HelpCard.tsx, route 3 of 4),
              drawn at every width and costing no glyph from the ration. It is
              a separate line from the note's `Why →` because the two answer
              different questions -- what the columns mean, and why one is
              missing -- that happen to live in one topic. */}
          <HelpLinks topics={TENANT_TOPICS} label="Reading this table:" />
        </section>
      )}
    </Screen>
  )
}

/** Where the Enforced column, and the budget the table leaves out, are explained. */
const TENANT_HELP: TopicId = 'tenant-fields'

/** The Tenants table's help link (AH-12), as a footer index. */
const TENANT_TOPICS: readonly TopicId[] = [TENANT_HELP]

/**
 * THE CEILING ADMISSION APPLIES TO A TENANT (AH-12): the smaller of its two
 * configured values. Both cap the same count -- the units its running work
 * holds, where every task costs at least one -- so the smaller binds, and it
 * is what every writer of the tenant pool writes as its hard limit:
 * `set_tenant_limits` and `ensure_tenant` (swarm_api/store.py),
 * scripts/register-tenant.sh, and terraform/infra/locals.tf `pool_tenants`.
 *
 * A value that is not a finite number is not a limit anyone can read, so the
 * cell is the em dash rather than `NaN` or a guess from the other value.
 */
function Enforced({ tenant: t }: { tenant: { max_active: number; capacity_units: number } }) {
  if (!Number.isFinite(t.max_active) || !Number.isFinite(t.capacity_units)) {
    return <i className="ctl-em">—</i>
  }
  return <>{Math.min(t.max_active, t.capacity_units)}</>
}
