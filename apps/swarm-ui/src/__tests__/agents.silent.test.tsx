// #179, THE LIST HALF: the Agents list says a worker has gone silent.
//
// A worker beats on its LEASE, never its task, so a list row (a task
// document) could not say its worker had stopped. `GET /v1/tasks` now carries,
// on each lease-holding row, the lease's `heartbeat_at`, the reconciler's
// `heartbeat_grace_seconds` and `heartbeat` (what the reading is) --
// `swarm_api/heartbeats.py`. AG-14 (#82): a stuck or silent worker needs a
// person, so its why line is drawn in `--warn`.
//
// MUTATIONS, one per block: return null from `silentWorkerLine` (no line);
// compare against a constant 90 instead of the row's grace; treat a null
// `heartbeat_at` as silent; ignore `heartbeat: 'not read'`; drop the
// `is-warn` class from the row's why line.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTasks: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen, silentWorkerLine } from '../Agents'
import { resetListSnap } from '../listSnap'

function ago(seconds: number, from: number = Date.now()): string {
  return new Date(from - seconds * 1000).toISOString()
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'acme',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: ago(3600),
    updated_at: ago(3600),
    started_at: ago(3000),
    completed_at: null,
    submitted_by: 'alex@acme.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: {},
    repository_ref: null,
    input: {},
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: `lease_${id}`,
    ...over,
  }
}

function beat(seconds: number | null, over: Partial<Task> = {}): Partial<Task> {
  return {
    heartbeat_at: seconds === null ? null : ago(seconds),
    heartbeat_grace_seconds: 90,
    heartbeat: 'read',
    ...over,
  }
}

async function land(tasks: Task[]): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} taskId={null} />)
  await waitFor(() => expect(container.querySelector('.row.is-compact')).not.toBeNull())
  await act(async () => {})
  return container as HTMLElement
}

function rowOf(container: HTMLElement, id: string): HTMLElement {
  const row = container.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"]`)
  expect(row, `no row for ${id}`).not.toBeNull()
  return row!
}

afterEach(() => {
  localStorage.removeItem('swarm.agents.list')
  resetListSnap()
})

describe('the Agents list draws a silent worker from the heartbeat on its row (#179)', () => {
  it('draws a --warn why line on a RUNNING row whose worker stopped beating, and none on a healthy one', async () => {
    const silent = task('task_silent00000000000001', 'RUNNING', beat(600))
    const healthy = task('task_healthy0000000000002', 'RUNNING', beat(20))
    const c = await land([silent, healthy])
    const why = rowOf(c, silent.id).querySelector('.why')
    expect(why, 'a silent worker drew no why line on the list').not.toBeNull()
    expect(why!.classList.contains('is-warn')).toBe(true)
    expect(why!.textContent).toMatch(/No heartbeat for 10m/)
    expect(why!.textContent).toMatch(/reconciler reclaims a stale lease/)
    expect(rowOf(c, healthy.id).querySelector('.why')).toBeNull()
  })

  it('draws it for every slot-holding state, and only those', async () => {
    const held = (['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING'] as TaskState[]).map((s, i) =>
      task(`task_held${i}000000000000000`, s, beat(600)),
    )
    const c = await land(held)
    for (const t of held) {
      expect(rowOf(c, t.id).querySelector('.why.is-warn')?.textContent, t.state).toMatch(/No heartbeat/)
    }
  })
})

describe('silentWorkerLine: the Agents row reads the reconciler’s rule, not a guess', () => {
  const now = Date.parse('2026-10-06T12:00:00Z')
  const at = (s: number) => ago(s, now)

  it('is silent only past the row’s own grace, judged strictly as the reconciler does', () => {
    const t = (s: number, grace: number) =>
      task('task_x', 'RUNNING', { heartbeat_at: at(s), heartbeat_grace_seconds: grace, heartbeat: 'read' })
    expect(silentWorkerLine(t(90, 90), now)).toBeNull()
    expect(silentWorkerLine(t(91, 90), now)).not.toBeNull()
    // The grace is the row's -- a reconciler configured to 300s is not
    // second-guessed by a constant 90 here.
    expect(silentWorkerLine(t(200, 300), now)).toBeNull()
    expect(silentWorkerLine(t(301, 300), now)).not.toBeNull()
  })

  it('never calls a lease that has never beaten silent: a booting worker is not a gone one', () => {
    const t = task('task_x', 'DISPATCHED', { heartbeat_at: null, heartbeat_grace_seconds: 90, heartbeat: 'read' })
    expect(silentWorkerLine(t, now)).toBeNull()
  })

  it('never draws a failed lease read as silence', () => {
    const t = task('task_x', 'RUNNING', { heartbeat_at: null, heartbeat_grace_seconds: 90, heartbeat: 'not read' })
    expect(silentWorkerLine(t, now)).toBeNull()
    const noLease = task('task_x', 'RUNNING', { heartbeat_at: null, heartbeat_grace_seconds: 90, heartbeat: 'no lease' })
    expect(silentWorkerLine(noLease, now)).toBeNull()
  })

  it('says nothing for a task that holds no slot, or from an API that sends no heartbeat', () => {
    const parked = task('task_x', 'PARKED', { heartbeat_at: at(600), heartbeat_grace_seconds: 90, heartbeat: 'read' })
    expect(silentWorkerLine(parked, now)).toBeNull()
    expect(silentWorkerLine(task('task_x', 'RUNNING'), now)).toBeNull()
  })
})
