// QA2-FIX-17 (part of #2694, the console visual QA pass 2 epic, and #2391):
// the findings whose main file is Overview.tsx.
//
//   ovag2-02  Running now said 'silent 2m' for a worker whose pane said
//             'Heartbeat silent 9d 20h ago': the chip drew the lease's
//             `silent_seconds` as of the read's `evaluated_at`, never aged.
//             It is aged to now, as Holders ages it, and drawn whole.
//   ovag2-04  the Pools card listed raw keys ('provider:anthropic:tenant:
//             u-bogdan', 'backend:CLOUD_RUN_JOB'); each pool is named as
//             Headroom names it, the key on hover.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, LeasePage, LeaseRow, Pool, Stats, Task, TaskPage } from '../types'
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

const Overview = await import('../Overview')

afterEach(cleanup)

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-29T10:00:00Z' }
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()
const SEC = 1000
const DAY = 24 * 3600 * SEC
const HOUR = 3600 * SEC

const STATS: Stats = { tenant_id: 'u-bogdan', tasks_by_state: {}, dispatch_paused: false, limits: {}, generated_at: '2026-10-29T10:00:00Z' }

function pool(name: string, active: number, limit: number | null): Pool {
  return {
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active,
    available: limit === null ? null : Math.max(0, limit - active),
    enabled: true,
  } as unknown as Pool
}

/**
 * The fixture console's shape: the lease page was evaluated 9d 20h ago (the
 * fixture clock), when the worker had been quiet for 140s.
 */
function stalePage(now: number): LeasePage {
  const read = now - (9 * DAY + 20 * HOUR)
  const lease: LeaseRow = {
    lease_id: 'c1', task_id: 'task_8ea5c26d', attempt_id: 'att_c1', tenant_id: 'u-bogdan', generation: 1,
    pools: ['global'], units: 1, dispatch_state: 'DISPATCHED',
    created_at: new Date(read - 600 * SEC).toISOString(),
    dispatch_deadline: new Date(read - 300 * SEC).toISOString(),
    expires_at: new Date(read + 120 * SEC).toISOString(),
    heartbeat_at: new Date(read - 140 * SEC).toISOString(),
    released_at: null, release_reason: null, released: false, expired: false, dispatch_overdue: false,
    silent_seconds: 140, heartbeat_ever: true, last_error: null,
  } as unknown as LeaseRow
  return {
    leases: [lease],
    thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
    evaluated_at: new Date(read).toISOString(),
    active_only: true,
    tenant_id: 'u-bogdan',
    units_held: 1,
  } as unknown as LeasePage
}

async function mount(tasks: Task[], leases: Result<LeasePage>, pools: Pool[] = []): Promise<HTMLElement> {
  const capacity: Capacity = { pools, runner_profiles: {}, tenant_id: 'u-bogdan', generated_at: '2026-10-29T10:00:00Z' }
  api.loadCapacity.mockResolvedValue(ok(capacity))
  api.loadTasks.mockResolvedValue(ok({ tasks, next_page_token: null, tenant_id: 'u-bogdan' } as TaskPage))
  api.loadLeases.mockResolvedValue(leases)
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadSpend.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Overview.OverviewScreen />)
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

describe('ovag2-02: Running now says how long the worker has been silent, now', () => {
  it('ages the read’s 140s by the 9d 20h since the read, and draws the figure whole', async () => {
    const now = Date.now()
    const t = task({
      id: 'task_8ea5c26d', tenant_id: 'u-bogdan', state: 'RUNNING',
      created_at: new Date(now - 10 * DAY).toISOString(), started_at: new Date(now - 10 * DAY).toISOString(),
      updated_at: new Date(now - 10 * DAY).toISOString(),
    })
    const el = await mount([t], ok(stalePage(now)))
    const chip = el.querySelector('.ov-running .ov-silent')
    expect(chip, 'the silent worker carries no mark').not.toBeNull()
    // MUTATION: put back `l.silent_seconds` as served in `silentByTask` -> 'silent 2m': red.
    // MUTATION: put back `.split(' ')[0]` in RunningRow -> 'silent 9d': red.
    expect(text(chip)).toBe('silent 9d 20h')
    // One chip, never wrapped between its units.
    expect(chip!.textContent).toBe('silent 9d 20h')
  })
})

describe('ovag2-04: a pool is named in words, its key on hover', () => {
  it('writes Headroom’s labels on the Pools card, with the raw key as the title', async () => {
    const el = await mount([], { status: 'empty', fetchedAt: Date.now() }, [
      pool('provider:anthropic:tenant:u-bogdan', 5, 5),
      pool('backend:CLOUD_RUN_JOB', 1, 10),
      pool('runner:claude-code', 2, 10),
      pool('resource:browser', 0, 4),
      pool('runner:browser', 0, 4),
    ])
    const names = [...el.querySelectorAll('.ov-pools .ov-pl .ov-idc')]
    const by = (key: string) => names.find((n) => n.getAttribute('title') === key)
    // MUTATION: draw `{p.name}` in PoolRow again -> the raw keys: red.
    expect(text(by('provider:anthropic:tenant:u-bogdan'))).toBe('anthropic · u-bogdan')
    expect(text(by('backend:CLOUD_RUN_JOB'))).toBe('Cloud Run')
    expect(text(by('runner:claude-code'))).toBe('claude-code')
    // MUTATION: pass no `among` from PoolsBody -> 'browser' twice, two ceilings under one name: red.
    expect(text(by('resource:browser'))).toBe('browser · resource')
    expect(text(by('runner:browser'))).toBe('browser · runner')
    expect(names.every((n) => !text(n).includes('provider:'))).toBe(true)
  })

  it('says the label in the row’s spoken summary too', () => {
    const { container } = render(<Overview.PoolRow pool={pool('provider:anthropic:tenant:u-bogdan', 5, 5)} />)
    expect(container.querySelector('.ov-pl')!.getAttribute('title')).toBe('anthropic · u-bogdan: 5 of 5 units in use, full')
  })
})
