// LANE QA2-FIX-12 (part of #2694, the console visual QA pass 2 epic): the
// Agents list's findings from sweep lane QA2-OVERVIEW-AGENTS on the fixture
// console. Each `it` names its finding and the mutation that turns it red.

import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadTasksInStates: vi.fn(),
  loadTask: vi.fn(),
  loadAttempts: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadLiveSession: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { AgentsScreen, CANCEL_NOTE, poolWords } = await import('../Agents')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function row(n: number, state: TaskState, over: Partial<Task> = {}): Task {
  const ended = state === 'SUCCEEDED' || state === 'FAILED' || state === 'CANCELLED'
  return runTask({
    id: `task_${n.toString(16).padStart(20, '0')}`,
    state,
    runner_profile: 'claude-code',
    created_at: new Date(Date.UTC(2026, 9, 29, 10, 0, n)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 9, 29, 11, 0, n)).toISOString(),
    started_at: ended ? new Date(Date.UTC(2026, 9, 29, 10, 0, n)).toISOString() : null,
    completed_at: ended ? new Date(Date.UTC(2026, 9, 29, 10, 6, 32 + n)).toISOString() : null,
    attempt_count: ended ? 1 : 0,
    max_attempts: 3,
    metadata: {},
    ...over,
  })
}

const queued = row(1, 'QUEUED')
const ready = row(2, 'READY', {
  waiting: {
    reason: 'READY_CAPACITY',
    summary: 'Ready; capacity binds on runner:merge (4 of 4 units in use)',
    since: null,
    next_check_at: null,
    resumes_when: 'runner:merge has room for 1 units and the scheduler admits it',
  } as unknown as Task['waiting'],
})
const cancelled = row(3, 'CANCELLED')

function serve(tasks: Task[]) {
  api.loadTasks.mockResolvedValue(ok({ tasks, next_page_token: null, tenant_id: 'eng' } satisfies TaskPage))
  api.loadTasksInStates.mockResolvedValue(ok({ tasks: [], next_page_token: null, tenant_id: 'eng' } satisfies TaskPage))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [{ generation: 1 }] }))
  api.loadResourceClasses.mockReturnValue(new Promise(() => {}))
  api.loadTask.mockReturnValue(new Promise(() => {}))
  api.loadLiveSession.mockReturnValue(new Promise(() => {}))
}

const rowOf = (t: Task) => document.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${t.id}"]`)
const idsIn = (h: HTMLElement) => [...h.closest('section')!.querySelectorAll('[data-task-id]')].map((r) => r.getAttribute('data-task-id'))

afterEach(() => {
  vi.clearAllMocks()
})

describe('QA2-FIX-12: the Agents list', () => {
  it('ovag2-10: a cancelled row says it was cancelled by request after its run time', async () => {
    serve([cancelled])
    render(<AgentsScreen onOpen={() => {}} list={{ tab: 'recent', state: null }} />)
    await waitFor(() => expect(rowOf(cancelled)).not.toBeNull())
    const note = rowOf(cancelled)!.querySelector('.cr-sub .when-note')!
    // MUTATION: put `to cancel` back.
    expect(CANCEL_NOTE).toBe('cancelled by request')
    expect(flat(note)).toBe('cancelled by request')
    expect(flat(rowOf(cancelled)!.querySelector('.cr-sub'))).toMatch(/ran 6m 3\ds · cancelled by request$/)
  })

  it('overview-agents-56: a queued row is not yet eligible, and a ready row waits for room in a named pool', async () => {
    serve([queued, ready])
    render(<AgentsScreen onOpen={() => {}} list={{ tab: 'waiting', state: null }} />)
    await waitFor(() => expect(rowOf(queued)).not.toBeNull())
    // MUTATION: file a QUEUED row under `No room` again.
    expect(idsIn(screen.getByRole('heading', { name: /^Not yet eligible/ }))).toEqual([queued.id])
    expect(idsIn(screen.getByRole('heading', { name: /^No room/ }))).toEqual([ready.id])
    // MUTATION: drop `poolWords` and the line names `runner:merge`.
    const why = flat(rowOf(ready)!.querySelector('.why'))
    expect(why).toBe('capacity: merge full')
    expect(rowOf(ready)!.querySelector('.why')!.getAttribute('title') ?? '').toContain('runner:merge')
  })

  it('overview-agents-56: a ready row with only `waiting_for` names its pool, the raw key in its title', async () => {
    const lead = { pool: 'provider:anthropic:tenant:u-bogdan', state: 'full', active: 5, limit: 5, units: 1, reason: 'PROVIDER_CONCURRENCY_LIMIT' }
    const bare = row(4, 'READY', {
      waiting_for: { as_of: '2026-10-29T11:00:00Z', admissible_now: false, holds_capacity: false, lead, pools: [lead], complete: true },
    } as Partial<Task>)
    serve([bare])
    render(<AgentsScreen onOpen={() => {}} list={{ tab: 'waiting', state: null }} />)
    await waitFor(() => expect(rowOf(bare)).not.toBeNull())
    expect(idsIn(screen.getByRole('heading', { name: /^No room/ }))).toEqual([bare.id])
    const why = rowOf(bare)!.querySelector('.why')!
    // MUTATION: drop the READY `waitingLine` branch in `rowReason` and the
    // line reads `waiting for: provider:anthropic:tenant:u-bogdan 5/5 units`.
    expect(flat(why)).toBe('waiting for: anthropic · u-bogdan 5/5 units')
    expect(why.getAttribute('title') ?? '').toContain('provider:anthropic:tenant:u-bogdan')
  })

  it('overview-agents-56: poolWords names the capacity line’s pool and leaves every other line alone', () => {
    expect(poolWords('capacity: provider:anthropic:tenant:eng 3/4 used')).toBe('capacity: anthropic · eng 3/4 used')
    expect(poolWords('capacity: backend:CLOUD_RUN_JOB paused')).toBe('capacity: Cloud Run paused')
    expect(poolWords('CI #1779: 5 pending')).toBe('CI #1779: 5 pending')
    expect(poolWords(null)).toBeNull()
  })
})
