import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
// `Em` and `Mark` live in AgentDetail.tsx, which is where `ABSENT_MARK` was
// written and which design-system.md §9.1 names as the source to promote from.
// One definition for the four screens of this group; a second copy of a mark
// whose whole job is to be recognisable is a contradiction in terms.
// `Chip` joins them for the same reason: §9.3 of design-system.md counted four
// status chips in this product and asked for one, and the rebuilt `.ctl-chip`
// is only a rebuild if the screens stop hand-rolling their own.
import { Chip, Em, Mark, type ChipTone } from './AgentDetail'
import { RECENT_STATES, RECENT_STATE_OF, type AgentList, type RecentState } from './agentlist'
import { loadTasks } from './api'
import { DispatchChip } from './Dispatch'
import { HelpCard } from './HelpCard'
import { Id, Screen } from './Shell'
import {
  CONCURRENCY_STATES,
  RESOURCE_UNITS,
  TERMINAL_STATES,
  elapsed,
  rollupState,
  stateTone,
  whyAgent,
  type Task,
  type TaskPage,
} from './types'

type Tab = 'live' | 'waiting' | 'recent'

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
 * 5s that is roughly 600 KB/s to a phone. Only the Live tab earns the fast
 * cadence, and only while it has rows; the `view=summary` parameter §8/P2
 * proposes is what would make it cheap.
 */
export const LIVE_POLL_MS = 5_000
export const IDLE_POLL_MS = 30_000

export function pollInterval(page: TaskPage | null): number {
  return page !== null && page.tasks.some((t) => tabOf(t) === 'live') ? LIVE_POLL_MS : IDLE_POLL_MS
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
  // WHEN THE ROWS ON SCREEN WERE READ. The row clock stops one interval past
  // this (see `AgentsBody`), so it is recorded from the read itself -- the
  // `fetchedAt` the Result carries -- and not from when React got round to
  // rendering it.
  const [readAt, setReadAt] = useState<number | null>(null)
  const load = useCallback(async () => {
    const r = await loadTasks()
    if (r.status === 'ok') setReadAt(r.fetchedAt)
    return r
  }, [])

  return (
    <Screen
      title="Agents"
      load={load}
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
        heading: 'No agents · real zero',
        // ONE SENTENCE, WHICH IS WHAT §6.9 ALLOWS AN EMPTY STATE. The second
        // sentence -- "nothing has been submitted under this tenant, or
        // everything has aged out of the page" -- was two guesses about a
        // cause this screen cannot see, and the link is where a reader finds
        // out which states a page holds.
        // NO `?` HERE (B7.4). The heading beside this sentence already reads
        // `No agents · real zero`, which is the whole of what `tenant-scope`
        // was guarding against -- a reader taking an empty list for a failed
        // read. Whose agents these are is the crumb and the provenance line
        // above, and the topic is one click away in the rail's Help section.
        // This screen keeps exactly one glyph, on the empty TAB below, where
        // the claim being made is about capacity rather than about the read.
        body: <>The read succeeded and returned nothing.</>,
      }}
    >
      {(d) => (
        <AgentsBody
          onOpen={onOpen}
          openTaskId={taskId}
          page={d}
          readAt={readAt}
          tab={tab}
          setTab={setTab}
          recentState={recentState}
          setRecentState={setRecentState}
          onList={onList}
          profile={profile}
          setProfile={setProfile}
          grouped={grouped}
          setGrouped={setGrouped}
        />
      )}
    </Screen>
  )
}

/**
 * The instant the row durations are computed at: the clock, until the rows
 * are more than one poll interval old, and then no further (AG-1).
 *
 * The rows are a READ, and a read has an age. A ticking clock over rows
 * nobody has re-read kept adding to `run 4m` after the agent had finished --
 * the page said running, the clock said still going, and the platform had
 * moved on. Past one interval a fresh read was due and has not arrived, so the
 * figures stop where the read can still vouch for them; the age of the read
 * is the Screen's to show. `readAt` null (not yet known) keeps the clock.
 */
export function rowClock(now: number, readAt: number | null, interval: number): number {
  return readAt === null ? now : Math.min(now, readAt + interval)
}

function AgentsBody({
  onOpen,
  openTaskId,
  page,
  readAt,
  tab,
  setTab,
  recentState,
  setRecentState,
  onList,
  profile,
  setProfile,
  grouped,
  setGrouped,
}: {
  onOpen: (taskId: string) => void
  openTaskId: string | null
  page: TaskPage
  readAt: number | null
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
}) {
  // One clock for every ticking duration on the screen, so a hundred rows do
  // not each hold their own interval -- and it stops advancing the rows once
  // they are older than one poll interval (`rowClock`).
  const [tick, setTick] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  const now = rowClock(tick, readAt, pollInterval(page))

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
  // failures check counts FAILED among these same newest 200, and a
  // server-side FAILED list would be a different, larger population answering
  // to the same number.
  const stateFilter = shown === 'recent' ? recentState : null
  const rows = useMemo(
    () =>
      page.tasks
        .filter((t) => tabOf(t) === shown)
        .filter((t) => stateFilter === null || t.state === RECENT_STATE_OF[stateFilter])
        .filter((t) => profile === '' || t.runner_profile === profile)
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1)),
    [page.tasks, shown, stateFilter, profile],
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
  const scopeSay = page.next_page_token
    ? `Every count and filter on this screen runs over the ${page.tasks.length} rows loaded into this page, not over the platform. More rows exist beyond it.`
    : `Every count and filter on this screen runs over the ${page.tasks.length} rows loaded into this page, not over the platform.`

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
          {page.tasks.length} loaded{page.next_page_token ? ' · more beyond' : ''}
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
            {shown === 'live' && <HelpCard topic="capacity" />}
          </h3>
        </div>
      ) : grouped && shown !== 'live' ? (
        <GroupedRows rows={rows} now={now} onOpen={onOpen} openTaskId={openTaskId} />
      ) : (
        <div className="rows">
          <RowHead />
          {rows.map((t) => (
            <TaskRow key={t.id} task={t} now={now} onOpen={onOpen} open={t.id === openTaskId} />
          ))}
        </div>
      )}
    </>
  )
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
}: {
  rows: Task[]
  now: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
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
            <div className="rows">
              <RowHead />
              {tasks.map((t) => (
                <TaskRow key={t.id} task={t} now={now} onOpen={onOpen} open={t.id === openTaskId} />
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
function RowHead() {
  return (
    <div className="row is-head">
      <span className="st">State</span>
      <span className="agent">Agent</span>
      <span className="owner">Owner</span>
      <span className="wf">Step</span>
      <span className="when">Elapsed</span>
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
}: {
  task: Task
  now: number
  onOpen: (taskId: string) => void
  /** This is the agent the inspector has open (AG-17). */
  open?: boolean
}) {
  const why = whyAgent(task)
  const el = elapsed(task, now)
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
      <Chip tone={stateTone(task.state)}>{task.state}</Chip>

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

      <span className={`when${el.ticking ? ' ticking' : ''}`}>{el.text}</span>

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
          the thing that gets dropped. */}
      {why && <span className="why">{why}</span>}
    </div>
  )
}
