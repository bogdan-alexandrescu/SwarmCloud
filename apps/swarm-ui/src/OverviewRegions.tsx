/**
 * THREE OVERVIEW REGIONS O1 ADDS (overview.html, "Lead and ledger", the
 * owner's pick 2026-10-01): the lifecycle band, "Waiting, and why" and
 * "Recent failures".
 *
 * WHERE EACH FIGURE COMES FROM, AND EACH SAYS SO:
 *
 *   Waiting, Holding capacity  `/v1/stats`, an exact count() per state for
 *                              the tenant -- a census, so no "of the page".
 *   Finished today             the task page: `/v1/stats` counts terminal
 *                              states for all time and has no "today", so
 *                              this is counted off the rows read and says
 *                              "of the N newest read" -- at every width
 *                              (QA G1-02, 2026-10-07: a phone hid the foot
 *                              and read 41 where the desktop read 62). A
 *                              page with more behind it is a lower bound,
 *                              `≥N` with the partial mark.
 *   Waiting, and why           the task page's waiting rows, grouped.
 *   Recent failures            the last 24 hours of the page AND of the
 *                              FAILED, DEAD_LETTERED and CANCELLED state
 *                              reads (`stateView`), which a page with more
 *                              behind it takes beside it (live QA OV-1); a
 *                              page that is every agent is listed whole, at
 *                              any age, as Needs a look counts it
 *                              (`failureWindow`, overview-agents-01).
 *
 * An unread count is an em dash with its reason as the title -- never a 0.
 */
import { useEffect, useState, type CSSProperties } from 'react'

import { agentName, shortTaskId, type AgentTab } from './agentlist'
import { loadTask } from './api'
import type { Result } from './fetch'
import { Dash } from './components/Chip'
import { Absent, Mark } from './primitives'
import { blindness } from './checks'
import { StateMark } from './marks'
import { addressToPath } from './paths'
import { staleFoot } from './Shell'
import { usePageClock } from './useNow'
import { formatDateTime, isToday, startOfDay, zoneLabel } from './timezone'
import { useTimeZone } from './useTimeZone'
import { ENDED_BADLY, WHOLE, hhmm, readReach, stateView as readStateView, type Coverage, type OverviewTaskPage, type StateView } from './overviewReads'
import { CONCURRENCY_STATES, TERMINAL_STATES, clockTime, poolLabel, reasonCopy, timeAgo, whyAgent, type Stats, type Task, type TaskPage, type TaskState } from './types'

/**
 * The path of one agent, under the list it actually sits in (browser QA,
 * 2026-10-04). A legacy `#work/task/<id>` resolved through the redirect to
 * `/agents/live/<id>` whatever the state, so a cancelled agent opened from
 * Recent failures landed on Live while the list beside it said otherwise.
 * The tabs are Agents' own: a slot-holding state is Live, a terminal one
 * Recent, anything else Waiting.
 */
export function agentPath(t: Pick<Task, 'id' | 'state'>): string {
  const tab: AgentTab = CONCURRENCY_STATES.has(t.state) ? 'live' : TERMINAL_STATES.has(t.state) ? 'recent' : 'waiting'
  return addressToPath(`work/task/${encodeURIComponent(t.id)}`, tab)
}

/**
 * WHAT THE WORK IS, BEFORE WHICH STEP OF IT (QA G1-09, 2026-10-07): six
 * Recent failures rows read "fix, review, implement, fix, review, implement".
 * The frozen `Task` has no title; a workflow's `title` and its `label` (as
 * `unit`) ride on every step task's metadata (`swarm_mcp/workflows.py`
 * `submit`, read back the same way by `stored_names`). MASKED metadata, like
 * all of it: a title the masker caught reads as its mask. Null when neither
 * is a non-blank string -- the row is then its step name alone, as before.
 *
 * ONLY ON A FULL ROW. The Overview polls `view=summary` (#168), which drops
 * `metadata`, so this is called on what `useRowTitles` reads, never on a page
 * row.
 */
export function rowTitle(t: Pick<Task, 'metadata'>): string | null {
  for (const key of ['title', 'unit']) {
    const v = t.metadata?.[key]
    if (typeof v === 'string' && v.trim() !== '') return v.trim()
  }
  return null
}

/** The row's link words: the title, then the step (`agentName`). */
export function rowLabel(t: Pick<Task, 'id' | 'step_id' | 'runner_profile'>, title: string | null): string {
  return title === null ? agentName(t) : `${title} · ${agentName(t)}`
}

/**
 * One title per workflow, or per lone task: every step task carries its
 * workflow's metadata, so one full read names all of its steps.
 *
 * Read AT MOST ONCE A SESSION per key -- a title is written at submission and
 * never changes -- so the 20 s poll adds no request once a workflow is named,
 * and #168's summary payload stays what it is. A read that fails is not
 * retried; its rows keep the step name alone, which is what they said before.
 * `TITLE_READS` bounds what one screen can start at once.
 */
