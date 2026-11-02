import { createContext, useContext, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { createPortal } from 'react-dom'
// `Em` and `Mark` live in AgentDetail.tsx, which is where `ABSENT_MARK` was
// written and which design-system.md §9.1 names as the source to promote from.
// One definition for the four screens of this group; a second copy of a mark
// whose whole job is to be recognisable is a contradiction in terms.
// `Chip` joins them for the same reason: §9.3 of design-system.md counted four
// status chips in this product and asked for one, and the rebuilt `.ctl-chip`
// is only a rebuild if the screens stop hand-rolling their own.
import { Em, Mark, timesRun, type ChipTone } from './AgentDetail'
import { TaskIdLine } from './TaskIdLine'
import { BrowserUseMark, browserUseOf, browserUseSay } from './BrowserUse'
import { BudgetRowMark, budgetRowSay, budgetStopLine, stoppedAtBudgetRow, useBudgetWarning } from './AgentBudget'
import { PHONE_PAGE_LIMIT, RECENT_STATES, RECENT_STATE_OF, agentKind, agentList, agentName, workflowHref, type AgentList, type RecentState } from './agentlist'
import { TASK_PAGE_LIMIT, USE_FIXTURES, loadAttempts, loadLiveSession, loadTask, loadTasks, loadTasksInState, type LiveSession, type ResourceClasses } from './api'
import { classUnits, useResourceClasses } from './Blockers'
import { pageReads, type Result } from './fetch'
import { isLiveBrowser, liveBrowserRows, needsYou } from './Handoff'
import { presetLabel } from './LiveBrowser'
import { theaterPath } from './LiveThumb'
import { HelpCard, phoneWidth } from './HelpCard'
import { toggleListSnap, useListSnap } from './listSnap'
import { listMayStart, panelWaiters, wakePanelWaiters } from './agentPanel'
import { focusSplitOnOpen } from './agent-split/focus'
import { tabStep } from './agent-split/tabs'
import './styles/agents.css'
import { PARK_WORD, Segmented, StateMark, ToneMark } from './components'
import { Id, Screen } from './Shell'
import { publishListCounts } from './Spine'
import { rowLabel, useRowTitles } from './OverviewRegions'
import { rowClock, useNow } from './useNow'
import { waitingShort } from './waitingShort'
import {
  CONCURRENCY_STATES,
  NEVER_WRITTEN,
  TERMINAL_STATES,
  WAITING_STATES,
  compareStarted,
  elapsed,
  formatDuration,
  poolLabel,
  rollupState,
  taskGroup,
  timeAgo,
  waitingLine,
  whyAgent,
  whyNeedsAction,
  type Task,
  type TaskPage,
  type TaskState,
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
 *
 * THE NAME STARTS WITH WHAT THE TAB SHOWS (visual QA QA-029, 2026-10-20). It
 * was the sentence alone, `Live: holding a pool slot, …`, which leaves out the
 * visible count: axe's label-content-name-mismatch, and a speech-input user
 * who says "click Live 6" reaches nothing. The name is `Live 6: <say>`.
 */
const TABS: { id: Tab; label: string; say: string }[] = [
  { id: 'live', label: 'Live', say: 'holding a pool slot, and costing money' },
  { id: 'waiting', label: 'Waiting', say: 'durable and free — READY or PARKED' },
  { id: 'recent', label: 'Recent', say: 'finished, one way or another' },
]

/**
 * THE BROWSER SWITCH'S ROWS (QA12-20): `liveBrowserRows` -- the RUNNING
 * browser agents, needs-you first -- then the browser agents still STARTING.
 * A starting browser was left out, so a browser agent the reader had just
 * launched was missing from the one filter that names browsers; it is listed
 * last, with a `starting` mark in its row, because its browser is not up yet.
 */
export function browserRows(tasks: readonly Task[]): Task[] {
  return [...liveBrowserRows(tasks), ...tasks.filter((t) => t.state === 'STARTING' && isLiveBrowser(t))]
}

/**
 * THE LIST'S READ TIME, FOR THE ROWS THAT READ ON THE LIST'S POLL (QA12-29):
 * the device outline. A context rather than a prop through every row
 * component, because one cell of one kind of row uses it.
 */
const ListReadAt = createContext<number | null>(null)

/**
 * EACH WORKFLOW STEP'S WORKFLOW TITLE (VQ-009, BGD's visual QA 2026-10-26):
 * of 21 live rows most read `build`, `implement` or `re-review`, while
 * Overview's Running now named the same agents `<title> · implement`. The
 * list reads the titles the way Overview does (`useRowTitles`: one full read
 * per workflow, once a session, at most 12 at a time, a shared cache), so the
 * two pages name an agent one way. A context, as `ListReadAt`, because only
 * the row's name uses it. A lone agent is never read for it: reading every
 * lone row in full would be a read per row, so its title comes on the summary
 * row itself (`ownTitle`, SUMMARY-TITLE).
 */
const RowTitleOf = createContext<((t: Task) => string | null) | null>(null)

/**
 * THE ROWS ARE UNDER THEIR WORKFLOW'S HEADING (visual QA pass 1,
 * overview-agents-53): the group's heading is the workflow's title, so a row
 * under it is its step alone, not the title again on every line.
 */
const UnderWorkflow = createContext(false)

/**
 * A STEP'S WORKFLOW TITLE, OFF ITS OWN SUMMARY ROW FIRST (visual QA pass 1,
 * overview-agents-60): every step carries its workflow's `metadata.title`,
 * and the summary view serves it, so the row is named on its first draw. It
 * waited for `useRowTitles`' read, and the one row read `report` in one
 * layout and `Audit the operator scripts… · report` in the other, depending
 * on whether that read had landed. The read stays the fallback for an API
 * whose summary sends no metadata.
 */
export function stepWorkflowTitle(t: Pick<Task, 'workflow_id' | 'step_id' | 'metadata'>, read: ((t: Task) => string | null) | null): string | null {
  if ((t.workflow_id ?? null) === null || (t.step_id ?? null) === null) return null
  const v = t.metadata?.title
  if (typeof v === 'string' && v.trim() !== '') return v.trim()
  return read?.(t as Task) ?? null
}

/** The rows that are a workflow's steps: the ones a workflow title names. */
function isStep(t: Task): boolean {
  return (t.workflow_id ?? null) !== null && (t.step_id ?? null) !== null
}

/**
 * A LONE AGENT'S OWN TITLE (SUMMARY-TITLE, BGD's decision on VQ-009,
 * 2026-10-26): `metadata.title`, which the list's summary view carries and
 * nothing else of the metadata (`codec.SUMMARY_METADATA_KEYS`), so the row
 * reads `Fix flaky lease heartbeat test (#248)` with no read per row. Null
 * for a step -- its name is the step, led by its workflow's title -- and for
 * a title that is absent, blank or not a string, or an API older than the
 * change, whose summary sends no metadata: the row is then `agentName`'s
 * `claude-code agent · 01234567`, as before.
 */
export function ownTitle(t: Pick<Task, 'step_id' | 'metadata'>): string | null {
  if ((t.step_id ?? null) !== null) return null
  const v = t.metadata?.title
  return typeof v === 'string' && v.trim() !== '' ? v.trim() : null
}

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
 * `view=summary` EXISTS NOW (#168), AND THE PHONE READS THE FULL PAGE (live QA
 * CH-4, 2026-10-11): 50 rows made the phone say "failed 0+" where the desktop
 * said "failed 6+" at the same minute. `PHONE_PAGE_LIMIT` is `TASK_PAGE_LIMIT`
 * (pageLimits.ts says why); the Overview's failures check counts over this
 * same page (OV-10), so the two moved together.
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
export { agentPanelGone, agentPanelOpening, agentPanelShown, listMayStart } from './agentPanel'

/** A page of the list, and the page size that read asked for. */
interface AgentsPage extends TaskPage {
  /** The `limit` the read asked for: `PHONE_PAGE_LIMIT` at phone width. */
  asked: number
  /** Further pages were read into it with "load more" (`MorePages`). */
  extended?: boolean
  /**
   * EVERY LIVE AGENT IS IN `tasks` (QA12-20): the page held them all, or the
   * live states were read in full beside it (`readStates`). False when a capped
   * page's live-state read failed or was itself capped, and Live's count is
   * then a lower bound like every other.
   */
  liveWhole?: boolean
  /**
   * EVERY WAITING AGENT IS IN `tasks` (VQ-012), as `liveWhole` says of the
   * live ones: the page held them all, or the waiting states were read in
   * full beside it (`readStates`).
   */
  waitingWhole?: boolean
}

/**
 * THE PAGES READ PAST THE FIRST, BY "LOAD MORE" (live QA WA-2, 2026-10-11).
 * The list said `200 loaded · more beyond` and never followed the
 * `next_page_token` it was handed, so a task older than the newest 200 could
 * not be reached from this screen at all.
 *
 * `tasks` STARTS WITH THE FIRST PAGE AS IT WAS when "load more" was pressed,
 * then each page after it. The first page goes on being re-read every few
 * seconds and the token does not move with it: rows that newer tasks push off
 * the re-read first page are older than every row on it, and they would fall
 * into a gap between it and the next page if the snapshot did not keep them.
 * Rows past the first page are not re-read; they are, overwhelmingly, finished
 * agents, whose rows no longer change.
 */
interface MorePages {
  tasks: Task[]
  /** The token for the page after the last one read; null when none is left. */
  token: string | null
}

/** The re-read first page, with every further page read into it after its own rows. */
export function withMorePages(page: AgentsPage, more: MorePages | null): AgentsPage {
  if (more === null) return page
  const fresh = new Set(page.tasks.map((t) => t.id))
  return {
    ...page,
    tasks: [...page.tasks, ...more.tasks.filter((t) => !fresh.has(t.id))],
    next_page_token: more.token,
    extended: true,
    // Read to the last page, the list holds every row, live ones included.
    liveWhole: page.liveWhole === true || more.token === null,
    waitingWhole: page.waitingWhole === true || more.token === null,
  }
}

/** One more page after `token`, appended to what is already held. */
function appendPage(held: readonly Task[], got: TaskPage | null, token: string | null): MorePages {
  const seen = new Set(held.map((t) => t.id))
  const rows = got === null ? [] : got.tasks.filter((t) => !seen.has(t.id))
  return { tasks: [...held, ...rows], token }
}

/**
 * THE LIST'S POLL PLAN (load-speed investigation, the owner's pick 2026-11-01,
 * part of #2688). Measured in the owner's browser, `GET /v1/tasks?limit=200&
 * view=summary` took 5.3 s on first load and 1.1-2.1 s on every poll, almost
 * all of it server time, and the list re-read that page every 5 s while Live
 * held a row -- then, only once it had answered, read the live and waiting
 * states one `?state=` read each.
 *
 * NOW A 5 s POLL IS ONE READ: the live and waiting states together, as one
 * `?state=A,B,...` list (`GET /v1/tasks` takes several states in one `in`
 * query, #2998), merged into the page already held. The 200-row page itself is
 * re-read every `PAGE_REREAD_MS`, and when it is, the state read is started at
 * the same instant beside it rather than after it. A live agent changes on the
 * order of seconds and every one of them is in the state read; a finished
 * agent's row no longer changes, so its page can be 30 s old.
 *
 * A ROW THAT LEFT THE LIVE AND WAITING STATES -- held as live or waiting, absent
 * from a whole state read -- finished, or was cancelled, since the page was
 * read. Its new row is the page's to bring, so it leaves Live at once and the
 * next poll re-reads the page (`due`), rather than drawing a finished agent as
 * running for up to 30 s or guessing the state it ended in.
 */
export const PAGE_REREAD_MS = IDLE_POLL_MS

/** The page the list holds between page reads, and when that page was read. */
export interface HeldPage {
  page: AgentsPage
  /** When the page read started. */
  at: number
  /** A row left the live and waiting states: the next poll re-reads the page. */
  due: boolean
}

/**
 * THE STATES ONE STATE READ ASKS FOR: every live state and every waiting state
 * a task is ever in. QUEUED is a workflow's state and never a task's
 * (`NEVER_WRITTEN`), so it is not asked for (VQ-012).
 */
const WAITING_READ: readonly TaskState[] = [...WAITING_STATES].filter((s) => !NEVER_WRITTEN.has(s))
export const LIVE_AND_WAITING: readonly TaskState[] = [...CONCURRENCY_STATES, ...WAITING_READ]
const IN_STATE_READ: ReadonlySet<TaskState> = new Set(LIVE_AND_WAITING)

/** Whether the list must read its 200-row page on this read, rather than the states alone. */
export function pageDue(held: HeldPage | null, now: number = Date.now()): boolean {
  return held === null || held.due || now - held.at >= PAGE_REREAD_MS
}

/**
 * ONE `?state=` READ OF THE LIVE AND WAITING STATES, summary rows. The route
 * takes `state=A,B` as one `state IN [...]` query (swarm-api routes/tasks.py
 * `_parse_states`, at most `STATE_IN_MAX` states); `loadTasksInState` sends the
 * value it is handed as the one `state` parameter, so the comma list goes to
 * the route as written. A FIXTURE BUILD filters its one fixture page instead,
 * through `loadTasks`, so its request log shows the one read.
 *
 * A read that throws or answers nothing is a failed read, never an empty
 * state, as the header's attempt read treats one (AgentSplit.tsx). Called
 * WITHOUT a microtask in front of it, so the read starts inside `pageReads`
 * (CH-2) when the page's load calls it.
 */
function readLiveAndWaiting(): Promise<Result<TaskPage> | null> {
  try {
    if (USE_FIXTURES) {
      return loadTasks(TASK_PAGE_LIMIT, { view: 'summary' }).then(
        (r): Result<TaskPage> | null => {
          if (r.status !== 'ok' && r.status !== 'stale') return r
          const tasks = r.data.tasks.filter((t) => IN_STATE_READ.has(t.state))
          return tasks.length === 0 ? { status: 'empty', fetchedAt: r.fetchedAt } : { ...r, data: { ...r.data, tasks } }
        },
        () => null,
      )
    }
    const states = LIVE_AND_WAITING.join(',') as TaskState
    return loadTasksInState(states, { view: 'summary' }).then((r) => r ?? null, () => null)
  } catch {
    return Promise.resolve(null)
  }
}

/** The state read as rows, and whether it holds every live and waiting task. */
function stateRows(r: Result<TaskPage> | null): { tasks: Task[]; whole: boolean; ok: boolean } {
  if (r !== null && r.status === 'empty') return { tasks: [], whole: true, ok: true }
  if (r !== null && r.status === 'ok') return { tasks: r.data.tasks, whole: !r.data.next_page_token, ok: true }
  if (r !== null && r.status === 'stale') return { tasks: r.data.tasks, whole: false, ok: false }
  return { tasks: [], whole: false, ok: false }
}

/** `rows` with every row of `extra` whose id it does not already hold after them. */
function joinRows(rows: readonly Task[], extra: readonly Task[]): Task[] {
  const held = new Set(rows.map((t) => t.id))
  return [...rows, ...extra.filter((t) => !held.has(t.id))]
}

/**
 * READ THE LIST: the page and the state read together when the page is due,
 * the state read alone otherwise. `held` is the screen's own (`AgentsScreen`),
 * written here after every read, so a tenant switch -- which remounts the
 * screen -- starts from no page.
 */
export async function loadAgentsPage(held: { current: HeldPage | null }): Promise<Result<AgentsPage>> {
  const last = held.current
  if (!pageDue(last) && last !== null) return pollStates(last, held)
  const startedAt = Date.now()
  const asked = phoneWidth() ? PHONE_PAGE_LIMIT : TASK_PAGE_LIMIT
  // The summary view (#168): no row on this screen draws `input`, `metadata`
  // or `result_summary` -- the inspector reads its own task, full, through
  // `loadTask` -- and the trap in `drawer.reread.test.tsx` holds that.
  // BOTH READS START HERE, before the first `await`.
  // A FIXTURE LIST IS ONE PAGE (`loadTasks`): it never hands out a token, so
  // there is no capped page to read the states beside, and the state read would
  // be the same fixture rows again.
  const [read, states] = await Promise.all([
    loadTasks(asked, { view: 'summary' }),
    USE_FIXTURES ? Promise.resolve(null) : readLiveAndWaiting(),
  ])
  const fresh = stateRows(states)
  let page: AgentsPage
  // AN EMPTY READ IS A PAGE OF NO ROWS, NOT A DIFFERENT SCREEN (V151, visual
  // QA #1038). Handed to `Screen` as `empty`, it replaced the whole body with
  // one panel: the tab strip, the search and the filters went with it, so the
  // screen a tenant with no agents saw was laid out unlike the one it would
  // see a minute later. The body draws each tab's own real zero, mark and
  // all, under the same chrome it always has.
  if (read.status === 'empty') {
    page = { tasks: fresh.ok ? fresh.tasks : [], next_page_token: null, asked, liveWhole: true, waitingWhole: true }
    held.current = { page, at: startedAt, due: false }
    return { status: 'ok', data: page, fetchedAt: read.fetchedAt, serverAt: read.serverAt }
  }
  if (read.status !== 'ok' && read.status !== 'stale') return read
  // A PAGE WITH NO TOKEN HOLDS EVERY ROW, live ones included (QA12-20). A
  // capped one holds every live and waiting row only when the state read beside
  // it was whole; a failed or capped state read leaves Live and Waiting lower
  // bounds, as they were. The state read's rows are the newer of the two for
  // a task both hold only by a hair, so the page's own row is kept.
  const capped = Boolean(read.data.next_page_token)
  page = {
    ...read.data,
    tasks: joinRows(read.data.tasks, fresh.tasks),
    asked,
    liveWhole: !capped || fresh.whole,
    waitingWhole: !capped || fresh.whole,
  }
  if (read.status === 'ok') held.current = { page, at: startedAt, due: false }
  return { ...read, data: page }
}

/**
 * A 5 s POLL: the state read alone, merged into the page held. A row the read
 * holds takes the read's row; a row held as live or waiting that a WHOLE read
 * no longer holds is dropped and the page marked `due`; every other row is the
 * page's. A failed state read is a failed read of the list: `Screen` keeps the
 * rows on screen, dimmed with their age, and backs off.
 */
async function pollStates(last: HeldPage, held: { current: HeldPage | null }): Promise<Result<AgentsPage>> {
  const states = await readLiveAndWaiting()
  const fresh = stateRows(states)
  if (!fresh.ok) {
    if (states !== null && states.status === 'error') return states
    return {
      status: 'error',
      error: { kind: 'unreachable', httpStatus: null, code: null, message: 'The live and waiting agents could not be read.' },
    }
  }
  const byId = new Map(fresh.tasks.map((t) => [t.id, t]))
  let left = false
  const kept: Task[] = []
  for (const t of last.page.tasks) {
    const now = byId.get(t.id)
    if (now !== undefined) kept.push(now)
    else if (fresh.whole && IN_STATE_READ.has(t.state)) left = true
    else kept.push(t)
  }
  const page: AgentsPage = {
    ...last.page,
    tasks: joinRows(kept, fresh.tasks),
    liveWhole: fresh.whole,
    waitingWhole: fresh.whole,
  }
  held.current = { page, at: last.at, due: last.due || left }
  const at = states !== null && (states.status === 'ok' || states.status === 'empty') ? states : null
  return { status: 'ok', data: page, fetchedAt: at?.fetchedAt ?? Date.now(), ...(at?.serverAt !== undefined ? { serverAt: at.serverAt } : {}) }
}

/**
 * WHILE AN AGENT IS OPEN THE LIST POLLS AT THE IDLE CADENCE: the reader is on
 * the agent, whose own reads keep it current, and the list beside it is
 * context. 5 s returns when the agent is closed and Live holds a row.
 */
export function listPollInterval(page: TaskPage | null, open: string | null): number {
  return open !== null ? IDLE_POLL_MS : pollInterval(page)
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
  // ONLY THE BROWSER AGENTS THAT DID NOT USE IT (BROWSER-USE-UI, part of
  // #2322): `?browser=unused`, where Platform counts' "Unused browser time"
  // links. Seeded from the address and reported through `onList` like the
  // toggles above, so the link survives a reload and the chip that clears it
  // writes the plain address back.
  const [browserUnused, setBrowserUnused] = useState(list?.browserUnused === true)
  // THE ADDRESS WINS WHEN IT NAMES A TAB, and only then. Keyed on the values
  // rather than the object, which App rebuilds on every route.
  const listTab = list?.tab ?? null
  const listState = list?.state ?? null
  const listGrouped = list?.grouped === true
  const listFirst = list?.failedFirst === true
  const listBrowserUnused = list?.browserUnused === true
  useEffect(() => {
    if (listTab === null) return
    setTab(listTab)
    setRecentState(listTab === 'recent' ? listState : null)
    setGrouped(listGrouped)
    setFailedFirst(listFirst)
    setBrowserUnused(listBrowserUnused)
  }, [listTab, listState, listGrouped, listFirst, listBrowserUnused])
  // RECENT'S SEARCH (#99). Held here beside `profile`, not in the address: a
  // query is a reader's scratch.
  const [query, setQuery] = useState('')
  // "LOAD MORE" (WA-2): the pages read past the first, kept across the first
  // page's re-reads, and the state of the read in flight.
  const [more, setMore] = useState<MorePages | null>(null)
  const [moreRead, setMoreRead] = useState<MoreRead>({ kind: 'idle' })
  const busy = useRef(false)
  // THE PAGE HELD BETWEEN PAGE READS (`loadAgentsPage`), and the first read's
  // wait for the agent panel (`listMayStart`). `open` is read through a ref so
  // the wait sees the address as it is when it wakes, not when it began.
  const held = useRef<HeldPage | null>(null)
  const open = useRef(taskId)
  open.current = taskId
  const waits = useRef(new Set<() => void>())
  useEffect(() => {
    wakePanelWaiters()
  }, [taskId])
  useEffect(
    () => () => {
      for (const wake of waits.current) panelWaiters.delete(wake)
      waits.current.clear()
    },
    [],
  )
  const load = async (): Promise<Result<AgentsPage>> => {
    if (held.current === null && !listMayStart(open.current)) {
      await new Promise<void>((resolve) => {
        const wake = () => {
          if (!listMayStart(open.current)) return
          panelWaiters.delete(wake)
          waits.current.delete(wake)
          resolve()
        }
        panelWaiters.add(wake)
        waits.current.add(wake)
      })
      // Past the wait the reads are still the page's (CH-2).
      return pageReads(() => loadAgentsPage(held))
    }
    return loadAgentsPage(held)
  }
  const loadMore = async (page: AgentsPage) => {
    const token = more !== null ? more.token : (page.next_page_token ?? null)
    if (token === null || busy.current) return
    busy.current = true
    setMoreRead({ kind: 'reading' })
    const got = await loadTasks(page.asked, { view: 'summary', pageToken: token })
    busy.current = false
    if (got.status === 'ok' || got.status === 'stale') {
      setMore((m) => appendPage(m === null ? page.tasks : m.tasks, got.data, got.data.next_page_token ?? null))
      setMoreRead({ kind: 'idle' })
    } else if (got.status === 'empty') {
      // An empty page after a token is the end of the list, and an answer.
      setMore((m) => appendPage(m === null ? page.tasks : m.tasks, null, null))
      setMoreRead({ kind: 'idle' })
    } else if (got.status === 'error') {
      setMoreRead({ kind: 'failed', why: got.error.message })
    }
  }

  return (
    <>
      <Screen
        title="Agents"
        load={load}
        // THE LIST RE-READS (AG-1). It read once and never again, while its
        // clock went on adding to every row: a finished agent read `running`
        // and was counted in Live. `Screen` owns the timer, the pause while the
        // tab is hidden, the back-off and the stop on an answer only a person
        // can change; this screen owns the cadence, which is a function of the
        // rows it holds.
        pollMs={(page) => listPollInterval(page, open.current)}
        summary={(first) => {
          const d = withMorePages(first, more)
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
            page={withMorePages(d, more)}
            onMore={() => void loadMore(withMorePages(d, more))}
            moreRead={moreRead}
            // WHEN THE ROWS ON SCREEN WERE READ, and the cadence in force, as
            // `Screen` read them -- not recorded a second time here. A stale
            // screen hands over the last GOOD read's time, which is the one the
            // rows describe.
            readAt={reading.fetchedAt}
            interval={reading.pollMs ?? listPollInterval(d, taskId)}
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
            browserUnused={browserUnused}
            setBrowserUnused={setBrowserUnused}
          />
        )}
      </Screen>
      {/* A SCRIM WHILE AN AGENT IS OPEN (BT-017, #2423): from 760px to 1099px
          the agent is an overlay on this list, and with no backdrop its edge
          cut the rows mid-word (`…NOT_YET, not`) as if they were one page.
          Drawn only in that band (agents.css `.ag-scrim`); it takes no pointer,
          so a row under it still opens, as it did. */}
      {taskId !== null && <div className="ag-scrim" aria-hidden="true" />}
    </>
  )
}

/**
 * The instant the row durations are computed at (AG-1). It lives in useNow.ts
 * now, beside the clock it caps, because the inspector's drawer caps its clock
 * the same way; re-exported so this screen's tests keep reading it from here.
 */
export { rowClock }

/** The "load more" read: none in flight, one in flight, or the last one failed. */
type MoreRead = { kind: 'idle' } | { kind: 'reading' } | { kind: 'failed'; why: string }

function AgentsBody({
  onOpen,
  openTaskId,
  page,
  onMore,
  moreRead,
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
  browserUnused,
  setBrowserUnused,
}: {
  onOpen: (taskId: string) => void
  openTaskId: string | null
  page: AgentsPage
  /** Read the page after the last one held (WA-2). */
  onMore: () => void
  moreRead: MoreRead
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
  /** Only the browser agents whose browser was not used (`?browser=unused`, #2322). */
  browserUnused: boolean
  setBrowserUnused: (b: boolean) => void
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
  // A list extended by "load more" to its last page holds every row too.
  const whole = (page.next_page_token ?? null) === null && (page.tasks.length < page.asked || page.extended === true)
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
  // NOT ON A LIST OF ONE STATE (visual QA QA-111): `failed` chosen, every row
  // is a failure and the checkbox orders nothing, so it is not offered there.
  const failFirstOffered = shown === 'recent' && stateFilter === null
  const failFirst = failFirstOffered && failedFirst
  // THE STARTED SORT (#376), on every tab: newest start, oldest start, then
  // back to the list's own order. A reader's scratch, so not in the address.
  const [startedSort, setStartedSort] = useState<StartedSort>(null)
  // THE "ALL | BROWSERS N" SWITCH (docs/design/live-browser.md §6, "All live
  // browsers"; lanes LB-L12, LB-UX3): on the Live tab, Browsers lists only
  // the browser agents, the ones waiting for a person first, then the rest
  // running, then the ones starting (`browserRows`), and N counts them. It
  // was a "Live browsers" checkbox; the chosen design draws a switch. A
  // reader's scratch, like the sort.
  //
  // `BROWSERS`, NOT `LIVE` (live-browser QA p12, QA12-20): the option read
  // `Live` inside the Live tab, one word for two different sets on one line.
  const [browsersOnly, setBrowsersOnly] = useState(false)
  const browsers = shown === 'live' && browsersOnly
  const liveBrowsers = useMemo(() => browserRows(page.tasks).length, [page.tasks])
  const sort: SortControl = {
    started: startedSort,
    onStarted: () => setStartedSort((s) => (s === null ? 'desc' : s === 'desc' ? 'asc' : null)),
  }
  const rows = useMemo(
    () =>
      browsers
        ? // Needs-you first IS this filter's order, so the started sort does
          // not reorder it; the profile and the search still narrow it.
          browserRows(page.tasks)
            .filter((t) => profile === '' || t.runner_profile === profile)
            .filter((t) => !browserUnused || browserUseOf(t)?.kind === 'unused')
            .filter((t) => needle === '' || matchesQuery(t, needle))
        : page.tasks
        .filter((t) => tabOf(t) === shown)
        .filter((t) => stateFilter === null || t.state === RECENT_STATE_OF[stateFilter])
        .filter((t) => profile === '' || t.runner_profile === profile)
        // Only an explicit `false` from the worker: a null verdict is not
        // "not used" (BrowserUse.tsx), so it is never listed here.
        .filter((t) => !browserUnused || browserUseOf(t)?.kind === 'unused')
        .filter((t) => needle === '' || matchesQuery(t, needle))
        .sort((a, b) => {
          if (failFirst) {
            const byFailed = Number(b.state === 'FAILED') - Number(a.state === 'FAILED')
            if (byFailed !== 0) return byFailed
          }
          if (startedSort !== null) return compareStarted(a, b, startedSort === 'asc')
          return listOrder(a, b, shown)
        }),
    [page.tasks, shown, stateFilter, profile, needle, failFirst, startedSort, browsers, browserUnused],
  )

  // EVERY ROW READ, BY ID, not only the ones this tab and its filters keep
  // (visual QA run 4, N23): a cascade-cancelled row names the step that failed
  // when that step's row is in the page, filtered out or not.
  const taskById = useMemo(() => new Map<string, Task>(page.tasks.map((t) => [t.id, t] as const)), [page.tasks])

  // The segment's counts, from the loaded Recent rows, like the tab badges --
  // UNDER THE PROFILE FILTER (live QA WA-9): with `indexer` chosen and 15 rows
  // on screen the segment read `all 181+`, the count of every profile's rows.
  // A count beside a filter counts what that filter lets through -- the
  // browser filter too (review, light-desktop-03-filtered: `all 6` over the
  // two rows `?browser=unused` drew).
  const stateCounts = useMemo(() => {
    const recent = page.tasks.filter(
      (t) =>
        tabOf(t) === 'recent' &&
        (profile === '' || t.runner_profile === profile) &&
        (!browserUnused || browserUseOf(t)?.kind === 'unused'),
    )
    return {
      all: recent.length,
      failed: recent.filter((t) => t.state === RECENT_STATE_OF.failed).length,
      cancelled: recent.filter((t) => t.state === RECENT_STATE_OF.cancelled).length,
      succeeded: recent.filter((t) => t.state === RECENT_STATE_OF.succeeded).length,
    }
  }, [page.tasks, profile, browserUnused])

  // EVERY REPORT CARRIES THE TOGGLES (G2-22): a tab click that wrote only
  // `{ tab, state }` would clear `group=wf` from the address, and the
  // address is what the screen re-reads its toggles from.
  const report = (over: { tab?: Tab; state?: RecentState | null; grouped?: boolean; failedFirst?: boolean; browserUnused?: boolean }) => {
    const t = over.tab ?? shown
    const st = over.state !== undefined ? over.state : recentState
    onList?.(
      agentList(t, st, {
        grouped: over.grouped ?? grouped,
        failedFirst: over.failedFirst ?? failedFirst,
        browserUnused: over.browserUnused ?? browserUnused,
      }),
    )
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
  const clearBrowserUnused = () => {
    setBrowserUnused(false)
    report({ browserUnused: false })
  }
  // A ROW OPENS UNDER THE TAB IT CAME FROM (N9, owner QA 2026-10-04). The
  // landing tab is this screen's own state until a click reports one, so a
  // row on a Recent the list LANDED on opened `/agents/live/<id>` and the
  // panel lit `Live 0` beside the Recent rows. The tab on screen is reported
  // first, so App writes the agent's address under it.
  const openRow = (taskId: string) => {
    // A ROW OPENED BY A CLICK IS ALREADY IN VIEW (visual QA pass 1,
    // overview-agents-55): the scroll below, meant for a deep link, ran on a
    // click too, and as the split opened it scrolled the page column until
    // the `Agents` heading sat under the sticky breadcrumb.
    scrolledTo.current = taskId
    report({})
    onOpen(taskId)
  }
  // A ROW FOUND UNDER ANOTHER TAB, or by its id beyond the page (WA-1),
  // opens under the tab its state puts it in, as every row does.
  const openElsewhere = (t: Task) => {
    const where = tabOf(t)
    setTab(where)
    // On Recent with no state filter, so a cancelled agent opened from a
    // `failed` view is not hidden by the filter it came through.
    setRecentState(null)
    report({ tab: where, state: null })
    onOpen(t.id)
  }
  const idQuery = WHOLE_TASK_ID.test(needle) ? needle : null

  // THE OPEN AGENT'S ROW IS SCROLLED INTO VIEW, ONCE, when it is first drawn
  // (live QA WA-15). A deep link marked the row `aria-current` and left it at
  // y=2030, below the fold. Once per agent: a reader who then scrolls the
  // list away from it is not pulled back on the next re-read.
  const scrolledTo = useRef<string | null>(null)
  useEffect(() => {
    if (openTaskId === null || scrolledTo.current === openTaskId) return
    const row = [...document.querySelectorAll<HTMLElement>('.row.is-compact[data-task-id]')].find(
      (r) => r.getAttribute('data-task-id') === openTaskId,
    )
    if (row === undefined) return
    scrolledTo.current = openTaskId
    if (typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'nearest' })
  }, [openTaskId, rows])

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
  // LIVE IS READ IN FULL (QA12-20), so its count is exact over a capped page
  // whenever the live-state reads answered whole (`readStates`): `Live 5+` with
  // exactly five live claimed a doubt there was no reason for.
  const liveWhole = page.liveWhole === true
  const waitingWhole = page.waitingWhole === true
  const tabBounded = (t: Tab): boolean => bounded && !(t === 'live' && liveWhole) && !(t === 'waiting' && waitingWhole)
  const countText = (n: number, b: boolean = bounded): string => (b ? `${n}+` : String(n))
  const countSay = (n: number): string => `at least ${n} among the ${page.tasks.length} newest read; more rows exist beyond them`
  // ON LIVE, NO "MORE" WHEN THERE IS NO MORE (QA12-20): `200 loaded · more
  // beyond` and `Load 200 more` sat under five live rows. More pages exist,
  // but none of them holds a live agent, so Live does not offer them.
  const liveAll = shown === 'live' && liveWhole
  // The sort button's word and name (QA-111): the order the list is in, then what a press sets.
  // THE DIRECTION IN WORDS (visual QA run 6, 6:flow-find-failure-desktop-03):
  // `Ended ↓` left the arrow to say which way; the words the sidebar uses,
  // `newest first`, were only in the name. The name starts with the label (QA-030).
  const ownWord = shown === 'recent' ? 'Ended' : 'Submitted'
  const ownOrder = shown === 'recent' ? 'newest end first' : 'newest submitted first'
  const sortWord =
    sort.started === null ? `${ownWord}, newest first` : sort.started === 'desc' ? 'Started, newest first' : 'Started, oldest first'
  const sortSay =
    sort.started === null
      ? `${sortWord}; press to sort by start, newest first`
      : sort.started === 'desc'
        ? `${sortWord}; press for oldest start first`
        : `${sortWord}; press for the list’s own order, ${ownOrder}`
  // The workflow titles of the steps on screen (VQ-009, `RowTitleOf`).
  const titleOf = useRowTitles(rows.filter((t) => isStep(t) && stepWorkflowTitle(t, null) === null))
  const liveSay = `Every agent holding a pool slot is listed: the live states are read in full, not only from the ${page.tasks.length} newest rows.`

  return (
    <ListReadAt.Provider value={readAt}>
    <RowTitleOf.Provider value={titleOf}>
      {/* THE SECTION'S PAGES AS A STRIP (agents.html V1; #503 at 390): Live,
          Waiting and Recent with their counts, underlined rather than boxed.
          On a phone it sticks under the header, so the reader can change tab
          from anywhere down the list. */}
      {/* THE TABS PATTERN (visual QA run 2, a11y-keyboard-001): role=tab
          with none of it -- every tab its own Tab stop, the arrows dead. The
          shown tab is the strip's one Tab stop; Left / Right / Home / End
          move focus and show the tab they land on (automatic activation:
          the rows are already loaded, so showing a tab is instant). */}
      <div
        className="ag-list-tabs"
        role="tablist"
        aria-label="Agents"
        onKeyDown={(e) => {
          if (e.altKey || e.ctrlKey || e.metaKey) return
          const drawn = [...e.currentTarget.querySelectorAll<HTMLElement>(':scope > [role="tab"]')]
          const at = drawn.indexOf(e.target as HTMLElement)
          const to = at === -1 ? null : tabStep(e.key, at, TABS.length)
          if (to === null) return
          e.preventDefault()
          drawn[to]?.focus()
          if (TABS[to]!.id !== shown) chooseTab(TABS[to]!.id)
        }}
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={shown === t.id}
            tabIndex={shown === t.id ? 0 : -1}
            aria-label={`${t.label} ${countText(counts[t.id], tabBounded(t.id))}: ${t.say}${tabBounded(t.id) ? ` — ${countSay(counts[t.id])}` : ''}`}
            onClick={() => chooseTab(t.id)}
          >
            {t.label}{' '}
            <span className={`badge${tabBounded(t.id) ? ' is-bound' : ''}`}>{countText(counts[t.id], tabBounded(t.id))}</span>
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
            title={liveAll && bounded ? liveSay : scopeSay}
            onChange={(e) => setQuery(e.target.value)}
          />
          {/* `N LOADED` IS THE HEADER'S, ONCE (visual QA pass 1,
              overview-agents-13): the subtitle reads `27 loaded · 6 live`, and
              the same `27 loaded` stood again here. The scope stays only where
              it says something the header does not: every live row read, the
              phone's page, or rows beyond the page. */}
          {(liveAll && bounded) || phonePage || page.next_page_token ? (
            <span className="ag-scope" aria-label={liveAll && bounded ? liveSay : scopeSay}>
              {liveAll && bounded
                ? `all ${counts.live} live`
                : phonePage
                  ? `showing the ${page.tasks.length} most recent`
                  : 'more beyond'}
            </span>
          ) : null}
        </div>

        {/* THE CONTROLS ARE ONE ITEM OF THE TOOLBAR (V147, VQA2-R13). Each was
            its own flex item, so at 1440 on Recent the row filled and `Started`
            alone dropped to a second line -- what the WA-10 rule in
            agents.css says must not happen. Grouped, they wrap together, and
            the checkboxes and `Started` are one unbreakable tail inside the
            group, so a narrow list never strands the last control either. */}
        <div className="ag-tools">
          <label className="ag-filter ag-filter-profile">
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

          {/* OFFERED ONLY WHEN THERE IS A LIVE BROWSER TO SHOW (#139 at 390). With
              none, `Live 0` can only empty the list, and on a phone the switch
              is a 44px row of its own that put the first Agents row past the
              screen's midpoint. Kept while it is on, so the way back to All
              never disappears under the reader. */}
          {shown === 'live' && (liveBrowsers > 0 || browsersOnly) && (
            <Segmented
              label="All live agents or the browser agents among them"
              value={browsersOnly ? 'browsers' : 'all'}
              options={[
                { key: 'all', label: 'All', title: 'Every agent holding a pool slot' },
                tabBounded('live')
                  ? {
                      key: 'browsers',
                      label: (
                        <>
                          Browsers
                          <em className="is-bound" title={countSay(liveBrowsers)}>
                            {countText(liveBrowsers)}
                          </em>
                        </>
                      ),
                      title: 'Browser agents only: the ones waiting for you first, then running, then starting',
                    }
                  : {
                      key: 'browsers',
                      label: 'Browsers',
                      count: liveBrowsers,
                      title: 'Browser agents only: the ones waiting for you first, then running, then starting',
                    },
              ]}
              onChange={(k) => setBrowsersOnly(k === 'browsers')}
            />
          )}

          {/* ⤢ THEATER ALL (§6): the first row's Browser tab in the theater,
              whose prev and next walk the live browsers from there. The first
              row is the one waiting for a person, when one is. Not in the
              fixture build, which serves no live session, actions or
              recordings: there it opened on 'Not found' (visual QA pass 1,
              overview-agents-17). */}
          {browsers && rows.length > 0 && !USE_FIXTURES && (
            <a className="ag-theater-all" href={theaterPath(rows[0]!.id, null)}>
              {/* SENTENCE CASE, AND WHAT IT DOES (visual QA pass 1,
                  overview-agents-18): it read `theater all`. */}
              ⤢ Watch all browsers
            </a>
          )}

          {/* THE ADDRESS'S BROWSER FILTER, NAMED WHILE IT IS ON (BROWSER-USE-UI,
              #2322). It arrives from Platform counts' link, not from a control
              here, so the list says why it is narrowed and × gives every agent
              back. Only an explicit "not used" passes it, never a dash. */}
          {browserUnused && (
            <span className="ag-filter bu-filter" data-filter="browser-unused">
              <ToneMark tone="warn">browser not used</ToneMark>
              <button
                type="button"
                className="bu-filter-clear"
                aria-label="Clear the browser filter: show every agent, not only the browser agents that did not use the browser"
                title="Clear the browser filter"
                onClick={clearBrowserUnused}
              >
                ×
              </button>
            </span>
          )}

          <div className="ag-tools-tail">
            {failFirstOffered && (
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

            {/* IT SAYS THE ORDER THE LIST IS IN (visual QA QA-111, run 2). Off,
                it read `Started ⇅` over a Recent list ordered by the newest
                END, so its word named the one order the list was not in.
                It now reads as a column head reads its sort: `Ended ↓` on
                Recent (`Submitted ↓` on Live and Waiting) for the list's own
                order, `Started ↓` and `Started ↑` sorted. Its name and title
                start with that word and say what a press does next. Run 6
                (6:flow-find-failure-desktop-03) read the arrow as a glyph to
                decode: the direction is words now, `Ended, newest first`,
                `Started, oldest first`, and the arrow is gone. */}
            {/* SORTED BY START (#376): newest start, oldest start, then the
                list's own order. It was the Started column's head; the compact
                list has no columns, so it is a control of the list.
                ITS NAME STARTS WITH ITS WORD, AND IT ALWAYS SHOWS A DIRECTION
                (visual QA QA-030, QA-111, 2026-10-20): it was named `Sort by
                start time` beside a visible `Started` (axe
                label-content-name-mismatch), and off it drew no glyph, so the
                list's own order -- on Recent the newest END first -- read as
                an unexplained `Started`. PR 1688 drew `⇅` there; run 2 still
                read `Started` as the opposite of the order, so off it now
                says the list's own order in words (QA-111, above). */}
            <button
              type="button"
              className="ag-sort"
              aria-pressed={sort.started !== null}
              aria-label={sortSay}
              title={sortSay}
              onClick={sort.onStarted}
            >
              {sortWord}
            </button>
          </div>
        </div>
      </div>

      {rows.length === 0 && idQuery !== null ? (
        // A WHOLE TASK ID IS LOOKED UP, NOT SEARCHED FOR (live QA WA-1).
        <IdLookup
          key={idQuery}
          id={idQuery}
          loaded={page.tasks.find((t) => t.id === idQuery) ?? null}
          shown={shown}
          rowsRead={page.tasks.length}
          now={now}
          vouched={vouched}
          classes={classes}
          onOpen={openElsewhere}
        />
      ) : rows.length === 0 && browserUnused ? (
        // THE BROWSER FILTER FOUND NONE, IN THE LOADED ROWS ONLY. Over a capped
        // page that is a partial answer and says so; whole, it is a real zero.
        <div className="ctl-empty" data-empty="browser-unused">
          <h3>
            <Mark
              kind={bounded ? 'partial' : 'zero'}
              say={`No browser agent that did not use its browser among the ${page.tasks.length} loaded rows of ${shown}${needle !== '' ? ` matches “${query.trim()}”` : ''}.${bounded ? ' More rows exist beyond them, so this is not an answer for the platform.' : ''} An agent whose browser use was not recorded is not listed here.`}
            />{' '}
            {`no browser left unused in the ${page.tasks.length} loaded`}
            {needle !== '' && ` matching “${query.trim()}”`}
          </h3>
        </div>
      ) : rows.length === 0 && bounded && (needle !== '' || stateFilter !== null) ? (
        // A SEARCH OR A STATE OVER A CAPPED PAGE FOUND NOTHING IN THE PAGE,
        // AND THAT IS ALL IT FOUND (live QA WA-1). It said `real zero`, a
        // measured absence, about rows it never read.
        <div className="ctl-empty">
          <h3>
            <Mark
              kind="partial"
              say={`No ${stateFilter !== null ? `${RECENT_STATE_OF[stateFilter]} ` : ''}agent among the ${page.tasks.length} loaded rows of ${shown}${needle !== '' ? ` matches “${query.trim()}”` : ''}. More rows exist beyond them, so this is not an answer for the platform: load more, or paste a whole task id to look it up.`}
            />{' '}
            {stateFilter !== null ? `nothing ${stateFilter} in the ${page.tasks.length} loaded` : `nothing in the ${page.tasks.length} loaded`}
            {needle !== '' && ` matching “${query.trim()}”`}
          </h3>
        </div>
      ) : rows.length === 0 ? (
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
                  ? 'No agent is holding a pool slot right now.'
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
        <WaitingGroups rows={rows} now={now} vouched={vouched} onOpen={openRow} openTaskId={openTaskId} classes={classes} grouped={grouped} bounded={tabBounded('waiting')} taskById={taskById} />
      ) : grouped && shown !== 'live' ? (
        <GroupedRows rows={rows} now={now} vouched={vouched} onOpen={openRow} openTaskId={openTaskId} classes={classes} taskById={taskById} />
      ) : (
        <FlatRows rows={rows} now={now} vouched={vouched} onOpen={openRow} openTaskId={openTaskId} classes={classes} taskById={taskById} />
      )}

      {/* LOAD MORE (live QA WA-2): the list stopped at its first page and
          said `more beyond` with nothing to reach it by. The next page is
          read into the same list, so every count, filter and search above
          runs over it as well. */}
      {bounded && !liveAll && (
        <div className="ag-more">
          <button type="button" className="ag-more-btn" disabled={moreRead.kind === 'reading'} onClick={onMore}>
            {moreRead.kind === 'reading' ? 'Loading…' : `Load ${page.asked} more`}
          </button>
          <span className="ag-more-note">{page.tasks.length} loaded · more beyond</span>
          {moreRead.kind === 'failed' && (
            <span className="ag-more-note is-bad" role="alert">
              <Mark kind="unread" say={`The next page could not be read: ${moreRead.why}`} /> {moreRead.why}
            </span>
          )}
        </div>
      )}
    </RowTitleOf.Provider>
    </ListReadAt.Provider>
  )
}

/** A whole task id, as the list's search box takes it: `task_` and twenty hex digits. */
export const WHOLE_TASK_ID = /^task_[0-9a-f]{20}$/

/**
 * A WHOLE TASK ID THAT IS NOT IN THE TAB (live QA WA-1, 2026-10-11).
 *
 * Pasting an id is the main reason to use the search box, and the search ran
 * over the loaded page only: `task_7898edecb15543d98553`, the 201st newest,
 * read `real zero · nothing in recent matching "task_…"` -- a measured
 * absence, for a task that existed. Now a whole id that the tab's rows do not
 * hold is looked for in two places, in order:
 *
 *   1. the loaded page's other tabs -- a pasted id on Live of an agent that
 *      has finished is in Recent, and the row says so;
 *   2. the server, `GET /v1/tasks/{id}`, which answers for the whole tenant.
 *      A 404 there IS a measured absence, and only then is it drawn as one.
 */
function IdLookup({
  id,
  loaded,
  shown,
  rowsRead,
  now,
  vouched,
  classes,
  onOpen,
}: {
  id: string
  /** The row, when the loaded page holds it under another tab. */
  loaded: Task | null
  shown: Tab
  rowsRead: number
  now: number
  vouched: number
  classes: ResourceClasses | null
  /** Opens a task under the tab its state puts it in. */
  onOpen: (task: Task) => void
}) {
  const [got, setGot] = useState<Result<Task> | null>(null)
  useEffect(() => {
    if (loaded !== null) return
    let gone = false
    void loadTask(id).then((r) => {
      if (!gone) setGot(r)
    })
    return () => {
      gone = true
    }
  }, [id, loaded])
  const found = loaded ?? (got !== null && (got.status === 'ok' || got.status === 'stale') ? got.data : null)
  if (found !== null) {
    const where = tabOf(found)
    return (
      <div className="ag-idfound" data-found={loaded !== null ? 'page' : 'server'}>
        <p className="ag-idfound-say">
          {loaded !== null
            ? `In ${where}, not ${shown}: this agent is ${found.state.toLowerCase()}.`
            : `Found by its id. It is not among the ${rowsRead} loaded rows; it is in ${where}.`}
        </p>
        <div className="rows">
          <TaskRow task={found} now={now} vouched={vouched} onOpen={() => onOpen(found)} classes={classes} />
        </div>
      </div>
    )
  }
  if (got === null || got.status === 'loading') {
    return (
      <div className="ctl-empty">
        <h3>
          <Mark kind="pending" say={`Looking up ${id} by its id: it is not among the ${rowsRead} loaded rows.`} /> looking up {id}
        </h3>
      </div>
    )
  }
  if (got.status === 'error' && got.error.httpStatus === 404) {
    return (
      <div className="ctl-empty">
        <h3>
          <Mark
            kind="zero"
            say={`The server has no agent ${id} in this tenant (HTTP 404). This is an answer for the whole tenant, not only for the loaded rows; it could be another tenant's agent.`}
          />{' '}
          no agent {id} in this tenant
        </h3>
      </div>
    )
  }
  return (
    <div className="ctl-empty">
      <h3>
        <Mark
          kind="unread"
          say={`${id} is not among the ${rowsRead} loaded rows, and looking it up by its id failed${got.status === 'error' ? `: ${got.error.message}` : ''}. This is a failed read, not an absence.`}
        />{' '}
        could not look up {id}
      </h3>
    </div>
  )
}

/**
 * THE LIST'S OWN ORDER, with the Started sort off (live QA WA-4, 2026-10-11).
 *
 * It was `updated_at`, newest first, on every tab, and a live task's
 * `updated_at` moves with each checkpoint and status write: over three 5s
 * re-reads one Live row jumped from 8th to 1st, under the pointer and the
 * keyboard focus. Live and Waiting are now in submission order, newest first
 * (`created_at`, which never changes), so a re-read moves no row that is
 * still there. Recent keeps the newest change first: a finished task's
 * `updated_at` is when it finished, and it does not move again. Ties go to
 * the id, so equal times cannot swap places between reads either.
 */
export function listOrder(a: Task, b: Task, tab: Tab): number {
  const ka = tab === 'recent' ? a.updated_at : a.created_at
  const kb = tab === 'recent' ? b.updated_at : b.created_at
  if (ka !== kb) return ka < kb ? 1 : -1
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
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
  taskById = null,
}: {
  rows: Task[]
  /** The clock the row's elapsed and waiting ages run on. */
  now: number
  /** The clock capped at the read, for the silent-worker line (`rowClock`). */
  vouched: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
  /** Every row the list read, by id: what a cascade cancel names its failed parent from (N23). */
  taskById?: ReadonlyMap<string, Task> | null
}) {
  const shared = flatShared(rows, rows.map((t) => rowReason(t, classes, vouched, taskById)))
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
          taskById={taskById}
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
  taskById = null,
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
  /** Every row the list read, by id: a workflow group's state is read over all of them. */
  taskById?: ReadonlyMap<string, Task> | null
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
    { key: 'not_eligible', title: 'Not yet eligible', rows: [] },
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
              <GroupedRows rows={g.rows} now={now} vouched={vouched} onOpen={onOpen} openTaskId={openTaskId} classes={classes} taskById={taskById} />
            ) : (
              <FlatRows rows={g.rows} now={now} vouched={vouched} onOpen={onOpen} openTaskId={openTaskId} classes={classes} />
            )}
          </section>
        ),
      )}
    </>
  )
}

