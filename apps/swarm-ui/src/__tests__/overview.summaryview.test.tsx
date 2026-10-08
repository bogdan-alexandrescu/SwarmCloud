// #168, THE UI HALF: the Overview polls `GET /v1/tasks?view=summary`.
//
// What was already on main when this file was written: the phone cap
// (`loadListPage` asks for `PHONE_PAGE_LIMIT` at 560px and under, OV-10 in
// `layout.overview.test.tsx`), the hidden-tab pause (`usePoll`, OV-16 there),
// and the API's `view=summary` (`routes/tasks.py` `list_tasks`, `codec.py`
// `task_to_api`, `tests/unit/control_plane/test_task_list_summary_view.py`).
// What was left is the screen asking for it, so the 20 s poll stops moving
// every row's `input`, `metadata` and `result_summary` -- §2.5's "~40x
// smaller payload" -- and a proof that nothing the screen draws needed them.
//
// Each test names the mutation that turns it red.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import type { Account, AccountsPage, Capacity, Stats, Task, TaskPage } from '../types'
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

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-23T10:00:00Z' }
}

const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()

/** `phoneWidth()` reads `matchMedia`, which jsdom does not have. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const ACCOUNT: Account = {
  account_id: 'eng:laptop',
  owner_tenant: 'eng',
  label: 'laptop',
  provider: 'anthropic-subscription',
  state: 'AVAILABLE',
  reason: '',
  lend_to: [],
  assigned: 0,
  windows: { five_hour: { utilization: 0.2, resets_at: '2099-01-01T00:00:00Z', reset: false } },
  observed_at: new Date().toISOString(),
  stale: false,
  unreadable_by: [],
  unreadable_now: [],
  last_assigned_at: null,
}

const ACCOUNTS: AccountsPage = { accounts: [ACCOUNT], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 }

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: {},
  dispatch_paused: false,
  limits: {},
  generated_at: '2026-09-23T10:00:00Z',
}

const CAPACITY: Capacity = { pools: [], runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-09-23T10:00:00Z' }

const SPEND = {
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

/** What `view=summary` leaves out of a row: `codec.SUMMARY_DROPPED_KEYS`. */
const DROPPED = [
  'input',
  'input_redaction_count',
  'metadata',
  'metadata_redaction_count',
  'result_summary',
  'result_summary_redaction_count',
] as const

/**
 * A row as `view=summary` serves it -- the dropped keys ABSENT -- except that
 * each dropped key is a trap: a NON-ENUMERABLE getter that records the read
 * and answers `undefined`, as the absent key would. Non-enumerable, so a
 * spread or `Object.keys` (which the real absent key is invisible to as well)
 * does not count as a read; only code that names the field does.
 */
function summaryRow(row: Task, touched: string[]): Task {
  const out: Record<string, unknown> = { ...row }
  for (const key of DROPPED) {
    delete out[key]
    Object.defineProperty(out, key, {
      enumerable: false,
      get: () => {
        touched.push(`${row.id}.${key}`)
        return undefined
      },
    })
  }
  return out as unknown as Task
}