const titles = new Map<string, string | null>()
const tried = new Set<string>()
const TITLE_READS = 12
const titleKey = (t: Pick<Task, 'id' | 'workflow_id'>): string => t.workflow_id ?? t.id

export function useRowTitles(shown: readonly Task[]): (t: Task) => string | null {
  const [, redraw] = useState(0)
  const wanted = new Map<string, string>()
  for (const t of shown) {
    const k = titleKey(t)
    if (!titles.has(k) && !tried.has(k) && !wanted.has(k) && wanted.size < TITLE_READS) wanted.set(k, t.id)
  }
  // A STRING, so the effect runs when the set of keys changes and not on
  // every render's new array.
  const ask = JSON.stringify([...wanted.entries()])
  useEffect(() => {
    let live = true
    for (const [k, id] of JSON.parse(ask) as [string, string][]) {
      if (tried.has(k)) continue
      tried.add(k)
      void (async () => {
        try {
          const r = await loadTask(id)
          if (r.status === 'ok' || r.status === 'stale') titles.set(k, rowTitle(r.data))
        } catch {
          // A read that threw names nothing; the row keeps its step name.
        }
        if (live) redraw((n) => n + 1)
      })()
    }
    return () => {
      live = false
    }
  }, [ask])
  return (t) => titles.get(titleKey(t)) ?? null
}

function rows(tasks: Result<TaskPage>): Task[] | null {
  return tasks.status === 'ok' || tasks.status === 'stale' ? tasks.data.tasks : null
}



const TERMINAL_BAND: readonly TaskState[] = ['SUCCEEDED', 'FAILED', 'DEAD_LETTERED', 'CANCELLED']

/** The three cells, each with the states under it in lifecycle order. A null `small` is the day's start, in the person's zone. */
const BAND: readonly { k: string; small: string | null; states: readonly TaskState[]; source: 'stats' | 'page' }[] = [
  { k: 'Waiting', small: 'costs nothing', states: ['QUEUED', 'READY', 'PARKED'], source: 'stats' },
  { k: 'Holding capacity', small: 'each holds a lease', states: ['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING'], source: 'stats' },
  { k: 'Finished today', small: null, states: ['SUCCEEDED', 'FAILED', 'DEAD_LETTERED', 'CANCELLED'], source: 'page' },
]

/**
 * Waiting / Holding capacity / Finished today, each broken down by state
 * under its mark (#503: the band carried three bare figures). The marks are
 * the brand's, so PARKED's violet bars and the four teal states here are the
 * same shapes the Agents list draws.
 */
