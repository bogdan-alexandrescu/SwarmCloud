import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import { createPortal } from 'react-dom'
// `Em` and `Mark` live in AgentDetail.tsx, which is where `ABSENT_MARK` was
// written and which design-system.md §9.1 names as the source to promote from.
// One definition for the four screens of this group; a second copy of a mark
// whose whole job is to be recognisable is a contradiction in terms.
// `Chip` joins them for the same reason: §9.3 of design-system.md counted four
// status chips in this product and asked for one, and the rebuilt `.ctl-chip`
// is only a rebuild if the screens stop hand-rolling their own.
import { Chip, Em, Mark, type ChipTone } from './AgentDetail'
import { PHONE_PAGE_LIMIT, RECENT_STATES, RECENT_STATE_OF, workflowHref, type AgentList, type RecentState } from './agentlist'
import { TASK_PAGE_LIMIT, loadTasks, type ResourceClasses } from './api'
import { classUnits, useResourceClasses } from './Blockers'
import type { Result } from './fetch'
import { HelpCard, phoneWidth } from './HelpCard'
import { toggleListSnap, useListSnap } from './listSnap'
import './styles/agents.css'
import { Segmented } from './components'
import { Id, Screen } from './Shell'
import { rowClock, useNow } from './useNow'
import {
  CONCURRENCY_STATES,
  TERMINAL_STATES,
  compareStarted,
  elapsed,
  rollupState,
  stateTone,
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
 * `'live'` when every tab is empty, which the screen does not reach: an empty
 * page renders the Screen's own empty state before this body mounts.
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
function shortTaskId(id: string): string {
  const cut = id.indexOf('_')
  return (cut === -1 ? id : id.slice(cut + 1)).slice(0, 8)
}

/**
 * HOW OFTEN THIS LIST IS WORTH RE-READING, from what the last read held.
 *
 * docs/web-ui/03-agents-and-workflows.md §2.5: 5s while the Live tab holds any
 * row, 30s otherwise. A live agent changes state on the order of seconds -- it
 * finishes, it is reclaimed, its cancel lands -- and a waiting or finished one
 * does not. Hidden tabs do not read at all; that half is `Screen`'s.
 *
 * THE COST IS THE PAYLOAD, NOT THE QUERY. Every row carries `input`,
 * `metadata` and `result_summary`, so a 200-row page is 2-4 MB (§2.5), and at
 * 5s that is roughly 600 KB/s. Only the Live tab earns the fast cadence, and
 * only while it has rows; the `view=summary` parameter §8/P2 proposes is what
 * would make it cheap. Until it exists, a phone reads `PHONE_PAGE_LIMIT` rows
 * rather than 200 -- see below.
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
 * the 50 most recent' rather than ship a 4 MB poll." `view=summary` does not
 * exist (routes/tasks.py and codec.py take no such parameter), and the list
 * polled the full 200-row page on every viewport -- 2-4 MB every 5s over
 * cellular, for as long as Live held a row and the tab was visible.
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
  const read = await loadTasks(asked)
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
  // THE ADDRESS WINS WHEN IT NAMES A TAB, and only then. Keyed on the two
  // values rather than the object, which App rebuilds on every route.
  const listTab = list?.tab ?? null
  const listState = list?.state ?? null
  useEffect(() => {
    if (listTab === null) return
    setTab(listTab)
    setRecentState(listTab === 'recent' ? listState : null)
  }, [listTab, listState])
  const [profile, setProfile] = useState<string>('')
  const [grouped, setGrouped] = useState(false)
  // RECENT'S SEARCH AND SORT (#99). Held here beside `profile`, not in the
  // address: a query is a reader's scratch, and `agentlist.ts` owns only the
  // tab and the state segment.
  const [query, setQuery] = useState('')
  const [failedFirst, setFailedFirst] = useState(false)

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
      empty={{
        // ONE `real zero`, AND IT IS THE PRIMITIVE'S. This heading read
        // `No agents · real zero` beside the mark `Absent` already draws in
        // the heading (#145), so the empty state said it twice -- and only
        // the mark has a sentence behind it. `emptystate.onemark.test.tsx`.
        heading: 'No agents',
        // ONE SENTENCE, WHICH IS WHAT §6.9 ALLOWS AN EMPTY STATE. The second
        // sentence -- "nothing has been submitted under this tenant, or
        // everything has aged out of the page" -- was two guesses about a
        // cause this screen cannot see, and the link is where a reader finds
        // out which states a page holds.
        // NO `?` HERE (B7.4). The heading beside this sentence already reads
        // `real zero · No agents`, which is the whole of what `tenant-scope`
        // was guarding against -- a reader taking an empty list for a failed
        // read. Whose agents these are is the crumb and the provenance line
        // above, and the topic is one click away in the rail's Help section.
        // This screen keeps exactly one glyph, on the empty TAB below, where
        // the claim being made is about capacity rather than about the read.
        body: <>The read succeeded and returned nothing.</>,
      }}
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
  // not each hold their own interval -- and it stops advancing the rows once
  // they are older than one poll interval (`rowClock`).
  // The SHARED 1s clock (useNow.ts), the cadence a running duration asks for:
  // the inspector's `run` ticks on the same instant, so a row and the drawer
  // beside it cannot disagree by a tick.
  const now = rowClock(useNow(1000), readAt, interval)

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

  const chooseTab = (next: Tab) => {
    setTab(next)
    onList?.({ tab: next, state: next === 'recent' ? recentState : null })
  }
  const chooseState = (next: RecentState | null) => {
    setRecentState(next)
    onList?.({ tab: 'recent', state: next })
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
            aria-label={t.say}
            onClick={() => chooseTab(t.id)}
          >
            {t.label} <span className="badge">{counts[t.id]}</span>
          </button>
        ))}
      </div>

      <div className="ctl-toolbar ag-list-head">
        {/* THE COLLAPSE TOGGLE IS THE LIST'S (#503): it sat as a boxed button
            at the top of the detail. « folds the list to its 64px strip of
            state marks and » brings it back, as `[` does; only beside an
            open agent, where there is a detail to give the room to. */}
        {openTaskId !== null && <AgCollapse />}

        <label className="ag-find">
          <input
            type="search"
            value={query}
            placeholder="Find by name or task id"
            aria-label="Find by name or task id"
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>

        <label className="ag-filter">
          <span className="ag-filter-k">profile:</span>
          <select value={profile} onChange={(e) => setProfile(e.target.value)}>
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
            options={(['all', ...RECENT_STATES] as const).map((s) => ({ key: s, label: s, count: stateCounts[s] }))}
            onChange={(s) => chooseState(s === 'all' ? null : s)}
          />
        )}

        {shown === 'recent' && (
          <label className="ag-filter check">
            <input
              type="checkbox"
              checked={failedFirst}
              onChange={(e) => setFailedFirst(e.target.checked)}
            />
            Failed first
          </label>
        )}

        {shown !== 'live' && (
          <label className="ag-filter check">
            <input
              type="checkbox"
              checked={grouped}
              onChange={(e) => setGrouped(e.target.checked)}
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

        <span className="is-end ag-scope" aria-label={scopeSay}>
          {phonePage
            ? `showing the ${page.tasks.length} most recent`
            : `${page.tasks.length} loaded${page.next_page_token ? ' · more beyond' : ''}`}
        </span>
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
        <WaitingGroups rows={rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} grouped={grouped} />
      ) : grouped && shown !== 'live' ? (
        <GroupedRows rows={rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} />
      ) : (
        <FlatRows rows={rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} />
      )}
    </>
  )
}

