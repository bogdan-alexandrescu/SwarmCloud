import { useMemo, useState } from 'react'
import { loadTasks } from './api'
import { Screen, timeAgo } from './Shell'
import { CONCURRENCY_STATES, TERMINAL_STATES, stateTone, type Task } from './types'

type Filter = 'live' | 'waiting' | 'done' | 'all'

export function AgentsScreen() {
  const [filter, setFilter] = useState<Filter>('live')

  return (
    <Screen
      title="Agents"
      load={loadTasks}
      summary={(d) => {
        const live = d.tasks.filter((t) => CONCURRENCY_STATES.has(t.state)).length
        // "holding capacity", not "running": LEASED and DISPATCHED cost a slot
        // before anything executes, and that distinction is the whole reason
        // invariant 3 counts from LEASED.
        return `${d.tasks.length} tasks · ${live} holding capacity`
      }}
      empty={{
        heading: 'No tasks',
        body: 'The read succeeded and returned nothing. This tenant has submitted no work, or everything has been purged.',
      }}
    >
      {(d) => <AgentTable tasks={d.tasks} filter={filter} setFilter={setFilter} />}
    </Screen>
  )
}

function AgentTable({
  tasks,
  filter,
  setFilter,
}: {
  tasks: Task[]
  filter: Filter
  setFilter: (f: Filter) => void
}) {
  const counts = useMemo(() => {
    const live = tasks.filter((t) => CONCURRENCY_STATES.has(t.state)).length
    const done = tasks.filter((t) => TERMINAL_STATES.has(t.state)).length
    return { live, waiting: tasks.length - live - done, done, all: tasks.length }
  }, [tasks])

  const shown = useMemo(() => {
    const by = (t: Task) =>
      filter === 'all' ||
      (filter === 'live' && CONCURRENCY_STATES.has(t.state)) ||
      (filter === 'done' && TERMINAL_STATES.has(t.state)) ||
      (filter === 'waiting' && !CONCURRENCY_STATES.has(t.state) && !TERMINAL_STATES.has(t.state))
    return tasks
      .filter(by)
      .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
  }, [tasks, filter])

  return (
    <>
      <div className="filters">
        {(['live', 'waiting', 'done', 'all'] as Filter[]).map((f) => (
          <button key={f} className={filter === f ? 'on' : ''} onClick={() => setFilter(f)}>
            {f} <span className="n">{counts[f]}</span>
          </button>
        ))}
      </div>

      {shown.length === 0 ? (
        // A filter matching nothing is NOT the platform being empty, and must
        // not borrow the empty-state language that says something about it.
        <div className="state">
          <h3>Nothing matches “{filter}”</h3>
          <p>
            {tasks.length === 1
              ? '1 task was read'
              : `${tasks.length} tasks were read`}
            ; none is in this group. Try “all”.
          </p>
        </div>
      ) : (
        <div className="rows">
          {shown.map((t) => (
            <TaskRow key={t.id} task={t} />
          ))}
        </div>
      )}
    </>
  )
}

function TaskRow({ task }: { task: Task }) {
  const holding = CONCURRENCY_STATES.has(task.state)
  return (
    <div className={`row${holding ? ' holding' : ''}`}>
      <span className={`dot ${stateTone(task.state)}`} aria-hidden />
      <span className="id" title={task.id}>
        {task.id}
      </span>
      <span className={`st ${stateTone(task.state)}`}>{task.state}</span>
      <span className="meta">{task.runner_profile}</span>
      <span className="meta when">{timeAgo(task.updated_at)}</span>
      <span className="badges">
        {task.attempt_count > 1 && (
          <span
            className="tag capped"
            title={`${task.attempt_count} of ${task.max_attempts} attempts used`}
          >
            try {task.attempt_count}/{task.max_attempts}
          </span>
        )}
        {task.workflow_id && (
          <span className="tag" title={`workflow ${task.workflow_id}, step ${task.step_id}`}>
            {task.step_id}
          </span>
        )}
        {task.cancel_requested && <span className="tag full">cancelling</span>}
        {task.state === 'BLOCKED' && task.blocked_by?.length ? (
          <span className="tag capped" title={`waiting on ${task.blocked_by.join(', ')}`}>
            blocked
          </span>
        ) : null}
      </span>
    </div>
  )
}