export function LifecycleBand({ stats, tasks }: { stats: Result<Stats>; tasks: Result<OverviewTaskPage> }) {
  const st = stats.status === 'ok' || stats.status === 'stale' ? stats.data : null
  // A SUCCESSFUL EMPTY READ IS A PAGE OF NO ROWS (visual QA V142,
  // 2026-10-16): `rows` is null for `empty`, so Finished today drew "—", not
  // measured, beside Waiting's and Holding's measured 0. The read succeeded
  // and returned every task there is -- none -- so it counts 0, whole.
  const page = tasks.status === 'empty' ? [] : rows(tasks)
  // THE FINISHED ROWS FROM EVERY READ THAT CARRIES THEM (live QA OV-6): the
  // failed, dead-lettered and cancelled come from their state reads too when
  // the page has more behind it, so only SUCCEEDED -- which no state read
  // covers, at 3,600 rows all-time -- is limited to the page.
  const finished =
    tasks.status === 'ok' || tasks.status === 'stale'
      ? stateView(tasks.data, TERMINAL_BAND)
      : tasks.status === 'empty'
        ? ({ rows: [], whole: () => true, since: null } satisfies StateView)
        : null
  const now = Date.now()
  // "TODAY" IS THE PERSON'S DAY (VQ-023, owner decision 2026-10-26): it
  // starts at 00:00 in the zone they picked in Profile, UTC until they pick
  // one, and the cell names that zone, so Overview and Timeline agree on it.
  const zone = useTimeZone()
  const midnight = startOfDay(now, zone)
  const sinceMidnight = `since 00:00 ${zoneLabel(midnight, zone)}`
  // The head's instant (#98), so `counted from 6 min ago` and `⟳ 6 min` agree.
  const at = usePageClock()
  return (
    <section className="ov-life" id="ov-band" aria-label="Waiting, working, done">
      {BAND.map((c) => {
        const read = c.source === 'stats' ? stats : tasks
        // A state the count did not report is absent, not zero, and an
        // absent part makes the total absent too.
        const parts = c.states.map((s) => {
          if (c.source === 'stats') {
            const v = st?.tasks_by_state[s]
            return { s, v: typeof v === 'number' ? v : null }
          }
          return {
            s,
            v: finished === null ? null : finished.rows.filter((t) => t.state === s && isToday(t.completed_at, now, zone)).length,
          }
        })
        const landed = c.source === 'stats' ? st !== null : page !== null
        const total = landed && parts.every((p) => p.v !== null) ? parts.reduce((n, p) => n + (p.v ?? 0), 0) : null
        // A LOWER BOUND WHEN THE READS ARE NOT EVERY TASK (QA G1-02,
        // 2026-10-07). A task created before the page's window can finish
        // today, and it is not on the page, so the count off a page with a
        // next page is "at least", never the day's figure. `/v1/stats` has no
        // `finished_at` window to ask instead.
        const partial = c.source === 'page' && finished !== null && c.states.some((s) => !finished.whole(s))
        // HOW FAR BACK, SAID (live QA OV-6): `≥35` on a phone beside `≥184`
        // on the desktop was a factor of five nobody could account for. The
        // read reaches back to the oldest task it holds; the foot says when.
        // IN THE WORDS EVERY PARTIAL FIGURE ON THE PAGE USES (visual QA
        // VQ-005, 2026-10-26, `readReach`): "created since 18:46 UTC" here
        // beside "partial: agents created since 09:06 UTC read" on Recent
        // failures read as two rules for one thing. The instants differ
        // because the samples do -- succeeded agents are read off the page
        // alone -- and the window, today since 00:00 in the person's zone, is the head's.
        const since = partial && finished?.since ? ` · ${readReach(finished.since)}` : ''
        const why =
          read.status === 'loading'
            ? 'still reading'
            : read.status === 'error'
              ? `${c.source === 'stats' ? 'the state counts' : 'the agent list'} could not be read: ${read.error.message}`
              : !landed
                ? 'nothing was returned to count'
                : 'a state the count did not report is not a zero'
        return (
          <div className="ov-lc" key={c.k}>
            <div className="ov-lc-h">
              <span>{c.k}</span>
              <small>{c.small ?? sinceMidnight}</small>
            </div>
            <div
              className="ov-lc-n"
              title={
                total === null
                  ? `Not measured: ${why}.`
                  : c.source === 'stats'
                    ? 'Counted by /v1/stats, one count per state, for the whole tenant.'
                    : partial
                      ? `At least ${total}: counted off the ${page?.length ?? 0} newest agents this page read${since === '' ? '' : `, which reach back to agents created at ${hhmm(finished?.since ?? '')}`}, and older agents that finished today are not on it.`
                      : page?.length === 0
                        ? 'The agent list was read and holds no agent, so nothing has finished today.'
                        : `Counted off the ${page?.length ?? 0} newest agents this page read, which is every agent.`
              }
            >
              {total !== null ? (
                partial ? (
                  <>
                    &ge;{total}{' '}
                    <Mark kind="partial" say={`At least ${total}: older agents that finished today are not on the page read.`} />
                  </>
                ) : (
                  total
                )
              ) : read.status === 'loading' ? (
                <span className="ov-reading">reading…</span>
              ) : (
                '—'
              )}
            </div>
            {landed && (
              <div className="ov-lc-ps">
                {parts.map((p) => (
                  <span className="ov-lc-p" key={p.s}>
                    <StateMark state={p.s} />
                    {p.v === null ? <b title="Not reported: not a zero.">&mdash;</b> : <b>{p.v}</b>}
                  </span>
                ))}
              </div>
            )}
            {/* PROVENANCE ONLY WHEN STALE (#98, owner ruling 2026-10-07,
                which narrows OV-16's "always"): the counts are on their own
                sixty-second read, and once that read is stale -- a failed
                refresh, or older than `AGED_AFTER_MS` -- the tile says how
                old they are. While fresh it is silent; the head's refresh
                control carries the screen's age. The page's figure still
                says it is the page's. */}
            {c.source === 'stats' && (stats.status === 'ok' || stats.status === 'stale') &&
              staleFoot(stats.fetchedAt, at, stats.status === 'stale') !== null && (
                <span className="ov-lc-foot">counted {staleFoot(stats.fetchedAt, at, stats.status === 'stale')}</span>
              )}
            {/* AT EVERY WIDTH (QA G1-02): a phone reads 50 and a desktop 200,
                so the two figures differ, and this is what says why. */}
            {c.source === 'page' && page !== null && (
              <span className="ov-lc-foot is-page">
                {page.length === 0 ? 'no agent exists' : `of the ${page.length} newest read${since}`}
              </span>
            )}
          </div>
        )
      })}
    </section>
  )
}

/** One reason the waiting tasks on the page give, and how many give it. */
export interface WaitGroup {
  key: string
  state: TaskState
  title: string
  detail: string
  n: number
  /** The workflows the detail names, by id, for the line's `title`. */
  ids: string[]
  /** The raw key of the pool a READY title names in words, for its `title`. */
  pool?: string
}

/**
 * The waiting rows of the page, grouped by the reason they wait (O1). The
 * groups are the platform's own three ways of waiting, and their remedies
 * differ: a PARKED task checkpointed and released everything, a READY one is
 * eligible and needs room in a pool, a QUEUED one is not yet eligible.
 *
 * The reason words are `reasonCopy` and `whyAgent`, the same ones the Agents
 * list prints, so a group here and a row there say the same thing.
 */
