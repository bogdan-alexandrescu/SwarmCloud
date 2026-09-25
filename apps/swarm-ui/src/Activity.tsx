import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
} from 'react'
import { loadTaskWindow, loadTenants } from './api'
import { helpAnchor, type TopicId } from './help'
import { HelpLinks } from './HelpCard'
import { sumReported, usd } from './measure'
import { Mark, Metric } from './primitives'
import { Id, Screen, timeAgo } from './Shell'
import {
  TERMINAL_STATES,
  bucketStart,
  pluralise,
  usageOf,
  type Bucket,
  type Task,
  type TaskWindow,
} from './types'

const BUCKETS: Bucket[] = ['hour', 'day', 'week', 'month']
const BUDGETS = [200, 500, 1000, 2000]

/**
 * Where the sentences that used to be on this screen now live.
 *
 * Typed as `TopicId` rather than spelled into an href, so a renamed topic is a
 * compile error here instead of a `?` that lands on the top of the Help page
 * and answers nothing -- the same rule `Dock.tsx` states for its own link.
 */
const WINDOW_HELP: TopicId = 'partial-read'
const SPEND_HELP: TopicId = 'tokens-reported'
const SCOPE_HELP: TopicId = 'tenant-scope'
const ABSENCE_HELP: TopicId = 'absent-vs-zero'

/** `#help/<topic>`, as an href. */
function helpHref(topic: TopicId): string {
  return `#${helpAnchor(topic)}`
}

/**
 * Screen A1 -- the Timeline pane of Work (it was History's until the nav
 * collapsed to three sections; the route is `#work/timeline`).
 *
 * THE ONE DESIGN RULE: bound by ROWS, label by the SPAN those rows covered.
 * The window header says "Last 500 tasks" -- or "All 170 tasks" when nothing
 * older exists -- and never "Last 7 days": the latter
 * becomes a lie the moment the window truncates, which is exactly the class
 * of lie this platform keeps shipping. The header then reports the span the
 * rows turned out to cover, which may be six hours or three months.
 *
 * The bucket control GROUPS, it does not filter. Selecting Day re-buckets
 * rows already fetched; it cannot fetch a different range, because list_tasks
 * applies exactly one inequality and it comes from the page token. The label
 * says "Group by" so nobody files a bug against a control doing what it says.
 */
export function ActivityScreen() {
  const [budget, setBudget] = useState(500)
  const [bucket, setBucket] = useState<Bucket>('day')
  const load = useCallback(() => loadTaskWindow(budget), [budget])

  return (
    <Screen
      // "Timeline", not "Activity". "Activity" was this screen's own name when
      // it was a top-level nav item, and the redesign retired it there for
      // being a paraphrase of a question rather than a name for a thing; it
      // then survived as the heading, so clicking through to this pane landed
      // on a page headed with the name of a section that no longer existed.
      // What this renders IS a timeline: rows bucketed by hour, day, week or
      // month across the span they turned out to cover.
      //
      // THE SECTION ABOVE IT HAS NOW CHANGED TWICE AND THE HEADING HAS NOT,
      // which is the point of the rule: Activity became History > Timeline,
      // and History became Work > Timeline when the nav collapsed to three
      // sections. The pane is the same route, the same read and the same
      // heading through both; `#activity/timeline` and `#history/timeline`
      // both still open it (App.tsx, SECTION_ALIASES).
      title="Timeline"
      load={load}
      summary={(w) => <WindowSummary window={w} />}
      // ONE SENTENCE, and it is the one that distinguishes this from a failed
      // read -- which is the distinction the whole product is built on. The
      // second sentence restated it and is gone.
      empty={{
        heading: 'No tasks in this tenant',
        body: 'The read succeeded and returned nothing.',
      }}
    >
      {(w) => (
        <>
          <WindowBar
            window={w}
            budget={budget}
            setBudget={setBudget}
            bucket={bucket}
            setBucket={setBucket}
          />
          <Chart window={w} bucket={bucket} />
          <Figures window={w} />
          <RunnerSplit window={w} />
          <People window={w} />
        </>
      )}
    </Screen>
  )
}

function WindowSummary({ window: w }: { window: TaskWindow }) {
  return (
    <>
      {w.tasks.length} tasks{w.moreExist && ' · older tasks exist'}
    </>
  )
}

/** The span these rows actually covered, in words. */
function spanOf(w: TaskWindow): string {
  if (!w.from || !w.to) return 'no span'
  const a = new Date(w.from)
  const b = new Date(w.to)
  const ms = b.getTime() - a.getTime()
  if (!Number.isFinite(ms)) return 'no span'
  const h = Math.round(ms / 3_600_000)
  const dur = h < 48 ? `${h}h` : `${Math.floor(h / 24)}d ${h % 24}h`
  const fmt = (d: Date) =>
    d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  return `${fmt(a)} → ${fmt(b)} (${dur})`
}

