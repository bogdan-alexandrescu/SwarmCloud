import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import { createPortal } from 'react-dom'
// `Em` and `Mark` live in AgentDetail.tsx, which is where `ABSENT_MARK` was
// written and which design-system.md §9.1 names as the source to promote from.
// One definition for the four screens of this group; a second copy of a mark
// whose whole job is to be recognisable is a contradiction in terms.
// `Chip` joins them for the same reason: §9.3 of design-system.md counted four
// status chips in this product and asked for one, and the rebuilt `.ctl-chip`
// is only a rebuild if the screens stop hand-rolling their own.
import { Em, Mark, type ChipTone } from './AgentDetail'
import { TaskIdLine } from './TaskIdLine'
import { PHONE_PAGE_LIMIT, RECENT_STATES, RECENT_STATE_OF, agentKind, agentList, agentName, shortTaskId, workflowHref, type AgentList, type RecentState } from './agentlist'
import { TASK_PAGE_LIMIT, loadTasks, type ResourceClasses } from './api'
import { classUnits, useResourceClasses } from './Blockers'
import type { Result } from './fetch'
import { HelpCard, phoneWidth } from './HelpCard'
import { toggleListSnap, useListSnap } from './listSnap'
import './styles/agents.css'
import { PARK_WORD, Segmented, StateMark, ToneMark } from './components'
import { Id, Screen } from './Shell'
import { publishListCounts } from './Spine'
import { rowClock, useNow } from './useNow'
import {
  CONCURRENCY_STATES,
  TERMINAL_STATES,
  compareStarted,
  elapsed,
  rollupState,
  taskGroup,
  whyAgent,
  whyNeedsAction,
  type Task,
  type TaskPage,
} from './types'

type Tab = 'live' | 'waiting' | 'recent'

/**
 * The STARTED column's sort (#376): null is the list's own order (most
 * recently changed first), otherwise by start, newest or oldest first. Rows
 * that never started sort after every row that did, either way.
 */
export type StartedSort = 'desc' | 'asc' | null

/** The head's sort control: the current order, and what a press sets next. */
interface SortControl {
  started: StartedSort
  onStarted: () => void
}

/**
 * A group's rolled-up state, in the chip's own tone vocabulary.
 *
 * `rollupState` answers in four words of its own and `stateTone` only takes a
 * TaskState, so the mapping has to happen somewhere. It happens here, in four
 * lines, rather than by widening either of those -- both live in `types.ts`,
 * which another track owns. `waiting` maps to `wait`, which `chipTone` draws
 * as the caution triangle: work that is held is not work that is fine.
 */
function rollTone(roll: ReturnType<typeof rollupState>): ChipTone {
  if (roll === 'running') return 'live'
  if (roll === 'succeeded') return 'ok'
  if (roll === 'failed') return 'bad'
  return 'wait'
}

/**
 * THE THREE TABS, AND WHERE THEIR SENTENCES WENT.
 *
 * `hint` was a `title=` on each tab -- "Holding a pool slot, and costing
 * money" -- which is exactly the medium design-system.md §8.3 names as the one
 * that fails: no visible anchor, no keyboard route, invisible in a screenshot.
 * It is `aria-label` now, which a keyboard and a screen reader both reach, and
 * the tab's own word plus its count is what a sighted reader needs. Which
 * states cost nothing is `#help/capacity`.
 */
const TABS: { id: Tab; label: string; say: string }[] = [
  { id: 'live', label: 'Live', say: 'Live: holding a pool slot, and costing money' },
  { id: 'waiting', label: 'Waiting', say: 'Waiting: durable and free — READY or PARKED' },
  { id: 'recent', label: 'Recent', say: 'Recent: finished, one way or another' },
]

function tabOf(t: Task): Tab {
  if (CONCURRENCY_STATES.has(t.state)) return 'live'
  if (TERMINAL_STATES.has(t.state)) return 'recent'
  return 'waiting'
}

/**
 * WHERE THE SCREEN OPENS: the first tab that has rows, in the tabs' own order.
 *
 * It opened on `'live'`, always. The audit's screenshot (ui-audit-and-build-
 * prompt.md §A1.2) is what that costs: "nothing in live" under a screen whose
 * question is "why has mine not moved?", with the three steps that had not
 * moved one tab over. Live is the one tab that cannot hold a step that has not
 * moved. Live still comes first when it has anything -- a held slot is the
 * costliest fact on the page -- so this changes only the landing that was
 * guaranteed to be empty.
 *
 * `'live'` when every tab is empty: an empty read is a page of no rows
 * (`loadAgentsPage`, V151), so the body mounts and lands on Live's own zero.
 */
function landingTab(counts: Readonly<Record<Tab, number>>): Tab {
  return TABS.find((t) => counts[t.id] > 0)?.id ?? 'live'
}

/**
 * The part of a task id a person can use: the first eight characters AFTER
 * the prefix. `task_b5dc2568713a40158851` -> `b5dc2568`.
 *
 * It was `id.slice(-8)` -- `40158851` -- the TAIL, which is not a prefix of
 * anything: it cannot be typed into a search, matched against the
 * `task_b5dc25…` the rest of the product prints, or found in the drawer title.
 * Ids are `<prefix>_<20 hex>` (swarm_common/models.py `new_id`); one without an
 * underscore is shown from its start, never from its end.
 */

/**
 * HOW OFTEN THIS LIST IS WORTH RE-READING, from what the last read held.
 *
 * docs/web-ui/03-agents-and-workflows.md §2.5: 5s while the Live tab holds any
 * row, 30s otherwise. A live agent changes state on the order of seconds -- it
 * finishes, it is reclaimed, its cancel lands -- and a waiting or finished one
 * does not. Hidden tabs do not read at all; that half is `Screen`'s.
 *
 * THE COST IS THE PAYLOAD, NOT THE QUERY. A full row carries `input`,
 * `metadata` and `result_summary`, so a 200-row page is 2-4 MB (§2.5), and at
 * 5s that is roughly 600 KB/s. Only the Live tab earns the fast cadence, and
 * only while it has rows. The list asks for `view=summary` (§8/P2, #168),
 * which leaves those three out of every row; a phone still reads
 * `PHONE_PAGE_LIMIT` rows rather than 200 -- see below.
 */
export const LIVE_POLL_MS = 5_000
export const IDLE_POLL_MS = 30_000

export function pollInterval(page: TaskPage | null): number {
  return page !== null && page.tasks.some((t) => tabOf(t) === 'live') ? LIVE_POLL_MS : IDLE_POLL_MS
}