/**
 * The Waiting group a task that needs no action is filed under: its park
 * reason, `Not yet eligible` for a queued one, or `No room`.
 *
 * A QUEUED TASK IS NOT SHORT OF ROOM (visual QA pass 2, overview-agents-56):
 * it sat under `No room` while Overview's waiting card calls it not yet
 * eligible (`waitGroups`). Only a READY task is eligible and waits for a pool.
 */
export function waitGroupOf(t: Task): { key: string; title: string } {
  if (t.state === 'QUEUED') return { key: 'not_eligible', title: 'Not yet eligible' }
  if (t.state !== 'PARKED') return { key: 'no_room', title: 'No room' }
  const r = t.park_reason ?? null
  if (r === null) return { key: 'park:none', title: 'Parked, reason not recorded' }
  const word = (PARK_WORD as Readonly<Record<string, string>>)[r] ?? r
  return { key: `park:${r}`, title: word.charAt(0).toUpperCase() + word.slice(1) }
}

/**
 * Recent's search (#99): the task id, the step id and the workflow id,
 * case-insensitively. `needle` is already trimmed and lower-cased.
 *
 * AND THE NAME THE ROW IS DRAWN BY (visual QA pass 1, overview-agents-15):
 * the box says "Find by name or task id", and `flaky` found nothing beside a
 * live `Fix flaky lease heartbeat test`. The summary row's `metadata.title`
 * -- a lone agent's own title, a step's workflow title -- and `agentName`'s
 * `claude-code agent · a073aff5` are searched too, so what the row says is
 * what the search finds.
 */