function WindowBar({
  window: w,
  budget,
  setBudget,
  bucket,
  setBucket,
}: {
  window: TaskWindow
  budget: number
  setBudget: (n: number) => void
  bucket: Bucket
  setBucket: (b: Bucket) => void
}) {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
  const spanDays =
    w.from && w.to ? (new Date(w.to).getTime() - new Date(w.from).getTime()) / 86_400_000 : 0

  return (
    <div className="window-bar">
      <div className="wb-span">
        {/* THE ROWS READ, NOT THE ROWS ASKED FOR. This printed the Rows
            control's budget -- `Last 500 tasks` -- over a window that held all
            170 of the tenant's tasks, which is a claim of truncation where
            there was none. When nothing older exists the window IS everything,
            and says so; when the read stopped short it is the newest N, and N
            is what was read. */}
        <strong>{`${w.moreExist ? 'Last' : 'All'} ${pluralise(w.tasks.length, 'task')}`}</strong>
        <span className="wb-detail">
          {spanOf(w)} · {zone}
        </span>
      </div>
      <div className="wb-controls">
        <label>
          Group by
          <select value={bucket} onChange={(e) => setBucket(e.target.value as Bucket)}>
            {BUCKETS.map((b) => (
              // Month over a span under 60 days renders one lonely column and
              // reads as "we only have one month of data". Disabled with the
              // reason, rather than drawn.
              <option key={b} value={b} disabled={b === 'month' && spanDays < 60}>
                {b}
                {b === 'month' && spanDays < 60 ? ' (span too short)' : ''}
              </option>
            ))}
          </select>
        </label>
        <label>
          Rows
          <select value={budget} onChange={(e) => setBudget(Number(e.target.value))}>
            {BUDGETS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>
      {/* A PARTIAL TOTAL IS NOT A TOTAL, and it is now drawn rather than
          narrated. `.ctl-mark.is-partial` is dashed on one edge only -- the
          side the missing part would have been on -- and the qualifier beside
          it names the window everything below is computed over. The 20-word
          sentence that said the same thing is `#help/partial-read`, and the
          full claim is the mark's accessible name, so a screen reader gets it
          at the mark instead of two lines away from it. */}
      {w.moreExist && (
        <p
          className="client-side wb-more"
          aria-label={`Older tasks exist beyond this window. Everything below describes these ${w.tasks.length} rows and the span above, not all time.`}
        >
          <i className="ctl-mark is-partial">partial</i>
          <span className="wb-more-note">
            these {w.tasks.length} rows only · older tasks exist
          </span>
          <a href={helpHref(WINDOW_HELP)}>Why &rarr;</a>
        </p>
      )}
    </div>
  )
}

/**
 * The start of the bucket after `ms`, in the viewer's zone -- `bucketStart`'s
 * other half, stepped with the local calendar's own setters so a DST change
 * moves the boundary with the clock rather than an hour off it.
 */
export function nextBucket(ms: number, bucket: Bucket): number {
  const d = new Date(ms)
  if (bucket === 'hour') d.setHours(d.getHours() + 1)
  else if (bucket === 'day') d.setDate(d.getDate() + 1)
  else if (bucket === 'week') d.setDate(d.getDate() + 7)
  else d.setMonth(d.getMonth() + 1)
  const next = bucketStart(d.toISOString(), bucket)
  // Strictly forward, whatever a zone transition does to the floor: a step
  // that landed back on `ms` would never terminate the walk below.
  return next !== null && next > ms ? next : d.getTime()
}

/** One column's counts. Every field is a count of rows, so zero is measured. */
interface Outcomes {
  succeeded: number
  failed: number
  cancelled: number
  open: number
}

/**
 * Every bucket from the first one that holds a row to the last, empty ones
 * included, in order.
 *
 * THE X-AXIS IS TIME, SO EVERY BUCKET IS DRAWN. Only buckets that held a task
 * used to get a column, so a four-day window grouped by hour drew nineteen
 * columns packed side by side: an idle night and the next busy hour sat
 * shoulder to shoulder, and the chart claimed a steady stream where there had
 * been a gap. An hour with nothing in it is a MEASURED zero -- every row in the
 * window was read and none landed there -- and it gets a column like any other.
 *
 * AND ONLY WHAT IS DRAWN MAKES A BUCKET. A `submitted` count keyed on
 * `created_at` was built here for an overlay that was never drawn, so a task
 * submitted on Monday and finished on Tuesday opened a Monday column with no
 * bar in it. The series that are drawn are the three outcomes (by
 * `completed_at`) and `still open` (by `created_at`, the only time an open task
 * has), and those are what decide the span.
 */
export function outcomeBuckets(
  tasks: readonly Task[],
  bucket: Bucket,
): { buckets: Array<[number, Outcomes]>; anomalies: number } {
  const map = new Map<number, Outcomes>()
  let anomalies = 0
  const cell = (k: number) => {
    let c = map.get(k)
    if (!c) {
      c = { succeeded: 0, failed: 0, cancelled: 0, open: 0 }
      map.set(k, c)
    }
    return c
  }

  for (const t of tasks) {
    const sub = bucketStart(t.created_at, bucket)
    const terminal = TERMINAL_STATES.has(t.state)
    const at = t.completed_at ? bucketStart(t.completed_at, bucket) : null

    if (terminal && at === null) {
      // A terminal task with no completed_at. Not dropped.
      anomalies++
      if (sub !== null) cell(sub).open++
      continue
    }
    if (!terminal) {
      if (sub !== null) cell(sub).open++
      continue
    }
    const c = cell(at as number)
    if (t.state === 'SUCCEEDED') c.succeeded++
    else if (t.state === 'FAILED' || t.state === 'DEAD_LETTERED') c.failed++
    else c.cancelled++
  }

  const keys = Array.from(map.keys()).sort((a, b) => a - b)
  const first = keys[0]
  const last = keys[keys.length - 1]
  const buckets: Array<[number, Outcomes]> = []
  if (first !== undefined && last !== undefined) {
    for (let k = first; k <= last; k = nextBucket(k, bucket)) {
      buckets.push([k, map.get(k) ?? { succeeded: 0, failed: 0, cancelled: 0, open: 0 }])
    }
  }
  return { buckets, anomalies }
}

/**
 * Up to this many columns, `axisLabels` labels every column. At 390 the chart
 * is about 330px across, so six columns sit at least 55px apart -- room for the widest
 * label (`Sep 21`, six characters of --t-micro mono, ~43px). Past it a column
 * can be at its 26px floor, 29px apart with the gap, and two labels side by
 * side run into each other (`01 PM02 PM03 PM`, which is what 390 already drew
 * before every empty hour got a column too). So past it, every other column.
 * Every column keeps its full time in its accessible name either way, and the
 * legend prints it when the column is picked (TS-9). A phone's HOURLY axis
 * does not use this: it thins at every length (`PHONE_HOUR_STRIDE`). A phone's
 * day, week and month axes do.
 */
const LABEL_ALL_UP_TO = 6

/**
 * AT PHONE WIDTH, AN HOURLY AXIS LABELS EVERY THIRD HOUR, WHATEVER ITS LENGTH
 * (TS-3, owner decision 2026-09-25: "labels thin to every 3rd hour at phone
 * width").
 *
 * Every other column is still too dense at 390: a 29px column pitch puts an
 * `01 PM` (~43px of --t-micro mono) against its neighbour's, which is what the
 * QA pass shot. Three columns is ~87px, room for the widest label and a gap.
 * The stride is ON THE CLOCK -- 00, 03, 06 ... -- rather than every third
 * column from wherever the axis starts, so the ticks read as a scale and not as
 * an accident of the window's first row.
 *
 * NO LENGTH EXCEPTION. A first cut kept every label on an hourly axis of six
 * columns or fewer, because those fit at 390 -- which is a different rule from
 * the one decided, and an axis whose tick spacing changes with the window's
 * length reads as two scales. Nothing is lost by thinning a short one: each
 * column names its full hour, and the legend prints it on a pick (TS-9).
 */
const PHONE_HOUR_STRIDE = 3

const dateLabel = (ms: number) =>
  new Date(ms).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })

