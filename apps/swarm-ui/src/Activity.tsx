import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  loadCapacity,
  loadMe,
  loadOutcomes,
  loadRunnerProfiles,
  loadStats,
  loadTasksInState,
  loadTenants,
  type OutcomeSection,
} from './api'
import { IDLE_POLL_MS } from './Agents'
import { CardFailed, CardSkeleton } from './CardSkeleton'
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
  type RowLink,
} from './Ledger'
import {
  MAX_BUCKETS,
  OUTCOMES_CACHE_S,
  SPANS,
  VERIFY_TENANT,
  cacheable,
  alignedWords,
  dayDocWords,
  filtersSet,
  hourAllowed,
  instantLabel,
  interval,
  ledgerRange,
  monthAllowed,
  nothingReadWords,
  outcomesQuery,
  parseView,
  pct,
  rangeRefusal,
  serializeView,
  spanCoverage,
  spanLengthMs,
  unitWord,
  viewDays,
  viewerZone,
  wallOf,
  withRows,
  type BucketChoice,
  type GroupBy,
  type Kind,
  type LedgerView,
  type OutcomeBucket,
  type Outcomes,
  type RowOutcome,
  type Span,
} from './outcomes'
import { Button, NamedMark, Segmented, StateMark, WarnMark } from './components'
import { Absent, Mark } from './primitives'
import { CountNote, Id, PageHead, RefreshControl, Screen, useClaimPageAge, useIdleStop, usePoll } from './Shell'
import { TIMELINE_POLL_MS, TimelinePages } from './TimelineLanes'
import { EndedRowsCard } from './TimelineRows'
import { useInView } from './useInView'
import { tableMode, usePhoneTables } from './capacityPoll'
import { AGE_TICK_MS, useNow } from './useNow'
import type { Pool, Tenant } from './types'
import './styles/admin.css'

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
      // The filters are remembered; an open figure's rows are not -- a bare
      // `#work/timeline` tomorrow should not reopen today's list of failures.
      rememberView(serializeView({ ...next, rows: null, at: null, rowsKey: null }))
      onView?.(q)
    },
    [onView],
  )

  // A Reliability row opens the rows behind it (#116): this page, narrowed to
  // that profile, tenant or person, keeping the span and the grouping. It is
  // the Timeline and not the Agents list because only the ledger reads the
  // same span; the Agents list reads its newest page, whatever the span.
  const rowLink = useCallback(
    (by: GroupBy, key: string): RowLink | null => {
      const next = narrowedTo(view, by, key)
      if (next === null) return null
      const q = serializeView(next)
      return { href: q === '' ? '#work/timeline/outcomes' : `#work/timeline/outcomes?${q}`, open: () => setView(next) }
    },
    [view, setView],
  )

  // A FIGURE OPENS THE TASKS BEHIND IT (#116), on this page: a bucket's (or
  // the span's) failed, cancelled or succeeded, and a Reliability cell's --
  // that person's, profile's or tenant's -- read from the same fold the
  // figure was counted from. The open figure is in the address, so the list
  // behind "Failed 21" is a link someone can send.
  const viewLink = useCallback(
    (next: LedgerView): RowLink => {
      const q = serializeView(next)
      return { href: q === '' ? '#work/timeline/outcomes' : `#work/timeline/outcomes?${q}`, open: () => setView(next) }
    },
    [setView],
  )
  const figureLink = useCallback(
    (outcome: RowOutcome, at: string | null): RowLink => viewLink(withRows(view, outcome, at)),
    [view, viewLink],
  )
  const cellLink = useCallback(
    (outcome: RowOutcome, rowKey: string): RowLink | null => viewLink(withRows(view, outcome, null, rowKey)),
    [view, viewLink],
  )
  const closeRows = useCallback(() => setView({ ...view, rows: null, at: null, rowsKey: null }), [view, setView])

  // ---- the ledger read ---------------------------------------------------
  // THE HEADLINE'S OWN READ, AND ONLY ITS SECTIONS (#377). The success rate,
  // the drawing, the facts line and the provenance foot are the page: they
  // read eagerly, and only what they draw (`LEDGER_SECTIONS`). Every card
  // below reads its own part when it scrolls into view (`LedgerPart`).
  const key = useMemo(() => outcomesQuery(view, tz).toString(), [view, tz])
  const [nonce, setNonce] = useState(0)
  const [pending, setPending] = useState(true)
  const [latest, setLatest] = useState<{ key: string; result: Result<Outcomes> } | null>(null)
  /** The last payload that drew, kept (dimmed) while a filter change is being read. */
  const [good, setGood] = useState<{ key: string; data: Outcomes; continues: boolean } | null>(null)
  /** How many buckets the last read of this query had built, so a continuation that builds nothing stops. */
  const built = useRef<{ key: string; read: number } | null>(null)
  useEffect(() => {
    let live = true
    setPending(true)
    // The same cast as the cards' (`loadPart`): the payload is the envelope and
    // `LEDGER_SECTIONS`, which is all the headline, the drawing, the facts and
    // the foot read (timeline.cards.test.tsx draws them from nothing more).
    const ledger = loadOutcomes(new URLSearchParams(key), LEDGER_SECTIONS) as Promise<Result<Outcomes>>
    ledger.then((r) => {
      if (!live) return
      setLatest({ key, result: r })
      if (r.status === 'ok' || r.status === 'stale') {
        const before = built.current?.key === key ? built.current.read : null
        built.current = { key, read: r.data.totals.buckets_read }
        setGood({ key, data: r.data, continues: buildContinues(r.data, before) })
      }
      setPending(false)
    })
    return () => {
      live = false
    }
  }, [key, nonce])

  // ---- the cards' reads (#377) ---------------------------------------------
  // ONE REQUEST PER DISTINCT ASK. Three cards ("Why tasks failed", "Reported
  // cost", "Why tasks were cancelled") draw from the same two sections; the
  // ones in view at once share one read rather than sending it three times,
  // and one that scrolls in later under the same filters and refresh is drawn
  // from it. A failed read is forgotten, so "try again" reads again; a filter
  // change or a refresh starts afresh.
  const parts = useRef(new Map<string, Promise<Result<Outcomes>>>())
  const loadPart = useCallback((query: string, sections: readonly OutcomeSection[], ask: string) => {
    const k = `${ask}\n${sections.join(',')}`
    let p = parts.current.get(k)
    if (p === undefined) {
      // Forget the reads of earlier filters and refreshes: `ask` is the query,
      // the refresh count and the card's retry count, one per line. Pruned
      // here rather than in an effect of this screen's, which would run after
      // the cards' own effects and drop the reads they had just shared.
      const now = ask.slice(0, ask.lastIndexOf('\n') + 1)
      for (const old of [...parts.current.keys()]) if (!old.startsWith(now)) parts.current.delete(old)
      // THE ONE CAST: a sectioned payload is the envelope and `sections` only,
      // and each card is handed exactly the sections it reads (`CARD_SECTIONS`;
      // timeline.cards.test.tsx draws every card from nothing more).
      p = loadOutcomes(new URLSearchParams(query), sections) as Promise<Result<Outcomes>>
      parts.current.set(k, p)
      const mine = p
      void mine.then((r) => {
        if (r.status !== 'ok' && r.status !== 'stale' && parts.current.get(k) === mine) parts.current.delete(k)
      })
    }
    return p
  }, [])

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
  //
  // AND IT IS READ ONLY IN VIEW (#377), as every card is. Its content is not
  // the ledger's: the other seven each read their own sections of it
  // (`LedgerPart`), this one reads the live counts. The card reads when it comes
  // within `CARD_ROOT_MARGIN` of the viewport, and an open, a refresh, a
  // filter change or a poll tick that lands while it is off-screen is read
  // when it scrolls back. A read still in flight when the card leaves is
  // dropped (its answer is ignored, so it cannot draw a late failure) and is
  // asked again on return. Without IntersectionObserver the card is always
  // "in view", so it reads on open as it always did.
  const [openRef, openInView] = useInView<HTMLDivElement>()
  const [openRetry, setOpenRetry] = useState(0)
  /** The ask whose reads both landed while the card was in view. */
  const openAnswered = useRef<string | null>(null)
  const [open, setOpen] = useState<OpenWork>({ stats: null, parked: null })
  const [openTick, setOpenTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => {
      if (typeof document === 'undefined' || document.visibilityState !== 'hidden') setOpenTick((n) => n + 1)
    }, IDLE_POLL_MS)
    return () => clearInterval(timer)
  }, [])
  /** What the card is asked to show: every reason it re-reads, as one value. */
  const openAsk = `${key}\n${nonce}\n${openTick}\n${openRetry}`
  useEffect(() => {
    if (!openInView || openAnswered.current === openAsk) return
    let live = true
    let landed = 0
    const land = () => {
      landed += 1
      if (landed === 2) openAnswered.current = openAsk
    }
    loadStats().then((r) => {
      if (!live) return
      land()
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
      land()
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
  }, [openInView, openAsk])
  const retryOpen = () => {
    // Back to the skeleton, not the failure, while the retry is in flight.
    setOpen({ stats: null, parked: null })
    setOpenRetry((n) => n + 1)
  }
  const openFailures = [
    open.stats?.status === 'error' ? `The live state counts could not be read: ${open.stats.message}` : null,
    open.parked?.status === 'error' ? `The parked list could not be read: ${open.parked.message}` : null,
  ].filter((f): f is string => f !== null)

  const [picked, setPicked] = useState<string | null>(null)
  const refresh = () => setNonce((n) => n + 1)
  // THE CADENCE (#117: both Timeline pages every `TIMELINE_POLL_MS`) and THE
  // ONE AGE (#98), on the head's refresh control.
  const { idle, resume } = useIdleStop(true)
  usePoll(TIMELINE_POLL_MS, refresh, idle)
  useClaimPageAge(true)

  // A PARTIAL BUILD CONTINUES BY ITSELF (QA G3-01, 2026-10-07). The route
  // builds a span's days a budget at a time and caches only a whole one, so
  // the next read carries on -- but the page waited for its 60 s poll or a
  // click for that read, and a 30d span sat at "6 of 30 days" with the cards
  // below it already reading the whole span. While a read is still building,
  // the next one is asked `CONTINUE_BUILD_MS` after it lands; it stops when a
  // read builds nothing more, and while the page is idle.
  const continuing = !pending && !idle && good !== null && good.key === key && good.continues
  useEffect(() => {
    if (!continuing) return
    const t = setTimeout(() => setNonce((n) => n + 1), CONTINUE_BUILD_MS)
    return () => clearTimeout(t)
  }, [continuing, good])

  // WHAT THE CARDS COVERED, so a partial headline can say the cards below it
  // read further (QA G3-01): each card reads its own part, often after the
  // headline's read has built more of the span.
  const [cardCover, setCardCover] = useState<{ key: string; whole: Readonly<Record<string, boolean>> }>({ key: '', whole: {} })
  const onCardCover = useCallback((query: string, id: string, whole: boolean) => {
    setCardCover((c) => {
      const base = c.key === query ? c.whole : {}
      return base[id] === whole && c.key === query ? c : { key: query, whole: { ...base, [id]: whole } }
    })
  }, [])
  const cardsRead = cardCover.key === key ? Object.values(cardCover.whole) : []

  const settled = latest !== null && latest.key === key ? latest.result : null
  const failure: ApiError | null = !pending && settled?.status === 'error' ? settled.error : null
  const data = good?.data ?? null
  const dim = pending || (good !== null && good.key !== key)

  // A zoom changes the buckets, so an open bucket's rows (named by its start) close with it.
  const zoom = (b: OutcomeBucket) =>
    setView({
      ...view,
      span: null,
      since: b.start,
      until: b.end,
      bucket: 'auto',
      back: serializeView({ ...view, back: null }),
      rows: null,
      at: null,
      rowsKey: null,
    })
  const mine = () => setView({ ...view, platform: false, tenant: [], exclude_tenant: [], group: view.group === 'tenant_id' ? 'runner_profile' : view.group })

  return (
    <>
      {/* TITLE LEFT, ACTIONS RIGHT (#138): the refresh, with its ticking
          age and the cadence; the range and the cache are the note over the
          first card. The age is the payload's (`generated_at`), which a
          cached answer carries from when it was counted. */}
      <PageHead title="Timeline">
        <RefreshControl
          readAt={data === null ? null : Date.parse(data.generated_at)}
          now={now}
          cadence={{ base: TIMELINE_POLL_MS, wait: TIMELINE_POLL_MS }}
          reading={pending}
          idle={idle}
          onRefresh={refresh}
          onResume={() => {
            resume()
            refresh()
          }}
        />
      </PageHead>

      {/* Outcomes is the Timeline's second page now (timeline.html pick A); Lanes is /timeline. */}
      <TimelinePages at="outcomes" otherTitle={data === null ? undefined : (bothRangesWords(data) ?? undefined)} />

      <CountNote>
        {data === null ? null : (
          <>
            {rangeWords(data)}
            {alignedWords(data) !== null && <span className="ol-aligned">{` · ${alignedWords(data)}`}</span>}
            {data.cached && ` · from the ${OUTCOMES_CACHE_S} s cache`}
          </>
        )}
      </CountNote>

      <LedgerToolbar
        view={view}
        setView={setView}
        admin={admin === true}
        profiles={profiles}
        tenants={tenants}
        data={data}
      />

      {failure !== null ? (
        // THE PAGE'S READ FAILED, SO NO CARD IS DRAWN (§6.9): a 403 or a 422 on
        // these filters is every card's answer too, and a card drawn beside a
        // failed headline would be a number under a failure.
        <LedgerFailed error={failure} onRetry={refresh} onMine={mine} />
      ) : (
        <>
          {data === null ? (
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
                continuing={continuing}
                cardsWhole={cardsRead.length === 0 ? null : { whole: cardsRead.filter(Boolean).length, of: cardsRead.length }}
                view={view}
                picked={picked}
                onPick={setPicked}
                onZoom={zoom}
                figureLink={figureLink}
              />
            </div>
          )}
          {view.rows !== null && <EndedRowsCard view={view} tz={tz} nonce={nonce} onClose={closeRows} />}
          {/* EVERY CARD READS ITS OWN PART, WHEN IT SCROLLS INTO VIEW (#377),
              whether or not the headline has landed: they are separate reads. */}
          <div className="ctl-cards ol-cards">
            <LedgerPart id="failures" title="Why tasks failed" className="ol-failures" query={key} nonce={nonce} load={loadPart} onCover={onCardCover} lines={[70, 62, 54, 48, 40, 34, 30, 26]}>
              {(d) => <FailureClassesCard data={d} picked={picked} />}
            </LedgerPart>
            <LedgerPart id="retries" title="Retries and attempts" className="ol-retries" query={key} nonce={nonce} load={loadPart} onCover={onCardCover} lines={[60, 72, 66, 58, 50, 56, 64]}>
              {(d) => <RetriesCard data={d} />}
            </LedgerPart>
            <LedgerPart id="latency" title="Time to result, by profile" className="ol-latency" query={key} nonce={nonce} load={loadPart} onCover={onCardCover} lines={[90, 40, 76, 76, 40, 76, 76]}>
              {(d) => <LatencyCard data={d} />}
            </LedgerPart>
            <LedgerPart id="reliability" title="Reliability" className="is-wide ol-reliability" wide query={key} nonce={nonce} load={loadPart} onCover={onCardCover} lines={[36, 92, 92, 92, 92]}>
              {(d) => (
                <ReliabilityCard
                  data={d}
                  group={view.group}
                  platform={view.platform}
                  onGroup={(g: GroupBy) => setView({ ...view, group: g })}
                  rowLink={rowLink}
                  cellLink={cellLink}
                />
              )}
            </LedgerPart>
            <LedgerPart id="workflows" title="Workflows that failed, and where" className="is-wide ol-workflows" wide query={key} nonce={nonce} load={loadPart} onCover={onCardCover} lines={[92, 92, 92, 92, 48]}>
              {(d) => <WorkflowsFailedCard data={d} spanLabel={view.span ?? 'this range'} />}
            </LedgerPart>
            <LedgerPart id="cost" title="Reported cost" className="ol-cost-card" query={key} nonce={nonce} load={loadPart} onCover={onCardCover} lines={[44, 70, 62, 62, 62, 54, 48]}>
              {(d) => <CostCard data={d} picked={picked} />}
            </LedgerPart>
            {/* The observed box. A grid of one, so the card inside stretches
                to the row as it did when it was the grid's own item. */}
            <div ref={openRef} className="ol-lazy">
              {openFailures.length > 0 ? (
                <CardFailed
                  title="Not finished yet"
                  note="span not applied"
                  className="ol-open"
                  failures={openFailures}
                  onRetry={retryOpen}
                />
              ) : open.stats === null ? (
                <CardSkeleton
                  title="Not finished yet"
                  note="span not applied"
                  className="ol-open"
                  say="The live state counts are still being read."
                  lines={OPEN_SKELETON}
                />
              ) : (
                <OpenWorkCard open={open} view={view} tenant={myTenant} now={now} />
              )}
            </div>
            <LedgerPart id="cancels" title="Why tasks were cancelled" className="ol-cancels" query={key} nonce={nonce} load={loadPart} onCover={onCardCover} lines={[60, 52, 44, 36, 30]}>
              {(d) => <CancelCausesCard data={d} picked={picked} />}
            </LedgerPart>
          </div>
          {data !== null && <Provenance data={data} />}
        </>
      )}

      <HelpLinks topics={READING_TOPICS} label="Reading this screen:" />
    </>
  )
}

/**
 * The loaded "Not finished yet" card's lines -- counts, the parks that need a
 * person, the ones that clear themselves, the link -- as skeleton widths.
 */
/**
 * The view narrowed to one Reliability row, or null when the address cannot
 * carry that key (a name `parseView` would drop): a link that silently lands
 * on the unfiltered page is worse than no link. A zoom's way back is kept.
 */
export function narrowedTo(view: LedgerView, by: GroupBy, key: string): LedgerView | null {
  const next: LedgerView =
    by === 'runner_profile'
      ? { ...view, profile: [key] }
      : by === 'tenant_id'
        ? { ...view, platform: true, tenant: [key], exclude_tenant: [] }
        : // The address carries a submitter in lower case (`parseView`), so the link does too.
          { ...view, submitted_by: [key.trim().toLowerCase()] }
  const back = parseView(serializeView(next))
  const kept = by === 'runner_profile' ? back.profile : by === 'tenant_id' ? back.tenant : back.submitted_by
  const want = by === 'submitted_by' ? key.trim().toLowerCase() : key
  return kept.length === 1 && kept[0] === want ? next : null
}

const OPEN_SKELETON = [78, 56, 64, 30] as const

/**
 * What the page itself reads, eagerly: the headline, the drawing (and its
 * Table), the facts line and the provenance foot. `previous` is the delta,
 * `coverage` the facts line's sealed days and the foot.
 */
const LEDGER_SECTIONS = ['buckets', 'totals', 'coverage', 'previous'] as const satisfies readonly OutcomeSection[]

/**
 * WHAT EACH CARD READS, AND NOTHING MORE (#377). Every card says how much of
 * the span it covers (`spanCoverage`), which is `totals` and `buckets`, so
 * every card asks for those two; the rest is the card's own block. "Time to
 * result" prints `wait_excluded` from `latency`, which carries it so the card
 * need not ask for all of `coverage` (a count() per terminal state per
 * tenant). The route caches the FOLD, keyed by the query without its sections
 * (the review of #391), so every card of one query in the same minute is a
 * projection of one scan; only `previous`, `coverage` and `workflows_failed`
 * cost reads of their own, once per fold (`swarm_api.outcomes.Outcomes.read`),
 * so no card asks for `previous`.
 */
const CARD_SECTIONS = {
  failures: ['buckets', 'totals'],
  retries: ['buckets', 'totals', 'retries'],
  latency: ['buckets', 'totals', 'latency'],
  reliability: ['buckets', 'totals', 'groups'],
  workflows: ['buckets', 'totals', 'workflows_failed'],
  cost: ['buckets', 'totals'],
  cancels: ['buckets', 'totals'],
} as const satisfies Record<string, readonly OutcomeSection[]>

type PartLoader = (query: string, sections: readonly OutcomeSection[], ask: string) => Promise<Result<Outcomes>>

/**
 * ONE CARD, READ WHEN IT SCROLLS INTO VIEW (#377): its own `GET /v1/outcomes`
 * with the page's filters and only its sections (`CARD_SECTIONS`).
 *
 *   * NOT IN VIEW, NOT READ. The card reads when it comes within
 *     `CARD_ROOT_MARGIN` of the viewport; before its first answer it is the
 *     skeleton in its own shape (`CardSkeleton`), which is static under
 *     reduced motion.
 *   * A FILTER CHANGE OR A REFRESH re-reads it only if it is in view. Off
 *     screen it keeps its last figures, dimmed as stale (`ctl-stale-body`,
 *     `data-stale`), and is read when it scrolls back -- dimmed until the new
 *     answer lands, never blanked.
 *   * A READ THAT LANDS LATE IS DROPPED: one still in flight when the card
 *     scrolls away, or when the filters change, is ignored, so it can draw
 *     neither an old figure nor a late failure. It is asked again on return.
 *   * A FAILED READ IS A FAILURE (`CardFailed`), with the server's words and
 *     "try again", which goes back to the skeleton and reads again.
 */
function LedgerPart({
  id,
  title,
  className,
  wide = false,
  query,
  nonce,
  load,
  onCover,
  lines,
  children,
}: {
  id: keyof typeof CARD_SECTIONS
  /** The card's title, drawn by its skeleton and its failure. */
  title: string
  /** The loaded card's own classes, so the skeleton and the failure stand in its place. */
  className: string
  /** A card that spans the row. The observed box is the grid item, so it takes the span. */
  wide?: boolean
  query: string
  nonce: number
  load: PartLoader
  /** Told, per answer, whether the card's read covered the whole span. */
  onCover?: (query: string, id: string, whole: boolean) => void
  /** The skeleton's line widths, in the loaded card's shape. */
  lines: readonly number[]
  children: (data: Outcomes) => ReactNode
}) {
  const sections = CARD_SECTIONS[id]
  const [ref, inView] = useInView<HTMLDivElement>()
  const [retry, setRetry] = useState(0)
  /** Every reason the card re-reads, as one value. */
  const ask = `${query}\n${nonce}\n${retry}`
  /** The ask whose answer landed while the card was in view. */
  const answered = useRef<string | null>(null)
  const [good, setGood] = useState<{ ask: string; data: Outcomes } | null>(null)
  const [failed, setFailed] = useState<{ ask: string; message: string } | null>(null)
  useEffect(() => {
    if (!inView || answered.current === ask) return
    let live = true
    load(query, sections, ask).then((r) => {
      if (!live) return
      answered.current = ask
      if (r.status === 'ok' || r.status === 'stale') {
        setGood({ ask, data: r.data })
        setFailed(null)
        onCover?.(query, id, spanCoverage(r.data).complete)
      } else {
        setFailed({ ask, message: r.status === 'error' ? r.error.message : 'the read did not complete' })
      }
    })
    return () => {
      live = false
    }
    // `sections` is fixed per `id`, and `ask` carries query, nonce and retry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inView, ask, load])
  const failure = failed !== null && failed.ask === ask ? failed : null
  const stale = failure === null && good !== null && good.ask !== ask
  const onRetry = () => {
    // Back to the skeleton, not the failure or an older figure, while the retry is in flight.
    setGood(null)
    setRetry((n) => n + 1)
  }
  return (
    <div
      ref={ref}
      className={stale ? 'ol-lazy ctl-stale-body' : 'ol-lazy'}
      data-card={id}
      data-stale={stale ? 'true' : undefined}
      aria-busy={stale && inView ? true : undefined}
      // `.ol-card.is-wide` spans the row as a grid item; wrapped, the box is the item.
      style={wide ? { gridColumn: '1 / -1' } : undefined}
    >
      {failure !== null ? (
        <CardFailed
          title={title}
          className={className}
          failures={[`${title} could not be read: ${failure.message}`]}
          onRetry={onRetry}
        />
      ) : good === null ? (
        <CardSkeleton title={title} className={className} say={`${title} is still being read.`} lines={lines} />
      ) : (
        children(good.data)
      )}
    </div>
  )
}

/** `Sep 12, 00:00 → now`, in the zone the server bucketed in. */
function rangeWords(d: Outcomes): string {
  return ledgerRange(d)
}

/**
 * What Lanes reads for the same named span: the last `span` to the minute,
 * ending at this read (QA G3-08). Null for a from–to range, which both pages
 * read exactly.
 */
export function lanesRangeWords(d: Outcomes): string | null {
  const ms = d.requested.span === null ? null : spanLengthMs(d.requested.span)
  if (ms === null) return null
  const until = Date.parse(d.generated_at)
  return `${instantLabel(new Date(until - ms).toISOString(), d.tz)} → now`
}

/** Both pages' ranges for one named span, for the link between them (QA G3-08). */
export function bothRangesWords(d: Outcomes): string | null {
  const aligned = alignedWords(d)
  const lanes = lanesRangeWords(d)
  if (aligned === null || lanes === null) return null
  return `Lanes reads the last ${d.requested.span} to the minute, ${lanes}; this page reads ${rangeWords(d)} (${aligned})`
}

/** How long after a partial read lands the next one is asked, to continue the build (QA G3-01). */
export const CONTINUE_BUILD_MS = 5_000

/**
 * Whether the next read continues building this payload: it is partial
 * because days were past the read's budget (or the span before it is), and
 * the last read built something -- `derived_now`, or more buckets than the
 * read before it of the same query (`readBefore`, null on the first). A read
 * that built nothing will not build more on the next, so it is left to the poll.
 */
export function buildContinues(d: Outcomes, readBefore: number | null): boolean {
  if (cacheable(d) || d.cached) return false
  const unbuilt =
    d.buckets.some((b) => b.state === 'unread' && b.unread_reason === 'derive_budget') ||
    (d.previous !== null && !d.previous.complete)
  if (!unbuilt) return false
  return d.coverage.derived_now > 0 || readBefore === null || d.totals.buckets_read > readBefore
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
  continuing,
  cardsWhole,
  view,
  picked,
  onPick,
  onZoom,
  figureLink,
}: {
  data: Outcomes
  /** The next read is already scheduled to continue this partial build. */
  continuing: boolean
  /** How many of the cards read so far covered the whole span, or null before any did. */
  cardsWhole: { whole: number; of: number } | null
  view: LedgerView
  picked: string | null
  onPick: (start: string | null) => void
  onZoom: (b: OutcomeBucket) => void
  figureLink: (outcome: RowOutcome, at: string | null) => RowLink
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
            {continuing && <span className="ol-q ol-building"> · still building, read again in {CONTINUE_BUILD_MS / 1000} s</span>}
          </span>
        )}
        {/* THE CARDS AND THE HEADLINE CAN COVER DIFFERENT DAYS (QA G3-01): each
            card reads its own part, often after the headline's read built
            more, so a card can be whole while this figure is partial. */}
        {!t.complete && !cov.none && cardsWhole !== null && cardsWhole.whole > 0 && (
          <span className="ol-q ol-cover-split">
            {cardsWhole.whole === cardsWhole.of ? 'cards' : `${cardsWhole.whole} of ${cardsWhole.of} cards`}: whole span · headline:{' '}
            {t.buckets_read} of {t.buckets} {unitWord(data.bucket)} built
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
          finished tasks carry no end time
          <a href={helpHref('outcome-buckets')}>Why &rarr;</a>
        </p>
      ) : null}
      {view.table ? (
        <LedgerTable data={data} />
      ) : (
        <OutcomeLedger data={data} picked={picked} onPick={onPick} onZoom={onZoom} figureLink={figureLink} />
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
 *
 * A CACHE HIT BUILT NOTHING. The route hands back the payload it kept, with
 * `cached: true` and the rest untouched, so `derived_now` -- like
 * `generated_at` and `reads` -- is the kept read's: the foot names that read
 * by its time instead of saying "this read" beside "0 reads this request"
 * (epic #222). The days built are rollup documents too (tenant-days over this
 * span and, when there is one, the span before it), so they take
 * `dayDocWords` like the sealed ones.
 */
function Provenance({ data }: { data: Outcomes }) {
  const c = data.coverage
  const generated = new Date(data.generated_at).toLocaleTimeString(undefined, { timeZone: data.tz, hourCycle: 'h23' })
  const built = `${c.derived_now} ${dayDocWords(data, c.derived_now)} built by ${data.cached ? `the ${generated} read` : 'this read'}`
  const clauses: ReactNode[] = [
    `${c.days.sealed} ${dayDocWords(data, c.days.sealed)} sealed`,
    c.days.live > 0 ? `${c.days.live} live` : null,
    // The headline's read only (#377): each card reads its own part, and says so by its own skeleton.
    data.cached ? `from the ${OUTCOMES_CACHE_S} s cache · 0 reads this request` : `${data.reads} reads for the headline; each card reads its own`,
    cacheable(data) ? `cached ${OUTCOMES_CACHE_S} s` : 'not cached: partial, the next read continues the build',
    `generated ${generated}`,
    c.derived_now > 0 ? built : null,
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
        <button type="button" className="c-link is-sm" onClick={onMine}>
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
      <button type="button" className="c-link is-sm" onClick={onRetry}>
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
          <button type="button" className="c-link is-sm" onClick={() => onChange([])}>
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
        className="c-link is-sm"
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
  // THE SPAN CONTROL, the canonical segmented control (#503 swap): every span
  // and `from–to` on a wide screen; on a phone 24h and `from–to` move to the
  // sheet (`is-wide-only` here, `ol-span-more` there).
  const pickSpan = (k: Span | 'range') => (k === 'range' ? setRange(!range) : choose(k))
  const aligned = data === null ? null : alignedWords(data)
  const spanOptions = (sheet: boolean) => [
    ...SPANS.filter((s) => !sheet || s === '24h').map((s) => ({
      key: s as Span | 'range',
      label: s,
      ...(!sheet && s === '24h' ? { className: 'is-wide-only' } : {}),
      // THE CHOSEN SPAN SAYS HOW IT WAS ALIGNED (QA G3-08): `24h` here is whole hours, not Lanes' last 24 hours.
      ...(aligned !== null && data?.requested.span === s ? { title: aligned } : {}),
    })),
    { key: 'range' as const, label: 'from–to', ...(sheet ? {} : { className: 'is-wide-only' }) },
  ]
  return (
    <div className="ctl-toolbar ol-toolbar" role="group" aria-label="Timeline filters">
      <Segmented className="ol-span" label="Span" value={view.span ?? 'range'} options={spanOptions(false)} onChange={pickSpan} />
      {back !== null && (
        // A zoom is a span change, and the chip is the way back to the span it came from.
        <button type="button" className="c-link is-sm ol-back" onClick={() => setView(back)}>
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
        <Segmented className="ol-span-more" label="More spans" value={view.span ?? 'range'} options={spanOptions(true)} onChange={pickSpan} />
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
          <Segmented
            className="ol-scope"
            label="Scope"
            value={view.platform ? 'platform' : 'tenant'}
            options={[
              { key: 'tenant', label: 'my tenant' },
              { key: 'platform', label: 'platform' },
            ]}
            onChange={(k) =>
              k === 'platform'
                ? setView({ ...view, platform: true })
                : setView({ ...view, platform: false, tenant: [], exclude_tenant: [], group: view.group === 'tenant_id' ? 'runner_profile' : view.group })
            }
          />
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
          <Button
            size="sm"
            className="ol-verify"
            aria-pressed={verifyOut}
            onClick={() =>
              setView({
                ...view,
                exclude_tenant: verifyOut ? view.exclude_tenant.filter((t) => t !== VERIFY_TENANT) : [...view.exclude_tenant, VERIFY_TENANT].sort(),
              })
            }
          >
            {verifyOut ? `include ${VERIFY_TENANT}` : `exclude ${VERIFY_TENANT}`}
          </Button>
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
        <Segmented
          className="ol-kind"
          label="Kind"
          value={view.kind}
          options={(['all', 'standalone', 'steps'] as const).map((k) => ({ key: k, label: KIND_LABEL[k] }))}
          onChange={(k) => setView({ ...view, kind: k })}
        />
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
/**
 * THE CEILING EACH TENANT'S POOL ENFORCES, READ FROM ITS POOL (G5-13, QA
 * 2026-10-07). `Enforced` was `Math.min(max_active, capacity_units)` worked
 * out here: a second statement of a rule the tenant pool already carries, and
 * one that mixes agents with units. It is now the pool's own
 * `effective_limit`, as `/v1/capacity` serves it to an admin -- the same
 * figure Pools and Pool limits print for `tenant:<id>`, and the one admission
 * actually compares against (it also folds in an adaptive or quota cap, which
 * the registry values never could).
 *
 * A READ BESIDE THE ROSTER, NOT IN FRONT OF IT. The roster is the page and
 * its read is the page's age; a capacity read that is slow or fails must not
 * hold the roster back, so it is read here, again each time the roster is,
 * and until it lands each Enforced cell is a dash that says why. A refresh
 * keeps the previous figures on screen while the next read is in flight.
 */
type TenantPools = Record<string, Pool> | 'reading' | 'unread'

function WithTenantPools({ roster, children }: { roster: unknown; children: (pools: TenantPools) => ReactNode }) {
  const [pools, setPools] = useState<TenantPools>('reading')
  useEffect(() => {
    let live = true
    Promise.resolve(loadCapacity()).then(
      (r) => {
        if (!live) return
        setPools(r.status === 'ok' || r.status === 'stale' ? Object.fromEntries(r.data.pools.map((p) => [p.name, p])) : 'unread')
      },
      () => {
        if (live) setPools('unread')
      },
    )
    return () => {
      live = false
    }
  }, [roster])
  return <>{children(pools)}</>
}

export function TenantsScreen() {
  // A record per tenant on a phone (QA G5-08): the roster below.
  const phone = usePhoneTables()
  return (
    <Screen
      title="Tenants"
      load={loadTenants}
      // WHAT THE ROSTER IS SCANNED FOR, COUNTED (#134). `no tenant key` is a
      // tenant with no credential of its own registered; it is NOT "cannot
      // run" -- an account lent to it on Capacity › Accounts still runs its
      // work -- so the count names the missing key and claims nothing more.
      summary={(d) =>
        `${d.tenants.length} tenants · ${d.tenants.filter((t) => t.credentials.length === 0).length} with no tenant key · ${
          d.tenants.filter((t) => t.enabled === false).length
        } disabled`
      }
      // One sentence, and it is the one that separates a real zero from a
      // failed read. The provisioning argument is `docs/`.
      empty={{
        heading: 'No tenants',
        body: 'The read succeeded and returned nothing.',
      }}
    >
      {(d) => (
        <WithTenantPools roster={d}>
          {(pools) => (
            <section className="section">
              {/* `is-scroll` (CH-13, design-system.md §7.3), for the same reason
                  as the People table above: eight columns compared down the
                  roster is a data table, so below 900px it scrolls sideways with
                  the tenant column held in view; only records of four columns or
                  fewer stack. It was `is-stacked`, because at 390pt everything
                  from `Enforced` rightwards sat behind a scrollbar this
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
                  wide table instead (`.ten-ident`, styles/admin.css).

                  AND BELOW 560px A RECORD PER TENANT (QA G5-08, 2026-10-07).
                  At 390 the scrolled roster was 1,785px in a 356px box, and
                  its rows were uneven because credential tags wrapped off
                  screen. On a phone it is the `data-label` record every other
                  capacity and admin table now draws (capacityPoll
                  `tableMode`); the held-column scroll stays from 561 to
                  899px. */}
              <div className={`table-wrap ${tableMode(phone)}`}>
                {/* FITTED AT 1440 (#503). `.ten-table` (styles/admin.css) lays the
                    roster out fixed above 900px with these widths, so it is the
                    panel's width and never wider: with the 84+236px nav it ran
                    past the panel edge and clipped Identity's copy buttons. One
                    head row, as admin-help.html's Tenants frame draws it: the two
                    registry values are one Configured column, `max · units`. */}
                <table className="pools ten-table" role="table">
                  <colgroup>
                    {TENANT_COLUMNS.map((c) => (
                      <col key={c} className={`ten-col-${c}`} />
                    ))}
                  </colgroup>
                  <thead role="rowgroup">
                    <tr role="row">
                      <th role="columnheader" scope="col" title="Tenant">Tenant</th>
                      <th role="columnheader" scope="col" title="Status">Status</th>
                      <th role="columnheader" scope="col" title="Kind">Kind</th>
                      <th role="columnheader" scope="col" title="Principal">Principal</th>
                      {/* THE CEILING ADMISSION ACTUALLY APPLIES (AH-12). The two
                          registry values were printed bare, and the figure that
                          binds -- the smaller, which every writer of the tenant
                          pool writes as its hard limit -- was nowhere. It is the
                          column; the two values it comes from sit under
                          `Configured`.

                          THE HEAD IS ITS LABEL AND NOTHING ELSE. The decided help
                          link is under the table, not a `?` in here: a glyph in a
                          `<th>` publishes its HelpNote as part of the column's
                          name, which a screen reader then reads on every cell. */}
                      <th role="columnheader" scope="col" className="n" title="Enforced">
                        Enforced
                      </th>
                      <th role="columnheader" scope="col" className="n" title="Configured">
                        Configured
                      </th>
                      <th role="columnheader" scope="col" title="Credentials">Credentials</th>
                      <th role="columnheader" scope="col" title="Identity">Identity</th>
                    </tr>
                  </thead>
                  <tbody role="rowgroup">
                    {d.tenants.map((t) => (
                      <tr role="row" key={t.tenant_id} className={t.enabled === false ? 'paused' : undefined}>
                        <th role="rowheader" scope="row">{t.tenant_id}</th>
                        <td role="cell" data-label="Status">
                          {/* THE BRAND MARKS (admin-help.html, Tenants): a disabled
                              tenant is the parked mark -- held on purpose, not
                              failed -- and an enabled one is the plain word, since
                              enabled is the normal case and needs no glyph. */}
                          {t.enabled === false ? (
                            <span className="ten-status">
                              <StateMark state="PARKED" label="disabled" />
                            </span>
                          ) : (
                            <span className="ten-status">
                              <NamedMark mark={null} hue="neu" word="enabled" />
                            </span>
                          )}
                        </td>
                        <td role="cell" data-label="Kind">{t.kind}</td>
                        <td role="cell" data-label="Principal" className="mono">
                          <Ident value={t.principal} noun="principal" tenant={t.tenant_id} />
                        </td>
                        <td role="cell" data-label="Enforced" className="n">
                          {/* To the tenant pool's own row on Pool limits, which
                              is where this ceiling is changed (#134). */}
                          <a
                            className="ctl-link"
                            href={`#admin/limits?pool=${encodeURIComponent(`tenant:${t.tenant_id}`)}`}
                            title={`effective limit of tenant:${t.tenant_id} · configured ${t.max_active} · ${t.capacity_units}u`}
                          >
                            <Enforced tenant={t} pools={pools} />
                          </a>
                        </td>
                        <td role="cell" data-label="Configured" className="n">
                          {/* Max active, then capacity units: the two values
                              the pool's hard limit is written from. Each word is on the
                              cell's name, so the short form is never the only
                              way to read it. */}
                          <span
                            title={`max active ${t.max_active} · capacity units ${t.capacity_units}`}
                            aria-label={`max active ${t.max_active}, capacity units ${t.capacity_units}`}
                          >
                            {t.max_active} · {t.capacity_units}u
                          </span>
                        </td>
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
                            // AMBER, NOT RED: a tenant with no key of its own can
                            // still run on an account lent to it (Capacity ›
                            // Accounts), so this is a warning and not a failure.
                            <WarnMark label="none registered" />
                          )}
                        </td>
                        <td role="cell" data-label="Identity" className="mono">
                          {/* null means NO IDENTITY, not an empty string. A blank
                              cell here reads as fine and it is the opposite. */}
                          {typeof t.service_account === 'string' ? (
                            <Ident value={t.service_account} noun="service account" tenant={t.tenant_id} />
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
        </WithTenantPools>
      )}
    </Screen>
  )
}

/**
 * AN IDENTITY, SHORTENED ON THE WIDE TABLE, AND ITS COPY (#134).
 *
 * Above 900px `.ten-ident` cuts the value to its column with an ellipsis
 * (AH-11, #503), and the copy control sits outside it so it is never cut.
 * The text under the ellipsis is whole, but selecting a cut 60-character
 * address out of a table cell is not a copy anyone can rely on -- so a button
 * beside it copies all of it, as the header's tenant id does (Brand.tsx
 * `TenantId`). Beside the value and not the value itself: here the value is
 * not the only thing in its cell a reader might want to select, and a whole
 * cell that copies on a click would fight the selection.
 *
 * SAID, NOT SILENT. The status says whether the copy landed; `navigator.
 * clipboard` is undefined outside a secure context and a write can be denied,
 * and a control that claimed success either way would be lying.
 */
/** How long a copy's outcome stays beside the button that asked for it. */
const COPY_SAID_MS = 4000

/**
 * AN IDENTITY IN THREE PIECES, SO THE CUT FALLS IN THE MIDDLE (G5-12, QA
 * 2026-10-07). Cut at its end, every Identity read "swarm-agent-work…" and
 * every Principal "…@saga.x…": the part that tells one tenant's identity from
 * the next is the part an end-ellipsis removes. The KEY is the tenant's own
 * part of the local part -- the tenant id where the local part ends in it,
 * the whole local part when it is short, otherwise its last 12 characters --
 * with its `@`; it never shrinks. The shared prefix before it and the domain
 * after it are each cut with their own ellipsis (`.ten-ident-head`,
 * `.ten-ident-tail`, styles/admin.css). The three pieces are one text, so a
 * selection and the title are still the whole value, and the copy control is
 * unchanged.
 */
export function identParts(value: string, tenant: string): { head: string; key: string; tail: string } {
  const at = value.lastIndexOf('@')
  const local = at === -1 ? value : value.slice(0, at)
  const tail = at === -1 ? '' : value.slice(at + 1)
  const own =
    tenant !== '' && (local === tenant || local.endsWith(`-${tenant}`))
      ? tenant
      : local.length <= SHORT_LOCAL
        ? local
        : local.slice(-OWN_TAIL)
  return { head: local.slice(0, local.length - own.length), key: at === -1 ? own : `${own}@`, tail }
}

/** A local part this short is shown whole: it is all key. */
const SHORT_LOCAL = 20
/** How much of a long local part that does not end in the tenant id is kept. */
const OWN_TAIL = 12

function Ident({ value, noun, tenant }: { value: string; noun: string; tenant: string }) {
  const parts = identParts(value, tenant)
  const [said, setSaid] = useState('')
  const refused = `copy refused; select the ${noun} instead`
  // SAID, THEN GONE. The outcome answers the click that asked; left in the
  // cell for the life of the screen, a roster copied from three times carried
  // three stale `copied` notes in its status regions.
  useEffect(() => {
    if (said === '') return
    const t = setTimeout(() => setSaid(''), COPY_SAID_MS)
    return () => clearTimeout(t)
  }, [said])
  const copy = () => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clipboard === undefined) {
      setSaid(refused)
      return
    }
    clipboard.writeText(value).then(
      () => setSaid(`${noun} copied`),
      () => setSaid(refused),
    )
  }
  return (
    <span className="ten-ident-row">
      <span className="ten-ident" title={value}>
        {parts.head !== '' && <span className="ten-ident-head">{parts.head}</span>}
        <span className="ten-ident-key">{parts.key}</span>
        {parts.tail !== '' && <span className="ten-ident-tail">{parts.tail}</span>}
      </span>
      <button type="button" className="ten-copy" aria-label={`Copy ${noun} ${value}`} title={`Copy the whole ${noun}`} onClick={copy}>
        copy
      </button>
      <span className="ten-copy-said" role="status">
        {said}
      </span>
    </span>
  )
}

/** The roster's columns, in order: the `col` classes styles/admin.css sizes. */
const TENANT_COLUMNS = ['tenant', 'status', 'kind', 'principal', 'enforced', 'configured', 'credentials', 'identity'] as const

/** Where the Enforced column, and the budget the table leaves out, are explained. */
const TENANT_HELP: TopicId = 'tenant-fields'

/** The Tenants table's help link (AH-12), as a footer index. */
const TENANT_TOPICS: readonly TopicId[] = [TENANT_HELP]

/**
 * THE CEILING ADMISSION APPLIES TO A TENANT (AH-12, G5-13): the tenant pool's
 * `effective_limit`, read, never derived here. Every writer of that pool sets
 * its hard limit from the two configured values (`set_tenant_limits` and
 * `ensure_tenant` in swarm_api/store.py, scripts/register-tenant.sh,
 * terraform/infra/locals.tf `pool_tenants`), and the pool's effective limit
 * is that hard limit with any adaptive or quota cap applied -- so the pool,
 * not this file, is where the rule lives.
 *
 * Four ways to have no figure, each a dash with its reason: the capacity read
 * has not landed, it failed, the tenant has no pool, or the pool has no limit
 * set (#374).
 */
function Enforced({ tenant: t, pools }: { tenant: Tenant; pools: TenantPools }) {
  const name = `tenant:${t.tenant_id}`
  if (pools === 'reading') return <i className="ctl-em" title="reading /v1/capacity">—</i>
  if (pools === 'unread') return <i className="ctl-em" title="/v1/capacity was not read">—</i>
  const pool = pools[name]
  if (pool === undefined) return <i className="ctl-em" title={`no ${name} pool in /v1/capacity`}>—</i>
  if (pool.effective_limit === null) return <i className="ctl-em" title={`${name} has no limit set`}>—</i>
  return <>{pool.effective_limit}</>
}
