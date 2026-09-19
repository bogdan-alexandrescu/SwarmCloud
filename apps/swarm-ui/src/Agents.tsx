import { useEffect, useMemo, useState } from 'react'
import { loadTasks } from './api'
import { Screen } from './Shell'
import {
  CONCURRENCY_STATES,
  RESOURCE_UNITS,
  TERMINAL_STATES,
  elapsed,
  rollupState,
  stateGlyph,
  stateTone,
  whyAgent,
  type Task,
  type TaskPage,
} from './types'

type Tab = 'live' | 'waiting' | 'recent'

const TABS: { id: Tab; label: string; hint: string }[] = [
  { id: 'live', label: 'Live', hint: 'Holding a pool slot, and costing money' },
  { id: 'waiting', label: 'Waiting', hint: 'Durable and free: READY or PARKED' },
  { id: 'recent', label: 'Recent', hint: 'Finished, one way or another' },
]

function tabOf(t: Task): Tab {
  if (CONCURRENCY_STATES.has(t.state)) return 'live'
  if (TERMINAL_STATES.has(t.state)) return 'recent'
  return 'waiting'
}

/**
 * Screen A -- Agents. One table, three tabs, no sub-pages.
 *
 * The tab counts come from the ROWS, never from /v1/stats, so the badge and
 * the table can never disagree with each other.
 */
export function AgentsScreen({ onOpen }: { onOpen: (taskId: string) => void }) {
  const [tab, setTab] = useState<Tab>('live')
  const [profile, setProfile] = useState<string>('')
  const [grouped, setGrouped] = useState(false)

  return (
    <Screen
      title="Agents"
      load={loadTasks}
      summary={(d) => {
        const live = d.tasks.filter((t) => tabOf(t) === 'live').length
        return (
          <>
            {d.tasks.length} loaded · {live} holding a slot
            {d.tenant_id && ` · tenant ${d.tenant_id}`}
          </>
        )
      }}
      empty={{
        heading: 'No agents',
        body: 'The read succeeded and returned nothing. Nothing has been submitted under this tenant, or everything has aged out of the page.',
      }}
    >
      {(d) => (
        <AgentsBody
          onOpen={onOpen}
          page={d}
          tab={tab}
          setTab={setTab}
          profile={profile}
          setProfile={setProfile}
          grouped={grouped}
          setGrouped={setGrouped}
        />
      )}
    </Screen>
  )
}

