import { useMemo } from 'react'

import { Chip, Em, Mark } from './AgentDetail'
import { agentName } from './agentlist'
import { loadChildren } from './api'
import type { Result } from './fetch'
import { useRead } from './RunFiles'
import { useNow } from './useNow'
import { TERMINAL_STATES, elapsed, stateTone, type Task, type TaskPage, type TaskState } from './types'

/**
 * CHILD TASKS IN THE SPLIT DETAIL (agent-detail-2.html, pick A).
 *
 * A running agent may submit helpers through its worker (D15,
 * docs/design/child-tasks.md). The parent gets a Children tab; a child gets a
 * "child of …" link above its title. Children are NOT workflow steps and are
 * never drawn as DAG nodes: the one source a family may be drawn from is
 * `GET /v1/tasks?parent_task_id=` (§6.3).
 *
 * NOT ON MAIN'S API YET. Nothing here may present a missing field as an
 * answer: a task document without `parent_task_id` is from an API that cannot
 * say, so the tab is not offered at all (`childrenServed`), and a list read
 * whose rows do not carry the field is a list route that ignored the filter --
 * it is the tenant's newest tasks, not this parent's children -- and is said
 * as "not served" (`childrenOf`).
 */

/** Whether this API serves the child fields at all: the KEY's presence, never its value. */
export function childrenServed(task: Task): boolean {
  return Object.prototype.hasOwnProperty.call(task, 'parent_task_id')
}

/** The park the parent takes while it waits (request 40), compared as a string: main's `ParkReason` lacks it. */
export const CHILDREN_INCOMPLETE = 'CHILDREN_INCOMPLETE'

export type ChildList = { served: true; children: Task[] } | { served: false }

/**
 * THE LIST READ, CHECKED. Every row must carry `parent_task_id`, or the route
 * did not apply the filter; a row naming another parent is dropped rather
 * than drawn as this one's.
 */
export function childrenOf(page: TaskPage, parentId: string): ChildList {
  if (page.tasks.some((t) => !childrenServed(t))) return { served: false }
  return { served: true, children: page.tasks.filter((t) => t.parent_task_id === parentId) }
}

/** The design's order: unfinished first (running, then queued), then failed, then succeeded; each by submission. */
const ORDER: readonly TaskState[] = [
  'RUNNING',
  'STARTING',
  'DISPATCHED',
  'LEASED',
  'READY',
  'PARKED',
  'QUEUED',
  'FAILED',
  'DEAD_LETTERED',
  'CANCELLED',
  'SUCCEEDED',
]

export function childOrder(children: readonly Task[]): Task[] {
  const rank = (t: Task) => {
    const i = ORDER.indexOf(t.state)
    return i < 0 ? ORDER.length : i
  }
  return [...children].sort((a, b) => rank(a) - rank(b) || (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0))
}

/** "Waiting on N": children that have not ended. A failed or cancelled child HAS ended. */
export function waitingOn(children: readonly Task[]): number {
  return children.filter((t) => !TERMINAL_STATES.has(t.state)).length
}

function requestId(t: Task): string | null {
  const v = (t.metadata as Record<string, unknown> | null)?.child_request_id
  return typeof v === 'string' && v !== '' ? v : null
}

