/**
 * THE OVERVIEW'S FIRST PAINT FIRES ITS READS AT ONCE (visual QA pass 1, the
 * owner's pick of 2026-11-13).
 *
 * `loadListPage` awaited the newest-N page and only then started the seven
 * `?state=` reads, so first paint waited for the sum of the two. The state
 * reads now start beside the page; the page alone still decides whether they
 * are used (only when it has a next page), so a small tenant's list is still
 * the page and nothing else.
 *
 * Each assertion names the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import type { Capacity, Stats, Task, TaskPage, TaskState } from '../types'
import { task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadTasksInState: vi.fn(),
  loadTask: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
}))
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

const { OverviewScreen } = await import('../Overview')

const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const empty = { status: 'empty' as const, fetchedAt: Date.now() }
const settle = async () => {
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
}

const CAPACITY: Capacity = { pools: [], runner_profiles: {}, tenant_id: 'eng', generated_at: new Date().toISOString() }
const SPEND = { tenantId: 'eng', tasksOnPage: 0, tasksWithAttempts: 0, tasksSampled: 0, failedReads: 0, failedDetail: null, attempts: 0, attemptsWithCost: 0, attemptsWithTokens: 0, costUsd: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0, from: null, to: null } as unknown as SpendRollup
const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 1, SUCCEEDED: 300, FAILED: 0, DEAD_LETTERED: 0, CANCELLED: 0 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date().toISOString(),
}

/** Older than the page: only the RUNNING state read carries it. */
const OLD_RUNNER: Task = task({ id: 'tsk_oldrun01', tenant_id: 'eng', state: 'RUNNING', step_id: null, workflow_id: null, runner_profile: 'claude-code', created_at: ago(600), updated_at: ago(1), started_at: ago(500), attempt_count: 1, metadata: null })
const PAGE: Task[] = Array.from({ length: 3 }, (_, i) =>
  task({ id: `tsk_done_${i}`, tenant_id: 'eng', state: 'SUCCEEDED', created_at: ago(10 + i), updated_at: ago(5), completed_at: ago(5) }),
)

/** The page read, held open until the test lets it land. */
function holdPage(more: boolean): () => void {
  let land: () => void = () => undefined
  api.loadTasks.mockImplementation(
    () =>
      new Promise<Result<TaskPage>>((resolve) => {
        land = () => resolve(ok({ tasks: PAGE, next_page_token: more ? 'more' : null, tenant_id: 'eng' }))
      }),
  )
  api.loadTasksInState.mockImplementation(async (state: TaskState) =>
    state === 'RUNNING' ? ok({ tasks: [OLD_RUNNER], next_page_token: null, tenant_id: 'eng' } satisfies TaskPage) : empty,
  )
  return () => land()
}

function serveRest(): void {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadLeases.mockResolvedValue(empty)
  api.loadProviders.mockResolvedValue(empty)
  api.loadWorkflows.mockResolvedValue(empty)
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(empty)
  api.loadSpend.mockResolvedValue(ok(SPEND))
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'gone' } })
}

afterEach(() => {
  cleanup()
  for (const fn of Object.values(api)) fn.mockReset()
})

describe("Overview's first paint", () => {
  it('starts the state reads while the page read is still out', async () => {
    serveRest()
    const land = holdPage(true)
    render(<OverviewScreen />)
    await settle()
    expect(api.loadTasks).toHaveBeenCalled()
    // MUTATION: await `loadTasks` before starting `loadStateReads` again.
    expect(api.loadTasksInState).toHaveBeenCalled()
    const states = api.loadTasksInState.mock.calls.map((c) => c[0] as TaskState)
    expect(new Set(states)).toEqual(new Set(['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING', 'FAILED', 'DEAD_LETTERED', 'CANCELLED']))
    land()
    await settle()
  })

  it('uses the state reads when the page has more behind it', async () => {
    serveRest()
    const land = holdPage(true)
    const { container } = render(<OverviewScreen />)
    await settle()
    land()
    await settle()
    // MUTATION: drop `byState` from the page that has a next page.
    expect(container.innerHTML).toContain('tsk_oldrun01'.slice(4))
  })

  it('keeps a complete page alone, whatever the state reads said', async () => {
    serveRest()
    const land = holdPage(false)
    const { container } = render(<OverviewScreen />)
    await settle()
    land()
    await settle()
    // MUTATION: attach `byState` to a page with no next page.
    expect(container.innerHTML).not.toContain('tsk_oldrun01'.slice(4))
  })
})