/**
 * The flat list. A row whose reason is the row above's says it only to a
 * screen reader (#100): the reader's eye already has it one line up.
 */
function FlatRows({
  rows,
  now,
  onOpen,
  openTaskId,
  classes,
}: {
  rows: Task[]
  now: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
}) {
  const shared = flatShared(rows.map((t) => rowReason(t, classes)))
  return (
    <div className="rows">
      {rows.map((t, i) => (
        <TaskRow
          key={t.id}
          task={t}
          now={now}
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
  onOpen,
  openTaskId,
  classes,
  grouped,
}: {
  rows: Task[]
  now: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
  grouped: boolean
}) {
  const split = { needs_action: [] as Task[], no_room: [] as Task[] }
  for (const t of rows) split[taskGroup(t, classUnits(classes, t.resource_class))].push(t)
  const groups = [
    { key: 'needs_action', title: 'Needs action', rows: split.needs_action },
    { key: 'no_room', title: 'No room', rows: split.no_room },
  ] as const
  return (
    <>
      {groups.map((g) =>
        g.rows.length === 0 ? null : (
          <section className={`section ag-wait-group${g.key === 'needs_action' ? ' needs-action' : ''}`} key={g.key} data-group={g.key}>
            <h2>
              {g.title} <span className="ag-scope">{g.rows.length}</span>
            </h2>
            {grouped ? (
              <GroupedRows rows={g.rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} />
            ) : (
              <FlatRows rows={g.rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} />
            )}
          </section>
        ),
      )}
    </>
  )
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

function rowReason(task: Task, classes: ResourceClasses | null): RowReason {
  // THE TASK'S WEIGHT, FROM THE CATALOGUE (#66) -- never `RESOURCE_UNITS`,
  // which is what fed `whyAgent`/`whyNeedsAction` a `full` reading for a pool
  // too small to ever admit this task. `classUnits` is null exactly when the
  // row's display units also are: the catalogue read is the same one and the
  // display badge keeps its own bundled fallback.
  const units = classUnits(classes, task.resource_class)
  return { text: whyAgent(task, units), warn: whyNeedsAction(task, units) }
}

/**
 * WHICH REASONS CAN BE SAID ONCE (#100). Only a routine one: a line that asks
 * a person to act is on every row it applies to, whatever its neighbours say,
 * because that is the line nobody may miss. An empty reason is never shared.
 */
function shareable(r: RowReason): boolean {
  return r.text !== '' && !r.warn
}

/** The flat list: a row whose reason equals the previous row's hides it. */
function flatShared(reasons: RowReason[]): boolean[] {
  return reasons.map((r, i) => i > 0 && shareable(r) && reasons[i - 1]!.text === r.text)
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
  onOpen,
  openTaskId,
  classes,
}: {
  rows: Task[]
  now: number
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
        const said = groupReasons(tasks.map((t) => rowReason(t, classes)))
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
              <Chip tone={rollTone(roll)}>{roll}</Chip>
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
function TaskRow({
  task,
  now,
  onOpen,
  open = false,
  classes,
  whyShared = false,
}: {
  task: Task
  now: number
  onOpen: (taskId: string) => void
  /** This is the agent the inspector has open (AG-17). */
  open?: boolean
  /** The resource-class catalogue, for #66's below-units verdict. Read once per list, not per row. */
  classes: ResourceClasses | null
  /** The reason is said once for this row and its neighbours (#100); keep it for a screen reader only. */
  whyShared?: boolean
}) {
  const why = rowReason(task, classes)
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
      {folded ? '»' : '«'} <kbd>[</kbd>
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
 * prefix the rest of the product prints (`shortTaskId`). The full id is in the
 * title either way, and on line 2 when the step is the name.
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
  const name = task.step_id ?? shortTaskId(task.id)
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
      <Chip tone={stateTone(task.state)} state={task.state}>{task.state}</Chip>
      <span className="agent cr-name">
        {task.step_id ? (
          <b title={task.id}>{name}</b>
        ) : (
          <b className="id" title={task.id}>
            {name}
          </b>
        )}
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
        {task.step_id && (
          <>
            {' · '}
            <span className="id" title={task.id}>
              {shortTaskId(task.id)}
            </span>
          </>
        )}
        {cancelling && (
          <>
            {' · '}
            <Chip tone="wait">cancelling</Chip>
          </>
        )}
        {/* WHAT A CANCELLED RUN'S FIGURE SPANS (#163), in words on line two:
            line one has room for the figure only. */}
        {cancelSpan && (
          <>
            {' · '}
            <span className="when-note">{CANCEL_SPAN}</span>
          </>
        )}
      </span>
      {why.text && !whyHidden && <span className={`why${why.warn ? ' is-warn' : ''}`}>{why.text}</span>}
      {card !== null &&
        createPortal(
          <div id={cardId} role="tooltip" className="ag-hovcard" style={{ top: card.top, left: card.left }}>
            <span className="ag-hovcard-head">
              <Chip tone={stateTone(task.state)} state={task.state}>{task.state}</Chip>
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
