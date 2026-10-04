import {
  createContext,
  Fragment,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import { HIDDEN_LINE_AFTER_MS, HiddenTabLine } from './AppStates'
import { Banner, Button } from './components'
import { chosenTenant, errorHeading, errorReassurance, pageReads, subscribeTenant, subscribeTenantSwitch, tenantSwitchSnapshot, type ApiError, type ApiErrorKind, type Result } from './fetch'
import { type TopicId } from './help'
import { HelpCard } from './HelpCard'
import { Absent, type LinkOut } from './primitives'
import { formatDuration, timeAgo } from './types'
import { AGE_TICK_MS, useNow } from './useNow'

/**
 * How often a screen re-reads: a fixed interval, or one chosen from what was
 * read (`null` data before the first read, and after one that returned
 * nothing). A function answering `null` means "do not poll".
 *
 * Agents is the caller this exists for (AG-1): docs/web-ui/03-agents-and-
 * workflows.md §2.5 asks for 5s while the Live tab holds rows and 30s
 * otherwise, which is a function of the rows.
 */
export type ScreenPoll<T> = number | ((data: T | null) => number | null)

/**
 * What `children` is told about the data it is drawing, beside the data.
 *
 * A screen that ticks its own clock over the rows -- Agents' elapsed column is
 * the case -- needs both: an elapsed figure keeps adding to rows that were read
 * at `fetchedAt`, and past one `pollMs` without a newer read that figure is
 * counting on data nobody has re-read, which is AG-1's "a finished agent reads
 * running".
 */
export interface ScreenReading {
  /** When the data being drawn was read, on this browser's clock. */
  fetchedAt: number
  /** The base cadence this screen re-reads at, or null when it does not poll. */
  pollMs: number | null
}

/**
 * HOW OLD A READ MAY GET BEFORE THE SCREEN STOPS PRESENTING IT AS CURRENT:
 * five minutes, after which the rows are dimmed and the sub-line says
 * `not refreshed` (CH-1).
 *
 * WHY FIVE. It is `MAX_BACKOFF_MS` below, on purpose: a polling screen that has
 * not produced a good read within its longest back-off is not being kept
 * current, whatever its cadence says. A screen that does not poll is read once
 * per visit, and five minutes is well past the interval on which the figures
 * these screens carry routinely move -- task states change in seconds, pool
 * counters with every admission. It is a chosen value, not a measured one;
 * nothing on the platform publishes a freshness budget for a console read.
 */
export const AGED_AFTER_MS = 5 * 60_000

/**
 * The longest a polling screen waits between reads while backing off. The
 * back-off doubles from the screen's own cadence per consecutive failure
 * (5s, 10s, 20s ...), so a 5s screen reaches this after six failures in a row.
 * Five minutes rather than longer because a screen someone is looking at must
 * still recover on its own within a sensible wait once the API is back.
 */
export const MAX_BACKOFF_MS = 5 * 60_000

/**
 * Failures that asking again cannot fix, so a polling screen stops asking.
 * Each needs a person first: an admin group, a sign-in, a permitted domain or
 * an enabled tenant. Polling them would spend the 20 rps per-principal budget
 * to learn the same answer every few seconds.
 */
const NEEDS_A_PERSON: ReadonlySet<ApiErrorKind> = new Set<ApiErrorKind>([
  'admin_required',
  // A refusal the client does not recognise: asking again gets the same 403.
  'forbidden',
  'session_expired',
  'unauthenticated',
  'wrong_domain',
  'tenant_disabled',
])

/** The accessible name of an empty state's mark when the screen gives none. */
const EMPTY_SAY = 'A real zero: the read succeeded and returned nothing.'

/**
 * The wait before the next read, given the base cadence and how many reads in
 * a row have failed. Doubling, capped at `MAX_BACKOFF_MS` -- or at the base
 * itself for a screen that already polls slower than that.
 */
export function nextPollDelay(base: number, failures: number): number {
  if (failures <= 0) return base
  return Math.min(base * 2 ** failures, Math.max(base, MAX_BACKOFF_MS))
}

function cadenceOf<T>(poll: ScreenPoll<T> | undefined, data: T | null): number | null {
  if (poll === undefined) return null
  const ms = typeof poll === 'function' ? poll(data) : poll
  return ms !== null && Number.isFinite(ms) && ms > 0 ? ms : null
}

/** `document.hidden`, false where there is no document (tests/run.mjs). */
function tabHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden === true
}

/**
 * True inside the routed PAGE -- the screen the rail points at -- and false in
 * the agent inspector drawn over it (CH-2). App provides it; `Screen` reads it
 * so that the page's reads stay the page's while an inspector is open over it,
 * and the head can speak for each one truthfully (`pageReads` in fetch.ts).
 */
