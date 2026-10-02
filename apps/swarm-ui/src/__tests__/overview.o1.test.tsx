// OVERVIEW, BUILT TO O1 "LEAD AND LEDGER" (overview.html, the owner's pick
// 2026-10-01), and the ten findings the 2026-10-02 audit (#503) measured
// against it at 1440 and 390.
//
//   * Needs a look is a row of check cards -- a mark, a title, a line, a
//     link -- not a figure over a bar, a heading and a bulleted list.
//   * The lifecycle band breaks each figure down by state, under its mark.
//   * No "Running N agents · Units held N of 100" figure row between the band
//     and the cards.
//   * Running now: State first, then Agent · profile, Runtime, Cost so far,
//     and RUNNING is the brand's teal haloed disc, never the accent-blue dot.
//   * Waiting, and why groups by reason, with a count per group.
//   * Headroom's rows stay inside the card: profile tiles, pool rows and an
//     account line stacked in one column, never two side-by-side groups.
//   * Recent failures end in an age and an Open link.
//   * At 390: one "Overview" (the h1 steps aside for the phone header), the
//     three lifecycle figures three across, and Running as a compact list.
//
// Each test names the mutation that turns it red.

import STYLES from '../styles.css?raw'
import OVERVIEW_CSS from '../styles/overview.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { cascade } from './cssgate'
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

const SHEET = `${STYLES}\n${OVERVIEW_CSS}`

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-02T10:00:00Z' }
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
  tasks_by_state: {
    QUEUED: 22,
    READY: 9,
    PARKED: 6,
    LEASED: 1,
    DISPATCHED: 1,
    STARTING: 1,
    RUNNING: 9,
    SUCCEEDED: 400,
    FAILED: 12,
    DEAD_LETTERED: 2,
    CANCELLED: 3,
  },
  dispatch_paused: false,
  limits: {},
  generated_at: '2026-10-02T10:00:00Z',
}

const POOL = {
  hard_limit: 4,
  adaptive_target: null,
  quota_derived_limit: null,
  enabled: true,
  updated_at: '2026-10-02T10:00:00Z',
}

const CAPACITY: Capacity = {
  pools: [
    { name: 'global', ...POOL, hard_limit: 40, effective_limit: 40, active: 31, available: 9 },
    { name: 'resource:browser', ...POOL, effective_limit: 4, active: 4, available: 0 },
    { name: 'tenant:eng', ...POOL, hard_limit: 20, effective_limit: 20, active: 14, available: 6 },
  ],
  runner_profiles: {
    browser: {
      resource_class: 'browser',
      backend: 'gke',
      units: 1,
      pools: ['global', 'resource:browser', 'tenant:eng'],
      admission: {
        units: 1,
        headroom: 0,
        basis: 'measured',
        blockers: [{ pool: 'resource:browser', reason: 'POOL_FULL', active: 4, limit: 4, group: 'no_room' }],
        binding: ['resource:browser'],
        counterfactual: [],
        complete: true,
        unread: [],
        uncapped: [],
      },
    },
    'claude-code': {
      resource_class: 'standard',
      backend: 'cloud_run',
      units: 1,
      pools: ['global', 'tenant:eng'],
      admission: {
        units: 1,
        headroom: 6,
        basis: 'measured',
        blockers: [],
        binding: ['tenant:eng'],
        counterfactual: [],
        complete: true,
        unread: [],
        uncapped: [],
      },
    },
  },
  tenant_id: 'eng',
  generated_at: '2026-10-02T10:00:00Z',
} as unknown as Capacity

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

const TASKS: Task[] = [
  task({ id: 'tsk_run_a', state: 'RUNNING', runner_profile: 'claude-code', step_id: 'fix-heartbeat', created_at: ago(30), started_at: ago(18), updated_at: ago(1) }),
  task({ id: 'tsk_start_b', state: 'STARTING', runner_profile: 'codex', created_at: ago(3), started_at: ago(1), updated_at: ago(1) }),
  task({ id: 'tsk_park_1', state: 'PARKED', park_reason: 'PROVIDER_QUOTA_EXHAUSTED', created_at: ago(40), updated_at: ago(5) }),
  task({ id: 'tsk_park_2', state: 'PARKED', park_reason: 'PROVIDER_QUOTA_EXHAUSTED', created_at: ago(41), updated_at: ago(5) }),
  task({ id: 'tsk_q_1', state: 'QUEUED', workflow_id: 'wf_refactor', depends_on: ['tsk_x'], created_at: ago(9), updated_at: ago(9) }),
  task({ id: 'tsk_q_2', state: 'QUEUED', workflow_id: 'wf_refactor', depends_on: ['tsk_x'], created_at: ago(9), updated_at: ago(9) }),
  task({ id: 'tsk_q_3', state: 'QUEUED', workflow_id: 'wf_docs', depends_on: ['tsk_y'], created_at: ago(9), updated_at: ago(9) }),
  task({ id: 'tsk_fail_1', state: 'FAILED', last_error: 'exit 137 out of memory', step_id: 'migrate-doc', created_at: ago(60), updated_at: ago(25), completed_at: ago(25) }),
  task({ id: 'tsk_dead_1', state: 'DEAD_LETTERED', created_at: ago(90), updated_at: ago(58), completed_at: ago(58) }),
]