function awaitResumes(t: Task): number | null {
  const v = (t.metadata as Record<string, unknown> | null)?.child_await_resumes
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function outputsOf(t: Task): number | null {
  if (t.state !== 'SUCCEEDED') return null
  const a = (t.result_summary as Record<string, unknown> | null)?.artifacts
  return Array.isArray(a) ? a.length : null
}

/**
 * THE LINK BACK TO THE PARENT, above a child's title. Renders nothing for a
 * task that is not a child, and nothing for an API that does not say.
 */
export function AgParentLink({ task, parent }: { task: Task; parent?: Task | null }) {
  const id = task.parent_task_id
  if (typeof id !== 'string' || id === '') return null
  return (
    <p className="ag-parent">
      child of{' '}
      <a className="ctl-link" href={`#work/task/${encodeURIComponent(id)}`}>
        {parent ? agentName(parent) : id}
      </a>
      {parent && <span className="mono"> {id}</span>}
      {task.parent_attempt_id ? <span className="ctl-sub"> · submitted by attempt {task.parent_attempt_id}</span> : null}
    </p>
  )
}

/** The Children tab's count, or null while it is unknown. */
export function useChildCount(task: Task | null, readKey: string): number | null {
  const id = task !== null && childrenServed(task) ? task.id : ''
  const held = useRead<TaskPage>(
    () => (id === '' ? Promise.resolve<Result<TaskPage>>({ status: 'loading', since: Date.now() }) : loadChildren(id)),
    id,
    readKey,
    null,
  )
  if (id === '') return null
  const s = held.state
  if (s.status !== 'ok' && s.status !== 'stale') return null
  const list = childrenOf(s.data, id)
  return list.served ? list.children.length : null
}

/**
 * THE CHILDREN TAB. Drawn only for an API that serves the fields
 * (`childrenServed`); the split decides that.
 */
export function AgChildrenPane({ task, readKey }: { task: Task; readKey: string }) {
  const now = useNow(1000)
  const held = useRead<TaskPage>(() => loadChildren(task.id), task.id, readKey, null)
  const state = held.state
  const list = useMemo(
    () => (state.status === 'ok' || state.status === 'stale' ? childrenOf(state.data, task.id) : null),
    [state, task.id],
  )

  const awaiting = task.state === 'PARKED' && task.park_reason === CHILDREN_INCOMPLETE
  const resumes = awaitResumes(task)

  return (
    <section className="section panel ag-children" aria-label="Children">
      {awaiting && (
        <div className="ag-children-await" role="note">
          <p>
            <b>Awaiting its children.</b> It parked with <code>children_incomplete</code> and holds{' '}
            <b>no lease and no capacity</b>: nothing it waits on is counted against a pool. The scheduler moves it to
            ready once every child has ended and each succeeded child&apos;s outputs are written. Nobody needs to act.
          </p>
          <p className="ag-children-deadline">
            <b>Await deadline</b> <Em />{' '}
            <Mark
              kind="absent"
              say="The API does not serve the await deadline (child_await_max_seconds, counted from the park). Past it the unfinished children are cancelled with await_expired and the parent moves to ready anyway."
            />{' '}
            not served
          </p>
        </div>
      )}

      {state.status === 'loading' && (
        <p className="art-loading">
          <Mark kind="pending" say="Reading this agent's children. The read is in flight." />
          <span className="ctl-pending art-loading-bar" />
        </p>
      )}
      {state.status === 'error' && (
        <p className="att-none">
          <Mark kind="unread" say="The children read did not complete. Nothing here says this agent has none." /> children
          not read · {state.error.message}
        </p>
      )}
      {list !== null && !list.served && (
        <p className="att-none">
          <Mark
            kind="absent"
            say="The list route answering this console does not apply the parent_task_id filter: its rows carry no parent field, so they are the tenant's newest tasks and not this agent's children."
          />{' '}
          not served · <code>GET /v1/tasks?parent_task_id=</code>
        </p>
      )}
      {list !== null && list.served && (
        <ChildTable task={task} children={list.children} now={now} resumes={resumes} />
      )}
    </section>
  )
}

function ChildTable({
  task,
  children,
  now,
  resumes,
}: {
  task: Task
  children: Task[]
  now: number
  resumes: number | null
}) {
  const rows = childOrder(children)
  const counts = new Map<TaskState, number>()
  for (const c of children) counts.set(c.state, (counts.get(c.state) ?? 0) + 1)
  return (
    <>
      <div className="ctl-toolbar ag-children-head">
        <h2>Children</h2>
        <span className="ag-children-facts">
          waiting on <b>{waitingOn(children)}</b> · {children.length} of <Em />{' '}
          <Mark
            kind="absent"
            say="The fan-out cap (max_children_per_task) is a setting the API does not serve, so it is not restated here."
          />{' '}
          · depth 1: a child cannot have children
        </span>
      </div>
      {children.length > 0 && (
        <p className="ag-children-mix" aria-label="Children by state">
          {[...counts.entries()].map(([s, n]) => (
            <span key={s} className="ag-children-mixpart">
              <Chip tone={stateTone(s)} state={s}>
                {s}
              </Chip>{' '}
              {n}
            </span>
          ))}
          <span className="ctl-sub">
            {' '}
            · await refunds {resumes === null ? <Em /> : resumes} used
          </span>
        </p>
      )}
      {children.length === 0 ? (
        <p className="att-none">
          <Mark kind="zero" say="The read applied the filter and returned no child: this agent has submitted none." /> no
          children
        </p>
      ) : (
        <div className="ctl-table is-scroll">
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">State</th>
                <th role="columnheader" scope="col">Child</th>
                <th role="columnheader" scope="col">Request id</th>
                <th role="columnheader" scope="col">Runner</th>
                <th role="columnheader" scope="col">From</th>
                <th role="columnheader" scope="col">Now</th>
                <th role="columnheader" scope="col">Outputs</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {rows.map((c) => {
                const req = requestId(c)
                const out = outputsOf(c)
                return (
                  <tr role="row" key={c.id} data-task-id={c.id}>
                    <td role="cell" data-label="State">
                      <Chip tone={stateTone(c.state)} state={c.state}>
                        {c.state}
                      </Chip>
                    </td>
                    <th role="rowheader" scope="row">
                      <a className="ctl-link" href={`#work/task/${encodeURIComponent(c.id)}`}>
                        {agentName(c)}
                      </a>
                    </th>
                    <td role="cell" data-label="Request id" className="mono">
                      {req ?? <Em />}
                    </td>
                    <td role="cell" data-label="Runner">
                      {c.runner_profile} · {c.resource_class}
                    </td>
                    <td role="cell" data-label="From" className="mono">
                      {c.parent_attempt_id ?? <Em />}
                    </td>
                    <td role="cell" data-label="Now">
                      {elapsed(c, now).text}
                    </td>
                    <td role="cell" data-label="Outputs">
                      {out === null ? (
                        <Em />
                      ) : (
                        `${out} file${out === 1 ? '' : 's'} written`
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="ctl-card-note">
        The parent reads each child&apos;s end when it resumes and decides for itself; a failed child does not fail{' '}
        {agentName(task)}. Source: <code>GET /v1/tasks?parent_task_id=</code>
      </p>
    </>
  )
}