/**
 * Which columns of an HOURLY axis are the first of their day, in the viewer's
 * zone: the chart's first column, and every one whose date differs from the
 * column before it. Every other grouping is a day or longer, so no column of it
 * starts a day partway through the axis.
 */
export function dayStarts(keys: readonly number[], bucket: Bucket): boolean[] {
  const dayOf = (ms: number) => new Date(ms).toDateString()
  return keys.map((k, i) => {
    if (bucket !== 'hour') return false
    const before = keys[i - 1]
    return before === undefined || dayOf(before) !== dayOf(k)
  })
}

/**
 * The label under each column, or `''` for a column that carries none.
 *
 * AN HOUR IS NOT A TIME WITHOUT ITS DAY. `08 PM` appeared three times across a
 * four-day window with nothing saying which evening each was. So in hour
 * buckets the first column of every day carries the DATE instead of its hour
 * -- when the columns are contiguous that column is midnight, so nothing is
 * lost -- and it is always labelled, whatever the thinning below would have
 * done. The column before a day's first is left bare when thinning, so the date
 * never has a neighbour's label pressed against it.
 */
export function axisLabels(keys: readonly number[], bucket: Bucket): string[] {
  const stride = bucket === 'month' || keys.length <= LABEL_ALL_UP_TO ? 1 : 2
  const starts = dayStarts(keys, bucket)
  const startsDay = (i: number): boolean => starts[i] === true
  const out = keys.map(() => '')
  let labelled = -Infinity
  keys.forEach((k, i) => {
    if (startsDay(i)) {
      // The chart's first column counts as a day's first -- unless the very
      // next column starts the next day, when two dates would sit side by
      // side. The later one is the boundary, so it is the one that is kept.
      if (i === 0 && stride > 1 && startsDay(1)) return
      out[i] = dateLabel(k)
      labelled = i
      return
    }
    if (i - labelled < stride) return
    if (stride > 1 && startsDay(i + 1)) return
    out[i] = labelFor(k, bucket)
    labelled = i
  })
  return out
}

/**
 * The labels a PHONE draws under an hourly axis: every day, and every third
 * hour on the clock between them (see `PHONE_HOUR_STRIDE`).
 *
 * The same two rules `axisLabels` keeps, at the wider stride: a day's first
 * column always carries its date, and no label lands within a stride of the
 * next day's date, so a date never has an hour pressed against it. An axis
 * that is not hourly draws exactly what a wide screen draws: the decision is
 * about hours, and a day, week or month label is a date, not a clock tick.
 *
 * The chart renders the UNION of this and `axisLabels`, and the sheet hides
 * each set's extras at the other width (`.col-label.is-wide-only` /
 * `.is-phone-only`), so the choice is made by the same breakpoint as every
 * other phone rule rather than by a second copy of it in script.
 */
