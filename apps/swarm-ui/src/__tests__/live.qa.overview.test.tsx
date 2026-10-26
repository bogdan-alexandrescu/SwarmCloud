// LIVE QA, OVERVIEW (swarm.example.com/overview, 2026-10-11): THE S1 AND S2
// FINDINGS THAT CAME FROM THE PHONE'S 50-ROW WINDOW, AND THE DESKTOP'S
// "LAST 24H" OVER A READ THAT REACHED BACK FIVE HOURS.
//
// The live tenant at 400x800: the 50 newest-created tasks were 35 SUCCEEDED,
// 11 PARKED and 4 RUNNING. Eleven tasks had FAILED in the last 24h, created
// earlier, and a fifth was RUNNING, created earlier. The phone said
// `last 24h · 0 failed` under `real zero` (OV-1), dropped the failed-tasks
// check (OV-2) and listed four running agents under "Holding capacity 5"
// (OV-4). Each case below builds that tenant and asserts the property, not
// the copy; each names the mutation that turns it red.

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

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-11T10:00:00Z' }
}
const empty = { status: 'empty' as const, fetchedAt: Date.now() }

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
  for (const fn of Object.values(api)) fn.mockReset()
})

const CAPACITY: Capacity = { pools: [], runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-11T10:00:00Z' }

const SPEND = { tenantId: 'eng', tasksOnPage: 0, tasksWithAttempts: 0, tasksSampled: 0, failedReads: 0, failedDetail: null, attempts: 0, attemptsWithCost: 0, attemptsWithTokens: 0, costUsd: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0, from: null, to: null } as unknown as SpendRollup

/**
 * The live tenant, 212 tasks: 200 newest-created finished or running ones on
 * the phone's page, then -- created earlier -- one running agent and eleven
 * that failed within the last two hours. Newest-created first, as the API
 * serves it. (The live phone read 50 rows; since live QA CH-4 it reads the
 * same 200 as a wide screen, so the page here is 200 deep.)
 */
function tenant(): Task[] {
  const fresh = Array.from({ length: 196 }, (_, i) =>
    task({ id: `tsk_ok_${i}`, tenant_id: 'eng', state: 'SUCCEEDED', created_at: ago(10 + i / 5), updated_at: ago(5 + i / 5), started_at: ago(9 + i / 5), completed_at: ago(5 + i / 5), attempt_count: 1 }),
  )
  const live = Array.from({ length: 4 }, (_, i) =>
    task({ id: `tsk_run_${i}`, tenant_id: 'eng', state: 'RUNNING', created_at: ago(60 + i), updated_at: ago(1), started_at: ago(50 + i), attempt_count: 1 }),
  )
  const early = task({ id: 'tsk_run_early', tenant_id: 'eng', state: 'RUNNING', created_at: ago(200), updated_at: ago(1), started_at: ago(30), attempt_count: 1 })
  const failed = Array.from({ length: 11 }, (_, i) =>
    task({ id: `tsk_bad_${i}`, tenant_id: 'eng', state: 'FAILED', last_error: `error ${i}`, created_at: ago(300 + i), updated_at: ago(15 + i), started_at: ago(290 + i), completed_at: ago(15 + i), attempt_count: 1 }),
  )
  return [...fresh, ...live, early, ...failed]
}

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 5, SUCCEEDED: 196, FAILED: 11, DEAD_LETTERED: 0, CANCELLED: 0 },
  dispatch_paused: false,
  limits: {},
  generated_at: '2026-10-11T10:00:00Z',
}

/**
 * Serve the tenant the way the API does: `loadTasks(limit)` the newest-created
 * `limit` with a next page when more exist, `loadTasksInState` every task in
 * one state (or `stateRead` instead).
 */
function serve(all: Task[], stateRead?: (state: TaskState) => Result<TaskPage>): void {
  api.loadTasks.mockImplementation(async (limit: number) =>
    ok({ tasks: all.slice(0, limit), next_page_token: all.length > limit ? 'next' : null, tenant_id: 'eng' } satisfies TaskPage),
  )
  api.loadTasksInState.mockImplementation(async (state: TaskState) => {
    if (stateRead) return stateRead(state)
    const rows = all.filter((t) => t.state === state)
    return rows.length === 0 ? empty : ok({ tasks: rows, next_page_token: null, tenant_id: 'eng' } satisfies TaskPage)
  })
}