export function waitGroups(
  all: readonly Task[],
  now: number = Date.now(),
  wfTitle: (workflowId: string) => string | null = () => null,
): WaitGroup[] {
  const groups = new Map<string, Omit<WaitGroup, 'ids'> & { eligible: string | null; workflows: Map<string, number> }>()
  for (const t of all) {
    let key: string
    let title: string
    let pool: string | undefined
    if (t.state === 'PARKED') {
      key = `P:${t.park_reason ?? ''}`
      title = t.park_reason ? reasonCopy(t.park_reason) : 'Parked'
    } else if (t.state === 'READY') {
      const line = whyAgent(t)
      key = `R:${line}`
      // THE POOL IN WORDS (QA2 ovag2-04): 'waiting for: anthropic · u-bogdan
      // 5/5 units', as Headroom writes it, never the raw key, which broke
      // mid-word at 390; the key is the title's hover.
      const lead = t.waiting_for?.lead?.pool
      pool = lead && line.includes(lead) ? lead : undefined
      const said = pool === undefined ? line : line.replace(pool, poolLabel(pool))
      title = said === '' ? 'Eligible, waiting for room' : said
    } else if (t.state === 'QUEUED') {
      const deps = (t.depends_on?.length ?? 0) > 0
      key = deps ? 'Q:deps' : 'Q:line'
      title = deps ? 'Waiting on earlier workflow steps' : 'In line for the scheduler'
    } else {
      continue
    }
    const g = groups.get(key) ?? { key, state: t.state, title, detail: '', n: 0, eligible: null, workflows: new Map(), ...(pool === undefined ? {} : { pool }) }
    g.n += 1
    if (t.next_eligible_at !== null && (g.eligible === null || t.next_eligible_at < g.eligible)) g.eligible = t.next_eligible_at
    if (t.workflow_id !== null) g.workflows.set(t.workflow_id, (g.workflows.get(t.workflow_id) ?? 0) + 1)
    groups.set(key, g)
  }
  return [...groups.values()]
    .map(({ eligible, workflows, ...g }) => {
      const ranked = [...workflows.entries()].sort((a, b) => b[1] - a[1])
      // THE COUNT CANNOT READ AS PART OF THE ID (visual QA V258, 2026-10-16):
      // "wf_audit_01 1" read as `wf_audit_011`. A times sign sets it apart.
      // A WORKFLOW BY ITS TITLE (visual QA pass 1, overview-agents-02,
      // 2026-10-28): the row read "wf_audit_01 ×1". It names the workflow
      // as Workflows and Agents rows do, once its title is read; the ids are
      // the line's `title`. Until then, or with no title, the id stands.
      const wfs = ranked.slice(0, 3).map(([w, n]) => `${wfTitle(w) ?? w} ×${n}`)
      // EVERY CONTRIBUTOR ACCOUNTED FOR (live QA OV-10, 2026-10-11): a row
      // counting 7 named three workflows of 2 each and dropped the seventh --
      // a fourth workflow, or a task in none. What the names leave is said.
      const rest = g.n - ranked.slice(0, 3).reduce((n, [, k]) => n + k, 0)
      const why =
        g.state === 'PARKED'
          ? 'Checkpointed and released. Holds nothing.'
          : g.state === 'READY'
            ? 'Eligible now. Admitted when every pool it needs has room.'
            : 'Not yet eligible. Holds nothing.'
      // A TIME THAT HAS PASSED IS WORDED AS PAST (visual QA run 6,
      // overview-automate-015, 2026-10-23): 'next eligible 11:51 UTC' under a
      // 12:00 clock read as still waiting for it. A parked task can pass its
      // time before the scheduler picks it up, so 'since' is a live case.
      const passed = eligible !== null && new Date(eligible).getTime() <= now
      const clauses = [
        ...(g.state === 'PARKED' && eligible !== null ? [`${passed ? 'eligible since' : 'next eligible'} ${hhmm(eligible)}`] : []),
        ...wfs,
        ...(rest > 0 && wfs.length > 0 ? [`+${rest} more`] : []),
        why,
      ]
      return { ...g, detail: clauses.join(' · '), ids: ranked.slice(0, 3).map(([w]) => w) }
    })
    .sort((a, b) => b.n - a.n || a.key.localeCompare(b.key))
}

/**
 * A card's nothing, drawn the way every other Overview card draws it (visual
 * QA V141/V142, 2026-10-11): the shared `Absent` in its in-card variant, so a
 * failed read is the `not read` mark and a real zero the `real zero` mark --
 * not a line of em text beside a card that draws the mark for the same read.
 */
/**
 * WHY A CARD IS BLIND, AS THE SENTENCE UNDER ITS HEADING (visual QA V259,
 * 2026-10-16). An in-card absence drew only "not read · Task list unread":
 * the reason was the mark's accessible name and nothing else, with no
 * visible text and no `title`, so a sighted reader could not learn why. The
 * one sentence §6.9 allows is the read's own reason; `say` stays the mark's
 * name, whole.
 */
