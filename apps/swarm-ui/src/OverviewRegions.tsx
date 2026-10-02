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
 *                              "of the N newest read".
 *   Waiting, and why           the task page's waiting rows, grouped.
 *   Recent failures            the task page's last 24 hours.
 *
 * An unread count is an em dash with its reason as the title -- never a 0.
 */
import type { Result } from './fetch'
import { StateMark } from './marks'
import { reasonCopy, timeAgo, whyAgent, type Stats, type Task, type TaskPage, type TaskState } from './types'

function rows(tasks: Result<TaskPage>): Task[] | null {
  return tasks.status === 'ok' || tasks.status === 'stale' ? tasks.data.tasks : null
}

/** Since 00:00 UTC today. O1 names the day in UTC, so the cut is UTC too. */
function isTodayUtc(iso: string | null, now: Date): boolean {
  if (iso === null) return false
  const d = new Date(iso)
  return (
    d.getUTCFullYear() === now.getUTCFullYear() && d.getUTCMonth() === now.getUTCMonth() && d.getUTCDate() === now.getUTCDate()
  )
}

/** The three cells, each with the states under it in lifecycle order. */
const BAND: readonly { k: string; small: string; states: readonly TaskState[]; source: 'stats' | 'page' }[] = [
  { k: 'Waiting', small: 'costs nothing', states: ['QUEUED', 'READY', 'PARKED'], source: 'stats' },
  { k: 'Holding capacity', small: 'each holds a lease', states: ['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING'], source: 'stats' },
  { k: 'Finished today', small: 'since 00:00 UTC', states: ['SUCCEEDED', 'FAILED', 'DEAD_LETTERED', 'CANCELLED'], source: 'page' },
]

/**
 * Waiting / Holding capacity / Finished today, each broken down by state
 * under its mark (#503: the band carried three bare figures). The marks are
 * the brand's, so PARKED's violet bars and the four teal states here are the
 * same shapes the Agents list draws.
 */
export function LifecycleBand({ stats, tasks }: { stats: Result<Stats>; tasks: Result<TaskPage> }) {
  const st = stats.status === 'ok' || stats.status === 'stale' ? stats.data : null
  const page = rows(tasks)
  const now = new Date()
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
            v: page === null ? null : page.filter((t) => t.state === s && isTodayUtc(t.completed_at, now)).length,
          }
        })
        const landed = c.source === 'stats' ? st !== null : page !== null
        const total = landed && parts.every((p) => p.v !== null) ? parts.reduce((n, p) => n + (p.v ?? 0), 0) : null
        const why =
          read.status === 'loading'
            ? 'still reading'
            : read.status === 'error'
              ? `${c.source === 'stats' ? 'the state counts' : 'the task list'} could not be read: ${read.error.message}`
              : !landed
                ? 'nothing was returned to count'
                : 'a state the count did not report is not a zero'
        return (
          <div className="ov-lc" key={c.k}>
            <div className="ov-lc-h">
              <span>{c.k}</span>
              <small>{c.small}</small>
            </div>
            <div
              className="ov-lc-n"
              title={
                total === null
                  ? `Not measured: ${why}.`
                  : c.source === 'stats'
                    ? 'Counted by /v1/stats, one count per state, for the whole tenant.'
                    : `Counted off the ${page?.length ?? 0} newest tasks this page read.`
              }
            >
              {total !== null ? total : read.status === 'loading' ? <span className="ov-reading">reading…</span> : '—'}
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
            {/* PROVENANCE, ALWAYS (OV-16): the counts are on their own
                sixty-second read, so their age is part of the figure; the
                page's figure says it is the page's. */}
            {c.source === 'stats' && (stats.status === 'ok' || stats.status === 'stale') && (
              <span className="ov-lc-foot">counted {timeAgo(stats.fetchedAt)}</span>
            )}
            {c.source === 'page' && page !== null && <span className="ov-lc-foot">of the {page.length} newest read</span>}
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
}