export const RoutedPage = createContext(false)

/**
 * ONE READ AGE PER SCREEN (#98). True inside the app frame, whose head
 * (`Head` in App.tsx) prints the age of the CURRENT screen's own reads (CH-2)
 * and whose dock prints the tab-wide one. A `Screen` in the frame therefore
 * does not print a third copy of it on its sub-line while the read is fresh:
 * "newest read 4s ago" in the head and "read 4s ago" under the title were one
 * fact said twice, and with two reads behind a screen the two could disagree.
 *
 * WHAT THE SUB-LINE STILL SAYS ABOUT FRESHNESS: everything that is not "this
 * is current". A failed refresh (`not refreshed · showing 4m ago`) and a read
 * older than `AGED_AFTER_MS` (`not refreshed · read 6m ago`) keep their age
 * beside the rows they dim -- a panel states its freshness when it is stale,
 * and only then. Outside the frame (a screen rendered on its own, as the unit
 * tests do) there is no head to carry the age, and the sub-line prints it.
 */
export const FrameAge = createContext(false)

/**
 * A screen that prints an age of its OWN data that the frame head cannot know
 * -- Platform counts, whose figures are as old as the count the server ran,
 * not as old as the newest read the screen made -- claims the age, and the
 * head prints none while it is mounted (#98). Counted, not flagged, so two
 * claims and one release still leave it claimed.
 */
let pageAgeClaims = 0
const pageAgeListeners = new Set<() => void>()

function subscribePageAge(fn: () => void): () => void {
  pageAgeListeners.add(fn)
  return () => {
    pageAgeListeners.delete(fn)
  }
}

function setPageAgeClaims(n: number): void {
  pageAgeClaims = n
  for (const fn of pageAgeListeners) fn()
}

/** Whether a mounted screen prints its own data's age (see `useClaimPageAge`). */
export function usePageAgeClaimed(): boolean {
  return useSyncExternalStore(
    subscribePageAge,
    () => pageAgeClaims > 0,
    () => false,
  )
}

/** Claim the screen's age for this component while it is mounted and `on`. */
export function useClaimPageAge(on: boolean): void {
  useLayoutEffect(() => {
    if (!on) return
    setPageAgeClaims(pageAgeClaims + 1)
    return () => setPageAgeClaims(pageAgeClaims - 1)
  }, [on])
}

/**
 * THE HEAD'S AGE OF THE CURRENT SCREEN'S READS (App.tsx `HeadAgeProvider`),
 * for the page head to draw on its title row (#503). Null outside the frame,
 * and when a screen prints its own data's age (`useClaimPageAge`).
 */
export const HeadAge = createContext<ReactNode>(null)

/**
 * THE SECTION'S `?` (App.tsx `SectionQuestion`), for the page head to draw by
 * its title when the screen has no topic of its own (visual QA Q2,
 * 2026-10-02). It sat alone on a breadcrumb row above the title ("Work ?");
 * the picked head has one row, and its `?` is a small button by the title.
 */
export const SectionHelp = createContext<ReactNode>(null)

let headRowClaims = 0
const headRowListeners = new Set<() => void>()

function subscribeHeadRow(fn: () => void): () => void {
  headRowListeners.add(fn)
  return () => {
    headRowListeners.delete(fn)
  }
}

/** Whether a mounted page head draws the head's age on its own title row. */
export function useHeadRowClaimed(): boolean {
  return useSyncExternalStore(
    subscribeHeadRow,
    () => headRowClaims > 0,
    () => false,
  )
}

function useClaimHeadRow(on: boolean): void {
  useLayoutEffect(() => {
    if (!on) return
    headRowClaims += 1
    for (const fn of headRowListeners) fn()
    return () => {
      headRowClaims -= 1
      for (const fn of headRowListeners) fn()
    }
  }, [on])
}

/**
 * THE TENANT A KEPT FORM WILL NOW SUBMIT AS (intake-tenants.html 2A), or null
 * when nobody switched while it was open. A submit button names it ("Submit
 * as platform"), so an unsent draft carried across a switch cannot be sent
 * to the new tenant by a person who still thinks it is going to the old one.
 */
export function useSubmitAs(): string | null {
  const s = useSyncExternalStore(subscribeTenantSwitch, tenantSwitchSnapshot, tenantSwitchSnapshot)
  return s !== null && s.kept ? s.to.name : null
}

/** What the sub-line says about the cadence. */
interface Cadence {
  /** The screen's own cadence. */
  base: number
  /** The wait actually in force: the cadence, or longer while backing off. */
  wait: number
}