export function readWhy(b: { why: string }): string {
  const s = b.why.trim()
  return /[.!?]$/.test(s) ? s : `${s}.`
}

export function TaskListAbsent({ tasks }: { tasks: Result<TaskPage> }) {
  // A read in flight is the skeleton every other card draws (Overview's `Reading`).
  if (tasks.status === 'loading') return <div className="ctl-ghost ov-ghost" style={{ '--rows': '3' } as CSSProperties} aria-hidden />
  if (tasks.status === 'error') {
    const b = blindness(tasks.error)
    return (
      <Absent
        className="ov-empty"
        kind={b.admin ? 'admin' : 'failed'}
        heading="Agent list unread"
        say={`${b.why} This card is blind; it is not reporting that nothing is here.`}
      >
        {readWhy(b)}
      </Absent>
    )
  }
  return (
    <Absent
      className="ov-empty"
      kind="zero"
      heading="No agent exists"
      say="The read succeeded and returned nothing."
    />
  )
}

/** A real zero over the page that was read, in the same picture as every other card's. */
function NoneAmong({ heading, n }: { heading: string; n: number }) {
  return (
    <Absent
      className="ov-empty"
      kind="zero"
      heading={heading}
      say={`The agent read succeeded and none of the ${n} newest agents is one of these.`}
    />
  )
}

/** The states `waitGroups` groups. */
const WAITING: ReadonlySet<TaskState> = new Set<TaskState>(['PARKED', 'READY', 'QUEUED'])

/** "Waiting, and why": one row per reason, with its count (O1 `.wlist`). */
export function WaitingWhy({ tasks }: { tasks: Result<TaskPage> }) {
  const all = rows(tasks)
  // ONE TASK PER WAITING WORKFLOW, so `useRowTitles` reads each title once.
  const perWorkflow = new Map<string, Task>()
  for (const t of all ?? []) {
    if (t.workflow_id !== null && WAITING.has(t.state) && !perWorkflow.has(t.workflow_id)) perWorkflow.set(t.workflow_id, t)
  }
  const titleOf = useRowTitles([...perWorkflow.values()])
  if (all === null) return <TaskListAbsent tasks={tasks} />
  const groups = waitGroups(all, Date.now(), (w) => {
    const t = perWorkflow.get(w)
    return t === undefined ? null : titleOf(t)
  })
  if (groups.length === 0) return <NoneAmong heading={`None among the ${all.length} newest`} n={all.length} />
  return (
    <div className="ov-wlist">
      {groups.slice(0, 6).map((g) => (
        <div className="ov-wr" key={g.key}>
          <span className="ov-wst">
            <StateMark state={g.state} />
          </span>
          <span className="ov-wt">
            <b title={g.pool}>{g.title}</b>
            <small title={g.ids.length > 0 ? g.ids.join(', ') : undefined}>{g.detail}</small>
          </span>
          <span className="ov-wn">{g.n}</span>
        </div>
      ))}
      {groups.length > 6 && <p className="ov-more">{groups.length - 6} more reasons on the Waiting list</p>}
    </div>
  )
}

const DAY_MS = 24 * 60 * 60 * 1000

// The state reads and their coverage live in `overviewReads.ts`, which the
// pure check layer imports too; re-exported for the screen and its tests.
export { ENDED_BADLY, WHOLE, hhmm, readReach, type Coverage, type OverviewTaskPage, type StateRead, type StateView } from './overviewReads'

/** A coverage that also names the window its failures are counted over (`failureWindow`). */
export type WindowedCoverage = Coverage & { window?: FailureWindow }

/**
 * `overviewReads.ts`'s `stateView`, carrying the page's failure window
 * (overview-agents-01 review, 2026-10-28). Overview.tsx draws Recent
 * failures' head note and the panel's count as `failuresOf(ended.rows, now,
 * ended)`, with no window, so they read "last 24h · 0 failed" above two
 * FAILED rows from 9 days ago that the body, on the check's window, listed.
 * The view now carries the window and `failuresOf` takes it when none is
 * passed, so every caller counts what Needs a look counts.
 */
export function stateView(page: OverviewTaskPage, states: readonly TaskState[]): StateView & { window: FailureWindow } {
  return { ...readStateView(page, states), window: failureWindow(page) }
}

