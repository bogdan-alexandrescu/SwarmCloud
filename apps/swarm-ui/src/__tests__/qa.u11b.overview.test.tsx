/**
 * BROWSER QA U11b (owner, 2026-10-04; live console at main 69416faf, 1440x900
 * light and dark and 390px): Overview.
 *
 *   N3   Recent failures: the 68px state badge overprinted the task name in a
 *        28px state cell. The cell draws the mark alone, its word in the title
 *        and the accessible name, and the column fits the mark.
 *   N4   Recent failures and Needs a look said "58m ago" for tasks that ended
 *        ~9h earlier: the age was `completed_at ?? updated_at`, and a later
 *        write bumps `updated_at`. The age is the END, and a task with no
 *        recorded end says so with a dash rather than borrowing a write time.
 *   N5   Headroom said "+39 can start now" for a profile Pools marks disabled.
 *        A disabled profile says disabled, with its reason, never headroom.
 *   N6   Running now: Runtime repeated "dispatched" (the State column's word)
 *        instead of a duration. A LEASED/DISPATCHED row times from its lease.
 *   N17  ~1100px of blank left column beside Headroom's long pool list: the
 *        pools are their own full-width card, laid out across it.
 *   D17  A profile name ("claude-code-review") broke at its hyphen: it is one
 *        line, never hyphenated, in a tile wide enough to hold it.
 *   D18  One Needs-a-look card took half the row with the other half empty:
 *        the cards fill the row (`auto-fit`, so an empty track collapses).
 *
 * MUTATIONS: give the failure mark its word back, fall back to `updated_at`,
 * draw `+N` for an unavailable profile, print `elapsed()`'s state word, put
 * the pools back in Headroom, drop the tile's nowrap or shrink its minimum,
 * or put `auto-fill` back on `.ov-atts` -- each turns a case red.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'
import { cellStyle, lengthPx, textPx } from './tablefit'

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
const { RecentFailures, failuresOf } = await import('../OverviewRegions')
const { newestFailureClause } = await import('../checks')

const WIDE: CascadeEnv = { width: 1440 }
const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}
function page(tasks: Task[]): Result<TaskPage> {
  return ok({ tasks, next_page_token: null, tenant_id: 'eng' } as unknown as TaskPage)
}

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('N3: the failure mark fits its column', () => {
  it('draws the mark alone, its word in the title, in a column that holds it', () => {
    const rows = [task({ id: 'tsk_dead', state: 'DEAD_LETTERED', completed_at: ago(30), updated_at: ago(30) })]
    const { container } = render(
      <div className="ov-page">
        <section className="ctl-card ov-card ov-failures">
          <RecentFailures tasks={page(rows)} />
        </section>
      </div>,
    )
    const cell = container.querySelector('tbody tr > td:first-child')!
    const mark = cell.querySelector<HTMLElement>('.sk-st')!
    expect(mark.querySelector('.sk-st-w'), 'the word is drawn in a 28px cell').toBeNull()
    expect(mark.getAttribute('title')).toBe('dead-lettered')
    expect(text(mark)).toBe('dead-lettered') // still read out, visually hidden
    const col = container.querySelector('col.ov-fc-mark')!
    const width = lengthPx(painted(col, 'width', WIDE), 0)!
    const st = cellStyle(cell, WIDE)
    expect(width - st.pl - st.pr, 'the 12px mark does not fit its column').toBeGreaterThanOrEqual(12)
  })
})

describe('N4: a failure\'s age is when it ended', () => {
  it('ages a failure from its end, not from a later write', () => {
    const rows = [task({ id: 'tsk_f', state: 'FAILED', created_at: ago(560), started_at: ago(550), completed_at: ago(540), updated_at: ago(58) })]
    const { container } = render(<RecentFailures tasks={page(rows)} />)
    expect(text(container.querySelector('tbody td.is-num'))).toBe('9h ago')
  })

  it('says a dash, with why, when no end was recorded, never the last write\'s age', () => {
    const rows = [task({ id: 'tsk_f', state: 'FAILED', created_at: ago(560), started_at: ago(550), completed_at: null, updated_at: ago(58) })]
    const { container } = render(<RecentFailures tasks={page(rows)} />)
    const age = container.querySelector('tbody td.is-num')!
    expect(text(age)).not.toMatch(/58m/)
    const dash = age.querySelector('.c-dash')!
    expect(dash, 'no dash').not.toBeNull()
    expect(dash.getAttribute('title')).toMatch(/no end time was recorded/i)
  })

  it('windows and orders on the END: a task that ended days ago is not "last 24h" because a later write bumped it', () => {
    const now = Date.now()
    const old = task({ id: 'tsk_old', state: 'FAILED', completed_at: ago(3 * 24 * 60), updated_at: ago(60) })
    const recent = task({ id: 'tsk_new', state: 'FAILED', completed_at: ago(120), updated_at: ago(120) })
    const earlier = task({ id: 'tsk_mid', state: 'DEAD_LETTERED', completed_at: ago(600), updated_at: ago(5) })
    const { rows, note } = failuresOf([old, earlier, recent], now)
    expect(rows.map((t) => t.id)).toEqual(['tsk_new', 'tsk_mid'])
    expect(note).toBe('last 24h · 1 failed · 1 dead-lettered')
  })

  it('lists a task with no recorded end after the dated rows and counts it apart', () => {
    const now = Date.now()
    const unended = task({ id: 'tsk_unended', state: 'FAILED', completed_at: null, updated_at: ago(1) })
    const dated = task({ id: 'tsk_dated', state: 'FAILED', completed_at: ago(300), updated_at: ago(300) })
    const { rows, note } = failuresOf([unended, dated], now)
    expect(rows.map((t) => t.id)).toEqual(['tsk_dated', 'tsk_unended'])
    expect(note).toBe('last 24h · 1 failed · 0 dead-lettered · 1 with no recorded end')
  })

  it('dates Needs a look\'s failed-task card from the newest END', () => {
    const later = task({ id: 'a', state: 'FAILED', completed_at: ago(540), updated_at: ago(58) })
    expect(newestFailureClause([later], Date.now())).toBe(' · newest 9h ago')
    const unended = task({ id: 'b', state: 'FAILED', completed_at: null, updated_at: ago(58) })
    expect(newestFailureClause([unended], Date.now())).toBe('')
  })
})

const ADMISSION = {
  units: 1,
  headroom: 39,
  basis: 'measured',
  blockers: [],
  binding: ['global'],
  counterfactual: [],
  complete: true,
  unread: [],
  uncapped: [],
}

describe('N5: a disabled profile shows no headroom', () => {
  it('says disabled and why, never "+39"', () => {
    const profile = {
      resource_class: 'standard',
      backend: 'cloud_run',
      provider: 'anthropic',
      units: 1,
      pools: ['global'],
      available: false,
      disabled_reason: 'the review runner is switched off for this tenant',
      admission: ADMISSION,
    } as unknown as Capacity['runner_profiles'][string]
    const { container } = render(<Overview.ProfileTile name="claude-code-review" profile={profile} byName={new Map()} tenant="eng" />)
    const tile = container.querySelector('.ov-hp')!
    expect(text(tile)).not.toMatch(/\+39/)
    expect(text(tile.querySelector('b'))).toBe('disabled')
    expect(text(tile.querySelector('small'))).toBe('the review runner is switched off for this tenant')
    expect(tile.getAttribute('title')).toMatch(/disabled/)
  })

  it('still draws headroom for an available profile', () => {
    const profile = { resource_class: 'standard', backend: 'cloud_run', provider: null, units: 1, pools: ['global'], admission: ADMISSION } as unknown as Capacity['runner_profiles'][string]
    const { container } = render(<Overview.ProfileTile name="claude-code" profile={profile} byName={new Map()} tenant="eng" />)
    expect(text(container.querySelector('.ov-hp b'))).toBe('+39')
  })
})

describe('N6: Running now times every row', () => {
  it('times a dispatched row from its lease, and leaves the state to the State column', () => {
    const t = task({ id: 'tsk_d', state: 'DISPATCHED', created_at: ago(12), started_at: null, updated_at: ago(1) })
    const { container } = render(
      <table>
        <tbody>
          <Overview.RunningRow task={t} leasedAt={ago(4)} />
        </tbody>
      </table>,
    )
    const runtime = container.querySelectorAll('tbody td.is-num')[0]!
    expect(text(runtime)).toMatch(/^4m \d+s$/)
    expect(text(runtime)).not.toMatch(/dispatched/i)
  })

  it('says a dash with why when no lease was read, never the state word', () => {
    const t = task({ id: 'tsk_d', state: 'LEASED', created_at: ago(12), started_at: null, updated_at: ago(1) })
    const { container } = render(
      <table>
        <tbody>
          <Overview.RunningRow task={t} />
        </tbody>
      </table>,
    )
    const runtime = container.querySelectorAll('tbody td.is-num')[0]!
    expect(text(runtime)).not.toMatch(/leased/i)
    expect(runtime.querySelector('.c-dash')?.getAttribute('title')).toMatch(/lease/)
  })

  it('times a running row from its start', () => {
    const t = task({ id: 'tsk_r', state: 'RUNNING', created_at: ago(30), started_at: ago(18), updated_at: ago(1) })
    const { container } = render(
      <table>
        <tbody>
          <Overview.RunningRow task={t} leasedAt={ago(20)} />
        </tbody>
      </table>,
    )
    expect(text(container.querySelectorAll('tbody td.is-num')[0]!)).toMatch(/^18m \d+s$/)
  })
})

const POOL = { hard_limit: 40, adaptive_target: null, quota_derived_limit: null, enabled: true, updated_at: ago(1) }
const CAPACITY = {
  pools: Array.from({ length: 14 }, (_, i) => ({ name: `provider:anthropic:pool-${i}`, ...POOL, effective_limit: 40, active: i, available: 40 - i })) as unknown as Pool[],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'cloud_run', provider: null, units: 1, pools: ['global'], admission: ADMISSION },
  },
  tenant_id: 'eng',
  generated_at: ago(1),
} as unknown as Capacity

async function mount(): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadTasks.mockResolvedValue(page([task({ id: 'tsk_r', state: 'RUNNING', started_at: ago(5) })]))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadAccountPool.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadSpend.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Overview.OverviewScreen />)
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

describe('N17: the pool list is its own full-width row', () => {
  it('draws the pools in a card of their own, across the page, not under Headroom', async () => {
    const el = await mount()
    const headroom = el.querySelector('#ov-headroom')!
    expect(headroom.querySelector('.ov-pl'), 'the pools still lengthen Headroom').toBeNull()
    const pools = el.querySelector<HTMLElement>('#ov-pools')!
    expect(pools, 'no Pools card').not.toBeNull()
    // Since U12 N17/D19 the cards are two column stacks in one grid; the
    // pools are that grid's full-width row under both, in neither column.
    expect(pools.closest('.ov-col'), 'the Pools card is inside a column').toBeNull()
    expect(painted(pools, 'grid-column', WIDE), 'the Pools card is inside a two-column row').toBe('1 / -1')
    expect(pools.querySelectorAll('.ov-pl').length).toBe(14)
    const grid = painted(pools.querySelector('.ov-pls')!, 'grid-template-columns', WIDE) ?? ''
    expect(grid, 'the pools are one column across 1056px').toMatch(/repeat\(auto-fill/)
  })
})

describe('D17: a profile name is one line, never hyphen-broken', () => {
  it('holds "claude-code-review" whole in the narrowest tile', () => {
    document.body.innerHTML =
      '<section class="ctl-card ov-card ov-headroom"><div class="ov-hps"><div class="ov-hp"><span class="ov-idc">claude-code-review</span><b>+6</b><small>binds global</small></div></div></section>'
    const name = document.querySelector('.ov-hp .ov-idc')!
    expect(painted(name, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(name, 'hyphens', WIDE) ?? 'manual').not.toBe('auto')
    const grid = painted(document.querySelector('.ov-hps')!, 'grid-template-columns', WIDE)!
    const min = Number(/minmax\((\d+)px/.exec(grid)?.[1])
    const tile = document.querySelector('.ov-hp')!
    const st = cellStyle(tile, WIDE)
    expect(min - st.pl - st.pr, grid).toBeGreaterThanOrEqual(textPx('claude-code-review', name, WIDE))
  })
})

describe('D18: the check cards fill their row', () => {
  it('collapses empty tracks, so one card is the row\'s width', () => {
    document.body.innerHTML = '<section class="ov-lead"><div class="ov-atts"></div></section>'
    const v = painted(document.querySelector('.ov-atts')!, 'grid-template-columns', WIDE)!
    expect(v).toMatch(/^repeat\(auto-fit,/)
  })
})