export function matchesQuery(t: Task, needle: string): boolean {
  const title = t.metadata?.title
  return [t.id, t.step_id, t.workflow_id, typeof title === 'string' ? title : null, agentName(t)].some(
    (v) => v != null && v.toLowerCase().includes(needle),
  )
}

/**
 * A row's why line and whether it asks a person to act -- `TaskRow`'s own
 * reading. `full` is the whole sentence when `text` is a short form of it
 * (`waitingShort`): the line's title and the row's accessible name say it.
 */
type RowReason = { text: string; warn: boolean; full?: string }

/** The sentence a row's title and accessible name carry: the whole one, where the line is cut. */
function whySaid(r: RowReason): string {
  return r.full ?? r.text
}

/**
 * A ROW WHOSE MARK IS DRAWN IN --bad (marks.tsx `STATE_MARK` hue `bad`):
 * FAILED and DEAD_LETTERED. Its reason takes the same red (V146), so the
 * mark, the leading rule and the sentence say one thing.
 */
export function failedRow(task: Pick<Task, 'state'>): boolean {
  return task.state === 'FAILED' || task.state === 'DEAD_LETTERED'
}

function rowReason(
  task: Task,
  classes: ResourceClasses | null,
  now: number,
  taskById: ReadonlyMap<string, Task> | null = null,
): RowReason {
  // A SILENT WORKER FIRST (#179, AG-14's fourth kind): `whyAgent` writes
  // nothing for a task that holds a slot, and this is the one thing about one
  // that needs a person.
  const silent = silentWorkerLine(task, now)
  if (silent !== null) return { text: silent, warn: true }
  // A STOP AT ITS BUDGET IS NOT A PLAIN FAILURE (#2459): the worker kept and
  // published the work, so the line says that and the amount, not the
  // `budget_exceeded: ...` error text, and asks nobody to act.
  if (stoppedAtBudgetRow(task)) return { text: budgetStopLine(task), warn: false }
  // THE TASK'S WEIGHT, FROM THE CATALOGUE (#66) -- never `RESOURCE_UNITS`,
  // which is what fed `whyAgent`/`whyNeedsAction` a `full` reading for a pool
  // too small to ever admit this task. `classUnits` is null exactly when the
  // row's display units also are: the catalogue read is the same one and the
  // display badge keeps its own bundled fallback.
  const units = classUnits(classes, task.resource_class)
  const warn = whyNeedsAction(task, units)
  // THE TASK'S OWN REASON ON A WAITING ROW (WAIT-UI-LISTS, #1782): every
  // merge step read "Waiting for the pull request checks to finish" during
  // the 2026-10-20/21 CI stall, queued, failing or done alike. WAIT-API's
  // `waiting.summary` says which; the line is its short form and the whole
  // sentence is `full`. The ink is still `whyNeedsAction`'s: the reason got
  // specific, not more urgent. An API that sends no `waiting` keeps today's
  // line.
  const short = tabOf(task) === 'waiting' && task.waiting ? poolWords(waitingShort(task.waiting)) : null
  if (short !== null) return { text: short, warn, full: task.waiting!.summary.trim() }
  // A READY ROW WITH ONLY `waiting_for` NAMES ITS POOL TOO (QA2-FIX-12
  // review, overview-agents-56): `waitingLine` read `waiting for:
  // provider:anthropic:tenant:u-bogdan 5/5 units` under `No room`. The line
  // names the pool by `poolLabel`; the raw key stays in the row's title.
  const raw = waitingLine(task)
  const pool = task.waiting_for?.lead?.pool
  if (raw !== null && pool) return { text: raw.replace(pool, poolLabel(pool)), warn, full: raw }
  // A CASCADE CANCEL NAMES THE STEP THAT FAILED (visual QA run 4, N23) when
  // that step is among the rows the list read; the meta line keeps its
  // `to cancel` span note (U10a D20) and the cause is said here.
  const text = whyAgent(task, units, taskById)
  // A ROW UNDER `NO ROOM` WITH NO REASON SAYS SO (visual QA pass 1,
  // overview-agents-56): a queued or ready row the scheduler recorded no
  // blocker for drew no line at all, so the reader could not tell a pool
  // that was full from a reason that was never written. A row that carries
  // one (`blocked_by`, `waiting`) names its pool above.
  if (text === '' && (task.state === 'READY' || task.state === 'QUEUED')) return { text: NO_BLOCKER, warn }
  return { text, warn }
}