/**
 * THE PHONE PAGE: 50 rows, not 200 (docs/web-ui/03-agents-and-workflows.md
 * §2.1 and §2.5).
 *
 * §2.5 states the condition this screen's 5s poll ships under: "Until
 * [view=summary] exists, the phone build must cap at limit=50 and say 'showing
 * the 50 most recent' rather than ship a 4 MB poll." The list polled the full
 * 200-row page on every viewport -- 2-4 MB every 5s over cellular, for as
 * long as Live held a row and the tab was visible.
 *
 * `view=summary` EXISTS NOW (#168) AND THE CAP STAYS. The summary view cuts
 * each row; the cap cuts the row count, and the Overview's failures check
 * counts over this same phone page (OV-10), so lifting it changes what two
 * screens' figures describe at once. Whether a phone should go back to 200
 * summary rows is a decision #168 left open, not one this read makes.
 *
 * "PHONE" IS `phoneWidth()` from HelpCard.tsx: 560px and under, the width at
 * which this list already draws as cards and a `?` takes a fingertip. §2.1
 * writes 640px for the card layout; the stylesheet has drawn the phone list at
 * 560px since before this, and one definition of phone keeps the two from
 * disagreeing about which layout a width gets. It is decided at each READ,
 * not once, so a rotated device takes the right page on its next poll.
 *
 * THE VALUE LIVES IN `agentlist.ts` (OV-10): the Overview's failures check
 * counts over the same page at the same width, so the figure and the list its
 * link opens describe one population. Re-exported here for this screen's
 * readers.
 */
export { PHONE_PAGE_LIMIT }

/** A page of the list, and the page size that read asked for. */
interface AgentsPage extends TaskPage {
  /** The `limit` the read asked for: `PHONE_PAGE_LIMIT` at phone width. */
  asked: number
}

async function loadAgentsPage(): Promise<Result<AgentsPage>> {
  const asked = phoneWidth() ? PHONE_PAGE_LIMIT : TASK_PAGE_LIMIT
  // The summary view (#168): no row on this screen draws `input`, `metadata`
  // or `result_summary` -- the inspector reads its own task, full, through
  // `loadTask` -- and the trap in `drawer.reread.test.tsx` holds that.
  const read = await loadTasks(asked, { view: 'summary' })
  // AN EMPTY READ IS A PAGE OF NO ROWS, NOT A DIFFERENT SCREEN (V151, visual
  // QA #1038). Handed to `Screen` as `empty`, it replaced the whole body with
  // one panel: the tab strip, the search and the filters went with it, so the
  // screen a tenant with no agents saw was laid out unlike the one it would
  // see a minute later. The body draws each tab's own real zero, mark and
  // all, under the same chrome it always has.
  if (read.status === 'empty') {
    return { status: 'ok', data: { tasks: [], next_page_token: null, asked }, fetchedAt: read.fetchedAt, serverAt: read.serverAt }
  }
  return read.status === 'ok' || read.status === 'stale'
    ? { ...read, data: { ...read.data, asked } }
    : read
}

/**
 * Screen A -- Agents. One table, three tabs, no sub-pages.
 *
 * The tab counts come from the ROWS, never from /v1/stats, so the badge and
 * the table can never disagree with each other.
 *
 * `taskId` is the agent the inspector has open, when one is (AG-17): App
 * passes it and the matching row is marked `aria-current`. OPTIONAL, so this
 * screen stands on its own and App can pass it whether or not it has yet.
 *
 * `list` AND `onList` ARE THE ADDRESS (OV-10). `list` is the tab and Recent
 * state the hash names; a tab named there overrides `landingTab`, and a hash
 * naming none leaves the tab and state where they are, so "land once, then
 * stay" is unchanged. Every tab and segment click is reported through
 * `onList` and App writes the address -- this screen never touches the hash.
 * Both optional, for the same reason `taskId` is.
 */
export function AgentsScreen({
  onOpen,
  taskId = null,
  list = null,
  onList,
}: {
  onOpen: (taskId: string) => void
  taskId?: string | null
  list?: AgentList | null
  onList?: ((list: AgentList) => void) | undefined
}) {
  // `null` until a page -- or the address -- has told the body where to land;
  // see `landingTab`.
  const [tab, setTab] = useState<Tab | null>(list?.tab ?? null)
  // The Recent tab's state filter. null is "all".
  const [recentState, setRecentState] = useState<RecentState | null>(list?.state ?? null)
  const [profile, setProfile] = useState<string>('')
  // GROUP BY WORKFLOW AND FAILED FIRST ARE IN THE ADDRESS (G2-22, dev QA
  // 2026-10-07): `?group=wf`, `?first=failed`. They lived only here, so a
  // filtered view could not be shared or reloaded. Seeded from the address
  // and reported through `onList` like the tab and the state.
  const [grouped, setGrouped] = useState(list?.grouped === true)
  const [failedFirst, setFailedFirst] = useState(list?.failedFirst === true)
  // THE ADDRESS WINS WHEN IT NAMES A TAB, and only then. Keyed on the values
  // rather than the object, which App rebuilds on every route.
  const listTab = list?.tab ?? null
  const listState = list?.state ?? null
  const listGrouped = list?.grouped === true
  const listFirst = list?.failedFirst === true
  useEffect(() => {
    if (listTab === null) return
    setTab(listTab)
    setRecentState(listTab === 'recent' ? listState : null)
    setGrouped(listGrouped)
    setFailedFirst(listFirst)
  }, [listTab, listState, listGrouped, listFirst])
  // RECENT'S SEARCH (#99). Held here beside `profile`, not in the address: a
  // query is a reader's scratch.
  const [query, setQuery] = useState('')

  return (
    <Screen
      title="Agents"
      load={loadAgentsPage}
      // THE LIST RE-READS (AG-1). It read once and never again, while its
      // clock went on adding to every row: a finished agent read `running`
      // and was counted in Live. `Screen` owns the timer, the pause while the
      // tab is hidden, the back-off and the stop on an answer only a person
      // can change; this screen owns the cadence, which is a function of the
      // rows it holds.
      pollMs={pollInterval}
      summary={(d) => {
        const live = d.tasks.filter((t) => tabOf(t) === 'live').length
        return (
          <>
            {d.tasks.length} loaded · {live} live
            {d.tenant_id && ` · ${d.tenant_id}`}
          </>
        )
      }}
      // NO `empty` PANEL (V151): `loadAgentsPage` never answers `empty`, so
      // a tenant with no agents keeps the tabs, search and filters, and each
      // tab draws its own real zero below them.
    >
      {(d, reading) => (
        <AgentsBody
          onOpen={onOpen}
          openTaskId={taskId}
          page={d}
          // WHEN THE ROWS ON SCREEN WERE READ, and the cadence in force, as
          // `Screen` read them -- not recorded a second time here. A stale
          // screen hands over the last GOOD read's time, which is the one the
          // rows describe.
          readAt={reading.fetchedAt}
          interval={reading.pollMs ?? pollInterval(d)}
          tab={tab}
          setTab={setTab}
          recentState={recentState}
          setRecentState={setRecentState}
          onList={onList}
          profile={profile}
          setProfile={setProfile}
          grouped={grouped}
          setGrouped={setGrouped}
          query={query}
          setQuery={setQuery}
          failedFirst={failedFirst}
          setFailedFirst={setFailedFirst}
        />
      )}
    </Screen>
  )
}

/**
 * The instant the row durations are computed at (AG-1). It lives in useNow.ts
 * now, beside the clock it caps, because the inspector's drawer caps its clock
 * the same way; re-exported so this screen's tests keep reading it from here.
 */
export { rowClock }

