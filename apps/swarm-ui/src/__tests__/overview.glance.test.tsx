// OVERVIEW, READ AT A GLANCE: #95, #97 AND #168.
//
// Three issues against the landing screen, each asking for the same kind of
// thing: that a figure says what it counts without a help card, and that the
// screen costs a phone what docs/web-ui §2.5 allows and nothing more.
//
//   #95  Headroom. "5 can start" beside "0 / 10" read as a contradiction,
//        because the 5 counts agents and the 10 counts units on the binding
//        pool, where a browser agent weighs 2. Every figure now names its
//        unit, and the binding column says it is the binding pool.
//   #97  Spend. `c-rd` and `c-wr` were never explained; the headline switched
//        from four decimals to two at $10; the token-mix bar was 90-95% cache
//        read; `no re-poll` was jargon; and the Headroom feet were captions
//        while the Running and Spend feet were --surface-2 bars.
//   #168 The task read. 200 full task documents every 20 s at every width,
//        and on in a background tab. A phone reads 50 (§2.5, already shipped
//        as OV-10); the wide page is named rather than defaulted; and the
//        timers stop while `document.hidden`.
//
// Each test names the mutation that turns it red.

import STYLES from '../styles.css?raw'
import OVERVIEW_CSS from '../styles/overview.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'

import { cascade } from './cssgate'
import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import type { Account, AccountsPage, Capacity, Stats, TaskPage } from '../types'

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
// `TASK_PAGE_LIMIT` beside the reads: a factory mock throws on any export it
// does not declare, and Overview names the full page it asks for.
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