/** HH:MM UTC, for a park's next eligible instant. */
function clock(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? `${d.toISOString().slice(11, 16)} UTC` : iso
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
export function waitGroups(all: readonly Task[]): WaitGroup[] {
  const groups = new Map<string, WaitGroup & { eligible: string | null; workflows: Map<string, number> }>()
  for (const t of all) {
    let key: string
    let title: string
    if (t.state === 'PARKED') {
      key = `P:${t.park_reason ?? ''}`
      title = t.park_reason ? reasonCopy(t.park_reason) : 'Parked'
    } else if (t.state === 'READY') {
      const line = whyAgent(t)
      key = `R:${line}`
      title = line === '' ? 'Eligible, waiting for room' : line
    } else if (t.state === 'QUEUED') {
      const deps = (t.depends_on?.length ?? 0) > 0
      key = deps ? 'Q:deps' : 'Q:line'
      title = deps ? 'Waiting on earlier workflow steps' : 'In line for the scheduler'
    } else {
      continue
    }
    const g = groups.get(key) ?? { key, state: t.state, title, detail: '', n: 0, eligible: null, workflows: new Map() }
    g.n += 1
    if (t.next_eligible_at !== null && (g.eligible === null || t.next_eligible_at < g.eligible)) g.eligible = t.next_eligible_at
    if (t.workflow_id !== null) g.workflows.set(t.workflow_id, (g.workflows.get(t.workflow_id) ?? 0) + 1)
    groups.set(key, g)
  }
  return [...groups.values()]
    .map(({ eligible, workflows, ...g }) => {
      const wfs = [...workflows.entries()].sort((a, b) => b[1] - a[1]).map(([w, n]) => `${w} ${n}`)
      const why =
        g.state === 'PARKED'
          ? 'Checkpointed and released. Holds nothing.'
          : g.state === 'READY'
            ? 'Eligible now. Admitted when every pool it needs has room.'
            : 'Not yet eligible. Holds nothing.'
      const clauses = [
        ...(g.state === 'PARKED' && eligible !== null ? [`next eligible ${clock(eligible)}`] : []),
        ...wfs.slice(0, 3),
        why,
      ]
      return { ...g, detail: clauses.join(' · ') }
    })
    .sort((a, b) => b.n - a.n || a.key.localeCompare(b.key))
}

/** "Waiting, and why": one row per reason, with its count (O1 `.wlist`). */
export function WaitingWhy({ tasks }: { tasks: Result<TaskPage> }) {
  const all = rows(tasks)
  if (all === null) {
    return (
      <p className="ctl-em" title={tasks.status === 'error' ? `The task list could not be read: ${tasks.error.message}` : undefined}>
        {tasks.status === 'loading' ? 'reading…' : tasks.status === 'empty' ? 'No task exists.' : '— not read'}
      </p>
    )
  }
  const groups = waitGroups(all)
  if (groups.length === 0) return <p className="ctl-em">Nothing waiting among the {all.length} newest.</p>
  return (
    <div className="ov-wlist">
      {groups.slice(0, 6).map((g) => (
        <div className="ov-wr" key={g.key}>
          <span className="ov-wst">
            <StateMark state={g.state} />
          </span>
          <span className="ov-wt">
            <b>{g.title}</b>
            <small>{g.detail}</small>
          </span>
          <span className="ov-wn">{g.n}</span>
        </div>
      ))}
      {groups.length > 6 && <p className="ov-more">{groups.length - 6} more reasons on the Waiting list</p>}
    </div>
  )
}

const DAY_MS = 24 * 60 * 60 * 1000

/** The failed, dead-lettered and cancelled rows of the last 24 hours, newest first. */
export function failuresOf(all: readonly Task[], now: number): { rows: Task[]; note: string } {
  const when = (t: Task) => new Date(t.completed_at ?? t.updated_at).getTime()
  const rows = all
    .filter((t) => t.state === 'FAILED' || t.state === 'DEAD_LETTERED' || t.state === 'CANCELLED')
    .filter((t) => now - when(t) <= DAY_MS)
    .sort((a, b) => when(b) - when(a))
  const n = (s: TaskState) => rows.filter((t) => t.state === s).length
  const note = [
    'last 24h',
    `${n('FAILED')} failed`,
    `${n('DEAD_LETTERED')} dead-lettered`,
    ...(n('CANCELLED') > 0 ? [`${n('CANCELLED')} cancelled`] : []),
  ].join(' · ')
  return { rows, note }
}

/** "Recent failures": mark, agent and why, age, Open (O1). */
export function RecentFailures({ tasks }: { tasks: Result<TaskPage> }) {
  const all = rows(tasks)
  if (all === null) {
    return (
      <p className="ctl-em" title={tasks.status === 'error' ? `The task list could not be read: ${tasks.error.message}` : undefined}>
        {tasks.status === 'loading' ? 'reading…' : tasks.status === 'empty' ? 'No task exists.' : '— not read'}
      </p>
    )
  }
  const now = Date.now()
  const { rows: failed } = failuresOf(all, now)
  if (failed.length === 0) return <p className="ctl-em">None among the {all.length} newest.</p>
  return (
    <table className="ov-tbl">
      <tbody>
        {failed.slice(0, 6).map((t) => {
          const href = `#work/task/${encodeURIComponent(t.id)}`
          const why = whyAgent(t) || (t.state === 'DEAD_LETTERED' ? 'Retry budget spent.' : '')
          return (
            <tr key={t.id}>
              <td>
                <StateMark state={t.state} />
              </td>
              <th scope="row">
                <a className="ov-name" href={href}>
                  {t.step_id ?? t.id}
                </a>
                {why !== '' && <span className="ov-sub">{why}</span>}
              </th>
              <td className="is-num">{timeAgo(t.completed_at ?? t.updated_at, now)}</td>
              <td className="is-num">
                <a className="ov-open" href={href}>
                  Open
                </a>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