function AgentsBody({
  onOpen,
  openTaskId,
  page,
  readAt,
  interval,
  tab,
  setTab,
  recentState,
  setRecentState,
  onList,
  profile,
  setProfile,
  grouped,
  setGrouped,
  query,
  setQuery,
  failedFirst,
  setFailedFirst,
}: {
  onOpen: (taskId: string) => void
  openTaskId: string | null
  page: AgentsPage
  readAt: number | null
  /** The cadence the rows are re-read at: how far past `readAt` they are good for. */
  interval: number
  tab: Tab | null
  setTab: Dispatch<SetStateAction<Tab | null>>
  /** The Recent tab's state filter (OV-10); null is "all". */
  recentState: RecentState | null
  setRecentState: (s: RecentState | null) => void
  /** Where a tab or state click is reported, so App can write the address. */
  onList: ((list: AgentList) => void) | undefined
  profile: string
  setProfile: (p: string) => void
  grouped: boolean
  setGrouped: (g: boolean) => void
  /** Recent's text search (#99), over the loaded rows; never in the address. */
  query: string
  setQuery: (q: string) => void
  /** Recent's "failed first" sort (#99). */
  failedFirst: boolean
  setFailedFirst: (f: boolean) => void
}) {
  // One clock for every ticking duration on the screen, so a hundred rows do
  // not each hold their own interval. The SHARED 1s clock (useNow.ts), the
  // cadence a running duration asks for: the inspector's `run` ticks on the
  // same instant, so a row and the drawer beside it cannot disagree by a tick.
  //
  // THE ROW'S ELAPSED AND WAITING AGES RUN ON IT UNCAPPED (G2-04, dev QA
  // 2026-10-07). They were held at one poll interval past the read
  // (`rowClock`), so with the read five minutes old the list said
  // `implement 12m 44s` beside an inspector saying `17m 33s` for the same
  // task. `now - started_at` stays true for as long as the task is in the
  // state the row shows, and whether it still is is the read's age to say:
  // `Screen` dims a stale or aged page and prints `not refreshed`. AG-1's
  // half that matters -- the list re-reads, so a finished agent does not stay
  // `running` -- is `pollMs` above, and unchanged.
  //
  // `vouched` IS STILL CAPPED, for the one figure that is a judgement rather
  // than a span: the silent-worker line (`silentWorkerLine`). A list that
  // stopped reading cannot see the beats that arrived since, and must not age
  // them into "No heartbeat for 5m".
  const now = useNow(1000)
  const vouched = rowClock(now, readAt, interval)

  // THE CATALOGUE, READ ONCE FOR THE WHOLE LIST (#66), never per row: forty
  // rows each mounting `useResourceClasses` would be forty reads of the same
  // route. `whyAgent`/`whyNeedsAction` (types.ts) use a row's weight to tell
  // a pool too small for its task apart from a genuinely full one -- #66's
  // own repro (`resource:browser` at `hard_limit 1`, a browser task) read
  // "busy platform-wide. (0/1)" here before this was threaded through.
  const classes = useResourceClasses()

  const counts = useMemo(() => {
    const c: Record<Tab, number> = { live: 0, waiting: 0, recent: 0 }
    for (const t of page.tasks) c[tabOf(t)]++
    return c
  }, [page.tasks])

  // THE PANEL COUNTS WHAT THESE TABS COUNT, FROM THIS READ (U10a D27): it
  // read `/v1/stats` on its own 30s clock and lagged the tabs. A page with
  // fewer rows than it asked for and no next page is every row there is;
  // a capped page is not, and the panel says so by keeping its own read.
  const whole = page.tasks.length < page.asked && (page.next_page_token ?? null) === null
  useEffect(() => {
    publishListCounts({ live: counts.live, waiting: counts.waiting, at: readAt ?? Date.now(), whole })
  }, [counts.live, counts.waiting, readAt, whole])
  useEffect(() => () => publishListCounts(null), [])

  // LAND ONCE, THEN STAY. The first page decides the tab; after that it is
  // pinned, so a refresh that brings a live agent does not pull the reader off
  // the Waiting row they were reading. A click is always the reader's.
  // A FUNCTIONAL update, so an effect scheduled by the first render can never
  // overwrite a click that landed before it ran.
  const shown: Tab = tab ?? landingTab(counts)
  useEffect(() => {
    if (tab === null) setTab((current) => current ?? shown)
  }, [tab, shown, setTab])

  const profiles = useMemo(
    () => Array.from(new Set(page.tasks.map((t) => t.runner_profile))).sort(),
    [page.tasks],
  )

  // THE RECENT STATE FILTER (OV-10) applies on Recent alone, over the same
  // loaded rows as every other count here -- the toolbar's scope qualifier
  // already says so for all of them. Client-side ON PURPOSE: the Overview's
  // failures check counts FAILED among the newest 200, this list is that same
  // newest page, and a server-side FAILED list would be a different, larger
  // population answering to the same number. (At phone width the page is the
  // newest `PHONE_PAGE_LIMIT`, the scope qualifier says so, and the Overview
  // counts over that same phone page there -- `loadListPage`.)
  const stateFilter = shown === 'recent' ? recentState : null
  // THE SEARCH IS EVERY TAB'S (agents.html V1: "Find by name or task id"
  // above the list; #503 found the live list had none). Applied after the
  // state and the profile, over the same loaded rows, so a pasted task id or
  // step name narrows what those two already chose, and the qualifier below
  // names it. The tab's empty state says when the search is why it is empty.
  // "Failed first" (#99) stays Recent's, the one tab that holds failures.
  const needle = query.trim().toLowerCase()
  const failFirst = shown === 'recent' && failedFirst
  // THE STARTED SORT (#376), on every tab: newest start, oldest start, then
  // back to the list's own order. A reader's scratch, so not in the address.
  const [startedSort, setStartedSort] = useState<StartedSort>(null)
  const sort: SortControl = {
    started: startedSort,
    onStarted: () => setStartedSort((s) => (s === null ? 'desc' : s === 'desc' ? 'asc' : null)),
  }
  const rows = useMemo(
    () =>
      page.tasks
        .filter((t) => tabOf(t) === shown)
        .filter((t) => stateFilter === null || t.state === RECENT_STATE_OF[stateFilter])
        .filter((t) => profile === '' || t.runner_profile === profile)
        .filter((t) => needle === '' || matchesQuery(t, needle))
        .sort((a, b) => {
          if (failFirst) {
            const byFailed = Number(b.state === 'FAILED') - Number(a.state === 'FAILED')
            if (byFailed !== 0) return byFailed
          }
          if (startedSort !== null) return compareStarted(a, b, startedSort === 'asc')
          return a.updated_at < b.updated_at ? 1 : -1
        }),
    [page.tasks, shown, stateFilter, profile, needle, failFirst, startedSort],
  )

  // The segment's counts, from the loaded Recent rows, like the tab badges.
  const stateCounts = useMemo(() => {
    const recent = page.tasks.filter((t) => tabOf(t) === 'recent')
    return {
      all: recent.length,
      failed: recent.filter((t) => t.state === RECENT_STATE_OF.failed).length,
      cancelled: recent.filter((t) => t.state === RECENT_STATE_OF.cancelled).length,
      succeeded: recent.filter((t) => t.state === RECENT_STATE_OF.succeeded).length,
    }
  }, [page.tasks])

  // EVERY REPORT CARRIES THE TOGGLES (G2-22): a tab click that wrote only
  // `{ tab, state }` would clear `group=wf` from the address, and the
  // address is what the screen re-reads its toggles from.
  const report = (over: { tab?: Tab; state?: RecentState | null; grouped?: boolean; failedFirst?: boolean }) => {
    const t = over.tab ?? shown
    const st = over.state !== undefined ? over.state : recentState
    onList?.(agentList(t, st, { grouped: over.grouped ?? grouped, failedFirst: over.failedFirst ?? failedFirst }))
  }
  const chooseTab = (next: Tab) => {
    setTab(next)
    report({ tab: next })
  }
  const chooseState = (next: RecentState | null) => {
    setRecentState(next)
    report({ tab: 'recent', state: next })
  }
  const chooseGrouped = (next: boolean) => {
    setGrouped(next)
    report({ grouped: next })
  }
  const chooseFailedFirst = (next: boolean) => {
    setFailedFirst(next)
    report({ failedFirst: next })
  }
  // A ROW OPENS UNDER THE TAB IT CAME FROM (N9, owner QA 2026-10-04). The
  // landing tab is this screen's own state until a click reports one, so a
  // row on a Recent the list LANDED on opened `/agents/live/<id>` and the
  // panel lit `Live 0` beside the Recent rows. The tab on screen is reported
  // first, so App writes the agent's address under it.
  const openRow = (taskId: string) => {
    report({})
    onOpen(taskId)
  }

  // THE SCOPE OF EVERY FIGURE ABOVE, IN ONE QUALIFIER.
  //
  // Two sentences carried this -- "filters apply to the N loaded rows" above
  // the table and "More rows exist beyond this page. Counts and grouping above
  // describe only what is loaded." below it -- and they said the same thing
  // twice, about the same page, in two places a reader has to hold together.
  // It is one `.ctl-card-note`-shaped qualifier in the toolbar now, carrying
  // the figure that varies and the fact that there is more; the sentence is
  // its accessible name. A count that looks server-side but is not is the same
  // lie as an error rendered as an empty list, so the qualifier is never
  // conditional on there being a next page -- only its second half is.
  //
  // A PHONE PAGE THAT STOPPED SHORT SAYS SO IN §2.5's WORDS. At phone width
  // the read asks for `PHONE_PAGE_LIMIT` rows, and when more exist the list is
  // the most recent ones -- `showing the 50 most recent` -- not a page that
  // merely happens to end early.
  //
  // THE SEARCH AND THE SORT ARE NAMED IN IT (#99). A search that finds nothing
  // here has searched the loaded page, not the platform, and "failed first"
  // orders only the failures this page holds.
  const phonePage = page.asked < TASK_PAGE_LIMIT && Boolean(page.next_page_token)
  const scopeSay = phonePage
    ? `Every count, filter, search and sort on this screen runs over the ${page.tasks.length} most recent agents, not over the platform. At this width the list reads ${PHONE_PAGE_LIMIT} rows rather than ${TASK_PAGE_LIMIT}, because every row carries its input and its output and the list re-reads every few seconds. More rows exist beyond it.`
    : page.next_page_token
      ? `Every count, filter, search and sort on this screen runs over the ${page.tasks.length} rows loaded into this page, not over the platform. More rows exist beyond it.`
      : `Every count, filter, search and sort on this screen runs over the ${page.tasks.length} rows loaded into this page, not over the platform.`

  // A COUNT OVER A CAPPED PAGE IS A LOWER BOUND, AND SAYS SO (G2-05, dev QA
  // 2026-10-07). The same data read `Recent 191` at 1440 (200 rows loaded)
  // and `Recent 46` on a phone (50), each printed as if it were the
  // population. With a next page the figure is `191+`, and its accessible
  // name says what it is at least, among how many newest rows. The list route
  // has no total to print instead: a count per tab would be a Firestore
  // aggregation per state set on every poll, and this screen's figures are
  // the page's by design (the Overview counts over the same page, OV-10).
  const bounded = Boolean(page.next_page_token)
  const countText = (n: number): string => (bounded ? `${n}+` : String(n))
  const countSay = (n: number): string => `at least ${n} among the ${page.tasks.length} newest read; more rows exist beyond them`

  return (
    <>
      {/* THE SECTION'S PAGES AS A STRIP (agents.html V1; #503 at 390): Live,
          Waiting and Recent with their counts, underlined rather than boxed.
          On a phone it sticks under the header, so the reader can change tab
          from anywhere down the list. */}
      <div className="ag-list-tabs" role="tablist" aria-label="Agents">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={shown === t.id}
            aria-label={bounded ? `${t.say} — ${countSay(counts[t.id])}` : t.say}
            onClick={() => chooseTab(t.id)}
          >
            {t.label} <span className={`badge${bounded ? ' is-bound' : ''}`}>{countText(counts[t.id])}</span>
          </button>
        ))}
      </div>

      <div className="ctl-toolbar ag-list-head">
        {/* THE COLLAPSE TOGGLE IS THE LIST'S (#503): it sat as a boxed button
            at the top of the detail. « folds the list to its 64px strip of
            state marks and » brings it back, as `[` does; only beside an
            open agent, where there is a detail to give the room to. */}
        {openTaskId !== null && <AgCollapse />}

        {/* THE SCOPE RIDES WITH THE SEARCH (V147, visual QA #1038). It was the
            toolbar's last, `is-end` item, and whenever the toolbar wrapped
            `26 loaded` was stranded on a line of its own. It qualifies what
            the search searches, so it sits at the search box's end, on the
            one line that always takes the slack. */}
        <div className="ag-find">
          <input
            type="search"
            value={query}
            placeholder="Find by name or task id"
            aria-label="Find by name or task id"
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="ag-scope" aria-label={scopeSay}>
            {phonePage
              ? `showing the ${page.tasks.length} most recent`
              : `${page.tasks.length} loaded${page.next_page_token ? ' · more beyond' : ''}`}
          </span>
        </div>

        <label className="ag-filter">
          <span className="ag-filter-k">profile:</span>
          {/* CAPPED, NOT SIZED TO ITS LONGEST OPTION (V061): the whole value is
              its title. */}
          <select value={profile} title={profile === '' ? 'all' : profile} onChange={(e) => setProfile(e.target.value)}>
            <option value="">all</option>
            {profiles.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>

        {/* RECENT, BY STATE (OV-10): pressed buttons rather than tabs -- it
            filters the tab it sits beside, it is not a fourth tab. Counts are
            from the loaded rows, like the tab badges, and DEAD_LETTERED is
            never offered: nothing writes it (`agentlist.ts`). */}
        {shown === 'recent' && (
          <Segmented
            label="Recent, by state"
            value={recentState ?? 'all'}
            // A capped page's count is drawn in the label as `2+` with its
            // sentence as the title (G2-05): `SegOption.count` is a number.
            options={(['all', ...RECENT_STATES] as const).map((s) =>
              bounded
                ? {
                    key: s,
                    label: (
                      <>
                        {s}
                        <em className="is-bound" title={countSay(stateCounts[s])}>
                          {countText(stateCounts[s])}
                        </em>
                      </>
                    ),
                  }
                : { key: s, label: s, count: stateCounts[s] },
            )}
            onChange={(s) => chooseState(s === 'all' ? null : s)}
          />
        )}

        {shown === 'recent' && (
          <label className="ag-filter check">
            <input
              type="checkbox"
              checked={failedFirst}
              onChange={(e) => chooseFailedFirst(e.target.checked)}
            />
            Failed first
          </label>
        )}

        {shown !== 'live' && (
          <label className="ag-filter check">
            <input
              type="checkbox"
              checked={grouped}
              onChange={(e) => chooseGrouped(e.target.checked)}
            />
            Group by workflow
          </label>
        )}

        {/* SORTED BY START (#376): newest start, oldest start, then the
            list's own order. It was the Started column's head; the compact
            list has no columns, so it is a control of the list. */}
        <button
          type="button"
          className="ag-sort"
          aria-pressed={sort.started !== null}
          aria-label={
            sort.started === null
              ? 'Sort by start time'
              : sort.started === 'desc'
                ? 'Sorted by start, newest first'
                : 'Sorted by start, oldest first'
          }
          onClick={sort.onStarted}
        >
          Started{sort.started === 'desc' ? ' ↓' : sort.started === 'asc' ? ' ↑' : ''}
        </button>
      </div>

      {rows.length === 0 ? (
        // A REAL ZERO, DRAWN AS ONE. The mark is the fact -- two words, always
        // rendered, legible with every card shut -- and the sentence that used
        // to stand here is its accessible name. Which states cost nothing is
        // `#help/capacity`, read from the set that owns them.
        <div className="ctl-empty">
          <h3>
            <Mark
              kind="zero"
              say={
                needle !== ''
                  ? `No ${stateFilter !== null ? `${RECENT_STATE_OF[stateFilter]} ` : ''}agent in the loaded page of ${shown} matches “${query.trim()}”.`
                  : shown === 'live'
                  ? 'No agent is holding a pool slot right now. This is a real zero from a successful read, not a failed one.'
                  : shown === 'waiting'
                    ? 'Nothing is waiting. Waiting work costs nothing, so an empty tab here is normal.'
                      : stateFilter !== null
                        ? `No ${RECENT_STATE_OF[stateFilter]} agent is in the loaded page.`
                        : 'Nothing has finished in the loaded page.'
              }
            />{' '}
            {/* THIS SCREEN'S ONE `?` (B7.4), AND ONLY ON THE EMPTY LIVE TAB
                (AG-32). An empty `live` tab is the one place on this screen
                where the mark alone can still be read wrongly: "nothing is
                running" and "nothing costs anything" are not the same claim,
                and which states hold a pool slot is invariant 1 rather than
                anything the row could show. That is the test for a glyph -- a
                platform rule no label can carry -- and it is not met by an
                empty Waiting or Recent tab, which make no claim about cost:
                the capacity topic opened from those answered a question
                nobody there was asking. `prose.runs.test.tsx` pins it. */}
            {stateFilter !== null ? `nothing ${stateFilter} in ${shown}` : `nothing in ${shown}`}
            {needle !== '' && ` matching “${query.trim()}”`}
            {shown === 'live' && needle === '' && <HelpCard topic="capacity" />}
          </h3>
        </div>
      ) : shown === 'waiting' ? (
        <WaitingGroups rows={rows} now={now} vouched={vouched} onOpen={openRow} openTaskId={openTaskId} classes={classes} grouped={grouped} bounded={bounded} />
      ) : grouped && shown !== 'live' ? (
        <GroupedRows rows={rows} now={now} vouched={vouched} onOpen={openRow} openTaskId={openTaskId} classes={classes} />
      ) : (
        <FlatRows rows={rows} now={now} vouched={vouched} onOpen={openRow} openTaskId={openTaskId} classes={classes} />
      )}
    </>
  )
}

