/**
 * THE THREE OVERVIEW REGIONS O1 ADDS (overview.html, "Lead and ledger", the
 * owner's pick 2026-10-01): the lifecycle band, "Waiting, and why" and
 * "Recent failures". Every figure is counted from the one task page the
 * Overview already reads (`/v1/tasks`), and each region SAYS that it is the
 * rows that page holds -- it is a page of the newest tasks, not a census, and
 * Platform counts is where a census lives.
 */
import type { Result } from './fetch'
import { StateMark } from './marks'
import { CONCURRENCY_STATES, WAITING_STATES as WAITING, whyAgent, type Task, type TaskPage } from './types'

function rows(tasks: Result<TaskPage>): Task[] | null {
  return tasks.status === 'ok' || tasks.status === 'stale' ? tasks.data.tasks : null
}

function isToday(iso: string | null, now: Date): boolean {
  if (iso === null) return false
  const d = new Date(iso)
  return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
}

/** Waiting / Holding capacity / Finished today, over the page of tasks read. */
export function LifecycleBand({ tasks }: { tasks: Result<TaskPage> }) {
  const all = rows(tasks)
  const now = new Date()
  const cells: { k: string; v: number | null; say: string }[] = [
    {
      k: 'Waiting',
      v: all === null ? null : all.filter((t) => WAITING.has(t.state)).length,
      say: 'queued, ready or parked: holds no capacity',
    },
    {
      k: 'Holding capacity',
      v: all === null ? null : all.filter((t) => CONCURRENCY_STATES.has(t.state)).length,
      say: 'leased, dispatched, starting or running',
    },
    {
      k: 'Finished today',
      v: all === null ? null : all.filter((t) => t.completed_at !== null && isToday(t.completed_at, now)).length,
      say: 'succeeded, failed or cancelled since midnight',
    },
  ]
  return (
    <div className="ov-band" role="group" aria-label="Waiting, working, done">
      {cells.map((c) => (
        <div className="ov-band-cell" key={c.k}>
          <span className="ov-band-k">{c.k}</span>
          <b className="ov-band-v">{c.v === null ? (tasks.status === 'loading' ? 'reading…' : '—') : c.v}</b>
          <small>{c.say}</small>
        </div>
      ))}
      <p className="ov-band-foot">of the {all === null ? '' : `${all.length} `}newest tasks this page read</p>
    </div>
  )
}

function TaskRows({ list, why }: { list: Task[]; why: (t: Task) => string }) {
  return (
    <ul className="ov-list">
      {list.map((t) => (
        <li key={t.id}>
          <a className="ov-list-row" href={`#work/task/${encodeURIComponent(t.id)}`}>
            <StateMark state={t.state} />
            <span className="id">{t.id}</span>
            <span className="ov-list-why">{why(t)}</span>
          </a>
        </li>
      ))}
    </ul>
  )
}

/** "Waiting, and why": the waiting rows with the reason each one gives. */
export function WaitingWhy({ tasks }: { tasks: Result<TaskPage> }) {
  const all = rows(tasks)
  if (all === null) return <p className="ctl-em">{tasks.status === 'loading' ? 'reading…' : 'not read'}</p>
  const waiting = all.filter((t) => WAITING.has(t.state)).slice(0, 8)
  if (waiting.length === 0) return <p className="ctl-em">Nothing is waiting in the tasks this page read.</p>
  return <TaskRows list={waiting} why={(t) => whyAgent(t)} />
}

/** "Recent failures": failed and dead-lettered rows, newest first. */
export function RecentFailures({ tasks }: { tasks: Result<TaskPage> }) {
  const all = rows(tasks)
  if (all === null) return <p className="ctl-em">{tasks.status === 'loading' ? 'reading…' : 'not read'}</p>
  const failed = all
    .filter((t) => t.state === 'FAILED' || t.state === 'DEAD_LETTERED')
    .sort((a, b) => (b.updated_at > a.updated_at ? 1 : b.updated_at < a.updated_at ? -1 : 0))
    .slice(0, 6)
  if (failed.length === 0) return <p className="ctl-em">No failure in the tasks this page read.</p>
  return <TaskRows list={failed} why={(t) => whyAgent(t)} />
}