/**
 * A capacity short line's pool in words (visual QA pass 2, overview-agents-56):
 * `capacity: runner:merge full` named the raw key; `poolLabel` is the name
 * every other pool on the console is drawn by. The whole sentence, raw key
 * and all, stays the row's title.
 */
export function poolWords(short: string | null): string | null {
  if (short === null) return null
  return short.replace(/^capacity: (\S+)/, (_, pool: string) => `capacity: ${poolLabel(pool)}`)
}

/**
 * The why line of a waiting row the scheduler recorded no blocker for. It is
 * also a grouped list's line for every such row, after their count (`2 no
 * pool named…`), so it names no one row: it read `2 No pool named: … this one
 * waits` (QA1-FIX-07 re-review).
 */
export const NO_BLOCKER = 'no pool named: the scheduler recorded no reason for the wait'

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
    if (i === 0 || !shareable(r) || whySaid(reasons[i - 1]!) !== whySaid(r)) return false
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
  reasons: { text: string; full: string; count: number }[]
} {
  const shared = reasons.map(() => false)
  const said: { text: string; full: string; count: number }[] = []
  let i = 0
  while (i < reasons.length) {
    let j = i + 1
    // THE WHOLE SENTENCE DECIDES (WAIT-UI-LISTS): two CI parks both cut to
    // "CI #1779: 5 pending" may still name different checks.
    while (j < reasons.length && whySaid(reasons[j]!) === whySaid(reasons[i]!) && shareable(reasons[j]!)) j++
    if (shareable(reasons[i]!) && j - i >= 2) {
      for (let k = i; k < j; k++) shared[k] = true
      const known = said.find((s) => s.full === whySaid(reasons[i]!))
      if (known) known.count += j - i
      else said.push({ text: reasons[i]!.text, full: whySaid(reasons[i]!), count: j - i })
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
  taskById = null,
}: {
  rows: Task[]
  now: number
  vouched: number
  onOpen: (taskId: string) => void
  openTaskId: string | null
  classes: ResourceClasses | null
  /** Every row the list read, by id: what a cascade cancel names its failed parent from (N23). */
  taskById?: ReadonlyMap<string, Task> | null
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
  const titleOf = useContext(RowTitleOf)

  return (
    <>
      {groups.map(([wf, tasks]) => {
        // THE WORKFLOW'S STATE, NOT THIS TAB'S (visual QA pass 1,
        // overview-agents-54): rolled up over this tab's rows, one workflow
        // read `succeeded` on Recent and `waiting` on Waiting while
        // Workflows said `running`. It is rolled up over every step of it the
        // list read, whichever tab each is on, as Workflows rolls up its
        // steps. "No workflow" is no workflow, so it has no state.
        const steps = wf === '' ? [] : taskById !== null ? [...taskById.values()].filter((t) => t.workflow_id === wf) : tasks
        const roll = wf === '' ? null : rollupState((steps.length > 0 ? steps : tasks).map((t) => t.state))
        // THE HEADING IS THE WORKFLOW'S TITLE (overview-agents-53): it was
        // the raw id in large mono, over rows that each repeated the title.
        // The id is the link's hover; an untitled workflow keeps its id.
        const wfName = wf === '' ? null : stepWorkflowTitle(tasks[0]!, titleOf)
        const said = groupReasons(tasks.map((t) => rowReason(t, classes, vouched, taskById)))
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
              ) : wfName !== null ? (
                <a className="ctl-link" href={workflowHref(wf)} title={wf}>
                  {wfName}
                </a>
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
              {roll !== null && <ToneMark tone={rollTone(roll)}>{roll}</ToneMark>}
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
              <p className="group-why" key={r.full} title={r.full === r.text ? undefined : r.full}>
                <b>{r.count}</b> {r.text}
              </p>
            ))}
            <UnderWorkflow.Provider value={wf !== ''}>
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
                  taskById={taskById}
                  whyShared={said.shared[i]}
                />
              ))}
            </div>
            </UnderWorkflow.Provider>
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
export function attemptsUsed(
  task: Pick<Task, 'attempt_count' | 'max_attempts'>,
  runs: number | null = task.attempt_count,
): {
  over: boolean
  text: string
  say: string
} {
  const over = task.attempt_count > task.max_attempts
  // THE HEADER'S WORDS AND THE HEADER'S COUNT (live-browser QA p12, QA12-12):
  // a resumed attempt read `try 1/3` here and `attempt 2 of 3` in the task
  // header. `runs` is `timesRun` over the attempt documents, as the header's
  // is; the charged counter is still what the ceiling is judged on (AG-15),
  // and the sentence says so when the two differ.
  //
  // NOT YET READ IS SAID, NOT GUESSED (visual QA QA-026, 2026-10-20). `null`
  // is a row whose attempt documents are still being read. It printed the
  // charged counter meanwhile, `attempt 1 of 3`, beside a header that over
  // the same three documents said `attempt 3 of 3`; the row now holds the
  // place until it has the figure the header has.
  if (runs === null) {
    const text = `attempt … of ${task.max_attempts}`
    return { over, text, say: over ? `${task.attempt_count} attempts against a cap of ${task.max_attempts}: ${task.attempt_count - task.max_attempts} over the ceiling` : `reading how many times it ran, of a cap of ${task.max_attempts}` }
  }
  const text = runs > 0 ? `attempt ${runs} of ${task.max_attempts}` : 'no attempt yet'
  const charged = runs !== task.attempt_count ? `, ${task.attempt_count} counted against the cap` : ''
  return {
    over,
    text,
    say: over
      ? `${task.attempt_count} attempts against a cap of ${task.max_attempts}: ${task.attempt_count - task.max_attempts} over the ceiling`
      : `${text}${charged}`,
  }
}

/**
 * The attempt documents each row last read, by task id (`useRuns`), the oldest
 * dropped past `RUNS_HELD`. It outlives a tenant or workspace switch in one
 * session. That is deliberate: an entry is found only by a row with that
 * exact task id, and it only seeds the figure drawn while that row's own read
 * is in flight; the read's answer replaces it.
 */
const RUNS_READ = new Map<string, { key: string; attempts: readonly unknown[] }>()
/** A page's worth of rows and its next: more than one list ever draws. */
const RUNS_HELD = 500

/**
 * HOW MANY TIMES A ROW'S TASK RAN, read as the task header reads it
 * (`timesRun` over `GET /v1/tasks/{id}/attempts`, QA12-12). Only for THE ROW
 * THE INSPECTOR HAS OPEN, when it shows the figure -- live, or over its cap --
 * and re-read only when the
 * task says its attempts changed (its generation, its counter or its state),
 * the header's own key, never on every list poll. Until the documents are
 * read, or when the read fails, the task's own counter, as the header falls
 * back to it.
 *
 * EVERY OTHER ROW TAKES ITS TOTAL FROM THE ROW (load-speed investigation, the
 * owner's pick 2026-11-01): one `/attempts` read per live row made a list of
 * twelve live agents twelve more requests on every key change, beside the
 * page. The row's `attempt_count` is the task's own counter, the figure the
 * header falls back to; the open row is the one drawn beside that header, so
 * it alone reads the documents and cannot contradict it.
 *
 * UNTIL THE FIRST READ LANDS, NULL (visual QA QA-026): the counter is the
 * figure only once a read has FAILED, never while one is in flight -- in
 * flight it is the one figure that can contradict the header beside it. A
 * re-read for a changed key keeps the documents already read (`timesRun`
 * still takes the larger of them and the new counter), so a row that has
 * its figure never drops back to the placeholder on a state change.
 */
function useRuns(task: Task, shows: boolean): number | null {
  const key = `${task.id}:${task.current_generation}:${task.attempt_count}:${task.state}`
  // A ROW DRAWN AGAIN KEEPS ITS FIGURE (visual QA pass 1, overview-agents-16):
  // a search that changes the list mounts its rows afresh, and each read
  // `attempt … of 3` for one read cycle though `1` was already known. The
  // documents a row last read are kept by task id, so a new row starts from
  // them while its own read is in flight; `timesRun` still takes the larger
  // of them and the task's counter, as a re-read for a changed key does.
  const [got, setGot] = useState<{ id: string; key: string; attempts: readonly unknown[] | null } | null>(() => {
    const held = RUNS_READ.get(task.id)
    return held === undefined ? null : { id: task.id, key: held.key, attempts: held.attempts }
  })
  useEffect(() => {
    if (!shows) return
    let live = true
    // A read that throws or answers nothing is a failed read: the counter.
    Promise.resolve()
      .then(() => loadAttempts(task.id))
      .then((r) => r ?? null, () => null)
      .then((r) => {
        if (!live) return
        const attempts = r === null ? null : r.status === 'ok' || r.status === 'stale' ? r.data.attempts : r.status === 'empty' ? [] : null
        setGot({ id: task.id, key, attempts })
        if (attempts !== null) {
          RUNS_READ.delete(task.id)
          RUNS_READ.set(task.id, { key, attempts })
          if (RUNS_READ.size > RUNS_HELD) RUNS_READ.delete(RUNS_READ.keys().next().value!)
        }
      })
    return () => {
      live = false
    }
    // `key` names everything the read depends on, the task id included.
  }, [key, shows])
  if (!shows) return task.attempt_count
  if (got === null || got.id !== task.id) return null
  return timesRun(task, got.attempts)
}

/** The qualifier on a CANCELLED row's elapsed figure (#163); `TaskRow` says why. */
export const CANCEL_SPAN = '(last start to cancel, may include parked time)'
/**
 * The mark line two carries for it; the sentence is its title (U10a D20).
 * Past tense, after `ran 6m 32s` (visual QA pass 2, ovag2-10): `to cancel`
 * said neither who cancelled nor what the figure spanned.
 */
export const CANCEL_NOTE = 'cancelled by request'

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
  taskById = null,
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
  /** Every row the list read, by id: what a cascade cancel names its failed parent from (N23). */
  taskById?: ReadonlyMap<string, Task> | null
}) {
  const why = rowReason(task, classes, vouched, taskById)
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
 *   line 2  profile (and model) · owner · `attempt n of m` on a live row,
 *           in the task header's words and from its count (QA12-12)
 *   then    the reason it waits or stopped, where there is one -- never
 *           dropped, for the reason `TaskRow` gives (the phone question)
 *
 * THE NAME IS THE STEP, THE TITLE, OR THE TASK ID. The step id is the name a
 * workflow gave it (#94), led by the workflow's title once read (VQ-009); a
 * lone task is named by its own title, which the summary row carries
 * (`ownTitle`, SUMMARY-TITLE), and without one by the id prefix the rest of
 * the product prints (`shortTaskId`). Either way the whole id is printed
 * under the name, with its copy (`TaskIdLine`, #94).
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
  const live = tabOf(task) === 'live'
  // THE RIGHT-HAND COLUMN HOLDS TIMES, NOTHING ELSE (visual QA QA-079,
  // QA-110, 2026-10-20). It mixed `1m 0s` with the words `leased` and
  // `dispatched` -- `elapsed` gives a slot-holding state its word alone,
  // because nothing records when it was leased (types.ts) -- so the column
  // read as neither. That word is line two's now, as `starting` is, and the
  // column is blank: there is no time to give. And a finished row's column
  // was its run length, so on a `failed` list two `5m 0s` rows could not say
  // which failure was the newest; it is WHEN IT ENDED, which is also the
  // order Recent is in (`listOrder`), and the run length moves to line two.
  const finished = TERMINAL_STATES.has(task.state)
  const endedAt = finished ? (task.completed_at ?? task.updated_at) : null
  const wordOnly = !finished && el.phase === 'waiting' && !el.ticking
  // AN AGE FOR THE ROWS WITH NO START (visual QA pass 1, overview-agents-19):
  // a leased or dispatched row's column was blank beside every other live
  // row's `8d 21h`. Nothing records when it was leased, so its age is from
  // when it was SUBMITTED, and the cell's title says that is what it counts.
  const submitted = wordOnly ? Date.parse(task.created_at ?? '') : NaN
  const sinceSubmit = Number.isFinite(submitted) ? formatDuration(now - submitted) : null
  const whenText = endedAt !== null ? timeAgo(endedAt, now) : wordOnly ? (sinceSubmit ?? '') : el.text
  const whenTitle =
    endedAt !== null
      ? `ended ${new Date(endedAt).toLocaleString()}`
      : wordOnly && sinceSubmit !== null
        ? `${sinceSubmit} since it was submitted at ${new Date(submitted).toISOString().slice(0, 16).replace('T', ' ')}Z: nothing records when it was ${task.state.toLowerCase()}`
        : undefined
  const ran = finished && el.phase !== 'unknown' ? (el.phase === 'ran' ? `ran ${el.text}` : el.text) : null
  const tries = attemptsUsed(task, useRuns(task, open && (live || task.attempt_count > task.max_attempts)))
  // EVERY LIVE AND WAITING ROW SAYS ITS STATE IN WORDS (visual QA pass 1,
  // overview-agents-11, -56): a running row was told only by its dot, and a
  // waiting row's `queued` or `ready` was in its accessible name and nowhere
  // on screen, beside two marks (ring, dotted ring) a reader cannot name.
  const waitingRow = tabOf(task) === 'waiting'
  const stateWord = live
    ? wordOnly
      ? el.text
      : task.state === 'STARTING'
        ? 'starting'
        : task.state.toLowerCase()
    : waitingRow
      ? task.state.toLowerCase()
      : null
  // NAMED FROM WHAT IT IS (walkthrough G): its step, else `browser check ·
  // 02e9705a` -- it was the bare hash. `agentName`'s rule, drawn below.
  // A STEP LEADS WITH ITS WORKFLOW'S TITLE (VQ-009), once it is read:
  // `<title> · implement`, Overview's `rowLabel`.
  const titleOf = useContext(RowTitleOf)
  const underWorkflow = useContext(UnderWorkflow)
  const wfFull = stepWorkflowTitle(task, titleOf)
  const wfTitle = underWorkflow ? null : wfFull
  // A LONE AGENT LEADS WITH ITS OWN TITLE (SUMMARY-TITLE), off the row itself.
  const loneTitle = ownTitle(task)
  const name = loneTitle ?? rowLabel(task, wfFull)
  const owner = task.submitted_by?.split('@')[0] ?? null
  const profile = task.model ? `${task.runner_profile} · ${task.model}` : task.runner_profile
  // WHETHER ITS BROWSER WAS USED (BROWSER-USE-UI, #2322): an ended agent on a
  // browser runtime only. A live one has no verdict yet, and its row already
  // carries the browser's thumbnail; the inspector says "recorded when it ends".
  const browserUse = browserUseOf(task)
  const browserEnded = browserUse !== null && browserUse.ended ? browserUse : null
  // AN UNKNOWN VERDICT DRAWS NOTHING ON THE ROW (visual QA pass 2,
  // overview-agents-27): `claude-opus-5 · browser — · alice` said nothing
  // between the model and the owner. The row's name keeps the reason (#100),
  // and the inspector draws the dash with it.
  const browserMark = browserEnded !== null && browserEnded.kind !== 'unknown' ? browserEnded : null
  // A BROWSER AGENT WHOSE BROWSER IS NOT UP YET (QA12-20): listed under
  // Browsers with the running ones, and marked, so it is not read as a
  // browser to watch.
  const startingBrowser = task.state === 'STARTING' && isLiveBrowser(task)
  // THE ONE FACT THAT CONTRADICTS THE STATE WORD: a stop is requested and the
  // lease is still held (invariant 3), so the row still reads RUNNING.
  const cancelling = task.cancel_requested && !TERMINAL_STATES.has(task.state)
  // A LIVE-BROWSER AGENT WAITING FOR A PERSON (live-browser.md §6): the row
  // is yellow and says so, held or parked, on whichever tab it is on.
  const waitsForYou = needsYou(task)
  // THE BUDGET (#2459): a live agent past its 80% warning, or an ended one
  // the budget stopped. No read and no mark for an agent with no budget.
  const { warning: budgetWarning } = useBudgetWarning(task)
  const budgetSay = budgetRowSay(task, budgetWarning, live)
  const cardId = `ag-card-${task.id}`

  const show = (row: HTMLElement) => {
    if (!stripFolded()) return
    const r = row.getBoundingClientRect()
    setCard({ top: r.top, left: r.right + 8 })
  }
  const hide = () => setCard(null)
  const move = (from: HTMLElement, step: 1 | -1): boolean => {
    const scope: ParentNode = from.closest('.work') ?? from.ownerDocument
    const all = [...scope.querySelectorAll<HTMLElement>('.row.is-compact > .cr-open')]
    const next = all[all.indexOf(from) + step]
    if (next === undefined) return false
    next.focus()
    return true
  }

  // THE OPENER'S NAME IS WHAT THE ROW SAID (#100: a reason hidden from sight
  // is never hidden from the row's name). As a button the row was named by
  // all its text; the opener is empty, so the same facts are its label.
  const say = [
    `${name} · ${task.id}`,
    task.state.toLowerCase().replace('_', '-'),
    endedAt !== null ? `ended ${whenText}` : whenText === '' ? null : whenText,
    ran,
    profile,
    browserEnded !== null ? browserUseSay(browserEnded) : null,
    owner ?? 'no owner recorded',
    live || tries.over ? tries.say : null,
    startingBrowser ? 'browser starting' : null,
    cancelling ? 'cancelling' : null,
    waitsForYou ? 'needs you' : null,
    budgetSay,
    cancelSpan ? CANCEL_SPAN : null,
    why.text === '' ? null : whySaid(why),
  ]
    .filter((p): p is string => p !== null && p !== undefined)
    .join(' · ')

  // THE SUB-LINE'S WHOLE TEXT IS ITS TITLE (BT-017, #2423): on a desktop row
  // the line is one line cut at its end, and the part cut is the owner, so
  // `… · claude-opus-5-5 · b…` named nobody. Its facts in the line's order,
  // with the owner's whole address where the line prints the name before @.
  const subSaid = [
    stateWord,
    live ? tries.text : null,
    budgetSay,
    profile,
    browserMark !== null ? browserUseSay(browserMark) : null,
    task.submitted_by ?? 'no owner recorded',
    ran,
    !live && !waitingRow && wordOnly ? el.text : null,
    !live && tries.over ? tries.text : null,
    cancelling ? 'cancelling' : null,
    waitsForYou ? 'needs you' : null,
    cancelSpan ? CANCEL_SPAN : null,
  ]
    .filter((p): p is string => p !== null && p !== undefined && p !== '')
    .join(' · ')

  // THE ROW IS NOT A BUTTON; IT HOLDS ONE (visual QA QA-032, 2026-10-20). It
  // was a `role="button"` div around the copy-id button, which axe reports as
  // nested-interactive: a screen reader flattens a button's content, so the
  // copy was announced as part of "open this agent" and could not be reached
  // as itself. The opener is `.cr-open`, an empty button laid over the whole
  // row (styles/agents.css) and named by the agent and its id; the copy is
  // its sibling, raised above it. A pointer still opens from anywhere on the
  // row (the div's click, which the opener's own click bubbles to), and the
  // keyboard walks the openers with ↑/↓ as it walked the rows.
  // `tabIndex={-1}` keeps the row a target for code that focuses it by its
  // `data-task-id` (agent-split/focus.ts returns focus there), and its focus
  // passes straight to the opener.
  //
  // THE ROW IS MARKED TOO (visual QA run 2, task-012). QA-032 moved
  // `aria-current` onto the opener, and styles.css draws the selection --
  // the `--surface-2` wash and the leading rule -- on `.row[aria-current]`,
  // so the open agent's row looked like every other. The opener keeps it
  // for a screen reader (focus lands there); the row carries it for the eye.
  return (
    <div
      className={`row clickable is-compact${waitsForYou ? ' is-needs-you' : ''}`}
      tabIndex={-1}
      aria-current={open ? 'true' : undefined}
      data-task-id={task.id}
      onClick={(e) => {
        // OPENED BY A KEY, FOCUS GOES INTO THE AGENT (a11y-keyboard-002):
        // Enter on the opener is a click with `detail` 0; a pointer's is 1+.
        const from = e.currentTarget.querySelector<HTMLElement>(':scope > .cr-open')
        if (e.detail === 0 && from !== null && e.target === from) focusSplitOnOpen(task.id, from)
        onOpen(task.id)
      }}
      onFocus={(e) => {
        // The row already has focus where it sits, so handing it to the opener
        // scrolls nothing (overview-agents-55): a plain `focus()` here nudged the
        // page column as the split opened beside the list.
        if (e.target === e.currentTarget)
          e.currentTarget.querySelector<HTMLElement>(':scope > .cr-open')?.focus({ preventScroll: true })
      }}
      onMouseEnter={(e) => show(e.currentTarget)}
      onMouseLeave={hide}
    >
      <button
        type="button"
        className="cr-open"
        aria-label={say}
        aria-current={open ? 'true' : undefined}
        aria-describedby={card !== null ? cardId : undefined}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            if (move(e.currentTarget, e.key === 'ArrowDown' ? 1 : -1)) e.preventDefault()
          }
        }}
        onFocus={(e) => show(e.currentTarget.parentElement!)}
        onBlur={hide}
      />
      <StateMark state={task.state} />
      <span className="agent cr-name">
        {/* The whole id is the name's hover; a lone task's short id is
            the id treatment, as a step's is on line two. */}
        {wfTitle !== null ? (
          // THE STEP IS NEVER THE PART CUT (agents.css `.cr-wf`): it is what
          // tells one workflow's rows apart, so the title gives way first.
          <b className="is-titled" title={`${name} · ${task.id}`}>
            <span className="cr-wf">{wfTitle}</span>
            <span className="cr-step">{'\u00a0· '}{task.step_id}</span>
          </b>
        ) : loneTitle !== null ? (
          // One text, so `.cr-name b`'s ellipsis cuts a long one (not the
          // step's flex `is-titled`); the whole title and the id are the hover.
          <b title={`${name} · ${task.id}`}>
            {loneTitle}
          </b>
        ) : (
          // THE ID ONCE (visual QA pass 1, overview-agents-14): an untitled
          // row read `claude-code agent · 460409cd` over `task_460409cd`.
          // The whole id is the line under the name, so the name is the kind.
          <b title={task.id}>{task.step_id ?? agentKind(task.runner_profile)}</b>
        )}
        {/* THE WHOLE ID, PRINTED, under the name (#94): it was a hover
            title, and an id read off the screen had to be hovered for. */}
        <TaskIdLine id={task.id} />
        {whyHidden && <span className="why is-shared">{whySaid(why)}</span>}
      </span>
      <span className={`when${el.ticking && endedAt === null ? ' ticking' : ''}`} title={whenTitle}>
        {whenText}
      </span>
      {/* A BROWSER ROW WRAPS BETWEEN ITS FACTS AT EVERY WIDTH (review,
          dark-desktop-02-inspector-used): beside the inspector its mark cut
          the owner and the run time off the one line (agents.css `.has-bu`). */}
      <span className={`cr-sub${browserMark !== null ? ' has-bu' : ''}`} title={subSaid}>
        {task.state === 'RUNNING' && isLiveBrowser(task) && <DeviceThumb taskId={task.id} />}
        {/* A LIVE ROW LEADS WITH WHERE IT IS (visual QA run 2, work-a-007):
            the line is one line cut at its end (agents.css `.cr-sub`), and
            at 390 a dispatched row read `claude-code · alex · dispatched ·
            attempt …` with `of 3` cut. The state word and the try are what
            a Live row is watched for; the profile and owner are the agent's
            constant facts, so they come after. At 390 the line now wraps
            between its facts (agents.css, run 5b QA-040), so none is cut. */}
        {/* A STARTING ROW SAYS SO TOO (visual QA run 6, 6:work-a-008): its
            column ticks, so `wordOnly` is false and the line opened on the
            try, while the leased and dispatched rows beside it, drawn with
            the same half-filled mark, led with their word. A browser agent's
            `starting` is the same word, marked as the browser not up yet
            (QA12-20), so it leads once and is not repeated at the end. */}
        {stateWord !== null && (
          <>
            {startingBrowser ? (
              <span className="cr-state cr-starting" title="The browser is starting: there is nothing to watch yet">
                <ToneMark tone="unknown">starting</ToneMark>
              </span>
            ) : (
              <span className="cr-state">{stateWord}</span>
            )}
            {' · '}
          </>
        )}
        {live && (
          <>
            <span className={`cr-try${tries.over ? ' is-over' : ''}`} aria-label={tries.say}>
              {tries.text}
            </span>
            {' · '}
          </>
        )}
        {/* THE BUDGET MARK COMES BEFORE THE PROFILE (#2459): the line is cut
            at its end on a desktop row, and this is what the row is watched
            for once it is there; the profile and owner are constant facts. */}
        {budgetSay !== null && (
          <>
            <BudgetRowMark task={task} live={live} warning={budgetWarning} />
            {' · '}
          </>
        )}
        <span className="cr-profile">{profile}</span>
        {' · '}
        {browserMark !== null && (
          <>
            <BrowserUseMark view={browserMark} />
            {' · '}
          </>
        )}
        <span className="cr-owner" title={task.submitted_by ?? undefined}>
          {owner ?? <Em />}
        </span>
        {ran !== null && (
          <>
            {' · '}
            <span className="cr-ran" title={cancelSpan ? `${ran} ${CANCEL_SPAN}` : undefined}>
              {ran}
            </span>
          </>
        )}
        {!live && !waitingRow && wordOnly && (
          <>
            {' · '}
            <span className="cr-state">{el.text}</span>
          </>
        )}
        {/* THE TRY ON A LIVE ROW, as V1 draws it (leading the line, above)
            -- and on any row whose count is over its cap (AG-15), which a
            finished row must still show: that is the platform running past
            its own ceiling. */}
        {!live && tries.over && (
          <>
            {' · '}
            <span className={`cr-try${tries.over ? ' is-over' : ''}`} aria-label={tries.say}>
              {tries.text}
            </span>
          </>
        )}
        {cancelling && (
          <>
            {' · '}
            <ToneMark tone="wait">cancelling</ToneMark>
          </>
        )}
        {waitsForYou && (
          <>
            {' · '}
            <ToneMark tone="wait">needs you</ToneMark>
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
          colour for one fact. A BUDGET STOP IS NOT (#2459): its line says the
          work was kept and published, and it is drawn dim, not as an error. */}
      {why.text && !whyHidden && (
        <span className={`why${why.warn ? ' is-warn' : ''}${failedRow(task) && !stoppedAtBudgetRow(task) ? ' is-bad' : ''}`} title={whySaid(why)}>
          {why.text}
        </span>
      )}
      {/* A HIDDEN REASON SAYS THAT IT IS ONE (G2-24, dev QA 2026-10-07): the
          row looked as if it had none. Muted, on the reason's own line; the
          sentence is its title for a pointer, and a screen reader already
          hears it from the visually-hidden copy in the name cell. */}
      {whyHidden && (
        <span className="why is-same" aria-hidden="true" title={whySaid(why)}>
          same reason
        </span>
      )}
      {/* THE STRIP ROW'S NAME (owner QA R7, 2026-10-04): folded to 64px the
          row draws only its mark, and its accessible name was the state word.
          Drawn only in the strip (styles/agents.css), for a screen reader;
          a pointer gets the same from the strip's hover card. */}
      <span className="cr-vh" aria-hidden="true">
        {name} · {task.id}
      </span>
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

/** The outline a live browser row draws: what shape of screen its browser is. */
export type DeviceShape = 'phone' | 'tablet' | 'desktop' | 'unknown'

/**
 * Which outline a session's device is drawn as. The preset's own served
 * size decides (§6.1: a fixed, named list in the gate), so a preset added
 * there is drawn by its size rather than missed by a name list here: not
 * mobile is a desktop, mobile with a short side of 600px or more a tablet,
 * other mobile a phone. A preset the read did not list falls back to its
 * name. Null -- the gate has not reported the device (LB-SEAM, §6.2) -- is
 * `unknown`, never a guessed desktop.
 */
export function deviceShape(session: Pick<LiveSession, 'device' | 'presets'> | null): DeviceShape {
  const device = session?.device ?? null
  if (device === null) return 'unknown'
  const p = session!.presets.find((x) => x.name === device.preset)
  if (p !== undefined) return !p.mobile ? 'desktop' : Math.min(p.width, p.height) >= 600 ? 'tablet' : 'phone'
  if (/ipad|tab/i.test(device.preset)) return 'tablet'
  if (/iphone|pixel|phone|android/i.test(device.preset)) return 'phone'
  return /desktop/i.test(device.preset) ? 'desktop' : 'unknown'
}

/**
 * A LIVE BROWSER ROW'S DEVICE OUTLINE (§6, "a device thumbnail per row"; lane
 * LB-UX3): a static phone, tablet or desktop outline in the session's
 * orientation -- not a stream. The workflow card's ~1 fps picture is a socket
 * per card (LiveThumb.tsx); forty rows of sockets on a list that re-reads
 * every 5s would be the cost of forty Browser tabs.
 *
 * RE-READ WITH THE LIST'S POLL (live-browser QA p12, QA12-29). It was read
 * once, when the row first drew, on the reasoning that the device moves only
 * when someone switches it -- and after a switch the row went on drawing the
 * old device for as long as the list was open. Still one small session read
 * per live browser row, never a socket; each list read (`ListReadAt`) brings
 * the next, and the outline drawn stays until it answers.
 */
function DeviceThumb({ taskId }: { taskId: string }) {
  const [got, setGot] = useState<{ taskId: string; session: LiveSession | null } | null>(null)
  const polledAt = useContext(ListReadAt)
  useEffect(() => {
    let live = true
    void loadLiveSession(taskId).then((r) => {
      if (live && (r.status === 'ok' || r.status === 'stale')) setGot({ taskId, session: r.data.session ?? null })
    })
    return () => {
      live = false
    }
  }, [taskId, polledAt])
  const session = got !== null && got.taskId === taskId ? got.session : null
  const shape = deviceShape(session)
  const device = session?.device ?? null
  // A desktop is landscape and a phone portrait unless the session says otherwise.
  const orientation = device?.orientation ?? (shape === 'phone' || shape === 'tablet' ? 'portrait' : 'landscape')
  const say =
    device === null
      ? 'Browser device not reported'
      : `Browser device: ${presetLabel(device.preset)}, ${device.orientation}`
  return (
    <span
      className={`ag-device is-${shape} is-${orientation}`}
      data-device={shape}
      data-orientation={orientation}
      role="img"
      aria-label={say}
      title={say}
    />
  )
}