/**
 * The failed, dead-lettered and cancelled rows of the window -- the one
 * passed, else the one `cover` carries (`stateView`), else the last 24 hours
 * -- newest first. THE WINDOW AND THE ORDER READ `completed_at` ONLY (browser QA N4,
 * 2026-10-04): `updated_at` is bumped by any later write, so a task that ended
 * days ago with no recorded end was listed and counted as "last 24h". A
 * terminal task with no recorded end is listed after the dated rows and
 * counted on its own in the note -- it is not known to be inside the window.
 *
 * "LAST 24H" ONLY WHEN THE READ COVERS IT (live QA OV-7, 2026-10-11). The
 * desktop said `last 24h · 11 failed` off a 200-row read that reached back
 * five hours. The rows are read newest-CREATED first, so a state `cover` says
 * is not whole is a lower bound: its figure is `≥N`, `partial` is true, and
 * the note says from when the read reaches -- never a measured count over a
 * window nobody read.
 *
 * A FAILURE BEFORE A CANCELLATION (visual QA run 2, FF-D3, 2026-10-20). The
 * newest row was a cancelled agent, so someone opening Overview to find the
 * newest failure read the wrong row first. Failed and dead-lettered rows lead,
 * newest-ended first; the cancelled follow them, newest-ended first, still
 * counted in the note. `RecentFailures` splits the two (owner decision c02,
 * 2026-10-22): the failures in the table, the cancelled under "N cancelled".
 */
export function failuresOf(
  all: readonly Task[],
  now: number,
  cover: WindowedCoverage = WHOLE,
  window: FailureWindow = cover.window ?? LAST_DAY,
): { rows: Task[]; note: string; partial: boolean } {
  const ended = (t: Task): number | null => {
    if (t.completed_at === null) return null
    const ms = new Date(t.completed_at).getTime()
    return Number.isFinite(ms) ? ms : null
  }
  const terminal = all.filter((t) => ENDED_BADLY.includes(t.state))
  const dated = terminal
    .filter((t) => {
      const ms = ended(t)
      return ms !== null && (window.newest !== null || now - ms <= DAY_MS)
    })
    .sort((a, b) => cancelledLast(a) - cancelledLast(b) || (ended(b) ?? 0) - (ended(a) ?? 0))
  const undated = terminal.filter((t) => ended(t) === null)
  const partial = ENDED_BADLY.some((s) => !cover.whole(s))
  const n = (s: TaskState) => `${cover.whole(s) ? '' : '≥'}${dated.filter((t) => t.state === s).length}`
  const note = [
    window.newest === null ? 'last 24h' : `of the ${window.newest} newest`,
    `${n('FAILED')} failed`,
    `${n('DEAD_LETTERED')} dead-lettered`,
    ...(dated.some((t) => t.state === 'CANCELLED') || !cover.whole('CANCELLED') ? [`${n('CANCELLED')} cancelled`] : []),
    ...(undated.length > 0 ? [`${undated.length} with no recorded end`] : []),
    ...(partial ? [readReach(cover.since)] : []),
  ].join(' · ')
  return { rows: [...dated, ...undated], note, partial }
}

/**
 * WHICH FAILURES THE CARD LISTS: THE ONES NEEDS A LOOK COUNTS (visual QA pass
 * 1, overview-agents-01, 2026-10-28). Needs a look said "2 failed agents among
 * the 27 most recent" while this card, over the same 27, said "None among the
 * 27 newest": the check (`checks.ts` `failureCheck`) counts every failure on a
 * page that is every agent, at any age, and windows to the last 24 hours only
 * when the page has more behind it and the FAILED state read landed; the card
 * windowed to 24 hours always, so two failures that ended two days earlier were
 * counted there and dropped here. The card now takes the check's window:
 * `newest` is the page's length when it reads the page whole, null for the
 * last 24 hours.
 */
export interface FailureWindow {
  newest: number | null
}

export const LAST_DAY: FailureWindow = { newest: null }

/** The check's window for this read, so Recent failures and Needs a look count the same rows. */
export function failureWindow(page: OverviewTaskPage): FailureWindow {
  const pageWhole = (page.next_page_token ?? null) === null
  const failedRead = page.byState?.some((r) => r.state === 'FAILED') ?? false
  return !pageWhole && failedRead ? LAST_DAY : { newest: page.tasks.length }
}

/** 1 for a cancelled agent, 0 for a failure: the sort key that lists failures first. */
function cancelledLast(t: Task): number {
  return t.state === 'CANCELLED' ? 1 : 0
}

/**
 * HOW LONG AGO A FAILURE ENDED (browser QA N4, 2026-10-04). The age was
 * `completed_at ?? updated_at`, and a later write -- a reconciler pass, an
 * account event -- bumps `updated_at`: tasks that ended ~9h earlier read
 * "58m ago". `completed_at` is the end every terminal writer records; a task
 * with none has no recorded end, and that is a dash with its reason, never
 * the time of the last write.
 */
function EndedAgo({ task: t, now }: { task: Task; now: number }) {
  const zone = useTimeZone()
  const at = clockTime(t.completed_at, now)
  if (t.completed_at === null || at === null) {
    return <Dash why={`No end time was recorded for this agent; its document was last written ${timeAgo(t.updated_at, now)}, which is not when it ended.`} />
  }
  // THE INSTANT UNDER THE AGE (#503): "25m ago" is what a reader scans for,
  // and the time it ended -- `clockTime`'s ISO UTC, the hover every other
  // start and end in this console carries -- is what they match to a log.
  // The person's zone leads it (VQ-023), labelled, so the hover agrees with
  // every other time on the page; the ISO instant stays for the log.
  return (
    <time dateTime={t.completed_at} title={`ended ${formatDateTime(t.completed_at, zone)} · ${at.title}`}>
      {timeAgo(t.completed_at, now)}
    </time>
  )
}