/**
 * The flat list. A row whose reason is the row above's, in the same workflow,
 * prints `same reason` and says the reason itself only to a screen reader
 * (#100, G2-24): the reader's eye already has it one line up.
 */
function FlatRows({
  rows,
  now,
  vouched,
  onOpen,
  openTaskId,
  classes,
}: {
  rows: Task[]
  /** The clock the row's elapsed and waiting ages run on. */
  now: number
  /** The clock capped at the read, for the silent-worker line (`rowClock`). */
  vouched: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
}) {
  const shared = flatShared(rows, rows.map((t) => rowReason(t, classes, vouched)))
  return (
    <div className="rows">
      {rows.map((t, i) => (
        <TaskRow
          key={t.id}
          task={t}
          now={now}
          vouched={vouched}
          onOpen={onOpen}
          open={t.id === openTaskId}
          classes={classes}
          whyShared={shared[i]}
        />
      ))}
    </div>
  )
}

/**
 * THE WAITING TAB, SPLIT BY REMEDY (owner decision 2026-10-01): "Needs
 * action" first -- a pool paused, set to zero, with no limit set, over its
 * ceiling (drift) or below this task's weight, or a park no timer ends -- then "No room", where
 * waiting is the answer. The split is `taskGroup` (types.ts), which files a
 * blocker by `blockerGroup`: the same rule Capacity's "Needs action" group
 * reads off the pools, so the two screens cannot disagree about a pool.
 *
 * Each group keeps the list's own order and, when "Group by workflow" is on,
 * groups its own rows by workflow. An empty group is not drawn.
 */