const { OverviewScreen } = await import('../Overview')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-23T10:00:00Z' }
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function account(over: Partial<Account>): Account {
  return {
    account_id: 'eng:laptop',
    owner_tenant: 'eng',
    label: 'laptop',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: null,
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

const EMPTY_TASKS: TaskPage = { tasks: [], next_page_token: null }
const EMPTY_STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: {},
  dispatch_paused: false,
  limits: {},
  generated_at: '2026-09-23T10:00:00Z',
}

/**
 * The issue's own picture: a browser agent weighs 2 units, the browser pool
 * holds 10 and none are in use, so 5 browser agents can start. A second
 * profile is bound by the caller's own tenant pool, whose column reads `your
 * tenant`.
 */
const CAPACITY: Capacity = {
  pools: [
    {
      name: 'resource:browser',
      hard_limit: 10,
      adaptive_target: null,
      quota_derived_limit: null,
      effective_limit: 10,
      active: 0,
      available: 10,
      enabled: true,
      updated_at: '2026-09-23T10:00:00Z',
    },
    {
      name: 'tenant:eng',
      hard_limit: 1,
      adaptive_target: null,
      quota_derived_limit: null,
      effective_limit: 1,
      active: 0,
      available: 1,
      enabled: true,
      updated_at: '2026-09-23T10:00:00Z',
    },
  ],
  runner_profiles: {
    browser: {
      resource_class: 'browser',
      backend: 'gke',
      provider: 'anthropic',
      units: 2,
      pools: ['resource:browser'],
      admission: {
        units: 2,
        headroom: 5,
        basis: 'measured',
        blockers: [],
        binding: ['resource:browser'],
        counterfactual: [],
        complete: true,
        unread: [],
        uncapped: [],
      },
    },
    'claude-code': {
      resource_class: 'standard',
      backend: 'cloudrun',
      provider: 'anthropic',
      units: 1,
      pools: ['tenant:eng'],
      admission: {
        units: 1,
        headroom: 1,
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
  generated_at: '2026-09-23T10:00:00Z',
}

function spend(over: Partial<SpendRollup> = {}): SpendRollup {
  return {
    tenantId: 'eng',
    tasksOnPage: 3,
    tasksWithAttempts: 3,
    tasksSampled: 3,
    failedReads: 0,
    failedDetail: null,
    attempts: 3,
    attemptsWithCost: 3,
    attemptsWithTokens: 3,
    costUsd: 1.25,
    inputTokens: 900,
    outputTokens: 400,
    cacheReadTokens: 120_000,
    cacheCreationTokens: 8_000,
    from: '2026-09-23T09:00:00Z',
    to: '2026-09-23T10:00:00Z',
    ...over,
  }
}

type Reads = Partial<Record<keyof typeof api, Result<unknown>>>

function arrange(reads: Reads = {}): void {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadTasks.mockResolvedValue(ok(EMPTY_TASKS))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue(ok(EMPTY_STATS))
  api.loadAccountPool.mockResolvedValue(ok(accountsPage([account({})])))
  api.loadSpend.mockResolvedValue(ok(spend()))
  for (const [name, result] of Object.entries(reads)) {
    api[name as keyof typeof api].mockResolvedValue(result)
  }
}

async function settle(): Promise<void> {
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
}

async function mount(reads: Reads = {}): Promise<HTMLElement> {
  arrange(reads)
  const { container } = render(<OverviewScreen />)
  await settle()
  return container
}

/** Headroom's tile for runner profile `name`, found by its name. */
function profileTile(el: HTMLElement, name: string): Element {
  const tile = [...el.querySelectorAll('.ov-headroom .ov-hp')].find((r) => text(r.querySelector('.ov-idc')) === name)
  expect(tile, `Headroom drew no tile for ${name}`).toBeDefined()
  return tile!
}

// ---------------------------------------------------------------------------
// #95 -- the headroom figures carry their units
// ---------------------------------------------------------------------------

describe('#95: every figure in the Headroom card names what it counts', () => {
  /**
   * "5 can start" and "0 / 10" side by side, the first in agents and the
   * second in units on the binding pool, where a browser agent weighs 2. O1
   * splits them: a profile tile counts AGENTS that can start (and its long
   * form says the weight), a pool row counts UNITS in use, and each says so.
   *
   * MUTATION: drop "agents" or the weight from the tile's long form, or
   * "units" from the pool row's.
   */
  it('counts agents on a profile tile and units on a pool row', async () => {
    const el = await mount()
    const browser = profileTile(el, 'browser')
    expect(text(browser.querySelector('b'))).toBe('+5')
    expect(browser.getAttribute('title') ?? '').toMatch(/5 more agents can start; 2 unit\(s\) per agent/)
    expect(profileTile(el, 'claude-code').getAttribute('title') ?? '').toMatch(/1 more agent can start;/)
    const pool = [...el.querySelectorAll('.ov-pools .ov-pl')].find((r) => text(r.querySelector('.ov-idc')) === 'resource:browser')!
    expect(text(pool.querySelector('b'))).toBe('0/10')
    expect(pool.getAttribute('title') ?? '').toMatch(/0 of 10 units in use/)
  })

  /**
   * The binding column named a pool -- `browser`, `your tenant` -- with no
   * verb, so it read as another figure. The tile's line says it binds.
   *
   * MUTATION: print the bare pool label again.
   */
  it('labels the binding pool as the one that binds', async () => {
    const el = await mount()
    expect(text(profileTile(el, 'browser').querySelector('small'))).toBe('binds browser')
    expect(text(profileTile(el, 'claude-code').querySelector('small'))).toBe('binds your tenant')
  })
})

// ---------------------------------------------------------------------------
// #97 -- the Spend card reads at a glance
// ---------------------------------------------------------------------------

describe('#97: the Spend card reads without a help card', () => {
  /**
   * Money switched from four decimals to two once it passed $10, so the
   * headline changed shape as it grew. It keeps two decimals at every size,
   * and the precise figure is the headline's accessible name.
   *
   * A cost under half a cent is `<$0.01`, never `$0.00`: rounded to two
   * places it would claim the sample was free, which it was not.
   *
   * MUTATION: restore `toFixed(4)` under $10, or drop the aria-label.
   */
  it('prints the headline with two decimals and publishes the precise figure', async () => {
    const cases: [number, string, string][] = [
      [0.0312, '$0.03', '$0.0312'],
      [1.25, '$1.25', '$1.25'],
      [12.345678, '$12.35', '$12.345678'],
      [0.004, '<$0.01', '$0.004'],
    ]
    for (const [cost, shown, precise] of cases) {
      const el = await mount({ loadSpend: ok(spend({ costUsd: cost })) })
      const figure = el.querySelector('.ov-spend .ov-figure')!
      expect(text(figure), `headline for ${cost}`).toBe(shown)
      expect(figure.getAttribute('aria-label') ?? '', `precise figure for ${cost}`).toContain(precise)
      el.remove()
    }
  })

  /**
   * `c-rd` and `c-wr`, explained nowhere. Written out.
   *
   * MUTATION: put the abbreviations back.
   */
  it('spells out cache read and cache write', async () => {
    const el = await mount()
    const keys = [...el.querySelectorAll('.ov-spend .ov-kv dt')].map(text)
    expect(keys).toContain('cache read tokens')
    expect(keys).toContain('cache write tokens')
    expect(text(el.querySelector('.ov-spend'))).not.toMatch(/\bc-(rd|wr)\b/)
  })

  /**
   * The mix bar was 90-95% cache read, with `in` under a pixel wide. The
   * issue allows replacing it with spend by runner profile, but the rollup
   * carries no per-profile split -- only totals -- so it is dropped.
   *
   * MUTATION: render `.ov-mix` again.
   */
  it('draws no token-mix bar', async () => {
    const el = await mount()
    expect(el.querySelector('.ov-spend .ov-mix')).toBeNull()
    expect(el.querySelector('.ov-spend .ov-mix-seg')).toBeNull()
    // The partial mark is the only picture the card draws: a Mark is
    // `role="img"` too, so the bar is looked for by its label, not its role.
    const pictures = [...el.querySelectorAll('.ov-spend [role="img"]')].map((n) => n.getAttribute('aria-label') ?? '')
    expect(pictures.filter((l) => /\bnot measured\b|proportion|\bin \d/.test(l))).toEqual([])
  })

  /**
   * `no re-poll` said, in the poll's own jargon, that the sum is on no timer.
   *
   * MUTATION: type `no re-poll` back.
   */
  it('says in plain words that only refresh re-sums', async () => {
    const el = await mount()
    const foot = text(el.querySelector('.ov-spend .ov-foot'))
    expect(foot).not.toMatch(/re-poll/)
    expect(foot).toContain('updates only on refresh')
  })
})

describe('#97: the Overview has one card-foot shape', () => {
  /** The value the cascade chooses, failing by name on a selector it could not read. */
  function won(el: Element, prop: string | readonly string[]): string | null {
    const r = cascade(`${STYLES}\n${OVERVIEW_CSS}`, el, prop, { width: 1440 })
    expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
    return r.winner?.value ?? null
  }

  /**
   * The feet are one family -- a caption under a hairline -- in every card
   * that has one: Running now, Cost so far and Headroom's pool list. Running
   * and Spend once drew full-bleed bars while Headroom drew captions.
   *
   * MUTATION: give one card's foot a fill or a corner of its own.
   */
  it('draws the Running, Cost and Headroom feet with one fill and one corner', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="ov-g21">' +
      '<section class="ctl-card ov-card ov-running"><p class="ov-foot">r</p></section>' +
      '<section class="ctl-card ov-card ov-spend"><p class="ov-foot">s</p></section>' +
      '</div><div class="ov-g21">' +
      '<section class="ctl-card ov-card ov-headroom"><p class="ov-foot">p</p></section>' +
      '</div>'
    const feet = [...host.querySelectorAll('.ov-foot')]
    expect(feet.length).toBe(3)
    const BG = ['background', 'background-color'] as const
    const shapes = feet.map((f) => `${won(f, BG)} | ${won(f, 'border-radius')}`)
    expect(new Set(shapes).size, `the feet draw ${shapes.length} shapes: ${shapes.join(' ; ')}`).toBe(1)
    expect(won(feet[0]!, BG), 'the feet are bars, not captions').toBeNull()
  })
})

// ---------------------------------------------------------------------------
// #168 -- the task read costs a phone what §2.5 allows
// ---------------------------------------------------------------------------

describe('#168: the Overview polls only while it can be seen', () => {
  function hide(hidden: boolean): void {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => (hidden ? 'hidden' : 'visible'),
    })
    document.dispatchEvent(new Event('visibilitychange'))
  }

  afterEach(() => {
    // Back to jsdom's own answer, so no later test in this file runs hidden.
    delete (document as { hidden?: boolean }).hidden
    delete (document as { visibilityState?: string }).visibilityState
    vi.useRealTimers()
  })

  async function advance(ms: number): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms)
    })
  }

  /**
   * A background tab kept re-reading 200 task documents every 20 s, and the
   * stats counts every 60 s, for as long as it stayed open. §2.5: "stop
   * entirely when `document.hidden`", and read at once on the way back.
   *
   * MUTATION: drop the visibility check, or the read on return.
   */
  it('stops the poll while the tab is hidden and reads at once when it comes back', async () => {
    vi.useFakeTimers()
    arrange()
    api.loadTasks.mockClear()
    api.loadStats.mockClear()
    render(<OverviewScreen />)
    await advance(100)
    const first = api.loadTasks.mock.calls.length
    expect(first, 'the Overview made no task read on mount').toBeGreaterThan(0)

    // The control: while visible, the twenty-second poll runs.
    await advance(20_000)
    const visible = api.loadTasks.mock.calls.length
    expect(visible, 'the poll never ran while visible').toBeGreaterThan(first)

    hide(true)
    const stats = api.loadStats.mock.calls.length
    await advance(5 * 60_000)
    expect(api.loadTasks.mock.calls.length, 'a hidden tab re-read the task page').toBe(visible)
    expect(api.loadStats.mock.calls.length, 'a hidden tab re-read the stats counts').toBe(stats)

    hide(false)
    await advance(10)
    expect(api.loadTasks.mock.calls.length, 'a tab coming back did not read at once').toBe(visible + 1)
    expect(api.loadStats.mock.calls.length, 'the overdue stats read did not run on return').toBe(stats + 1)

    // And the poll resumes on its own cadence.
    await advance(20_000)
    expect(api.loadTasks.mock.calls.length, 'the poll did not resume').toBe(visible + 2)
  })
})