/**
 * "Recent failures": mark, agent and why, age (O1). The title is the row's
 * one link (owner decision c03, 2026-10-22).
 *
 * THE FAILURES ALONE, THE CANCELLED FOLDED (owner decision c02, 2026-10-22;
 * visual QA run 2 FF-D3, run 4 flow-find-failure-desktop-D1). One mixed list
 * put cancelled rows beside the failures someone opened the card to find. The
 * table lists FAILED and DEAD_LETTERED, newest first, with its own "N more"
 * past six; under it "N cancelled" opens the cancelled rows in the same row
 * design, newest first -- one tap away, and a step the cascade cancelled still
 * names the parent that failed.
 */
export function RecentFailures({ tasks, now = Date.now() }: { tasks: Result<OverviewTaskPage>; now?: number }) {
  // `now` IS THE CHECKS' INSTANT on the Overview (visual QA VQ-005,
  // 2026-10-26), so this card's 24 hours and Needs a look's end at the same
  // moment and count the same failures.
  const all = rows(tasks)
  const read = tasks.status === 'ok' || tasks.status === 'stale' ? tasks.data : null
  const view = read === null ? null : stateView(read, ENDED_BADLY)
  const found = read === null || view === null ? null : failuresOf(view.rows, now, view)
  // `failuresOf` lists each kind newest first, so a split keeps both orders.
  const ended = found === null ? [] : found.rows
  const failed = ended.filter((t) => t.state !== 'CANCELLED')
  const cancelled = ended.filter((t) => t.state === 'CANCELLED')
  const titleOf = useRowTitles(failed.length > 0 ? failed.slice(0, FAILURE_ROWS) : cancelled.slice(0, FAILURE_ROWS))
  if (all === null) return <TaskListAbsent tasks={tasks} />
  // THE ROWS READ, BY ID (visual QA run 4, N23): a step the cascade cancelled
  // names the parent that failed when that parent is among them -- the page
  // and the per-state reads `stateView` merged, which is where a failed
  // parent older than the page is.
  const byId = new Map<string, Task>(all.map((t) => [t.id, t] as const))
  for (const t of view?.rows ?? []) byId.set(t.id, t)
  const folded =
    cancelled.length > 0 ? (
      <details className="ov-more ov-failures-cancelled">
        <summary>{cancelled.length} cancelled</summary>
        {failureTable(cancelled, titleOf, now, 'Recent failures, cancelled', byId)}
      </details>
    ) : null
  // NOT A REAL ZERO OVER A READ THAT DID NOT REACH (live QA OV-1): the phone
  // drew `real zero` over its 50 newest while 11 tasks had failed. When the
  // reads behind this card are not every failed, dead-lettered and cancelled
  // task, an empty list is "none among what was read", marked partial.
  if (failed.length === 0) {
    const since = view?.since ? ` (only agents created since ${hhmm(view.since)})` : ''
    if (cancelled.length > 0) {
      // NO FAILURE, SOME CANCELLED (c02): the card says there is no failure
      // and still offers the cancelled under it.
      return (
        <>
          {found?.partial ? (
            <Absent
              className="ov-empty"
              kind="partial"
              heading={`No failure among the ${all.length} newest read`}
              say={`No failed or dead-lettered agent among the rows read, but the reads did not reach every such agent${since}. Not a zero: unknown beyond what was read.`}
            />
          ) : (
            <Absent
              className="ov-empty"
              kind="zero"
              heading={`No failure among the ${all.length} newest`}
              say={`The agent read succeeded and no failed or dead-lettered agent is among the ${all.length} newest.`}
            />
          )}
          {folded}
        </>
      )
    }
    if (found?.partial) {
      return (
        <Absent
          className="ov-empty"
          kind="partial"
          heading={`None among the ${all.length} newest read`}
          say={`No failed, dead-lettered or cancelled agent among the rows read, but the reads did not reach every such agent${since}. Not a zero: unknown beyond what was read.`}
        />
      )
    }
    return <NoneAmong heading={`None among the ${all.length} newest`} n={all.length} />
  }
  const shown = failed.slice(0, FAILURE_ROWS)
  return (
    <>
    {failureTable(shown, titleOf, now, 'Recent failures', byId)}
    {/* THE REST, COUNTED AND IN PLACE (visual QA V144, 2026-10-11): the card
        stopped at six rows and said nothing about a seventh, while Running
        now beside it says "N more". The same disclosure, the same words. */}
    {failed.length > shown.length && (
      <details className="ov-more ov-failures-more">
        <summary>{failed.length - shown.length} more</summary>
        {failureTable(failed.slice(FAILURE_ROWS), titleOf, now, 'Recent failures, the rest', byId)}
      </details>
    )}
    {folded}
    </>
  )
}

