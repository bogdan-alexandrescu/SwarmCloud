// VISUAL QA PASS 1, LANE QA1-FIX-16 (part of #2596): Overview's Running now
// and Recent token cost, as CLD's live walk-through read them (10-27 build).
//
//   C-01: Cost so far was a dash on every running row (8 of 8) with its
//     reason in each dash's title alone. A running attempt records its cost
//     when it ends, so a first attempt has no figure; the foot now says so on
//     the surface, once.
//   C-02: Recent token cost sampled the 12 newest agents that had run, which
//     on a busy workspace are the running ones, so it read "12 of 12
//     unmeasured". The sample now leaves out every agent holding capacity,
//     and a page whose only agents that have run still hold capacity says so
//     instead of "none of them has started".
//   C-03: the head said "21 hold capacity" beside the Holding capacity tile's
//     20. The head counts the rows it lists; the tile keeps the one count.
//
// MUTATIONS: pass `job.page` to loadSpend again -> the sample case fails;
// drop the running branch of the empty state -> "No attempt has ended" fails;
// put `hold capacity` back in the head -> the C-03 case fails; drop the foot
// clause, or draw it when every row has a total -> the C-01 cases fail.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import type { Capacity, Stats, Task, TaskPage } from '../types'
import { task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
}))
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

const { OverviewScreen } = await import('../Overview')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-28T10:00:00Z' }
}

const CAPACITY: Capacity = { pools: [], runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-28T10:00:00Z' }

function stats(byState: Record<string, number>): Stats {
  return { tenant_id: 'eng', tasks_by_state: byState, dispatch_paused: false, limits: {}, generated_at: '2026-10-28T10:00:00Z' }
}

function rollup(over: Partial<SpendRollup> = {}): SpendRollup {
  return {
    tenantId: 'eng', tasksOnPage: 1, tasksWithAttempts: 1, tasksSampled: 1, failedReads: 0, failedDetail: null,
    attempts: 1, attemptsWithCost: 1, attemptsWithTokens: 1, costUsd: 0.5,
    inputTokens: 10, outputTokens: 10, cacheReadTokens: 0, cacheCreationTokens: 0,
    from: '2026-10-28T09:00:00Z', to: '2026-10-28T09:00:00Z', ...over,
  }
}

const STARTED = new Date(Date.now() - 5 * 60_000).toISOString()

/** A first attempt still running: the API read its attempts and none has a cost yet. */
const runningFirst = (id: string): Task =>
  task({ id, state: 'RUNNING', attempt_count: 1, started_at: STARTED, attempts: 1, cost_usd_total: null, cost_incomplete: true, last_attempt_cost_usd: null, attempts_read: 'ok' })

const failedOnce = (id: string): Task =>
  task({ id, state: 'FAILED', attempt_count: 1, started_at: STARTED, completed_at: new Date().toISOString(), attempts: 1, cost_usd_total: 0.5, cost_incomplete: false, last_attempt_cost_usd: 0.5, attempts_read: 'ok' })

async function mount(tasks: Task[], byState: Record<string, number>, spend: Result<SpendRollup> = ok(rollup())): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadTasks.mockResolvedValue(ok({ tasks, next_page_token: null, tenant_id: 'eng' } satisfies TaskPage))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadAccountPool.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue(ok(stats(byState)))
  api.loadSpend.mockResolvedValue(spend)
  const { container } = render(<OverviewScreen />)
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('C-02: Recent token cost samples agents whose attempts have ended', () => {
  it('leaves every agent holding capacity out of the page it sums', async () => {
    await mount([runningFirst('tsk_run_a'), failedOnce('tsk_failed_b'), runningFirst('tsk_run_c')], { RUNNING: 2, FAILED: 1 })
    expect(api.loadSpend).toHaveBeenCalled()
    const page = api.loadSpend.mock.calls[0]![0] as TaskPage
    expect(page.tasks.map((t) => t.id)).toEqual(['tsk_failed_b'])
  })

  it('says no attempt has ended, not that none has started, when every agent that has run is running', async () => {
    const el = await mount([runningFirst('tsk_run_a')], { RUNNING: 1 }, { status: 'empty', fetchedAt: Date.now() })
    const card = el.querySelector('.ov-spend')!
    expect(text(card)).toContain('No attempt has ended')
    expect(text(card)).not.toContain('none of them has started')
  })
})

describe('C-03: Running now counts its rows, the tile counts capacity', () => {
  it('heads the card with the rows it lists, not a second capacity count', async () => {
    const el = await mount([runningFirst('tsk_run_a'), runningFirst('tsk_run_c')], { RUNNING: 1 })
    const note = text(el.querySelector('#ov-running .ctl-card-note'))
    expect(note).toBe('2 listed')
    expect(text(el.querySelector('#ov-running'))).not.toMatch(/hold capacity/)
    // The foot still names the stats count when the two differ.
    expect(text(el.querySelector('#ov-running .ov-foot'))).toContain('stats say 1')
  })
})

describe('C-01: Cost so far says on the surface why a running row is a dash', () => {
  it('names when the cost appears while a row has none yet', async () => {
    const el = await mount([runningFirst('tsk_run_a')], { RUNNING: 1 })
    expect(text(el.querySelector('#ov-running tbody tr td:last-child'))).toBe('—')
    expect(text(el.querySelector('#ov-running .ov-foot'))).toContain('cost so far shows once an attempt ends')
  })

  it('says nothing when every row carries a total', async () => {
    const retried = task({ id: 'tsk_retry', state: 'RUNNING', attempt_count: 2, started_at: STARTED, attempts: 2, cost_usd_total: 1.2, cost_incomplete: true, last_attempt_cost_usd: null, attempts_read: 'ok' })
    const el = await mount([retried], { RUNNING: 1 })
    expect(text(el.querySelector('#ov-running .ov-foot'))).not.toContain('attempt ends')
  })
})