async function mount(): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadLeases.mockResolvedValue(empty)
  api.loadProviders.mockResolvedValue(empty)
  api.loadWorkflows.mockResolvedValue(empty)
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(empty)
  api.loadSpend.mockResolvedValue(ok(SPEND))
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'gone' } })
  const { container } = render(<OverviewScreen />)
  for (let i = 0; i < 60; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

const text = (el: Element | null): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('OV-1/OV-2: a phone counts the failures its 200 newest do not hold', () => {
  /**
   * MUTATION: drop the state reads from `loadListPage`, or count Recent
   * failures off `page.tasks` alone.
   */
  it('shows 11 failed and no real-zero badge on a phone page with none of them on it', async () => {
    media(true)
    serve(tenant())
    const el = await mount()
    // The control: the phone read the 200-row page, and none of the 11 is on it.
    expect(api.loadTasks.mock.calls.every((c) => c[0] === 200)).toBe(true)
    const card = el.querySelector('#ov-failures')!
    expect(text(card)).toMatch(/last 24h · 11 failed/)
    expect(card.querySelector('.ctl-mark.is-zero')).toBeNull()
    // All eleven listed: six rows and "5 more".
    expect(card.querySelectorAll('.ov-name').length).toBe(11)
  })

  /** MUTATION: count the failures check over the page alone. */
  it('keeps the failed-tasks check in Needs a look', async () => {
    media(true)
    serve(tenant())
    const el = await mount()
    expect(text(el.querySelector('#ov-needs'))).toMatch(/11 failed agents in the last 24 hours/)
  })

  /**
   * The read of the failed tasks did not land: the page alone answers, and
   * what it answers is "partial", never a measured zero.
   *
   * MUTATION: draw `NoneAmong` (the real-zero mark) whenever the list is empty.
   */
  it('says partial, not real zero, when the failed read did not land', async () => {
    media(true)
    serve(tenant(), () => ({ status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' } }) as Result<TaskPage>)
    const el = await mount()
    const card = el.querySelector('#ov-failures')!
    expect(card.querySelector('.ctl-mark.is-zero')).toBeNull()
    expect(text(card)).toMatch(/≥0 failed/)
    expect(text(card)).toMatch(/partial/)
    // And the check is not "clear": the state counts say 11 FAILED.
    expect(text(el.querySelector('#ov-needs'))).not.toMatch(/none of the 200 most recently created (tasks|agents) is FAILED/)
  })
})

describe('OV-4: Running now lists every agent holding capacity', () => {
  /** MUTATION: draw Running now from `page.tasks` alone. */
  it('lists the fifth running agent, created before the 200 newest', async () => {
    media(true)
    serve(tenant())
    const el = await mount()
    const names = [...el.querySelectorAll('#ov-running .ov-prun li a.ov-prun-n')].map((a) => a.getAttribute('title') ?? '')
    expect(names.length).toBe(5)
    expect(names.some((n) => n.includes('tsk_run_early'))).toBe(true)
    // No "stats say 5" caveat: the list is the census.
    expect(text(el.querySelector('#ov-running'))).not.toMatch(/stats say/)
  })
})

describe('OV-7: "last 24h" says partial when the read did not cover it', () => {
  /**
   * The desktop: the FAILED read itself has more behind it, so the figure is
   * a lower bound and the note says from when the read reaches.
   *
   * MUTATION: drop the `≥` / "partial" from `failuresOf`'s note.
   */
  it('marks the count as a lower bound with the window it covers', async () => {
    media(false)
    const all = tenant()
    // 250 more successes, so the desktop's 200-row page has a next page too.
    const more = Array.from({ length: 250 }, (_, i) =>
      task({ id: `tsk_more_${i}`, tenant_id: 'eng', state: 'SUCCEEDED', created_at: ago(2 + i / 1000), updated_at: ago(1), completed_at: ago(1), attempt_count: 1 }),
    )
    serve([...more, ...all], (state) => {
      const rows = all.filter((t) => t.state === state)
      // The FAILED read is cut: 6 of the 11, and a next page.
      if (state === 'FAILED') return ok({ tasks: rows.slice(0, 6), next_page_token: 'next', tenant_id: 'eng' })
      return rows.length === 0 ? empty : ok({ tasks: rows, next_page_token: null, tenant_id: 'eng' })
    })
    const el = await mount()
    const note = text(el.querySelector('#ov-failures .ov-card-note, #ov-failures header, #ov-failures'))
    expect(note).toMatch(/last 24h · ≥6 failed/)
    expect(note).toMatch(/partial: agents created since \d\d:\d\d UTC read/)
    expect(text(el.querySelector('#ov-needs'))).toMatch(/≥6 failed agents in the last 24 hours/)
  })

  /** The control: a read that covers every failed task claims the window, with no mark. */
  it('claims last 24h exactly when every failed task was read', async () => {
    media(false)
    serve(tenant())
    const el = await mount()
    const card = text(el.querySelector('#ov-failures'))
    expect(card).toMatch(/last 24h · 11 failed/)
    expect(card).not.toMatch(/partial/)
  })
})

describe('OV-6: Finished today says how far back the phone read reaches', () => {
  /** MUTATION: drop the `created since` clause (`readReach`, VQ-005) from the band's page foot. */
  it('names the instant the page reaches back to', async () => {
    media(true)
    // One tenant, served and counted: `ago()` reads the clock, so a second
    // `tenant()` built at the assertion is minutes later on a slow runner,
    // and just after midnight UTC one more row has ended "today" (#2375's
    // run at 00:39 read 145 on screen against 146 recounted).
    const rows = tenant()
    const day = new Date().toISOString().slice(0, 10)
    serve(rows)
    const el = await mount()
    const done = el.querySelectorAll('#ov-band .ov-lc')[2]!
    expect(text(done.querySelector('.ov-lc-foot.is-page'))).toMatch(/^of the 200 newest read · partial: agents created since \d\d:\d\d UTC read$/)
    // The eleven failures are counted from their own read, not dropped: the
    // phone's page holds 196 finished rows, and with the failed read 207 --
    // whichever of them ended today in UTC, so a run just after midnight
    // counts what the screen counts.
    const today = rows.filter((t) => (t.state === 'SUCCEEDED' || t.state === 'FAILED') && (t.completed_at ?? '').startsWith(day)).length
    expect(text(done.querySelector('.ov-lc-n'))).toMatch(new RegExp(`^≥${today}\\b`))
  })
})

describe('the S3 items the Overview fixes in its own code', () => {
  /**
   * OV-10: a reason counting 7 named three workflows of 2 and dropped the
   * seventh task. MUTATION: drop the `+N more` clause in `waitGroups`.
   */
  it('OV-10: Waiting, and why accounts for every task it counts', async () => {
    const { waitGroups } = await import('../OverviewRegions')
    const parked = (id: string, wf: string | null) =>
      task({ id, tenant_id: 'eng', state: 'PARKED', park_reason: 'CHECKS_PENDING', workflow_id: wf, created_at: ago(5), updated_at: ago(1) })
    const rows = [
      parked('a1', 'wf_a'), parked('a2', 'wf_a'),
      parked('b1', 'wf_b'), parked('b2', 'wf_b'),
      parked('c1', 'wf_c'), parked('c2', 'wf_c'),
      parked('d1', 'wf_d'),
    ]
    const [g] = waitGroups(rows)
    expect(g!.n).toBe(7)
    expect(g!.detail).toMatch(/\+1 more/)
  })

  /**
   * OV-12: two binding pools, and `resource:browser` beside `runner:browser`.
   * MUTATION: print `h.binding` (the first) alone.
   */
  it('OV-12: a Headroom tile names every binding pool, each told apart', async () => {
    const { ProfileTile } = await import('../Overview')
    const profile = {
      resource_class: 'browser', backend: 'cloud_run', provider: null, units: 1, pools: ['resource:browser', 'runner:browser'], available: true,
      admission: { units: 1, headroom: 10, basis: 'measured', blockers: [], binding: ['resource:browser', 'runner:browser'], counterfactual: [], complete: true, unread: [], uncapped: [] },
    } as unknown as Capacity['runner_profiles'][string]
    const { container } = render(<ProfileTile name="browser" profile={profile} byName={new Map()} tenant="eng" />)
    expect(text(container.querySelector('small'))).toBe('binds browser · resource + browser · runner')
  })

  /** OV-15: the busy pools first. MUTATION: draw the pools in read order. */
  it('OV-15: Pools are drawn busiest first', async () => {
    const { busiestFirst } = await import('../Overview')
    const pool = (name: string, active: number, limit: number | null) => ({ name, active, effective_limit: limit, hard_limit: limit }) as unknown as Capacity['pools'][number]
    const order = busiestFirst([pool('a', 0, 40), pool('b', 5, 40), pool('c', 1, null), pool('d', 4, 4)]).map((p) => p.name)
    expect(order).toEqual(['d', 'b', 'a', 'c'])
  })
})