/** A page with every population the screen draws: running, waiting, parked, failed, finished, a workflow's steps. */
function rows(): Task[] {
  return [
    task({ id: 'tsk_run_1', tenant_id: 'eng', state: 'RUNNING', created_at: ago(40), started_at: ago(30), updated_at: ago(1), current_lease_id: 'lse_1' }),
    task({ id: 'tsk_run_2', tenant_id: 'eng', state: 'RUNNING', workflow_id: 'wf-nightly', step_id: 'build', created_at: ago(39), started_at: ago(29), updated_at: ago(1) }),
    task({ id: 'tsk_ready', tenant_id: 'eng', state: 'READY', created_at: ago(20), updated_at: ago(2) }),
    task({ id: 'tsk_parked', tenant_id: 'eng', state: 'PARKED', park_reason: 'quota', created_at: ago(18), updated_at: ago(3) }),
    task({ id: 'tsk_failed', tenant_id: 'eng', state: 'FAILED', workflow_id: 'wf-nightly', step_id: 'test', last_error: 'exit 1', created_at: ago(60), updated_at: ago(5), completed_at: ago(5) }),
    task({ id: 'tsk_done', tenant_id: 'eng', state: 'SUCCEEDED', created_at: ago(90), started_at: ago(80), updated_at: ago(70), completed_at: ago(70) }),
  ].map((t) => ({ ...t, attempt_count: t.state === 'READY' ? 0 : 1 }))
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
  api.loadAccountPool.mockResolvedValue(ok(ACCOUNTS))
  api.loadSpend.mockResolvedValue(ok(SPEND))
  for (const [name, result] of Object.entries(reads)) api[name as keyof typeof api].mockResolvedValue(result)
  const { container } = render(<OverviewScreen />)
  await settle()
  return container
}

describe("#168: the Overview's task poll asks for view=summary", () => {
  /**
   * The phone page, without the three heavy fields. The limit is OV-10's;
   * this pins the view beside it.
   *
   * MUTATION: drop `{ view: 'summary' }` from `loadListPage`.
   */
  it('asks the Overview phone read for the 50-row summary page', async () => {
    media(true)
    api.loadTasks.mockClear()
    await mount()
    const calls = api.loadTasks.mock.calls
    expect(calls.length, 'the Overview made no task read').toBeGreaterThan(0)
    expect(calls.filter((c) => c[0] !== 50 || (c[1] as { view?: string } | undefined)?.view !== 'summary')).toEqual([])
  })

  /**
   * The control: the wide screen asks for the 200-row page -- the limit
   * still differs by width, so a test that passed whatever was asked could
   * not pass both of these -- and asks for it in the summary view too.
   *
   * MUTATION: ask for the summary only on a phone.
   */
  it('asks the Overview wide read for the 200-row summary page', async () => {
    media(false)
    api.loadTasks.mockClear()
    await mount()
    const calls = api.loadTasks.mock.calls
    expect(calls.length).toBeGreaterThan(0)
    expect(calls.filter((c) => c[0] !== 200 || (c[1] as { view?: string } | undefined)?.view !== 'summary')).toEqual([])
  })
})

describe('#168: nothing the Overview draws reads a field view=summary drops', () => {
  /**
   * The trap works: a row that is read by name records it. Without this the
   * next test's empty list could mean the trap never fires.
   */
  it('records a read of a dropped field (the trap, Overview control)', () => {
    const touched: string[] = []
    const row = summaryRow(task({ id: 'tsk_probe' }), touched)
    expect(Object.keys(row)).not.toContain('result_summary')
    expect({ ...row }).not.toHaveProperty('metadata')
    expect(touched, 'a spread or a key listing counted as a read').toEqual([])
    void row.result_summary
    void row.input
    expect(touched).toEqual(['tsk_probe.result_summary', 'tsk_probe.input'])
  })

  /**
   * The screen renders the summary page and names none of the six keys.
   *
   * MUTATION: read `t.result_summary` (or `t.metadata`) anywhere the
   * Overview draws a task row -- a Running row, a failure, a waiting group.
   */
  it('renders the Overview from summary rows without reading input, metadata or result_summary', async () => {
    media(false)
    const touched: string[] = []
    const page = rows().map((t) => summaryRow(t, touched))
    const el = await mount({ loadTasks: ok({ tasks: page, next_page_token: null, tenant_id: 'eng' } as TaskPage) })
    // The page was DRAWN, not dropped on the floor: the running agents reach
    // the screen, so an empty `touched` is a reading of real rendering.
    expect(el.textContent ?? '').toMatch(/running/i)
    expect(el.innerHTML, 'the running agent never reached the screen').toContain('tsk_run_1')
    expect(touched, 'the Overview read a field the summary view does not serve').toEqual([])
  })
})