export function phoneAxisLabels(keys: readonly number[], bucket: Bucket): string[] {
  if (bucket !== 'hour') return axisLabels(keys, bucket)
  const starts = dayStarts(keys, bucket)
  const startsDay = (i: number): boolean => starts[i] === true
  const dayWithin = (i: number): boolean => {
    for (let j = 1; j < PHONE_HOUR_STRIDE; j++) if (startsDay(i + j)) return true
    return false
  }
  const out = keys.map(() => '')
  let labelled = -Infinity
  keys.forEach((k, i) => {
    if (startsDay(i)) {
      // The chart's first column is a day's first only because the chart
      // starts there; when the real boundary is within a stride, it wins.
      if (i === 0 && dayWithin(0)) return
      out[i] = dateLabel(k)
      labelled = i
      return
    }
    if (new Date(k).getHours() % PHONE_HOUR_STRIDE !== 0) return
    if (i - labelled < PHONE_HOUR_STRIDE) return
    if (dayWithin(i)) return
    out[i] = labelFor(k, bucket)
    labelled = i
  })
  return out
}

/**
 * A bucket's FULL name, for the readout and the column's accessible name
 * (TS-9): the date and hour of an hour, the date of a day, `week of` its
 * Monday, and the month with its year. Never the axis's thinned label -- a
 * picked column has to say exactly which bucket it is.
 */
export function bucketName(ms: number, bucket: Bucket): string {
  const d = new Date(ms)
  if (bucket === 'hour') return `${dateLabel(ms)} ${d.toLocaleTimeString(undefined, { hour: '2-digit' })}`
  if (bucket === 'day') return dateLabel(ms)
  if (bucket === 'week') return `week of ${dateLabel(ms)}`
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
}

/** One column's counts as a sentence: the accessible name of its bar. */
const countsSaid = (c: Outcomes): string =>
  `${c.succeeded} succeeded, ${c.failed} failed, ${c.cancelled} cancelled, ${c.open} still open`

/**
 * The window's totals, SUMMED FROM THE COLUMNS THE CHART DRAWS -- never from
 * `w.tasks` -- so the readout and the bars cannot disagree (TS-9). A terminal
 * row with no `completed_at` is in a column's `open`, so it is in this `open`.
 */
export function windowTotals(buckets: ReadonlyArray<readonly [number, Outcomes]>): Outcomes {
  const t: Outcomes = { succeeded: 0, failed: 0, cancelled: 0, open: 0 }
  for (const [, c] of buckets) {
    t.succeeded += c.succeeded
    t.failed += c.failed
    t.cancelled += c.cancelled
    t.open += c.open
  }
  return t
}

/**
 * Stacked columns, one per bucket, every bucket from the first to the last.
 *
 * The three outcomes bucket on `completed_at`; `still open` buckets on
 * `created_at`, because an open task has no other time. The legend says which
 * series is on which basis.
 *
 * `completed_at` is safe to bucket on because EVERY writer that moves a task
 * terminal sets it -- worker, API cancel, scheduler cancel, reconciler
 * reclaim. So a terminal row with a null completed_at is a data bug, and it
 * is counted in "still open" AND surfaced, rather than dropped.
 *
 * THE LEGEND IS THE READOUT (TS-9, owner decision 2026-09-25). A bar's values
 * lived only in a hover `title=`, which a phone cannot open and a keyboard
 * cannot reach (§8.3), and the per-series counts appeared nowhere. Now each
 * legend entry carries its count -- the window's by default, one column's
 * while a column is picked -- and each column is an image NAMED with its full
 * time and its four counts. Hover, tap and focus all pick; `all`, Escape and
 * leaving the chart put the window back. The legend is deliberately NOT a live
 * region: the focused column's name already says the same counts, and a live
 * legend would read them twice on every arrow press and chatter on hover.
 *
 * ONE TAB STOP, WITH A ROVING TABINDEX. Forty columns are not forty stops on
 * the way to the next control; the arrows move between them, Home and End jump
 * to either end, and the stop starts on the newest column because that is
 * where the chart opens (TS-3).
 *
 * "LEAVING THE CHART" MEANS LEAVING THE CHART AND ITS READOUT. `all` sits in
 * the legend, below the columns, so every real way of reaching it -- the
 * pointer moving down to it, a Tab from the picked column, a tap -- leaves
 * `.chart` first. With leave and focus-out on `.chart` itself, each of those
 * cleared the pick, and clearing the pick unmounts `all`: the button could not
 * be pressed by anything but a test, and a Tab onto it dropped focus to
 * <body>. So both are on `.chart-readout`, which holds the columns and the
 * legend that reads them out. Escape is on it too, so it works on `all` as
 * well as on a column.
 */