function WaitingGroups({
  rows,
  now,
  vouched,
  onOpen,
  openTaskId,
  classes,
  grouped,
  bounded,
}: {
  rows: Task[]
  now: number
  vouched: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
  grouped: boolean
  /** More rows exist beyond the page, so a group's count is a lower bound (G2-05). */
  bounded: boolean
}) {
  // A PARKED TASK IS FILED UNDER ITS OWN PARK REASON (N12, owner QA
  // 2026-10-04): `/agents/waiting` headed a step waiting on an earlier step
  // `No room 1`, while its row said why it waited -- and nothing was short of
  // room. `No room` is for a task the pools refused; a park is grouped by the
  // reason the writer recorded, in the pill's words (`PARK_WORD`), and a
  // reason this client does not know is its raw value, never hidden.
  const groups: { key: string; title: string; rows: Task[] }[] = [
    { key: 'needs_action', title: 'Needs action', rows: [] },
    { key: 'no_room', title: 'No room', rows: [] },
  ]
  for (const t of rows) {
    const at = taskGroup(t, classUnits(classes, t.resource_class)) === 'needs_action' ? { key: 'needs_action', title: '' } : waitGroupOf(t)
    let g = groups.find((x) => x.key === at.key)
    if (g === undefined) {
      g = { key: at.key, title: at.title, rows: [] }
      groups.push(g)
    }
    g.rows.push(t)
  }
  return (
    <>
      {groups.map((g) =>
        g.rows.length === 0 ? null : (
          <section className={`section ag-wait-group${g.key === 'needs_action' ? ' needs-action' : ''}`} key={g.key} data-group={g.key}>
            <h2>
              {g.title}{' '}
              <span
                className="ag-scope"
                aria-label={bounded ? `at least ${g.rows.length}: counted over the loaded page, and more rows exist beyond it` : undefined}
              >
                {bounded ? `${g.rows.length}+` : g.rows.length}
              </span>
            </h2>
            {grouped ? (
              <GroupedRows rows={g.rows} now={now} vouched={vouched} onOpen={onOpen} openTaskId={openTaskId} classes={classes} />
            ) : (
              <FlatRows rows={g.rows} now={now} vouched={vouched} onOpen={onOpen} openTaskId={openTaskId} classes={classes} />
            )}
          </section>
        ),
      )}
    </>
  )
}