/**
 * Every screen loads through this, so no screen can forget a state.
 *
 * `children` only ever receives DATA. There is no way to reach it with a
 * failure and no way to reach it with zero rows, which is the property the
 * whole app is built on -- a 403 cannot reach a component that renders rows,
 * and neither can an empty list pretending to be one.
 *
 * This component also OWNS the stale rule. A screen's `load` does not have to
 * remember the last good data: if a refresh fails while data is on screen,
 * this converts the failure into `stale` and keeps the rows visible, dimmed,
 * with the age and the error in the header. Putting that in one place is why
 * it cannot be forgotten per-screen.
 *
 * AND THE AGE RULE (CH-1). Every age this renders moves on the shared 5s
 * clock, and a read older than `AGED_AFTER_MS` takes the same dimmed,
 * `not refreshed` treatment as a failed refresh -- the data is no less real,
 * it is simply no longer a description of now.
 *
 * AND POLLING, WHEN ASKED (AG-1). `pollMs` re-reads on a cadence through the
 * same path the refresh button takes, so the stale rule above covers a failed
 * poll for free. It pauses while the tab is hidden and reads at once when the
 * tab comes back, doubles its wait after each failure in a row up to
 * `MAX_BACKOFF_MS`, honours a 429's Retry-After, and stops altogether on an
 * answer only a person can change. The cadence is printed beside the age.
 */