function AgentsBody({
  onOpen,
  page,
  tab,
  setTab,
  profile,
  setProfile,
  grouped,
  setGrouped,
}: {
  onOpen: (taskId: string) => void
  page: TaskPage
  tab: Tab
  setTab: (t: Tab) => void
  profile: string
  setProfile: (p: string) => void
  grouped: boolean
  setGrouped: (g: boolean) => void
}) {
  // One clock for every ticking duration on the screen, so a hundred rows do
  // not each hold their own interval.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const counts = useMemo(() => {
    const c: Record<Tab, number> = { live: 0, waiting: 0, recent: 0 }
    for (const t of page.tasks) c[tabOf(t)]++
    return c
  }, [page.tasks])

  const profiles = useMemo(
    () => Array.from(new Set(page.tasks.map((t) => t.runner_profile))).sort(),
    [page.tasks],
  )

  const rows = useMemo(
    () =>
      page.tasks
        .filter((t) => tabOf(t) === tab)
        .filter((t) => profile === '' || t.runner_profile === profile)
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1)),
    [page.tasks, tab, profile],
  )

  return (
    <>
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={tab === t.id ? 'on' : ''}
            title={t.hint}
            onClick={() => setTab(t.id)}
          >
            {t.label} <span className="badge">{counts[t.id]}</span>
          </button>
        ))}
      </div>

      <div className="filters">
        <label>
          Profile
          <select value={profile} onChange={(e) => setProfile(e.target.value)}>
            <option value="">all</option>
            {profiles.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        {tab !== 'live' && (
          <label className="check">
            <input
              type="checkbox"
              checked={grouped}
              onChange={(e) => setGrouped(e.target.checked)}
            />
            Group by workflow
          </label>
        )}
        {/* Every filter here runs over the loaded page only, and says so. A
            count that looks server-side but is not is the same lie as an
            error rendered as an empty list. */}
        <span className="client-side">filters apply to the {page.tasks.length} loaded rows</span>
      </div>

      {rows.length === 0 ? (
        <div className="state">
          <h3>Nothing in this tab</h3>
          <p>
            {tab === 'live'
              ? 'No agent is holding a pool slot right now. This is a real zero from a successful read.'
              : tab === 'waiting'
                ? 'Nothing is waiting. READY and PARKED work costs nothing, so an empty tab here is normal.'
                : 'Nothing has finished in the loaded page.'}
          </p>
        </div>
      ) : grouped && tab !== 'live' ? (
        <GroupedRows rows={rows} now={now} onOpen={onOpen} />
      ) : (
        <div className="rows">
          {rows.map((t) => (
            <TaskRow key={t.id} task={t} now={now} onOpen={onOpen} />
          ))}
        </div>
      )}

      {page.next_page_token && (
        <p className="client-side page-note">
          More rows exist beyond this page. Counts and grouping above describe
          only what is loaded.
        </p>
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
}: {
  rows: Task[]
  now: number
  onOpen: (taskId: string) => void
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
              {wf === '' ? 'No workflow' : wf}
              <span className={`roll ${roll}`}>{roll}</span>
              <span className="client-side">
                {tasks.length} step{tasks.length === 1 ? '' : 's'} in this page
              </span>
            </h2>
            <div className="rows">
              {tasks.map((t) => (
                <TaskRow key={t.id} task={t} now={now} onOpen={onOpen} />
              ))}
            </div>
          </section>
        )
      })}
    </>
  )
}

function TaskRow({
  task,
  now,
  onOpen,
}: {
  task: Task
  now: number
  onOpen: (taskId: string) => void
}) {
  const holding = CONCURRENCY_STATES.has(task.state)
  const why = whyAgent(task)
  const el = elapsed(task, now)
  const units = RESOURCE_UNITS[task.resource_class]
  // Driven by the flag, not by an optimistic state flip. A cancel on a LEASED
  // or RUNNING task writes only cancel_requested -- the state does not change
  // until the worker or reconciler releases the lease, because releasing it
  // from the API would decrement a pool a live container still occupies.
  const cancelling = task.cancel_requested && !TERMINAL_STATES.has(task.state)

  return (
    <div
      className={`row clickable${holding ? ' holding' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(task.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen(task.id)
        }
      }}
    >
      <span className={`st ${stateTone(task.state)}`}>
        <span aria-hidden>{stateGlyph(task.state)}</span> {task.state}
      </span>

      <span className="agent">
        <b>{task.runner_profile}</b>
        {task.model && <span className="model">{task.model}</span>}
        <span className="id" title={task.id}>
          {task.id.slice(-8)}
        </span>
      </span>

      <span className="owner" title={task.submitted_by ?? undefined}>
        {task.submitted_by?.split('@')[0] ?? '—'}
      </span>

      <span className="wf">
        {task.step_id ? (
          <span className="tag" title={`workflow ${task.workflow_id}`}>
            {task.step_id}
          </span>
        ) : (
          <span className="dash">—</span>
        )}
      </span>

      <span className={`when${el.ticking ? ' ticking' : ''}`}>{el.text}</span>

      <span className="try" title={`${task.attempt_count} of ${task.max_attempts} attempts used`}>
        {task.attempt_count}/{task.max_attempts}
      </span>

      <span className="class">
        {task.resource_class}
        {units !== undefined && <span className="units"> · {units}u</span>}
      </span>

      <span className="badges">{cancelling && <span className="tag full">cancelling…</span>}</span>

      {/* The reason this screen exists on a phone: someone is checking why
          their agent has not moved. It outranks every identifier and is never
          the thing that gets dropped. */}
      {why && <span className="why">{why}</span>}
    </div>
  )
}
