// OVERVIEW, WAVE 3 (lane B27): #89, #90, #91, #92, #93, and #168's pin.
//
// Five issues filed against the old console, each re-checked against the
// "Lead and ledger" Overview (#432) and built only where it was still missing:
//
//   #89  Account headroom cut its own headline's account and the busy one.
//   #90  The failed-task item named no workflow; Running rows named none.
//   #91  The waiting backlog never reached the figure strip.
//   #92  "2 workers silent" named nobody, and the failed item had no age.
//   #93  The Running card cut a handful of agents to four.
//   #168 Already built (OV-10 and `overview.glance.test.tsx`); pinned there.
//
// Each test names the mutation that turns it red.

import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import type { Account, AccountsPage, Capacity, LeasePage, LeaseRow, Stats, Task, TaskPage } from '../types'
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

const { OverviewScreen, accountRowsShown } = await import('../Overview')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-23T10:00:00Z' }
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()

function account(label: string, used: number, over: Partial<Account> = {}): Account {
  return {
    account_id: `eng:${label}`,
    owner_tenant: 'eng',
    label,
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: { five_hour: { utilization: used, resets_at: '2099-01-01T00:00:00Z', reset: false } },
    observed_at: new Date().toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

function accountsPage(accounts: Account[]): AccountsPage {
  return { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 }
}

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: {},
  dispatch_paused: false,
  limits: {},
  generated_at: '2026-09-23T10:00:00Z',
}

const CAPACITY: Capacity = { pools: [], runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-09-23T10:00:00Z' }

const SPEND: SpendRollup = {
  tenantId: 'eng',
  tasksOnPage: 0,
  tasksWithAttempts: 0,
  tasksSampled: 0,
  failedReads: 0,
  failedDetail: null,
  attempts: 0,
  attemptsWithCost: 0,
  attemptsWithTokens: 0,
  costUsd: 0,
  inputTokens: 0,
  outputTokens: 0,
  cacheReadTokens: 0,
  cacheCreationTokens: 0,
  from: null,
  to: null,
} as unknown as SpendRollup

function lease(taskId: string, silentSeconds: number, over: Partial<LeaseRow> = {}): LeaseRow {
  return {
    lease_id: `lease-${taskId}`,
    task_id: taskId,
    attempt_id: `att-${taskId}`,
    tenant_id: 'eng',
    generation: 1,
    pools: ['global'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: ago(30),
    dispatch_deadline: ago(25),
    expires_at: new Date(Date.now() + 60 * MIN).toISOString(),
    heartbeat_at: ago(silentSeconds / 60),
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: silentSeconds,
    heartbeat_ever: true,
    last_error: null,
    ...over,
  }
}

function leasePage(leases: LeaseRow[]): LeasePage {
  return {
    leases,
    thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
    evaluated_at: new Date().toISOString(),
    active_only: true,
    tenant_id: 'eng',
    units_held: leases.length,
  } as unknown as LeasePage
}

function running(n: number, over: Partial<Task> = {}): Task {
  return task({
    id: `tsk_run_${n}`,
    tenant_id: 'eng',
    state: 'RUNNING',
    created_at: ago(60 + n),
    started_at: ago(50 - n),
    updated_at: ago(1),
    ...over,
  })
}

type Reads = Partial<Record<keyof typeof api, Result<unknown>>>

async function settle(): Promise<void> {
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
}

async function mount(reads: Reads = {}): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadTasks.mockResolvedValue(ok({ tasks: [], next_page_token: null } satisfies TaskPage))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(ok(accountsPage([account('laptop', 0.2)])))
  api.loadSpend.mockResolvedValue(ok(SPEND))
  for (const [name, result] of Object.entries(reads)) api[name as keyof typeof api].mockResolvedValue(result)
  const { container } = render(<OverviewScreen />)
  await settle()
  return container
}

function page(tasks: Task[]): Result<TaskPage> {
  return ok({ tasks, next_page_token: null, tenant_id: 'eng' } as TaskPage)
}

function problem(el: HTMLElement, pattern: RegExp): HTMLElement {
  const row = [...el.querySelectorAll<HTMLElement>('.ov-problem')].find((p) => pattern.test(text(p.querySelector('b'))))
  expect(row, `no attention item matches ${pattern}: ${[...el.querySelectorAll('.ov-problem b')].map(text).join(' | ')}`).toBeDefined()
  return row!
}

// ---------------------------------------------------------------------------
// #89
// ---------------------------------------------------------------------------

describe('#89: overview account headroom keeps the headline account and the busy one on screen', () => {
  /**
   * The issue's own picture: four accounts, sorted least-room first and cut
   * to three, so the account with the MOST room -- the headline's -- is the
   * one that gets cut, and the one serving agents goes with it.
   *
   * MUTATION: go back to `sort(rank).slice(0, ACCOUNT_ROWS)`.
   */
  const FOUR = [
    account('a', 0.95),
    account('b', 0.9),
    account('c', 0.8),
    account('roomy', 0.28),
  ]

  it('draws the headline’s own account as a row, and its figure matches', async () => {
    const el = await mount({ loadAccountPool: ok(accountsPage(FOUR)) })
    const figure = text(el.querySelector('.ov-headroom .ctl-dial .ctl-dial-figure'))
    expect(figure).toBe('28% used · roomy')
    const names = [...el.querySelectorAll('.ov-headroom .ov-dialrow-rows .ctl-util-name b')].map(text)
    expect(names, 'the headline’s account was cut from the rows').toContain('roomy')
    expect(names.length).toBe(3)
  })

  it('keeps every account serving agents, then fills worst-first', () => {
    const accounts = [
      account('a', 0.95),
      account('b', 0.9),
      account('busy', 0.6, { assigned: 2 }),
      account('roomy', 0.28),
    ]
    const shown = accountRowsShown(accounts, 'eng:roomy').map(({ a }) => a.label)
    // Pinned: roomy (headline) and busy (assigned > 0); one slot left, worst first.
    expect(shown).toEqual(['a', 'busy', 'roomy'])
  })

  it('shows every pinned account even when they outnumber the slots', () => {
    const accounts = [
      account('a', 0.95, { assigned: 1 }),
      account('b', 0.9, { assigned: 1 }),
      account('c', 0.8, { assigned: 1 }),
      account('roomy', 0.28),
    ]
    expect(accountRowsShown(accounts, 'eng:roomy').map(({ a }) => a.label)).toEqual(['a', 'b', 'c', 'roomy'])
  })
})

// ---------------------------------------------------------------------------
// #90
// ---------------------------------------------------------------------------

describe('#90: the overview names the workflow a failure came from', () => {
  /**
   * MUTATION: drop the workflow clause from `failureCheck`'s headline.
   */
  it('names the workflow behind the failed-task count', async () => {
    const tasks = [
      task({ id: 'tsk_f1', state: 'FAILED', workflow_id: 'wf-nightly', step_id: 'build', updated_at: ago(3), completed_at: ago(3) }),
      task({ id: 'tsk_f2', state: 'FAILED', workflow_id: 'wf-nightly', step_id: 'test', updated_at: ago(4), completed_at: ago(4) }),
      task({ id: 'tsk_f3', state: 'FAILED', updated_at: ago(9), completed_at: ago(9) }),
    ]
    const el = await mount({ loadTasks: page(tasks) })
    const p = problem(el, /failed task/)
    expect(text(p.querySelector('b'))).toMatch(/2 in workflow wf-nightly/)
  })

  /**
   * The Running card listed agents with no workflow attached.
   *
   * MUTATION: drop the workflow link from `RunningRow`.
   */
  it('marks a running agent with the workflow it is a step of, linked', async () => {
    const el = await mount({ loadTasks: page([running(1, { workflow_id: 'wf-nightly', step_id: 'build' })]) })
    const link = el.querySelector<HTMLAnchorElement>('.ov-running a[href^="#work/workflows"]')
    expect(link, 'the running row names no workflow').not.toBeNull()
    expect(link!.getAttribute('href')).toBe('#work/workflows?wf=wf-nightly')
    expect(text(link)).toMatch(/wf-nightly/)
  })
})

// ---------------------------------------------------------------------------
// #91
// ---------------------------------------------------------------------------

describe('#91: the overview shows how much work is waiting, and says waiting costs nothing', () => {
  /**
   * MUTATION: drop the Waiting tile, count a CONCURRENCY state into it, or
   * point its link anywhere but the Waiting tab.
   */
  it('counts QUEUED, READY and PARKED from /v1/stats and links the Waiting tab', async () => {
    const el = await mount({
      loadStats: ok({ ...STATS, tasks_by_state: { QUEUED: 12, READY: 4, PARKED: 5, RUNNING: 5, LEASED: 1 } }),
    })
    const tile = [...el.querySelectorAll<HTMLElement>('.ctl-metrics .ctl-metric')].find((t) =>
      text(t.querySelector('.ctl-metric-label')).startsWith('Waiting'),
    )
    expect(tile, 'the figure strip has no Waiting tile').toBeDefined()
    expect(text(tile!.querySelector('.ctl-metric-value'))).toMatch(/^21/)
    const href = tile!.getAttribute('href') ?? tile!.querySelector('a')?.getAttribute('href')
    expect(href).toBe('#work/running/waiting')
    // The wording on the surface says waiting creates no demand.
    expect(text(tile)).toMatch(/no capacity/)
  })
})

// ---------------------------------------------------------------------------
// #92
// ---------------------------------------------------------------------------

describe('#92: the overview’s attention items say which workers are silent and how fresh the failures are', () => {
  /**
   * MUTATION: drop the task ids from the silent headline, or the link label.
   */
  it('names the silent workers and labels the link with its destination', async () => {
    const el = await mount({
      loadLeases: ok(leasePage([lease('tsk_quiet_a', 300), lease('tsk_quiet_b', 200), lease('tsk_fine', 10)])),
    })
    const p = problem(el, /silent/)
    const headline = text(p.querySelector('b'))
    expect(headline).toMatch(/tsk_quiet_a/)
    expect(headline).toMatch(/tsk_quiet_b/)
    expect(headline).not.toMatch(/tsk_fine/)
    expect(text(p.querySelector('a'))).toMatch(/^holders/)
  })

  /**
   * MUTATION: drop the age clause from `failureCheck`.
   */
  it('says how old the newest failure is', async () => {
    const tasks = [
      task({ id: 'tsk_f1', state: 'FAILED', updated_at: ago(3), completed_at: ago(3) }),
      task({ id: 'tsk_f2', state: 'FAILED', updated_at: ago(120), completed_at: ago(120) }),
    ]
    const el = await mount({ loadTasks: page(tasks) })
    expect(text(problem(el, /failed task/).querySelector('b'))).toMatch(/newest 3m ago/)
  })

  /**
   * The Running card is built from tasks; a silent lease was invisible on it.
   * A silent row is marked, and sorts first so the cut cannot hide it.
   *
   * MUTATION: drop the lease join from `RunningBody`, or the silent-first sort.
   */
  it('marks a silent agent on its Running row and keeps it on screen', async () => {
    const rows = Array.from({ length: 12 }, (_, i) => running(i))
    // The newest-started agent would sort last; it is the silent one.
    const quiet = running(99, { id: 'tsk_quiet', started_at: ago(1) })
    const el = await mount({
      loadTasks: page([...rows, quiet]),
      loadLeases: ok(leasePage([lease('tsk_quiet', 400)])),
    })
    const visible = [...el.querySelectorAll('.ov-running > .ov-rows > table tbody tr')]
    const row = visible.find((r) => text(r).includes('tsk_quiet'))
    expect(row, 'the silent agent is not among the visible Running rows').toBeDefined()
    expect(row!.querySelector('.ov-silent'), 'the silent agent’s row carries no mark').not.toBeNull()
    expect(text(row!.querySelector('.ov-silent'))).toMatch(/silent 6m/)
    // The control: a beating agent is not marked.
    expect(el.querySelectorAll('.ov-running .ov-silent').length).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// #93
// ---------------------------------------------------------------------------

describe('#93: the overview’s Running card shows every running agent, or discloses the rest in place', () => {
  /**
   * MUTATION: put `RUNNING_ROWS` back to 4.
   */
  it('shows five running agents as five rows', async () => {
    const el = await mount({ loadTasks: page(Array.from({ length: 5 }, (_, i) => running(i))) })
    expect(el.querySelectorAll('.ov-running tbody tr').length).toBe(5)
    expect(el.querySelector('.ov-running details')).toBeNull()
  })

  /**
   * MUTATION: drop the disclosure, so rows past the cap vanish again.
   */
  it('discloses the rest in place past the cap', async () => {
    const el = await mount({ loadTasks: page(Array.from({ length: 11 }, (_, i) => running(i))) })
    expect(el.querySelectorAll('.ov-running > .ov-rows > table tbody tr').length).toBe(8)
    const more = el.querySelector('.ov-running details.ov-more')
    expect(more, 'the rows past the cap are not disclosed in place').not.toBeNull()
    expect(text(more!.querySelector('summary'))).toBe('3 more')
    expect(more!.querySelectorAll('tbody tr').length).toBe(3)
  })
})