export function Screen<T>({
  title,
  help,
  load,
  summary,
  empty,
  pollMs,
  skeleton,
  children,
}: {
  title: string
  /** The screen's one `?`, after its title, when it explains the whole screen. See `PageHead`. */
  help?: TopicId
  load: () => Promise<Result<T>>
  /** One line under the title once data is in. */
  summary?: (data: T) => ReactNode
  /**
   * Shown when the read SUCCEEDED and returned nothing. Different from failure.
   *
   * Drawn as the shared `.ctl-empty` (§6.9): the `real zero` mark, `heading`,
   * `body` as its one sentence, and `link` as the way out. `say` is the mark's
   * accessible name, when the screen has a better sentence than the default.
   */
  empty?: { heading: string; body: ReactNode; link?: LinkOut; say?: string }
  /** Re-read on this cadence. Omitted, the screen reads once per mount. */
  pollMs?: ScreenPoll<T>
  /**
   * What the first read draws while it is in flight, in place of the generic
   * `SkeletonRows`: a screen whose layout is known before its data (the
   * Workflows list's toolbar and table head, #113) draws that layout, so
   * nothing moves when the data lands. Omitted, the generic rows.
   */
  skeleton?: ReactNode
  children: (data: T, reading: ScreenReading) => ReactNode
}) {
  const [state, setState] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)
  /** The last read that produced rows. Survives a failed refresh on purpose. */
  const lastGood = useRef<{ data: T; fetchedAt: number } | null>(null)
  /** Set by a 429 so the retry control can say how long, rather than lying. */
  const [pausedUntil, setPausedUntil] = useState<number | null>(null)
  const now = useNow(AGE_TICK_MS)
  /** Whether this screen is the page, rather than the inspector over it. */
  const page = useContext(RoutedPage)
  /** Whether the frame's head carries this screen's age (#98). */
  const frameAge = useContext(FrameAge)

  // ---- polling ----------------------------------------------------------
  //
  // The cadence is read through a ref because callers pass an inline arrow,
  // which is a new function on every render; a dependency on it would re-plan
  // the next read on every tick of the age clock.
  const pollRef = useRef(pollMs)
  pollRef.current = pollMs
  /** Consecutive failed reads. The back-off is a function of this. */
  const failures = useRef(0)
  /** When the next read is due, or null when none is planned. */
  const dueAt = useRef<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [cadence, setCadence] = useState<Cadence | null>(null)
  /**
   * Whether the read about to start was asked for by the poll timer, or by a
   * tab coming back, rather than by a person. Read and cleared by the load
   * effect: only a person's refresh puts a screen holding no rows back to
   * `loading`.
   */
  const byPoll = useRef(false)
  /**
   * A read is in flight because the TENANT changed under a screen that was
   * kept mounted (a kept Submit form, intake-tenants.html 2A). Its rows are
   * the old tenant's until the answer lands, so they are dimmed meanwhile.
   */
  const [rereading, setRereading] = useState(false)
  /** How long the tab was hidden, when it came back with a read due; null otherwise. */
  const [awayMs, setAwayMs] = useState<number | null>(null)
  const hiddenAt = useRef<number | null>(tabHidden() ? Date.now() : null)

  const disarm = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  /** Start the timer for the planned read -- unless the tab is hidden. */
  const arm = useCallback(() => {
    disarm()
    if (dueAt.current === null || tabHidden()) return
    timer.current = setTimeout(() => {
      timer.current = null
      dueAt.current = null
      byPoll.current = true
      setNonce((n) => n + 1)
    }, Math.max(0, dueAt.current - Date.now()))
  }, [disarm])

  /** Plan the next read after one settles. `stop` plans none. */
  const plan = useCallback(
    (data: T | null, outcome: 'ok' | 'failed' | 'stop', pauseUntil: number | null) => {
      const base = outcome === 'stop' ? null : cadenceOf(pollRef.current, data)
      if (base === null) {
        dueAt.current = null
        disarm()
        setCadence(null)
        return
      }
      failures.current = outcome === 'failed' ? failures.current + 1 : 0
      // A 429's Retry-After is a floor under the back-off, never a shortcut.
      const wait = Math.max(
        nextPollDelay(base, failures.current),
        pauseUntil === null ? 0 : pauseUntil - Date.now(),
      )
      dueAt.current = Date.now() + wait
      setCadence({ base, wait })
      arm()
    },
    [arm, disarm],
  )

  useEffect(() => {
    let live = true
    // A refresh with data on screen must NOT blank it back to skeletons.
    //
    // NOR MAY A POLL BLANK A SCREEN THAT HOLDS NO ROWS. An empty read and a
    // failed first read both leave `lastGood` null, so this reset used to fire
    // on every poll: a tenant with no agents, polled every 30s, lost its empty
    // panel to skeleton rows and back each time, and a screen backing off after
    // a failure unmounted its failure panel -- and the `Try again` under the
    // reader's pointer -- on every retry. A poll leaves whatever answer is on
    // screen until the next answer replaces it. The refresh button still shows
    // `loading`: a person pressed it and is owed a sign that it took.
    const polled = byPoll.current
    byPoll.current = false
    if (!lastGood.current && !polled) setState({ status: 'loading', since: Date.now() })

    // The page's reads are the page's, even under an open inspector (CH-2).
    const reading = page ? pageReads(load) : load()
    reading.then((next) => {
      if (!live) return
      setRereading(false)
      setAwayMs(null)

      if (next.status === 'ok') {
        lastGood.current = { data: next.data, fetchedAt: next.fetchedAt }
        setPausedUntil(null)
        setState(next)
        plan(next.data, 'ok', null)
        return
      }
      if (next.status === 'empty') {
        // A genuine zero replaces the old rows. Keeping them would be the
        // mirror of the bug this app is about: showing data that is gone.
        lastGood.current = null
        setPausedUntil(null)
        setState(next)
        plan(null, 'ok', null)
        return
      }
      if (next.status === 'error') {
        const prev = lastGood.current
        const pauseUntil =
          next.error.kind === 'rate_limited' && next.error.retryAfterSeconds
            ? Date.now() + next.error.retryAfterSeconds * 1000
            : null
        if (pauseUntil !== null) setPausedUntil(pauseUntil)
        // THE STALE RULE. Data in hand plus a failed refresh is never a blank
        // screen -- it is the old data, dimmed, labelled with its age.
        setState(
          prev
            ? { status: 'stale', data: prev.data, fetchedAt: prev.fetchedAt, error: next.error }
            : next,
        )
        plan(prev?.data ?? null, NEEDS_A_PERSON.has(next.error.kind) ? 'stop' : 'failed', pauseUntil)
        return
      }
      setState(next)
    })
    return () => {
      live = false
    }
    // `load` is recreated per render by callers; nonce is the retry trigger,
    // and a poll is a retry the timer presses.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce])

  // A hidden tab reads nothing; a tab coming back reads at once if a read fell
  // due while it was away, and otherwise resumes the wait it was part-way
  // through. Registered only on a screen that polls.
  const polls = pollMs !== undefined
  useEffect(() => {
    if (!polls || typeof document === 'undefined') return
    const onVisibility = () => {
      if (tabHidden()) {
        hiddenAt.current = Date.now()
        disarm()
        return
      }
      const away = hiddenAt.current === null ? 0 : Date.now() - hiddenAt.current
      hiddenAt.current = null
      if (dueAt.current !== null && dueAt.current <= Date.now()) {
        // THE TAB WAS AWAY AND STOPPED READING: say so until the read lands
        // (states.html §13), rather than letting the ages grow in silence.
        if (away >= HIDDEN_LINE_AFTER_MS) setAwayMs(away)
        dueAt.current = null
        byPoll.current = true
        setNonce((n) => n + 1)
      } else {
        arm()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [polls, arm, disarm])

  // A TENANT SWITCH UNDER A SCREEN THAT STAYED MOUNTED re-reads it in place.
  // The shell remounts every other page on a switch, so this fires only
  // under a kept form (SkyShell `keepOnSwitch`): the form's draft is kept,
  // and what it was drawn from -- runner profiles, pools -- is read again as
  // the new tenant. As a poll, so the rows (and the draft) stay mounted.
  const tenant = useSyncExternalStore(subscribeTenant, chosenTenant, chosenTenant)
  const seenTenant = useRef(tenant)
  useEffect(() => {
    if (seenTenant.current === tenant) return
    seenTenant.current = tenant
    disarm()
    dueAt.current = null
    byPoll.current = true
    setRereading(true)
    setNonce((n) => n + 1)
  }, [tenant, disarm])

  // Nothing fires after the screen is gone.
  useEffect(() => disarm, [disarm])

  // The refresh button is a read NOW: whatever was planned is replaced by it,
  // and the read it causes plans the next one.
  const retry = useCallback(() => {
    disarm()
    dueAt.current = null
    byPoll.current = false
    setNonce((n) => n + 1)
  }, [disarm])

  const data =
    state.status === 'ok' ? state.data : state.status === 'stale' ? state.data : null
  // A read that succeeded, and is older than the screen trusts. The same
  // treatment as a failed refresh: the rows stay, dimmed, `not refreshed`.
  const readAt = state.status === 'ok' || state.status === 'empty' ? state.fetchedAt : null
  const aged = readAt !== null && now - readAt > AGED_AFTER_MS
  const reading: ScreenReading | null =
    state.status === 'ok' || state.status === 'stale'
      ? { fetchedAt: state.fetchedAt, pollMs: cadence?.base ?? null }
      : null

  return (
    <>
      {/* NO ENVIRONMENT BADGE HERE ANY MORE. Every screen used to print a
          hardcoded `dev` beside its own title -- a word nothing in this app
          had measured, repeated on sixteen screens. Overview.tsx had already
          refused to draw it and said why; Brand.tsx now draws the real one
          once, in the product header, from something that was actually
          established. A per-screen copy would be a second opinion about the
          environment, and the second opinion is the one that gets believed
          because it is next to what you are reading. */}
      <PageHead title={title} help={help} meta={metaOf(state, summary)}>
        <SubLine
          state={state}
          summary={summary}
          pausedUntil={pausedUntil}
          onRetry={retry}
          now={now}
          aged={aged}
          cadence={cadence}
          frameAge={frameAge}
        />
      </PageHead>

      <HiddenTabLine wasHiddenMs={awayMs} reading={awayMs !== null} />

      {state.status === 'stale' && (
        <StaleBanner error={state.error} fetchedAt={state.fetchedAt} now={now} />
      )}

      {state.status === 'loading' && (skeleton ?? <SkeletonRows />)}
      {state.status === 'error' && <FailedPanel error={state.error} onRetry={retry} />}

      {/* THE SHARED EMPTY STATE (CH-10), where this was a hand-built `.state`
          box: no mark, so a real zero told itself from a failed read by
          colour alone, and a `Checked just now.` at 16px mono, because
          `.state p` outranked `.checked-at`. The panel is §6.9's fixed shape
          now: mark, heading, one sentence, a link out.

          AND THE `Checked …` LINE TICKS (CH-1/CH-10, settled on #87,
          2026-09-25). #145 deleted it as a repeat of the sub-line's age; the
          box had asked for it to TICK, because it never moved -- a panel that
          said `Checked just now.` at nine in the morning said it at noon. It
          is back as the primitive's foot, at the micro step, and it reads the
          sub-line's instant from the sub-line's clock (`now`, `useNow` at
          `AGE_TICK_MS`), so the two can never disagree. */}
      {state.status === 'empty' && empty && (
        <Absent
          kind="zero"
          heading={empty.heading}
          say={empty.say ?? EMPTY_SAY}
          link={empty.link}
          foot={`Checked ${timeAgo(state.serverAt ?? state.fetchedAt, now)}.`}
        >
          {empty.body}
        </Absent>
      )}

      {/* Dimmed when stale or aged, and the dimming is the signal that the
          numbers below are from an earlier read. */}
      {data !== null && reading !== null && (
        <div className={state.status === 'stale' || aged || rereading ? 'stale-body' : undefined}>
          {children(data, reading)}
        </div>
      )}
    </>
  )
}

/**
 * THE PAGE HEAD, WRITTEN ONCE (AH-25, design-system §6.12).
 *
 * A title over one line of provenance: what was read, how old it is, and the
 * screen's read control -- with the cost of that control, when it has one,
 * printed on it (#138). Inside the frame the age of a fresh read is the
 * head's, not this line's (`FrameAge`, #98). No description sentence; the
 * sentence a screen is allowed lives behind its `?`.
 *
 * `Screen` renders this on fourteen routes. Platform counts renders it too,
 * because it reads on a button rather than on mount and so cannot be a
 * `Screen`, and it used to draw a second shape of head for that reason: the
 * control pinned right in a `.ctl-page-head` and its cost a whole toolbar row
 * below. Help renders it as §6.12's one exception: it reads nothing, so its
 * line says what the page is and which topic is showing. Every caller has a
 * line, so `children` is required -- there is no bare head to style.
 *
 * `help`, WHEN A SCREEN'S ONE `?` EXPLAINS THE WHOLE SCREEN (AH-24). The
 * owner's slot rule is after the label or heading, never after a value, and
 * a property of the whole screen -- the Workflows board's absent figures --
 * has no label nearer than the screen's own title. So the glyph follows the
 * `<h1>`, outside it: a heading's accessible name is its words, not the
 * topic's short form. Passed as `help="<id>"` so `tests/help.test.ts` counts
 * it against the ration on the screen that asked for it.
 *
 * `.ctl-page-head` is left to the heads this does not describe: Overview's
 * facts row and the API reads page.
 */
export function PageHead({
  title,
  help,
  meta,
  headingId,
  action,
  children,
}: {
  title: string
  help?: TopicId
  /** The page's one action, drawn by the title (Platform counts' billed run). */
  action?: ReactNode
  /** The `<h1>`'s id, for a region that is labelled by it. */
  headingId?: string
  /** What was read, as a fact: a mono chip beside the title ("37 · none hold capacity"). */
  meta?: ReactNode
  /** The provenance: its age, its cadence and its read control, right-aligned. */
  children: ReactNode
}) {
  // ONE ROW (#503, "Page head"): the title, the meta chip, and right-aligned
  // "read · poll · refresh", as the picked frames draw it. The head's age of
  // this screen's reads (`HeadAge`) joins the row when the frame times this
  // screen (`FrameAge`), and the head beside the breadcrumb then prints none.
  const frame = useContext(FrameAge)
  const headAge = useContext(HeadAge)
  const takes = frame && headAge !== null
  const sectionHelp = useContext(SectionHelp)
  useClaimHeadRow(takes)
  return (
    <div className="c-phead">
      <div className="head">
        {/* Two lines at most, the whole title in its tooltip (walkthrough C). */}
        <h1 id={headingId} title={title}>{title}</h1>
        {/* ONE `?` BY THE TITLE: the screen's own topic when it has one,
            otherwise its section's question (Q2). */}
        {help !== undefined ? <HelpCard topic={help} /> : sectionHelp}
        {action}
      </div>
      <p className="sub">
        {meta !== undefined && meta !== null && meta !== '' && (
          <>
            <span className="c-meta">{meta}</span>
            {/* The chip and the age are two facts: said as two to a reader
                of the text, drawn apart by the row's gap. */}
            <span className="sk-vh"> · </span>
          </>
        )}
        <span className="c-age">
          {takes && <span className="ctl-head-age">{headAge}</span>}
          {takes && hasContent(children) && <span aria-hidden> · </span>}
          {children}
        </span>
      </p>
    </div>
  )
}

/** Whether a node draws anything (a SubLine that has nothing to add returns null). */
function hasContent(n: ReactNode): boolean {
  return n !== null && n !== undefined && n !== false && n !== ''
}

/** The meta chip: what the read found, before the provenance. */
function metaOf<T>(state: Result<T>, summary: ((data: T) => ReactNode) | undefined): ReactNode {
  if (state.status === 'empty') return 'Nothing to show'
  if ((state.status === 'ok' || state.status === 'stale') && summary !== undefined) return summary(state.data)
  return null
}

/** `parts`, with ` · ` between those that draw something. */
function dots(parts: readonly ReactNode[]): ReactNode {
  const drawn = parts.filter(hasContent)
  if (drawn.length === 0) return null
  return drawn.map((p, i) => (
    <Fragment key={i}>
      {i > 0 && ' · '}
      {p}
    </Fragment>
  ))
}

function SubLine<T>({
  state,
  pausedUntil,
  onRetry,
  now,
  aged,
  cadence,
  frameAge,
}: {
  state: Result<T>
  summary?: (data: T) => ReactNode
  pausedUntil: number | null
  onRetry: () => void
  /** The shared clock's instant, so this age agrees with the head's. */
  now: number
  /** A successful read older than `AGED_AFTER_MS`. */
  aged: boolean
  cadence: Cadence | null
  /** The frame's head prints the age of a fresh read (`FrameAge`, #98). */
  frameAge: boolean
}) {
  const paused = pausedUntil !== null && pausedUntil > Date.now()
  const retryBtn = (
    <button onClick={onRetry} disabled={paused}>
      {paused ? `paused ${Math.ceil((pausedUntil - Date.now()) / 1000)}s` : 'refresh'}
    </button>
  )
  // THE CADENCE BESIDE THE AGE, so a reader knows the age is going to move and
  // how soon. While backing off it is the wait actually in force, and says so.
  const cadenceText =
    cadence === null
      ? null
      : cadence.wait > cadence.base
        ? `every ${formatDuration(cadence.wait)}, backing off`
        : `every ${formatDuration(cadence.base)}`
  // `not refreshed` is the stale wording, and an aged read earns it too: it is
  // true, and it is what a reader scanning for a frozen screen looks for.
  const unrefreshed = aged ? <strong>not refreshed</strong> : null
  // THE AGE OF A FRESH READ IS THE HEAD'S (#98), on this row (`PageHead`).
  // Printed here only where no head carries it, or once it is no longer
  // fresh -- the stale case below always prints its own. And whenever the
  // DATA is older than the fetch: a cached payload's `generated_at`
  // (`serverAt`) can be 40m old on a read that landed just now, and the head,
  // which times the fetch, would say `just now` beside it. The data's own age
  // is then the one that matters.
  const served =
    (state.status === 'ok' || state.status === 'empty') &&
    state.serverAt !== undefined &&
    state.fetchedAt - Date.parse(state.serverAt) > AGE_TICK_MS
  const ownAge = aged || !frameAge || served

  switch (state.status) {
    case 'loading':
      return <>Reading…</>
    case 'ok':
    case 'empty':
      return <>{dots([unrefreshed, ownAge ? `read ${timeAgo(state.serverAt ?? state.fetchedAt, now)}` : null, cadenceText, retryBtn])}</>
    case 'stale':
      return <>{dots([<strong key="n">not refreshed</strong>, `showing ${timeAgo(state.fetchedAt, now)}`, cadenceText, retryBtn])}</>
    case 'error':
      return state.error.kind === 'admin_required' ? (
        <>Admin only.</>
      ) : (
        <>Could not read. {retryBtn}</>
      )
  }
}

/**
 * Shown above data that is real but no longer current. Deliberately not a
 * toast: at 390pt a toast sits under the thumb and gets dismissed by accident,
 * and the one thing this must guarantee is that the failure is still on screen
 * when the operator looks.
 *
 * THE SENTENCE BECAME A FACTS STRIP. "The numbers below were read 4m ago and
 * have not been refreshed since" was a sentence wrapped around two facts and a
 * verb. The facts are the same two, keyed, in the strip below; the third fact
 * -- that the rows underneath are dimmed -- is carried by `.stale-body`, which
 * is an attribute of the rows themselves and therefore cannot drift away from
 * them the way a paragraph above them can.
 *
 * The age moves with the shared clock (CH-1). It was read once, so a banner
 * that appeared saying `4m ago` still said `4m ago` an hour later -- the one
 * number on the page whose whole job is to grow.
 */
function StaleBanner({ error, fetchedAt, now }: { error: ApiError; fetchedAt: number; now: number }) {
  // THE PAGE TIER (states.html C): ONE banner over the dimmed older data.
  // The canonical `Banner`, amber, because the rows below are real and only
  // old -- a failure with nothing to show is `FailedPanel`, in place.
  return (
    <div className="app-banner stale-note">
      <Banner tone="warn" title={`${errorHeading(error)} — showing older data`}>
        <span className="ctl-facts">
          <span className="ctl-fact">
            <b>read</b> {timeAgo(fetchedAt, now)}
          </span>{' '}
          <span className="ctl-fact">
            <b>since</b> {error.message}
          </span>
        </span>
      </Banner>
    </div>
  )
}

/**
 * The panel that exists because this platform's defining bug was rendering a
 * failed read as an empty one.
 *
 * WHAT IS ALLOWED TO STAY, AND WHY IT IS EXACTLY TWO LINES. This panel is an
 * empty state, so its shape is the fixed one: a mark, a heading, ONE sentence,
 * and a way out. The mark is `.ctl-mark`, which names which kind of nothing
 * this is in two words and in a border style that survives greyscale; the
 * sentence is the server's own message, because it is the only part that says
 * where to look.
 *
 * `errorReassurance` IS NOT DECORATION AND IS NOT PROSE TO BE MOVED. It is the
 * invariant itself, in the one state where no encoding can carry it: a failed
 * read renders no figure, and the absence of a figure is not a thing a reader
 * can see. Everything else on this panel was cut; this stays.
 */
export function FailedPanel({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  const reload = error.kind === 'session_expired' || error.kind === 'unauthenticated'

  // AN ADMIN GATE IS NOT A FAILURE. A non-admin genuinely cannot read
  // /v1/admin/*, and painting that red -- with "this is a failure to read the
  // platform" under it -- tells someone their platform is broken when they
  // are simply not an admin. The Trouble board got this right panel-by-panel
  // and every screen using this component got it wrong.
  //
  // "You are not in an admin group, so this screen has nothing to show you" is
  // gone: it restated the heading, and the blue solid `.ctl-mark.is-admin`
  // now carries the same claim as a shape.
  if (error.kind === 'admin_required') {
    return (
      <div className="state admin-gate" role="status">
        <h3>
          <i className="ctl-mark is-admin">admin only</i> {errorHeading(error)}
        </h3>
        <p>Nothing is wrong with the platform, and nothing failed.</p>
        <p className="checked-at">{error.message}</p>
      </div>
    )
  }

  return (
    <div className="state failed">
      <h3>
        <i className="ctl-mark is-unread">not read</i> {errorHeading(error)}
      </h3>
      <p>{error.message}</p>
      <p className="state-invariant">{errorReassurance(error)}</p>
      {error.httpStatus !== null && (
        <p className="checked-at">
          HTTP {error.httpStatus}
          {error.code ? ` · ${error.code}` : ''}
        </p>
      )}
      {typeof error.detail === 'string' && <pre>{error.detail}</pre>}
      {reload ? (
        <Button kind="primary" onClick={() => window.location.reload()}>
          Reload to sign in
        </Button>
      ) : error.kind === 'rate_limited' ? (
        // A COUNTDOWN, NEVER A RETRY BUTTON. The server has just told us how
        // long to wait; offering "Try again" invites someone to hammer the
        // wall they were told about, and the token bucket is 20 rps per
        // principal per instance -- every open tab counts against it.
        <p className="checked-at">
          paused {error.retryAfterSeconds ?? 'a few'}s — the API asked us to wait
        </p>
      ) : (
        <Button onClick={onRetry}>Try again</Button>
      )}
    </div>
  )
}

/**
 * THE GEOMETRY A VALUE WILL OCCUPY, while it is still being read.
 *
 * The three inline numbers here were the only off-scale corner left in the
 * frame: `borderRadius: 8` is not one of 0/2/6/10/14/999, and a React inline
 * style is the one place the sheet's corner scale cannot see. They are a class
 * now, so `spaceprobe.ts` grades them like everything else.
 */
export function SkeletonRows({ rows = 6 }: { rows?: number }) {
  return (
    <div className="section" aria-hidden>
      {Array.from({ length: rows }, (_, i) => (
        <div className="skeleton ctl-skeleton-row" key={i} />
      ))}
    </div>
  )
}

/**
 * MOVED to types.ts, and re-exported here rather than left behind as a second
 * copy.
 *
 * `checks.ts` needed it, and that module is pure on purpose -- Results in,
 * `Check[]` out, no React anywhere in its import graph, which is what lets
 * `node --test` exercise it without a DOM. Importing it from this file would
 * have pulled every component in Shell.tsx along with it.
 *
 * The eleven existing `import { timeAgo } from './Shell'` sites are untouched.
 */
export { timeAgo }

/**
 * AN IDENTIFIER, RENDERED SO IT CAN BE PASTED. B17.
 *
 * `styles.css:178` uppercases `.section > h2`, and two screens put an id in
 * one: `Workflows.tsx` prints the workflow id there and `Agents.tsx` prints it
 * again as a group heading. The result on screen is
 * `WF_BCDC9180E4FB4A209F31` for an id that is lowercase in Firestore, in the
 * API, in every log line and in the URL you would paste it into. A displayed
 * id that differs from the real one is not a cosmetic problem: it is unusable
 * for the one thing an id is for. `QuotaDetail.tsx:94-96` already refuses to
 * do this to tenant ids and writes the reason beside the refusal; this is that
 * refusal made general.
 *
 * IT IS A COMPONENT AND A CLASS, NOT A CONVENTION. `.id` in styles.css sets
 * `text-transform: none` on the element ITSELF, so it wins over any ancestor's
 * transform by inheritance rather than by out-specifying it -- which means a
 * heading, chip or table cell added later cannot break it from above. Wrapping
 * the value is the only thing a caller has to remember, and
 * `src/__tests__/brand.test.tsx` asserts the computed style rather than the
 * source text, so deleting the CSS rule fails the suite.
 */
export function Id({
  children,
  title,
}: {
  children: ReactNode
  title?: string
}) {
  return (
    <span className="id" title={title}>
      {children}
    </span>
  )
}

/**
 * THE ELEVEN-ITEM NAV IS DELETED, not left exported for nothing to import.
 *
 * `App.tsx`'s `Rail` replaced it: six sections named after objects, every tab
 * always rendered, a position that means one thing. This component survived
 * the redesign as an export nothing called -- eleven buttons including
 * `Trouble`, a destination `App.tsx` deliberately removed and whose hash now
 * resolves to Overview. A second, stale answer to "what are this product's
 * sections", compiling and shipping in the bundle, is exactly the kind of
 * thing somebody renders again by autocomplete.
 *
 * Nothing imported it: `grep -rn "Nav" src/*.tsx` found only the declaration.
 */
