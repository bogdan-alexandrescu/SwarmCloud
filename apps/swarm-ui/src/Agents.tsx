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
import { PHONE_PAGE_LIMIT, RECENT_STATES, RECENT_STATE_OF, type AgentList, type RecentState } from './agentlist'
import { TASK_PAGE_LIMIT, loadTasks, type ResourceClasses } from './api'
import { classUnits, useResourceClasses } from './Blockers'
import { DispatchChip } from './Dispatch'
import type { Result } from './fetch'
import { HelpCard, phoneWidth } from './HelpCard'
import { Id, Screen } from './Shell'
import { rowClock, useNow } from './useNow'
import {
  CONCURRENCY_STATES,
  RESOURCE_UNITS,
  TERMINAL_STATES,
  accountText,
  compareStarted,
  elapsed,
  rollupState,
  startedOf,
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
  // THE SEARCH AND THE SORT (#99) are Recent's too, for the same reason and
  // over the same rows: applied after the state and the profile, so a pasted
  // task id or step name narrows what those two already chose, and the
  // qualifier below names both. Off Recent they do nothing, as the state does,
  // so a query typed there cannot silently empty another tab.
  const needle = shown === 'recent' ? query.trim().toLowerCase() : ''
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
      <div className="ctl-toolbar">
        <div className="ctl-seg" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={shown === t.id}
              aria-label={t.say}
              onClick={() => chooseTab(t.id)}
            >
              {t.label} <span className="badge">{counts[t.id]}</span>
            </button>
          ))}
        </div>

        {/* RECENT, BY STATE (OV-10). The same `.ctl-seg` as the tabs, but
            pressed buttons rather than tabs: it filters the tab it sits
            beside, it is not a fourth tab. Counts are from the loaded rows,
            like the tab badges, and DEAD_LETTERED is never offered --
            nothing writes it (`agentlist.ts`). */}
        {shown === 'recent' && (
          <div className="ctl-seg" role="group" aria-label="Recent, by state">
            {([null, ...RECENT_STATES] as const).map((s) => (
              <button
                key={s ?? 'all'}
                type="button"
                aria-pressed={recentState === s}
                onClick={() => chooseState(s)}
              >
                {s ?? 'all'} <span className="badge">{stateCounts[s ?? 'all']}</span>
              </button>
            ))}
          </div>
        )}

        <label className="ag-filter">
          <span className="ctl-eyebrow">profile</span>
          <select value={profile} onChange={(e) => setProfile(e.target.value)}>
            <option value="">all</option>
            {profiles.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>

        {/* RECENT, SEARCHED (#99). A pasted full task id, a step name or a
            workflow id, matched case-insensitively over the loaded rows. It
            is a reader's scratch, so it is not written to the address. */}
        {shown === 'recent' && (
          <label className="ag-filter">
            <span className="ctl-eyebrow">search</span>
            <input
              type="search"
              value={query}
              placeholder="task, step or workflow id"
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
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
                shown === 'live'
                  ? 'No agent is holding a pool slot right now. This is a real zero from a successful read, not a failed one.'
                  : shown === 'waiting'
                    ? 'Nothing is waiting. Waiting work costs nothing, so an empty tab here is normal.'
                    : needle !== ''
                      ? `No ${stateFilter !== null ? `${RECENT_STATE_OF[stateFilter]} ` : ''}agent in the loaded page matches “${query.trim()}”.`
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
            {shown === 'live' && <HelpCard topic="capacity" />}
          </h3>
        </div>
      ) : shown === 'waiting' ? (
        <WaitingGroups rows={rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} sort={sort} grouped={grouped} />
      ) : grouped && shown !== 'live' ? (
        <GroupedRows rows={rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} sort={sort} />
      ) : (
        <FlatRows rows={rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} sort={sort} />
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
  sort,
}: {
  rows: Task[]
  now: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
  sort?: SortControl
}) {
  const shared = flatShared(rows.map((t) => rowReason(t, classes)))
  // BESIDE AN OPEN AGENT THE LIST IS THE COMPACT COLUMN (agents.html V1):
  // two-line rows and no column heads, which name columns it no longer has.
  const compact = openTaskId !== null
  return (
    <div className="rows">
      {!compact && <RowHead sort={sort} />}
      {rows.map((t, i) => (
        <TaskRow
          key={t.id}
          task={t}
          now={now}
          onOpen={onOpen}
          open={t.id === openTaskId}
          classes={classes}
          whyShared={shared[i]}
          compact={compact}
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
  sort,
  grouped,
}: {
  rows: Task[]
  now: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
  sort?: SortControl
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
              <GroupedRows rows={g.rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} sort={sort} />
            ) : (
              <FlatRows rows={g.rows} now={now} onOpen={onOpen} openTaskId={openTaskId} classes={classes} sort={sort} />
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
  sort,
}: {
  rows: Task[]
  now: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
  sort?: SortControl
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
              {wf === '' ? 'No workflow' : <Id>{wf}</Id>}
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
              {openTaskId === null && <RowHead sort={sort} />}
              {tasks.map((t, i) => (
                <TaskRow
                  key={t.id}
                  task={t}
                  now={now}
                  onOpen={onOpen}
                  open={t.id === openTaskId}
                  classes={classes}
                  whyShared={said.shared[i]}
                  compact={openTaskId !== null}
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
 * THE COLUMN HEADS (AG-16), on the row's own grid.
 *
 * A list of forty rows reading `1/3`, `1u` and `4m 12s` with nothing saying
 * which is attempts, which is weight and which is elapsed left a reader to
 * infer three columns from their values. This is a `.row` with the SAME cell
 * classes in the SAME order as `TaskRow`, so it sits on the same named-line
 * template and every breakpoint that drops a column -- the inspector's two
 * stages, the phone card -- drops its head with it, by construction rather
 * than by a second list of widths. `is-head` is the one hook the sheet needs
 * to set it in the label treatment and to hide it where rows become cards.
 *
 * The flags column has no head: it is empty on most rows, and the chip in it
 * names itself.
 */
function RowHead({ sort }: { sort?: SortControl }) {
  const started = sort?.started ?? null
  return (
    <div className="row is-head">
      <span className="st">State</span>
      <span className="agent">Agent</span>
      <span className="owner">Owner</span>
      <span className="wf">Step</span>
      {/* SORTABLE (#376). A button inside the head cell, so the head stays a
          `.row` on the rows' own template; `aria-sort` names the order, and
          the arrow is the visible half of it. Without a control (a caller
          that passes none) it is the plain label. */}
      <span
        className="started"
        aria-sort={started === null ? 'none' : started === 'asc' ? 'ascending' : 'descending'}
      >
        {sort ? (
          <button type="button" className="ag-sort" onClick={sort.onStarted}>
            Started{started === 'desc' ? ' ↓' : started === 'asc' ? ' ↑' : ''}
          </button>
        ) : (
          'Started'
        )}
      </span>
      <span className="when">Elapsed</span>
      <span className="acct">Account</span>
      <span className="try">Try</span>
      <span className="class">Class</span>
      <span className="badges" />
    </div>
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

function TaskRow({
  task,
  now,
  onOpen,
  open = false,
  classes,
  whyShared = false,
  compact = false,
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
  /** The list sits beside an open agent: draw the two-line compact row (agents.html V1). */
  compact?: boolean
}) {
  const why = rowReason(task, classes)
  // Said once by a neighbour or the group header (#100), and never a warn line.
  const whyHidden = whyShared && !why.warn
  if (compact) {
    return <CompactRow task={task} now={now} onOpen={onOpen} open={open} why={why} whyHidden={whyHidden} />
  }
  const el = elapsed(task, now)
  const start = startedOf(task, now)
  const account = accountText(task.account)
  const tries = attemptsUsed(task)
  const units = RESOURCE_UNITS[task.resource_class]
  // Driven by the flag, not by an optimistic state flip. A cancel on a LEASED
  // or RUNNING task writes only cancel_requested -- the state does not change
  // until the worker or reconciler releases the lease, because releasing it
  // from the API would decrement a pool a live container still occupies.
  const cancelling = task.cancel_requested && !TERMINAL_STATES.has(task.state)

  return (
    // `holding` IS GONE FROM THE MARKUP AS WELL AS FROM THE SHEET. It tinted
    // the row's border in the accent to say "this agent holds a pool slot",
    // which is precisely what the Live tab selects for -- true of every row in
    // one tab and of no row in the other two. The state chip's `is-live` mark
    // says it per row; see `.row.holding` in styles.css for the argument.
    <div
      className="row clickable"
      role="button"
      tabIndex={0}
      data-task-id={task.id}
      // WHICH ROW IS OPEN (AG-17). With the inspector open no row said which
      // agent it was showing; the one it is gets `aria-current`, which a
      // screen reader announces and the sheet draws (a surface step and an
      // ink rule, design-system.md §1.3). `undefined` rather than `false`,
      // so the attribute is absent on every other row.
      aria-current={open ? 'true' : undefined}
      onClick={() => onOpen(task.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen(task.id)
        }
      }}
    >
      {/* ONE MARK AND ONE WORD, WHICH IS THE WHOLE CHIP DECISION (§6.6).
          This column drew the state THREE TIMES: a `.ctl-dot` silhouette, then
          `stateGlyph`'s bullet, then the word in 13px uppercase tracked mono in
          a saturated hue. Forty rows of that is the owner's "decorative double
          dot" and "status chips are heavy" in one column.

          `.ctl-chip` is the primitive that replaced all three: a 10px mark
          carrying the silhouette, and the word in the sans face at --t-body in
          FULL INK -- which is a stronger reading of the state than the coloured
          uppercase was, because the word no longer competes with the hue for
          the same channel. The mark is `aria-hidden` inside the primitive, so
          a screen reader gets the word once. */}
      <Chip tone={stateTone(task.state)} state={task.state}>{task.state}</Chip>

      {/* ONE LINE, NOT THREE. This was a flex COLUMN -- profile over model over
          id -- which is what made a 30px row 72px tall and the list read as
          stacked cards rather than as a list. The one screen the owner named as
          already right (`#work/workflows`) puts ten facts on one 37px line;
          three facts get one line here for the same reason. */}
      <span className="agent">
        <b>{task.runner_profile}</b>
        {task.model && <span className="model">{task.model}</span>}
        <span className="id" title={task.id}>
          {shortTaskId(task.id)}
        </span>
        {/* A SHARED REASON LIVES IN THIS CELL, NOT IN THE ROW'S GRID (#100).
            As its own grid item it had to be placed somewhere: pinned to the
            first cell, it pushed every auto-placed cell one column right and
            wrapped the last onto a new line; in flow it cost a line of
            row-gap. Here it is absolutely positioned inside `.agent`, which
            is `position: relative` and clips, so it takes no cell, no gap and
            no line, and stays in the row's accessible name. */}
        {whyHidden && <span className="why is-shared">{why.text}</span>}
      </span>

      <span className="owner" title={task.submitted_by ?? undefined}>
        {task.submitted_by?.split('@')[0] ?? <Em />}
      </span>

      <span className="wf">
        {/* THE BOX CAME OFF THE STEP ID, and it is the one cell that was
            measurably broken: `.tag` draws a bordered box that does not shrink,
            so `scan-terraform` in a 110px column overlapped the elapsed time
            beside it at 1440px -- twice on the shipped list, three times with
            the drawer open. An id is not a status and does not get a status's
            chrome; `<Id>` is the one rule for every identifier on every screen
            (B17) and the column position is what says which id this is. */}
        {task.step_id ? (
          // The accessible name carries BOTH ids. It used to carry only the
          // workflow's, which meant a screen reader was told the workflow and
          // never the step -- the visible string. Naming both is what the
          // sighted reader gets from the column plus the cell.
          <span
            className="wf-step"
            aria-label={`workflow ${task.workflow_id}, step ${task.step_id}`}
          >
            <Id>{task.step_id}</Id>
          </span>
        ) : (
          <span className="ctl-em">—</span>
        )}
      </span>

      {/* STARTED, WITH THE SUBMIT TIME UNDER IT (#376). Local wall-clock
          time from `clockTime`, the full UTC instant and its age in the
          hover. A task with no start reads `never started` -- whatever its
          state -- with its submit time still beside it. */}
      <span className={`started${start.never ? ' is-never' : ''}`} title={start.title}>
        <span className="started-at">{start.text}</span>
        <span className="started-sub" title={start.submittedTitle}>
          sub {start.submitted}
        </span>
      </span>

      <span className={`when${el.ticking ? ' ticking' : ''}`}>{el.text}</span>

      {/* THE ACCOUNT THIS AGENT RUNS ON (#379), from its own events. The
          words for "none" are the API's answer: no model call, not assigned
          yet, not read -- never a guess. */}
      <span className={`acct${account.known ? '' : ' is-none'}`} title={account.title}>
        {account.text}
      </span>

      {/* OVER THE CAP IS A PROBLEM, AND IT LOOKS LIKE ONE (AG-15). `83/3`
          rendered exactly like `1/3`. `is-over` is the design system's word
          for more held than a ceiling allows (the pool bars' `.is-over`);
          the overage is in the accessible name, not only in the arithmetic. */}
      <span className={`try${tries.over ? ' is-over' : ''}`} aria-label={tries.say}>
        {task.attempt_count}/{task.max_attempts}
      </span>

      <span className="class">
        {task.resource_class}
        {units !== undefined && <span className="units"> · {units}u</span>}
      </span>

      {/* The dispatch chip renders NOTHING for a plain `collect` task, which is
          most of them, and something for every task that will push or open a
          pull request. That asymmetry is the point: the rows worth spotting in
          a list of forty are the ones that are going to write to a repository. */}
      <span className="badges">
        <DispatchChip task={task} />
        {/* A STATE, SO IT IS A CHIP. `cancelling` is the one thing on this row
            that contradicts the state word beside it -- the task still reads
            RUNNING because the lease is still held (invariant 3) and only
            `cancel_requested` is set. It was a bordered uppercase `.tag` in
            --bad, which drew it louder than the state it qualifies; as a chip
            it is a caution mark and the word, in the same vocabulary as every
            other state on the screen. */}
        {cancelling && <Chip tone="wait">cancelling</Chip>}
      </span>

      {/* The reason this screen exists on a phone: someone is checking why
          their agent has not moved. It outranks every identifier and is never
          the thing that gets dropped.

          ITS INK SAYS WHETHER SOMEONE HAS TO ACT (AG-14). Every line was
          `--warn`, so a step waiting on the step before it was the same yellow
          as a failure. `whyNeedsAction` (types.ts) is the rule: a failure,
          work that can never be admitted (a pool paused or set to zero, a
          spent budget), a missing credential. Routine waits and cancellations
          are plain ink. A silent worker gets no line HERE: a row is a task
          document and the heartbeat is on the lease (#179); the inspector,
          which reads events, draws one.

          SAID ONCE WHEN THE NEIGHBOURS SHARE IT (#100). Then this line is
          not drawn: the row above or the group header already says it, and
          the `.agent` cell carries it visually hidden, so every row still
          answers "why" when read on its own. Never a warn line, and never
          hover-only: `flatShared`/`groupReasons` decide it. */}
      {why.text && !whyHidden && <span className={`why${why.warn ? ' is-warn' : ''}`}>{why.text}</span>}
    </div>
  )
}

/**
 * WHETHER THE LIST IS FOLDED TO THE 64px STRIP. `AgentDrawer` (App.tsx) owns
 * the snap and writes it on the root as `data-agent-list`, which is also what
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
  const tries = attemptsUsed(task)
  const name = task.step_id ?? shortTaskId(task.id)
  const owner = task.submitted_by?.split('@')[0] ?? null
  const profile = task.model ? `${task.runner_profile} · ${task.model}` : task.runner_profile
  const live = tabOf(task) === 'live'
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
      <span className={`when${el.ticking ? ' ticking' : ''}`}>{el.text}</span>
      <span className="cr-sub">
        <span className="cr-profile">{profile}</span>
        {' · '}
        <span className="cr-owner" title={task.submitted_by ?? undefined}>
          {owner ?? <Em />}
        </span>
        {live && (
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