function Chart({ window: w, bucket }: { window: TaskWindow; bucket: Bucket }) {
  const { buckets, anomalies } = useMemo(() => outcomeBuckets(w.tasks, bucket), [w.tasks, bucket])
  const keys = useMemo(() => buckets.map(([k]) => k), [buckets])
  const labels = useMemo(() => axisLabels(keys, bucket), [keys, bucket])
  const phoneLabels = useMemo(() => phoneAxisLabels(keys, bucket), [keys, bucket])
  // THE DAY BOUNDARY, DRAWN as well as labelled: a rule down the first column
  // of each day after the first, so a four-day hourly axis reads as four days
  // even where thinning left a label out.
  const boundaries = useMemo(() => dayStarts(keys, bucket), [keys, bucket])
  const totals = useMemo(() => windowTotals(buckets), [buckets])

  // Both held BY BUCKET START, not by index: regrouping or a re-read redraws
  // the columns, and an index would then silently point at a different bucket.
  // A start no longer drawn reads as "nothing picked" and "the newest column".
  const [picked, setPicked] = useState<number | null>(null)
  const [stop, setStop] = useState<number | null>(null)
  const pickedAt = picked === null ? -1 : keys.indexOf(picked)
  const stopAt = stop !== null && keys.includes(stop) ? keys.indexOf(stop) : keys.length - 1
  const shown = pickedAt >= 0 ? buckets[pickedAt]![1] : totals
  const clear = () => setPicked(null)

  // TS-3: OPEN AT THE NEWEST END. The scroller used to open at its oldest
  // column, so at 390 the six newest hours -- the ones a reader came for --
  // were off the right edge with nothing to say so. `scrollWidth` is past the
  // end, which a browser clamps to the end. The left edge then carries a fade
  // for exactly as long as something older is off-screen (`has-older`).
  // Keyed on the AXIS -- its grouping and its two ends -- not on the rows'
  // identity, so a refresh that read the same window leaves a reader who had
  // scrolled back where they were, and one that drew a newer bucket shows it.
  const scroller = useRef<HTMLDivElement>(null)
  const cols = useRef<Array<HTMLDivElement | null>>([])
  const [older, setOlder] = useState(false)
  const axis = `${bucket}:${keys[0] ?? ''}:${keys[keys.length - 1] ?? ''}`
  useLayoutEffect(() => {
    const el = scroller.current
    if (el === null) return
    el.scrollLeft = el.scrollWidth
    setOlder(el.scrollLeft > 0)
  }, [axis])

  // `all` AND ESCAPE PUT THE WINDOW BACK, AND FOCUS STAYS IN THE CHART. `all`
  // exists only while a column is picked, so pressing it unmounts it -- and a
  // button that goes while it holds focus drops focus to <body> (WCAG 2.4.3).
  // When it held focus, focus goes back to the column the chart's one tab stop
  // is on, WITHOUT picking it: the same state Escape on a column leaves, the
  // column focused and the legend reading the window. `quiet` is how the
  // column's focus handler tells this apart from a reader landing on it, and
  // `preventScroll` keeps the scroller where the reader left it.
  const allButton = useRef<HTMLButtonElement>(null)
  const quiet = useRef(false)
  const restore = () => {
    const hadFocus = allButton.current !== null && allButton.current === document.activeElement
    clear()
    if (!hadFocus) return
    quiet.current = true
    try {
      cols.current[stopAt]?.focus({ preventScroll: true })
    } finally {
      quiet.current = false
    }
  }

  // On the readout -- the columns and the legend -- so it works on `all` too.
  const onReadoutKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') restore()
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const last = keys.length - 1
    const next =
      e.key === 'ArrowRight'
        ? Math.min(last, stopAt + 1)
        : e.key === 'ArrowLeft'
          ? Math.max(0, stopAt - 1)
          : e.key === 'Home'
            ? 0
            : e.key === 'End'
              ? last
              : null
    // Tab is never handled here: the chart is one stop, and Tab leaves it.
    if (next === null || last < 0) return
    e.preventDefault()
    cols.current[next]?.focus()
  }
  // Focus leaving the chart AND ITS READOUT is leaving it, the same as the
  // pointer leaving them; moving from a column to `all` is not.
  const onBlur = (e: FocusEvent<HTMLDivElement>) => {
    const to = e.relatedTarget
    if (!(to instanceof Node) || !e.currentTarget.contains(to)) clear()
  }

  const max = Math.max(1, ...buckets.map(([, c]) => c.succeeded + c.failed + c.cancelled + c.open))

  return (
    <section className="section">
      <h2>Outcomes by {bucket}</h2>
      {/* THE QUALIFIER, where 28 words of warning paragraph used to be. The
          fact -- these rows carry no `completed_at` and are counted as open
          rather than dropped -- is a fact about this chart, so it sits on the
          chart's own header line and cannot drift away from it. WHY it is a
          data bug (every writer that moves a task terminal sets the field) is
          the mark's accessible name and one click away. */}
      {anomalies > 0 && (
        <p
          className="ctl-panel-note"
          aria-label={`${anomalies} terminal tasks carry no completed_at. Every writer that moves a task terminal sets it, so this is a data bug; they are counted as still open rather than dropped.`}
        >
          <i className="ctl-mark is-unread">not read</i> {anomalies} with no{' '}
          <code>completed_at</code>, counted as open
          <a href={helpHref(ABSENCE_HELP)}>Why &rarr;</a>
        </p>
      )}
      {/* THE CHART AND ITS READOUT ARE ONE THING TO LEAVE (TS-9). `all` is in
          the legend, so the pointer, a Tab and a tap all leave `.chart` on
          the way to it; only leaving this clears the pick. */}
      <div className="chart-readout" onMouseLeave={restore} onBlur={onBlur} onKeyDown={onReadoutKeyDown}>
        <div
          ref={scroller}
          className={older ? 'chart has-older' : 'chart'}
          role="group"
          aria-label={`Task outcomes by ${bucket}`}
          onKeyDown={onKeyDown}
          onScroll={(e) => setOlder(e.currentTarget.scrollLeft > 0)}
        >
          {buckets.map(([k, c], i) => {
            const total = c.succeeded + c.failed + c.cancelled + c.open
            // The UNION of the wide and the phone labels; the sheet hides each
            // set's extras at the other width (TS-3).
            const wide = labels[i] ?? ''
            const phone = phoneLabels[i] ?? ''
            const labelClass =
              wide !== '' && phone === ''
                ? 'col-label is-wide-only'
                : wide === '' && phone !== ''
                  ? 'col-label is-phone-only'
                  : 'col-label'
            const cls = ['col']
            if (i > 0 && boundaries[i] === true) cls.push('is-day-start')
            if (i === pickedAt) cls.push('is-picked')
            return (
              // An empty bucket is a column with four zero-height segments whose
              // name says `0 succeeded, 0 failed, ...`: a measured zero, drawn
              // where it happened.
              <div
                ref={(el) => {
                  cols.current[i] = el
                }}
                className={cls.join(' ')}
                key={k}
                role="img"
                aria-label={`${bucketName(k, bucket)}: ${countsSaid(c)}`}
                tabIndex={i === stopAt ? 0 : -1}
                data-total={total}
                onMouseEnter={() => setPicked(k)}
                // A TAP PICKS TOO: a phone has no hover, and not every phone
                // moves focus to what was tapped.
                onClick={() => setPicked(k)}
                onFocus={(e) => {
                  setStop(k)
                  // Focus handed back by `restore` lands here without picking,
                  // and without moving the scroller.
                  if (quiet.current) return
                  setPicked(k)
                  // Inside TS-3's scroller: an arrow press onto a column past
                  // the edge brings it into view. jsdom has no scrollIntoView.
                  e.currentTarget.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
                }}
              >
                <div className="stackcol">
                  <i className="open" style={{ height: `${(c.open / max) * 100}%` }} />
                  <i className="cancelled" style={{ height: `${(c.cancelled / max) * 100}%` }} />
                  <i className="failed" style={{ height: `${(c.failed / max) * 100}%` }} />
                  <i className="succeeded" style={{ height: `${(c.succeeded / max) * 100}%` }} />
                </div>
                {/* A no-break space where a label is thinned out, so every
                    column's label line is the same height and the bars stay on
                    one baseline. */}
                <span className={labelClass}>{wide || phone || ' '}</span>
              </div>
            )
          })}
        </div>
        {/* THE LEGEND IS A LEGEND AGAIN, AND EACH BASIS SITS BESIDE ITS SERIES.
            The key used to end in one `by completed_at` after all four swatches,
            which put `still open` under it too -- and an open task has no
            `completed_at`; it is bucketed by `created_at`. Two series, two
            bases, each fused to the swatches it qualifies.
            AND IT IS THE READOUT (TS-9): each series' count beside its swatch,
            the window's until a column is picked, then that column's after its
            full name. Not `aria-live` -- see the component's comment. */}
        <p className="chart-legend">
          {pickedAt >= 0 && <span className="cl-at">{bucketName(keys[pickedAt]!, bucket)} ·</span>}
          {pickedAt >= 0 && ' '}
          <span className="k succeeded" /> succeeded <b className="cl-n">{shown.succeeded}</b>
          <span className="cl-sep"> · </span>
          <span className="k failed" /> failed <b className="cl-n">{shown.failed}</b>
          <span className="cl-sep"> · </span>
          <span className="k cancelled" /> cancelled <b className="cl-n">{shown.cancelled}</b>{' '}
          <span className="cl-basis">
            by <code>completed_at</code>
          </span>
          <span className="cl-sep"> · </span>
          <span className="k open" /> still open <b className="cl-n">{shown.open}</b>{' '}
          <span className="cl-basis">
            by <code>created_at</code>
          </span>
          {pickedAt >= 0 && ' '}
          {pickedAt >= 0 && (
            <button type="button" className="sbf-mini cl-all" ref={allButton} onClick={restore}>
              all
            </button>
          )}
        </p>
      </div>
    </section>
  )
}