function page(tasks: Task[]): Result<TaskPage> {
  return ok({ tasks, next_page_token: null, tenant_id: 'eng' } as TaskPage)
}

type Reads = Partial<Record<keyof typeof api, Result<unknown>>>

async function settle(): Promise<void> {
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
}

async function mount(reads: Reads = {}): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadTasks.mockResolvedValue(page(TASKS))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(
    ok(accountsPage([account('laptop', 0.3), account('desk', 0.92), account('spare', 0.1, { state: 'REAUTH_REQUIRED' })])),
  )
  api.loadSpend.mockResolvedValue(ok(SPEND))
  for (const [name, result] of Object.entries(reads)) api[name as keyof typeof api].mockResolvedValue(result)
  const { container } = render(<OverviewScreen />)
  await settle()
  return container
}

afterEach(cleanup)

describe('O1: Needs a look is a row of check cards', () => {
  /** MUTATION: put the dial, the "N things need attention" heading or the bulleted list back. */
  it('draws each open problem as a card with a mark, a title, a line and a link', async () => {
    const el = await mount()
    const lead = el.querySelector('#ov-needs')!
    expect(text(lead.querySelector('.ov-lh h2'))).toBe('Needs a look')
    const cards = [...lead.querySelectorAll<HTMLAnchorElement>('a.ov-att')]
    expect(cards.length, 'no check card').toBeGreaterThan(0)
    for (const c of cards) {
      expect(c.getAttribute('href'), 'a check card goes nowhere').toMatch(/^#/)
      expect(c.querySelector('.sk-st[data-mark]'), 'a check card has no mark').not.toBeNull()
      expect(text(c.querySelector('.ov-att-t b')).length).toBeGreaterThan(0)
      expect(text(c.querySelector('.ov-att-t small')).length).toBeGreaterThan(0)
      expect(text(c.querySelector('.ov-att-go'))).toMatch(/→$/)
    }
    expect(lead.querySelector('.ov-dial, .ctl-dial'), 'the lead still draws a figure over a bar').toBeNull()
    expect(lead.querySelector('ul'), 'the lead still draws a bulleted list').toBeNull()
    expect(text(lead)).not.toMatch(/need(s)? attention/)
  })

  it('says how many checks found something, of how many, on this read', async () => {
    const el = await mount()
    expect(text(el.querySelector('#ov-needs .ov-lh .ov-cnt'))).toMatch(/^\d+ (check|checks) of \d+ · derived on this read/)
  })

  it('lays the cards three across at 1440', async () => {
    const el = await mount()
    const atts = el.querySelector('#ov-needs .ov-atts')!
    expect(cascade(SHEET, atts, 'grid-template-columns', { width: 1440 }).winner?.value).toBe('repeat(3, minmax(0, 1fr))')
  })
})

describe('O1: the lifecycle band breaks each figure down by state', () => {
  /** MUTATION: drop `.ov-lc-ps`, or count the band off the task page instead of /v1/stats. */
  it('counts waiting and holding from /v1/stats, each state under its own mark', async () => {
    const el = await mount()
    const cells = [...el.querySelectorAll('#ov-band .ov-lc')]
    expect(cells.map((c) => text(c.querySelector('.ov-lc-h span')))).toEqual(['Waiting', 'Holding capacity', 'Finished today'])
    const parts = (c: Element) =>
      [...c.querySelectorAll('.ov-lc-p')].map((p) => [p.querySelector('.sk-st')?.getAttribute('data-mark'), text(p.querySelector('.sk-st-w')), text(p.querySelector('b'))])
    expect(text(cells[0]!.querySelector('.ov-lc-n'))).toBe('37')
    expect(parts(cells[0]!)).toEqual([
      ['queued', 'queued', '22'],
      ['ready', 'ready', '9'],
      ['parked', 'parked', '6'],
    ])
    expect(text(cells[1]!.querySelector('.ov-lc-n'))).toBe('12')
    expect(parts(cells[1]!).map((p) => p[1])).toEqual(['leased', 'dispatched', 'starting', 'running'])
    expect(parts(cells[1]!).find((p) => p[1] === 'running')).toEqual(['running', 'running', '9'])
    expect(parts(cells[2]!).map((p) => p[1])).toEqual(['succeeded', 'failed', 'dead-lettered', 'cancelled'])
  })

  it('counts finished today off the page it read, and says so', async () => {
    const el = await mount()
    const done = [...el.querySelectorAll('#ov-band .ov-lc')][2]!
    // The failed and dead-lettered rows completed 25 and 58 minutes ago; today
    // is only certain for both when it is past 01:00 UTC.
    if (new Date().getUTCHours() >= 1) expect(text(done.querySelector('.ov-lc-n'))).toBe('2')
    expect(text(done.querySelector('.ov-lc-h small'))).toMatch(/since 00:00 UTC/)
    expect(text(done)).toMatch(/of the 9 newest read/)
  })

  it('draws a dash with its reason, never 0, when the counts are unread', async () => {
    const el = await mount({ loadStats: { status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' } } as Result<unknown> })
    const waiting = el.querySelector('#ov-band .ov-lc')!
    expect(text(waiting.querySelector('.ov-lc-n'))).toBe('—')
    expect(waiting.querySelector('.ov-lc-n')?.getAttribute('title') ?? '').toMatch(/could not be read/)
    expect(waiting.querySelectorAll('.ov-lc-p b').length).toBe(0)
  })

  it('keeps the three figures three across at 390', async () => {
    const el = await mount()
    const band = el.querySelector('#ov-band')!
    expect(cascade(SHEET, band, 'grid-template-columns', { width: 390 }).winner?.value).toBe('repeat(3, minmax(0, 1fr))')
  })
})

describe('O1: no figure row between the band and the cards', () => {
  /** MUTATION: render `MetricStrip` again. */
  it('draws no Running / Waiting / Units held tile', async () => {
    const el = await mount()
    expect(el.querySelector('.ctl-metrics')).toBeNull()
    expect(text(el)).not.toMatch(/Units held/)
  })
})

describe('O1: Running now', () => {
  /** MUTATION: put Agent first, or the old `ctl-chip is-live` dot back. */
  it('heads State, Agent · profile, Runtime, Cost so far, in that order', async () => {
    const el = await mount()
    const card = el.querySelector('#ov-running')!
    expect(text(card.querySelector('.ctl-card-title'))).toBe('Running now')
    expect([...card.querySelectorAll('thead th')].map(text)).toEqual(['State', 'Agent · profile', 'Runtime', 'Cost so far'])
  })

  it('draws RUNNING as the teal haloed disc, not the accent dot', async () => {
    const el = await mount()
    const row = [...el.querySelectorAll('#ov-running tbody tr')].find((r) => text(r).includes('fix-heartbeat'))!
    const mark = row.querySelector('td:first-child .sk-st')!
    expect(mark.getAttribute('data-mark')).toBe('running')
    expect(mark.getAttribute('data-hue')).toBe('live')
    expect(el.querySelector('#ov-running .ctl-chip'), 'the old chip dot is still drawn').toBeNull()
    expect(text(row)).toContain('claude-code')
  })

  it('draws Cost so far as a dash with its reason, because no task route serves it', async () => {
    const el = await mount()
    const cell = el.querySelector('#ov-running tbody tr td:last-child')!
    expect(text(cell)).toBe('—')
    expect(cell.querySelector('[title]')?.getAttribute('title') ?? '').toMatch(/not served/)
  })

  it('counts the rows that hold capacity in the head', async () => {
    const el = await mount()
    expect(text(el.querySelector('#ov-running .ctl-card-note'))).toBe('2 hold capacity')
  })
})

describe('O1: Waiting, and why groups by reason', () => {
  /** MUTATION: list one row per task again. */
  it('draws one row per reason with its count, and no task ids', async () => {
    const el = await mount()
    const rows = [...el.querySelectorAll('#ov-waiting .ov-wr')]
    expect(rows.length).toBe(2)
    const byCount = rows.map((r) => [r.querySelector('.sk-st')?.getAttribute('data-mark'), text(r.querySelector('.ov-wn'))])
    expect(byCount).toEqual([
      ['queued', '3'],
      ['parked', '2'],
    ])
    expect(text(rows[0]!.querySelector('.ov-wt small'))).toMatch(/wf_refactor 2 · wf_docs 1/)
    expect(text(el.querySelector('#ov-waiting'))).not.toMatch(/tsk_/)
  })
})

describe('O1: Headroom keeps its rows inside the card', () => {
  /** MUTATION: put the two side-by-side `.ov-groups` back. */
  it('stacks profile tiles, pool rows and the account line in one column', async () => {
    const el = await mount()
    const card = el.querySelector('#ov-headroom')!
    expect(card.querySelector('.ov-groups')).toBeNull()
    const tiles = [...card.querySelectorAll('.ov-hp')]
    expect(tiles.map((t) => text(t.querySelector('.ov-idc')))).toEqual(['browser', 'claude-code'])
    expect(text(tiles[0]!.querySelector('b'))).toBe('0')
    expect(tiles[0]!.querySelector('b')?.classList.contains('is-zero')).toBe(true)
    expect(text(tiles[1]!.querySelector('b'))).toBe('+6')
    expect(text(tiles[1]!.querySelector('small'))).toBe('binds your tenant')
    expect([...card.querySelectorAll('.ov-pl')].map((p) => text(p.querySelector('b')))).toEqual(['31/40', '4/4', '14/20'])
    const acc = text(card.querySelector('.ov-acc'))
    expect(acc).toMatch(/2 of 3 usable/)
    expect(acc).toMatch(/1 needs sign-in/)
  })

  it('wraps the tiles to the card rather than overflowing it', async () => {
    const el = await mount()
    const hps = el.querySelector('#ov-headroom .ov-hps')!
    expect(cascade(SHEET, hps, 'grid-template-columns', { width: 1440 }).winner?.value).toBe('repeat(auto-fill, minmax(92px, 1fr))')
    const pl = el.querySelector('#ov-headroom .ov-pl')!
    expect(cascade(SHEET, pl, 'grid-template-columns', { width: 1440 }).winner?.value).toBe('minmax(0, 118px) minmax(0, 1fr) auto')
  })
})

describe('O1: Recent failures', () => {
  /** MUTATION: drop the age cell or the Open link. */
  it('ends every row in an age and an Open link to the agent', async () => {
    const el = await mount()
    const rows = [...el.querySelectorAll('#ov-failures tbody tr')]
    expect(rows.length).toBe(2)
    expect(text(rows[0]!.querySelector('td.is-num'))).toBe('25m ago')
    const open = rows[0]!.querySelector<HTMLAnchorElement>('a.ov-open')!
    expect(text(open)).toBe('Open')
    expect(open.getAttribute('href')).toBe('#work/task/tsk_fail_1')
    expect(rows[0]!.querySelector('.sk-st')?.getAttribute('data-mark')).toBe('failed')
    expect(rows[1]!.querySelector('.sk-st')?.getAttribute('data-mark')).toBe('dead')
    expect(text(el.querySelector('#ov-failures .ctl-card-note'))).toMatch(/last 24h · 1 failed · 1 dead-lettered/)
  })
})

describe('O1 at 390', () => {
  /** MUTATION: let the h1 show at phone width, or drop the compact list. */
  it('says Overview once: the h1 steps aside for the phone header', async () => {
    const el = await mount()
    const h1 = el.querySelector('.ov-head h1')!
    expect(cascade(SHEET, h1, 'position', { width: 390 }).winner?.value).toBe('absolute')
    expect(cascade(SHEET, h1, 'position', { width: 1440 }).winner?.value ?? 'static').not.toBe('absolute')
  })

  it('draws Running as a compact list of mark, name and elapsed, and hides the table', async () => {
    const el = await mount()
    const list = el.querySelector('#ov-running .ov-prun')!
    expect(list).not.toBeNull()
    const first = list.querySelector('li')!
    expect(first.querySelector('.sk-st')?.getAttribute('data-mark')).toBe('running')
    expect(text(first.querySelector('.ov-prun-n'))).toBe('fix-heartbeat')
    expect(cascade(SHEET, list, 'display', { width: 1440 }).winner?.value).toBe('none')
    expect(cascade(SHEET, list, 'display', { width: 390 }).winner?.value ?? 'block').not.toBe('none')
    const table = el.querySelector('#ov-running .ov-rows')!
    expect(cascade(SHEET, table, 'display', { width: 390 }).winner?.value).toBe('none')
  })
})