/** The Waiting group a task that needs no action is filed under: its park reason, or `No room`. */
export function waitGroupOf(t: Task): { key: string; title: string } {
  if (t.state !== 'PARKED') return { key: 'no_room', title: 'No room' }
  const r = t.park_reason ?? null
  if (r === null) return { key: 'park:none', title: 'Parked, reason not recorded' }
  const word = (PARK_WORD as Readonly<Record<string, string>>)[r] ?? r
  return { key: `park:${r}`, title: word.charAt(0).toUpperCase() + word.slice(1) }
}

/**
 * Recent's search (#99): the task id, the step id and the workflow id,
 * case-insensitively. `needle` is already trimmed and lower-cased.
 */
function matchesQuery(t: Task, needle: string): boolean {
  return [t.id, t.step_id, t.workflow_id].some((v) => v != null && v.toLowerCase().includes(needle))
}

/** A row's why line and whether it asks a person to act -- `TaskRow`'s own reading. */
type RowReason = { text: string; warn: boolean }

/**
 * A ROW WHOSE MARK IS DRAWN IN --bad (marks.tsx `STATE_MARK` hue `bad`):
 * FAILED and DEAD_LETTERED. Its reason takes the same red (V146), so the
 * mark, the leading rule and the sentence say one thing.
 */
export function failedRow(task: Pick<Task, 'state'>): boolean {
  return task.state === 'FAILED' || task.state === 'DEAD_LETTERED'
}

function rowReason(task: Task, classes: ResourceClasses | null, now: number): RowReason {
  // A SILENT WORKER FIRST (#179, AG-14's fourth kind): `whyAgent` writes
  // nothing for a task that holds a slot, and this is the one thing about one
  // that needs a person.
  const silent = silentWorkerLine(task, now)
  if (silent !== null) return { text: silent, warn: true }
  // THE TASK'S WEIGHT, FROM THE CATALOGUE (#66) -- never `RESOURCE_UNITS`,
  // which is what fed `whyAgent`/`whyNeedsAction` a `full` reading for a pool
  // too small to ever admit this task. `classUnits` is null exactly when the
  // row's display units also are: the catalogue read is the same one and the
  // display badge keeps its own bundled fallback.
  const units = classUnits(classes, task.resource_class)
  return { text: whyAgent(task, units), warn: whyNeedsAction(task, units) }
}

/**
 * THE LIST'S SILENT-WORKER LINE (#179), or null. AG-14 (#82) names a stuck or
 * silent worker as one of the four kinds a `--warn` why line is for; the
 * inspector draws it from the task's events (`SilentWorker` in
 * AgentDetail.tsx), and a list row draws it from the heartbeat the task routes
 * now carry on each lease-holding row (`swarm_api/heartbeats.py`).
 *
 * THE RECONCILER'S RULE, NOT A GUESS: beaten at least once and quiet for
 * LONGER THAN `heartbeat_grace_seconds` -- the row's own grace, resolved
 * server-side the way the reconciler resolves it (`routes/leases.py` judges
 * `silent_seconds > grace` too). No constant here: a reconciler configured to
 * 300s would otherwise be second-guessed at 90.
 *
 * NO LINE WHEN THE ROW CANNOT SAY: a `heartbeat` other than `'read'` (the
 * lease read failed, or there is no lease) is "could not look", never "nothing
 * happened"; a null `heartbeat_at` is a lease that has never beaten -- a
 * booting worker, which the reconciler judges by its dispatch deadline
 * instead -- and an older API sends none of the three fields at all.
 *
 * `now` is the list's VOUCHED clock (`rowClock`), which stops advancing once
 * the page is older than a poll, so a list that stopped reading does not age
 * a beat it can no longer see into silence. The row's elapsed figure runs on
 * the uncapped clock (G2-04); this line does not.
 */
export function silentWorkerLine(task: Task, now: number): string | null {
  if (!CONCURRENCY_STATES.has(task.state)) return null
  if (task.heartbeat !== 'read') return null
  const grace = task.heartbeat_grace_seconds
  if (task.heartbeat_at == null || typeof grace !== 'number') return null
  const beat = Date.parse(task.heartbeat_at)
  if (!Number.isFinite(beat)) return null
  const quiet = Math.floor((now - beat) / 1000)
  if (!(quiet > grace)) return null
  const mins = Math.floor(quiet / 60)
  const span = mins >= 1 ? `${mins}m` : `${quiet}s`
  return `No heartbeat for ${span}. The worker may be gone; the reconciler reclaims a stale lease.`
}

/**
 * WHICH REASONS CAN BE SAID ONCE (#100). Only a routine one: a line that asks
 * a person to act is on every row it applies to, whatever its neighbours say,
 * because that is the line nobody may miss. An empty reason is never shared.
 */
function shareable(r: RowReason): boolean {
  return r.text !== '' && !r.warn
}

/**
 * The flat list: a row whose reason equals the previous row's, IN THE SAME
 * WORKFLOW, hides it (G2-24, dev QA 2026-10-07). Across workflows it hid too,
 * so a row of another workflow looked as if it had no reason at all. A row
 * with no workflow never shares: two lone tasks are not one group.
 */
function flatShared(rows: readonly Task[], reasons: RowReason[]): boolean[] {
  return reasons.map((r, i) => {
    if (i === 0 || !shareable(r) || reasons[i - 1]!.text !== r.text) return false
    const wf = rows[i]!.workflow_id ?? null
    return wf !== null && rows[i - 1]!.workflow_id === wf
  })
}

/**
 * A workflow group: every run of two or more consecutive rows with one
 * routine reason moves that reason to the header, with the number of rows it
 * covers. Two runs of the same reason in one group are one header line.
 */
function groupReasons(reasons: RowReason[]): {
  shared: boolean[]
  reasons: { text: string; count: number }[]
} {
  const shared = reasons.map(() => false)
  const said: { text: string; count: number }[] = []
  let i = 0
  while (i < reasons.length) {
    let j = i + 1
    while (j < reasons.length && reasons[j]!.text === reasons[i]!.text && shareable(reasons[j]!)) j++
    if (shareable(reasons[i]!) && j - i >= 2) {
      for (let k = i; k < j; k++) shared[k] = true
      const known = said.find((s) => s.text === reasons[i]!.text)
      if (known) known.count += j - i
      else said.push({ text: reasons[i]!.text, count: j - i })
    }
    i = j
  }
  return { shared, reasons: said }
}

/**
 * "Agents grouped by workflow" -- the list half of the request.
 *
 * Client-side over the loaded page, and labelled as such: a header saying
 * "3 steps" when the workflow has nine and six fell off page one is the same
 * lie in a new place.
 */