function labelFor(ms: number, bucket: Bucket): string {
  const d = new Date(ms)
  if (bucket === 'hour') return d.toLocaleTimeString(undefined, { hour: '2-digit' })
  if (bucket === 'month') return d.toLocaleDateString(undefined, { month: 'short' })
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

/**
 * The figures under the chart, on the shared boxless metric strip (TS-11).
 *
 * THREE, AND NOT "SUBMITTED". The fourth tile printed `w.tasks.length` under a
 * sub-line that repeated the window span word for word -- and the window bar
 * directly above already prints both (`All 170 tasks`, then the span), as does
 * the screen's summary line. A fact drawn twice is one of them waiting to
 * disagree, which is why Overview dropped its attention tile for the same
 * reason. And no healthy figure is boxed (§6.2): the bordered `.tile` was the
 * last per-number box on this screen.
 */
function Figures({ window: w }: { window: TaskWindow }) {
  const rows = w.tasks
  const completed = rows.filter((t) => t.completed_at !== null)
  const succeeded = completed.filter((t) => t.state === 'SUCCEEDED').length
  const attempts = rows.reduce((n, t) => n + t.attempt_count, 0)

  return (
    <div className="ctl-metrics">
      <Metric
        label="Completed"
        value={String(completed.length)}
        sub={
          completed.length > 0
            ? `${Math.round((succeeded / completed.length) * 100)}% succeeded`
            : 'none completed in window'
        }
      />
      {/* A FIGURE'S `sub` IS A QUALIFIER, NEVER A DEFINITION (§6.2). "a lease
          reclaimed before dispatch counts here" is the definition; "admissions,
          not runs" is the qualifier, and it is the half that changes how the
          figure is read. */}
      <Metric label="Attempts consumed" value={String(attempts)} sub="admissions, not runs" />
      <SpendFigure tasks={rows} />
    </div>
  )
}

/**
 * `total_cost_usd` off one row's result, or null when the result carries no
 * finite cost. A row can carry token counts and no cost, and that row is NOT
 * a cost of zero -- it is a cost nobody reported.
 */
function resultCost(t: Task): number | null {
  const v = usageOf(t)?.['total_cost_usd']
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * Token spend: THE MEASURED SUM, DRAWN AS PARTIAL (TS-12, owner decision
 * 2026-09-25) -- Overview's treatment of its own Token spend (OV-4), with the
 * Workflows table's "from result" source note (WF-5).
 *
 * The tile used to print a ROW COUNT where the figure goes -- `50 of 170`,
 * under "Tokens & spend" -- so no token count or dollar amount appeared
 * anywhere on the screen. Now the figure is the sum over the k of n rows whose
 * result carries a cost (`sumReported`, null until one does), and the foot
 * says what it is a sum of. It is PARTIAL in two ways, and the mark names
 * both: rows whose result carries no cost are not in it, and a result records
 * only its task's LAST attempt, so a task that ran more than once is in it at
 * one attempt's cost.
 *
 * Client-side over the rows already fetched: no extra read. A per-attempt
 * figure would come from `GET /v1/attempts?since=<window start>` -- a few
 * paged reads with server-side coverage -- never a fan-out per task.
 *
 * NEVER $0.00 FOR AN ABSENCE. With no cost in any result the tile still
 * stands, because tokens were asked for and an absent tile answers nothing,
 * and it says `not recorded` beside the absent mark. A MEASURED zero -- a
 * result that reported a cost of 0 -- is `$0.00`, because it was measured.
 */
function SpendFigure({ tasks }: { tasks: readonly Task[] }) {
  const sum = sumReported(tasks, resultCost)
  if (sum === null) {
    // The dashed rule (`tone="absent"`) and the mark are what a reader sees
    // instead of a number; the argument for why is `#help/tokens-reported`.
    return (
      <Metric
        label="Token spend"
        value="not recorded"
        tone="absent"
        explain={SPEND_HELP}
        sub={
          <>
            <Mark
              kind="absent"
              say="Token spend: no task in this window carries a cost in its result. That is an absent measurement, not a spend of zero."
            />{' '}
            <a className="ctl-link" href={helpHref(SPEND_HELP)}>
              Why &rarr;
            </a>
          </>
        }
      />
    )
  }
  const n = tasks.length
  const summed = tasks.filter((t) => resultCost(t) !== null)
  const k = summed.length
  const reran = summed.filter((t) => t.attempt_count > 1).length
  const partial = k < n || reran > 0
  const say =
    `Summed from the results of ${k} of ${n} tasks, so it can fall short in two ways: ` +
    `${n - k} ${n - k === 1 ? 'task carries' : 'tasks carry'} no cost in its result, and a result ` +
    `records only its task’s last attempt` +
    (reran > 0 ? ` — ${reran} of the summed ${reran === 1 ? 'task' : 'tasks'} ran more than once.` : '.')
  return (
    <Metric
      label="Token spend"
      value={usd(sum)}
      foot={`${k} of ${n} tasks · from result`}
      explain={SPEND_HELP}
      sub={partial ? <Mark kind="partial" say={say} /> : undefined}
    />
  )
}

function RunnerSplit({ window: w }: { window: TaskWindow }) {
  const counts = new Map<string, number>()
  for (const t of w.tasks) counts.set(t.runner_profile, (counts.get(t.runner_profile) ?? 0) + 1)
  const entries = Array.from(counts.entries()).sort((a, b) => b[1] - a[1])
  const max = Math.max(1, ...entries.map(([, n]) => n))

  return (
    <section className="section">
      <h2>By runner profile</h2>
      {/* Whatever string arrives, never a hardcoded list: the catalogue is
          frozen contract data and can gain entries. */}
      {entries.map(([name, n]) => (
        <div className="split-row" key={name}>
          <span className="sr-name">{name}</span>
          <span className="sr-bar">
            <i style={{ width: `${(n / max) * 100}%` }} />
          </span>
          <span className="sr-n">{n}</span>
        </div>
      ))}
    </section>
  )
}

/**
 * Screen A2 -- People. Derived from A1's rows at zero extra reads.
 *
 * There is no server-side grouping or filtering by owner: list_tasks accepts
 * only state, workflow_id and runner_profile, and no index covers
 * submitted_by. So this is client-side over the window and says so.
 */
function People({ window: w }: { window: TaskWindow }) {
  const people = useMemo(() => {
    const m = new Map<string, Task[]>()
    for (const t of w.tasks) {
      const who = t.submitted_by ?? 'unattributed'
      const list = m.get(who)
      if (list) list.push(t)
      else m.set(who, [t])
    }
    return Array.from(m.entries()).sort((a, b) => b[1].length - a[1].length)
  }, [w.tasks])

  return (
    <section className="section">
      <h2>People</h2>
      {/* `is-scroll` NOW (CH-13, design-system.md §7.3): six columns compared
          across rows is a data table, so below 900px it scrolls with the
          engineer column held in view; only records of four columns or fewer
          stack. What follows is why it was `is-stacked`, and the problem it
          named is the one the held column answers.
          F6 OF `docs/audits/2026-09-23/overflow-inventory.md`,
          which measured this exact table at 390pt: `clientWidth: 358` against a
          `scrollWidth` of 512, so 30% of it was behind an `overflow-x: auto`
          that paints no scrollbar on this platform. The three columns hiding
          there are `Failed`, `Attempts` and `Last seen` — a per-engineer table
          showing tasks and successes and NOT failures reads as a clean record
          for everyone on it. Below 900px each row becomes a stacked record with
          its own key column (§B6.3 in `styles.css`); `data-label` is what
          supplies that key, as an attribute so the rendered-word budgets are
          unchanged, and the explicit `role`s keep the ARIA table that changing
          `display` would otherwise drop. */}
      <div className="table-wrap is-scroll">
        <table className="pools" role="table">
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">Engineer</th>
              <th role="columnheader" scope="col" className="n">Tasks</th>
              <th role="columnheader" scope="col" className="n">Succeeded</th>
              <th role="columnheader" scope="col" className="n">Failed</th>
              <th role="columnheader" scope="col" className="n">Attempts</th>
              <th role="columnheader" scope="col">Last seen</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {people.map(([who, rows]) => (
              <tr role="row" key={who}>
                <th role="rowheader" scope="row">{who}</th>
                <td role="cell" data-label="Tasks" className="n">{rows.length}</td>
                <td role="cell" data-label="Succeeded" className="n">{rows.filter((t) => t.state === 'SUCCEEDED').length}</td>
                <td role="cell" data-label="Failed" className="n">{rows.filter((t) => t.state === 'FAILED').length}</td>
                <td role="cell" data-label="Attempts" className="n">{rows.reduce((n, t) => n + t.attempt_count, 0)}</td>
                <td role="cell" data-label="Last seen">
                  {timeAgo(
                    rows.map((t) => t.updated_at).sort().slice(-1)[0] ?? rows[0]!.created_at,
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {/* THE PROVENANCE STRIP (§8.4.4): when, over what, from where. One line.
          The 44-word version also explained that there is no server-side index
          on `submitted_by` and that an engineer outside the window is absent
          rather than zero -- the first is an argument and belongs in
          `#help/tenant-scope`, the second is the invariant and is now the
          mark, which is attached to the table instead of sitting under it. */}
      {/* PARTIAL ONLY WHEN IT IS. The mark said `partial` on every visit,
          including over a window that held every task the tenant has -- and a
          mark that is always there stops being read on the day it is true.
          `moreExist` is the one fact that makes this table a subset: older
          tasks were not read, so an engineer whose work is all older is absent
          rather than zero. Without it the table is the whole tenant, still
          grouped client-side, which is what the line then says. */}
      {w.moreExist ? (
        <p
          className="ctl-panel-note"
          aria-label={`Grouped client-side over the ${w.tasks.length} rows in the window. There is no server-side filter or index on submitted_by, so an engineer whose work fell outside the window is absent here rather than shown as zero.`}
        >
          <i className="ctl-mark is-partial">partial</i>
          client-side over {w.tasks.length} rows in the window
          <a href={helpHref(SCOPE_HELP)}>Why &rarr;</a>
        </p>
      ) : (
        <p
          className="ctl-panel-note"
          aria-label={`Grouped client-side over all ${pluralise(w.tasks.length, 'task')} this tenant has. The window holds every one, so nobody's work is missing from this table.`}
        >
          client-side over all {pluralise(w.tasks.length, 'task')}
          <a href={helpHref(SCOPE_HELP)}>Why &rarr;</a>
        </p>
      )}
    </section>
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