/** `wf_ccdd1422ab` -> `wf_ccdd14…`, as Running now's phone rows shorten it. */
function shortWf(id: string): string {
  return id.length <= 10 ? id : `${id.slice(0, 9)}…`
}

/**
 * THE WHOLE WORKFLOW ID AT DESKTOP WIDTH (visual QA V263, owner decision Q5,
 * 2026-10-16). A row read "synthesis · wf_5e5ad3…" at 1440 with ~300px free,
 * and its `title` repeated the cut text, so the id could not be reached from
 * the row. The id is drawn whole; the sheet cuts it to the short form only at
 * the phone breakpoint (`.ov-wf-id`, overview.css), so the text, the title
 * and a screen reader all carry the whole id at every width.
 */
function WfId({ id }: { id: string }) {
  return shortWf(id) === id ? <>{id}</> : <span className="ov-wf-id">{id}</span>
}

/** Rows the Recent failures card draws before its "N more". */
const FAILURE_ROWS = 6

/**
 * NAMED (visual QA run 3, a11y-keyboard-003, 2026-10-21): a screen reader's
 * table list read "table, 4 columns" for each, with nothing to tell the six
 * rows from the "N more" under them. `name` is the card's title, and each
 * disclosure's table says what it holds: the rest, or the cancelled.
 */
function failureTable(
  failed: Task[],
  titleOf: (t: Task) => string | null,
  now: number,
  name: string,
  taskById: ReadonlyMap<string, Task>,
) {
  return (
    // FIXED COLUMNS (browser QA D2, 2026-10-04): the row head inherited the
    // head row's `nowrap`, so one long error drew a 1532px table in a 1056px
    // card and pushed the age off it. The name takes what the mark and age
    // columns leave; the error clamps to two lines, whole in its title.
    <table className="ov-tbl ov-fails" aria-label={name}>
      <colgroup>
        <col className="ov-fc-mark" />
        <col className="ov-fc-name" />
        <col className="ov-fc-age" />
      </colgroup>
      <tbody>
        {failed.map((t) => {
          const href = agentPath(t)
          const why = whyAgent(t, null, taskById) || (t.state === 'DEAD_LETTERED' ? 'Retry budget spent.' : '')
          // AN UNTITLED STEP SAYS WHOSE IT IS (live QA OV-16, 2026-10-11):
          // five of eleven rows read "merge" and nothing else. With no title
          // read, the row names its workflow by its short id.
          const title = titleOf(t)
          const wf = title === null ? t.workflow_id : null
          const label = rowLabel(t, title)
          // THE TOOLTIP HOLDS WHAT THE LINK SAYS, THEN THE WHOLE ID (CIFIX-2632,
          // 2026-10-28): a step's link ends in its short id, `fan_3`, and a
          // tooltip of `<label> · task_fan_3` did not contain it, so the
          // clamped name was not whole in its title (walk.long C). The short
          // id stays in the words; the full id still ends the tooltip.
          const short = (t.step_id ?? null) !== null ? shortTaskId(t.id) : null
          const words = [label, ...(wf === null ? [] : [wf]), ...(short === null ? [] : [short])].join(' · ')
          const tip = short === t.id ? words : `${words} · ${t.id}`
          return (
            <tr key={t.id}>
              {/* THE MARK ALONE (browser QA N3, 2026-10-04): the 68px badge
                  overprinted the name in this 28px column. The word is the
                  mark's title and its accessible name. */}
              <td>
                <StateMark state={t.state} bare />
              </td>
              <th scope="row">
                <a className="ov-name" href={href} title={tip}>
                  {label}
                  {wf !== null && (
                    <>
                      {' · '}
                      <WfId id={wf} />
                    </>
                  )}
                  {/* EVERY ROW ENDS IN ITS TASK ID (visual QA 5b:ff-desktop-02,
                      #1680 box 6343791868; BGD's decision of 2026-10-23): a
                      lone agent's name ends in its short id, `agentName`'s,
                      but a workflow step's read 'title · checkpoints' and
                      could not be matched to its row on Agents without
                      opening it. The step's link ends in the same short id,
                      `<title> · <step> · <id>`, in an element of its own:
                      drawn as an id, it is the user's data, not a code
                      identifier (walk.copy E). */}
                  {short !== null && (
                    <>
                      {' · '}
                      <span className="mono">{short}</span>
                    </>
                  )}
                </a>
                {why !== '' && (
                  <span className="ov-sub ov-why" title={why}>
                    {why}
                  </span>
                )}
              </th>
              {/* THE ROW ENDS IN ITS AGE (owner decision c03, 2026-10-22):
                  the row-end Open repeated the title's link, so a row read as
                  two links to one place. The title is the only one. */}
              <td className="is-num">
                <EndedAgo task={t} now={now} />
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