function GroupedRows({
  rows,
  now,
  vouched,
  onOpen,
  openTaskId,
  classes,
}: {
  rows: Task[]
  now: number
  vouched: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
}) {
  const groups = useMemo(() => {
    const m = new Map<string, Task[]>()
    for (const t of rows) {
      const key = t.workflow_id ?? ''
      const list = m.get(key)
      if (list) list.push(t)
      else m.set(key, [t])
    }
    // Standalone agents collect under one group, last.
    return Array.from(m.entries()).sort(([a], [b]) =>
      a === '' ? 1 : b === '' ? -1 : a.localeCompare(b),
    )
  }, [rows])

  return (
    <>
      {groups.map(([wf, tasks]) => {
        const roll = rollupState(tasks.map((t) => t.state))
        const said = groupReasons(tasks.map((t) => rowReason(t, classes, vouched)))
        return (
          <section className="section group" key={wf || 'standalone'}>
            <h2>
              {/* B17: the same workflow id the Workflows screen prints, and
                  the same reason it is not uppercased here either. "No
                  workflow" is a sentence, not an id, so it is not wrapped. */}
              {/* ONE CLICK TO THE WORKFLOW (#94): the id is a link to its
                  page. `N here` below stays the honest count until a
                  per-workflow read exists. */}
              {wf === '' ? (
                'No workflow'
              ) : (
                <a className="ctl-link" href={workflowHref(wf)}>
                  <Id>{wf}</Id>
                </a>
              )}
              {/* THE ROLLUP PILL WAS THE LAST FILLED PILL ON THIS SCREEN.
                  `.roll` was a 999px pill with an 18%-tint background and the
                  word in `--*-ink`, uppercase, tracked, 600 -- the exact
                  silhouette §6.6 measured at 102x23px and replaced. It is the
                  same kind of fact as every other state here, so it is drawn
                  the same way. `.roll`'s four rules went with it; nothing else
                  rendered them. */}
              <ToneMark tone={rollTone(roll)}>{roll}</ToneMark>
              {/* THE FIGURE IS THE FACT. "3 steps in this page" said `3` and
                  then re-said, in four more words, the thing the toolbar's
                  scope qualifier already says once for the whole screen. A
                  header saying "3 steps" when the workflow has nine and six
                  fell off page one is the lie this qualifier exists to
                  prevent, and the count plus the mark is the shape of it. */}
              <span
                className="ag-scope"
                aria-label={`${tasks.length} of this workflow's steps are in the loaded page. The workflow may have more; this grouping is client-side over what was loaded.`}
              >
                {tasks.length} here
              </span>
            </h2>
            {/* A REASON THE GROUP'S ROWS SHARE, SAID ONCE (#100). Twenty-one
                parked steps each printing "Waiting on an earlier step in its
                workflow." is twenty-one lines of one sentence; it is said here
                with its count, and the rows it covers keep it in their
                accessible name only. */}
            {said.reasons.map((r) => (
              <p className="group-why" key={r.text}>
                <b>{r.count}</b> {r.text}
              </p>
            ))}
            <div className="rows">
              {tasks.map((t, i) => (
                <TaskRow
                  key={t.id}
                  task={t}
                  now={now}
                  vouched={vouched}
                  onOpen={onOpen}
                  open={t.id === openTaskId}
                  classes={classes}
                  whyShared={said.shared[i]}
                />
              ))}
            </div>
          </section>
        )
      })}
    </>
  )
}

/**
 * The accessible name of the attempts cell, and whether it is over its cap.
 *
 * OVER THE CEILING IS `>`, NOT `>=` (AG-15). `3/3` is a task that used every
 * attempt it was allowed -- normal for anything that failed -- while `4/3` and
 * `83/3` are a task that ran MORE times than its cap: the retry-cap defect
 * `task_d18d8d8b044d469cb43c` hit at 83 attempts. Only the second is a
 * problem with the platform, and only it gets the treatment and the overage
 * in words.
 */
export function attemptsUsed(task: Pick<Task, 'attempt_count' | 'max_attempts'>): {
  over: boolean
  say: string
} {
  const over = task.attempt_count > task.max_attempts
  return {
    over,
    say: over
      ? `${task.attempt_count} attempts against a cap of ${task.max_attempts}: ${task.attempt_count - task.max_attempts} over the ceiling`
      : `${task.attempt_count} of ${task.max_attempts} attempts used`,
  }
}

/** The qualifier on a CANCELLED row's elapsed figure (#163); `TaskRow` says why. */
export const CANCEL_SPAN = '(last start to cancel, may include parked time)'
/** The mark line two carries for it; the sentence is its title (U10a D20). */
export const CANCEL_NOTE = 'to cancel'

/**
 * ONE ROW OF THE LIST, AND IT IS ALWAYS THE COMPACT ROW (agents.html V1).
 *
 * THE TEN-COLUMN TABLE IS GONE (#503). With no agent open the list was a
 * full-width table -- state, agent, owner, step, started, elapsed, account,
 * try, class, badges -- and at 1440 its Agent column measured 26px: the name
 * was one character and an ellipsis, the head read "Agen", and the dispatch
 * badge sat clipped in a 15px cell. V1 is one list at every width: the mark,
 * the name and the elapsed time, then profile · owner · try, then the reason.
 * The rest -- when it started, the account, the class, the dispatch -- is the
 * detail's, one click away, where it has room.
 */
export function TaskRow({
  task,
  now,
  vouched = now,
  onOpen,
  open = false,
  classes,
  whyShared = false,
}: {
  task: Task
  /** The clock the elapsed figure runs on. */
  now: number
  /** The clock capped at the read, for the silent-worker line; `now` when not given. */
  vouched?: number
  onOpen: (taskId: string) => void
  /** This is the agent the inspector has open (AG-17). */
  open?: boolean
  /** The resource-class catalogue, for #66's below-units verdict. Read once per list, not per row. */
  classes: ResourceClasses | null
  /** The reason is said once for this row and its neighbours (#100); keep it for a screen reader only. */
  whyShared?: boolean
}) {
  const why = rowReason(task, classes, vouched)
  const whyHidden = whyShared && !why.warn
  return <CompactRow task={task} now={now} onOpen={onOpen} open={open} why={why} whyHidden={whyHidden} />
}

/**
 * « AND », IN THE LIST'S HEADER (agents.html V1). One store with the divider
 * (listSnap.ts), so the toggle, the divider and `[` move one value.
 */
function AgCollapse() {
  const snap = useListSnap()
  const folded = snap === 'strip'
  return (
    <button
      type="button"
      className="ag-collapse"
      aria-pressed={folded}
      aria-label={folded ? 'Open the list again ([)' : 'Fold the list to a strip ([)'}
      title={folded ? 'Open the list again ([)' : 'Fold the list to a strip ([)'}
      onClick={toggleListSnap}
    >
      {/* THE GLYPH ALONE (V147): the boxed `[` key hint beside it read as
          `« [`, half a bracket pair. The key is in the title and the name. */}
      {folded ? '»' : '«'}
    </button>
  )
}

/**
 * WHETHER THE LIST IS FOLDED TO THE 64px STRIP. `AgentSplit` writes the snap
 * (listSnap.ts) on the root as `data-agent-list`, which is also what
 * the sheet folds the list by; reading the same attribute here keeps the row
 * and the sheet on one answer rather than two copies of the snap.
 */
function stripFolded(): boolean {
  return typeof document !== 'undefined' && document.documentElement.dataset.agentList === 'strip'
}

/** Where the hover card is drawn: beside the strip row, in viewport pixels. */
type CardAt = { top: number; left: number }

/**
 * THE COMPACT ROW (agents.html V1, decided 2026-10-01): two lines, as drawn.
 *
 *   line 1  the state mark, the agent's name, its elapsed time
 *   line 2  profile (and model) · owner · `try n/m` on a live row
 *   then    the reason it waits or stopped, where there is one -- never
 *           dropped, for the reason `TaskRow` gives (the phone question)
 *
 * THE NAME IS THE STEP, OR THE TASK ID. A task carries no title; the step id
 * is the name a workflow gave it (#94), and a lone task is named by the id
 * prefix the rest of the product prints (`shortTaskId`). Either way the whole
 * id is printed under the name, with its copy (`TaskIdLine`, #94).
 *
 * FOLDED TO THE STRIP, the sheet leaves only the mark (`:root[data-agent-list
 * ='strip']`), and a mark alone says nothing about which agent it is. So
 * hovering or focusing the row draws a card beside it -- name, state, elapsed,
 * profile and owner -- and ↑/↓ move between rows, which is how a keyboard
 * reader walks a column of marks. The card is in a portal on `document.body`
 * because the list column clips and the strip hides every child of the row
 * but the mark; it is `role="tooltip"` and the row's `aria-describedby` while
 * it is up.
 */
function CompactRow({
  task,
  now,
  onOpen,
  open,
  why,
  whyHidden,
}: {
  task: Task
  now: number
  onOpen: (taskId: string) => void
  open: boolean
  why: RowReason
  whyHidden: boolean
}) {
  const [card, setCard] = useState<CardAt | null>(null)
  const el = elapsed(task, now)
  // The same qualifier as the full row's (#163). Line one of the compact row
  // has room for the figure only, so here it is the cell's `title`: the figure
  // is not presented as run time without saying what it spans.
  const cancelSpan = task.state === 'CANCELLED' && el.phase === 'ran'
  const tries = attemptsUsed(task)
  // NAMED FROM WHAT IT IS (walkthrough G): its step, else `browser check ·
  // 02e9705a` -- it was the bare hash. `agentName`'s rule, drawn below.
  const name = agentName(task)
  const owner = task.submitted_by?.split('@')[0] ?? null
  const profile = task.model ? `${task.runner_profile} · ${task.model}` : task.runner_profile
  const live = tabOf(task) === 'live'
  // THE ONE FACT THAT CONTRADICTS THE STATE WORD: a stop is requested and the
  // lease is still held (invariant 3), so the row still reads RUNNING.
  const cancelling = task.cancel_requested && !TERMINAL_STATES.has(task.state)
  const cardId = `ag-card-${task.id}`

  const show = (row: HTMLElement) => {
    if (!stripFolded()) return
    const r = row.getBoundingClientRect()
    setCard({ top: r.top, left: r.right + 8 })
  }
  const hide = () => setCard(null)
  const move = (row: HTMLElement, step: 1 | -1): boolean => {
    const scope: ParentNode = row.closest('.work') ?? row.ownerDocument
    const all = [...scope.querySelectorAll<HTMLElement>('.row.is-compact')]
    const next = all[all.indexOf(row) + step]
    if (next === undefined) return false
    next.focus()
    return true
  }

  return (
    <div
      className="row clickable is-compact"
      role="button"
      tabIndex={0}
      data-task-id={task.id}
      aria-current={open ? 'true' : undefined}
      aria-describedby={card !== null ? cardId : undefined}
      onClick={() => onOpen(task.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen(task.id)
          return
        }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          if (move(e.currentTarget, e.key === 'ArrowDown' ? 1 : -1)) e.preventDefault()
        }
      }}
      onMouseEnter={(e) => show(e.currentTarget)}
      onMouseLeave={hide}
      onFocus={(e) => show(e.currentTarget)}
      onBlur={hide}
    >
      <StateMark state={task.state} />
      <span className="agent cr-name">
        {/* The whole id is the name's hover; a lone task's short id is
            the id treatment, as a step's is on line two. */}
        <b title={task.id}>
          {task.step_id ?? (
            <>
              {agentKind(task.runner_profile)} · <span className="id" title={task.id}>{shortTaskId(task.id)}</span>
            </>
          )}
        </b>
        {/* THE WHOLE ID, PRINTED, under the name (#94): it was a hover
            title, and an id read off the screen had to be hovered for. */}
        <TaskIdLine id={task.id} />
        {whyHidden && <span className="why is-shared">{why.text}</span>}
      </span>
      <span
        className={`when${el.ticking ? ' ticking' : ''}`}
        title={cancelSpan ? `${el.text} ${CANCEL_SPAN}` : undefined}
      >
        {el.text}
      </span>
      <span className="cr-sub">
        <span className="cr-profile">{profile}</span>
        {' · '}
        <span className="cr-owner" title={task.submitted_by ?? undefined}>
          {owner ?? <Em />}
        </span>
        {/* THE TRY ON A LIVE ROW, as V1 draws it -- and on any row whose
            count is over its cap (AG-15), which a finished row must still
            show: that is the platform running past its own ceiling. */}
        {(live || tries.over) && (
          <>
            {' · '}
            <span className={`cr-try${tries.over ? ' is-over' : ''}`} aria-label={tries.say}>
              try {task.attempt_count}/{task.max_attempts}
            </span>
          </>
        )}
        {cancelling && (
          <>
            {' · '}
            <ToneMark tone="wait">cancelling</ToneMark>
          </>
        )}
        {/* WHAT A CANCELLED RUN'S FIGURE SPANS (#163), as a short mark on
            line two whose title is the whole sentence (U10a D20, owner QA
            2026-10-04): the sentence itself pushed the hash and the rest of
            the line off the row. It is LAST, so it is the part that gives way. */}
        {cancelSpan && (
          <>
            {' · '}
            <span className="when-note" title={CANCEL_SPAN} aria-label={CANCEL_SPAN}>
              {CANCEL_NOTE}
            </span>
          </>
        )}
      </span>
      {/* THE REASON IS CLAMPED AND WHOLE IN ITS TITLE (V061): a failed row
          printed its whole multi-line error. ON A FAILED ROW IT IS RED, as the
          mark and the rule beside it are (V146): it was --warn amber, a third
          colour for one fact. */}
      {why.text && !whyHidden && (
        <span className={`why${why.warn ? ' is-warn' : ''}${failedRow(task) ? ' is-bad' : ''}`} title={why.text}>
          {why.text}
        </span>
      )}
      {/* A HIDDEN REASON SAYS THAT IT IS ONE (G2-24, dev QA 2026-10-07): the
          row looked as if it had none. Muted, on the reason's own line; the
          sentence is its title for a pointer, and a screen reader already
          hears it from the visually-hidden copy in the name cell. */}
      {whyHidden && (
        <span className="why is-same" aria-hidden="true" title={why.text}>
          same reason
        </span>
      )}
      {/* THE STRIP ROW'S NAME (owner QA R7, 2026-10-04): folded to 64px the
          row draws only its mark, and its accessible name was the state word.
          Drawn only in the strip (styles/agents.css), for a screen reader;
          a pointer gets the same from the strip's hover card. */}
      <span className="cr-vh">{name} · {task.id}</span>
      {card !== null &&
        createPortal(
          <div id={cardId} role="tooltip" className="ag-hovcard" style={{ top: card.top, left: card.left }}>
            <span className="ag-hovcard-head">
              <StateMark state={task.state} />
              <span className="ag-hovcard-when">{el.text}</span>
            </span>
            <b>{name}</b>
            <small>
              {profile} · {owner ?? 'no owner recorded'}
            </small>
            <small className="ag-hovcard-hint">click to open · ↑↓ to move</small>
          </div>,
          document.body,
        )}
    </div>
  )
}
