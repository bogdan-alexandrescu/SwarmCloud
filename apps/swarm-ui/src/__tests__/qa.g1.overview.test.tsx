/**
 * QA PASS G1 ON SWARM.SAGA.XYZ (2026-10-07): the Overview, at 1512 and 390.
 *
 *   G1-02  "Finished today" read 62 on a desktop and 41 on a phone at the same
 *          moment, under the same label: both were counted off the task page
 *          each width loaded, and the phone hid the foot that said so. A count
 *          off a page with more behind it is now a lower bound (`≥N`, the kit's
 *          partial mark), and the foot naming the page stays at 390.
 *   G1-03  "Cost so far" summed the 12 newest attempts, so the headline went
 *          down over time. The card is "Recent token cost", noted "last 12
 *          attempts".
 *   G1-04  Needs a look said a FAILED task "may retry" beside an error that
 *          said it failed without a retry. FAILED is terminal; attempts left
 *          do not make it retry.
 *   G1-07  On a phone Recent failures came after 29 pool rows, and the sticky
 *          `↑ Top` pill sat over the last rows' figures.
 *   G1-09  Running now and Recent failures named a row by its step only:
 *          "implement / review / fix". The row leads with the task's title.
 *          Its link did not look like one.
 *   G1-10  The failures card's "All recent →" opened only the failed filter,
 *          dropping the cancelled and dead-lettered rows the card listed.
 *
 * Each `it` names the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { deriveChecks, type CheckInputs } from '../checks'
import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import type { Stats, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
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
  loadTask: vi.fn(),
}))
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

const { OverviewScreen } = await import('../Overview')
const { LifecycleBand } = await import('../OverviewRegions')

const WIDE: CascadeEnv = { width: 1512 }
const TABLET: CascadeEnv = { width: 820 }
const PHONE: CascadeEnv = { width: 390 }
const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}
const empty = { status: 'empty', fetchedAt: Date.now() } as const

function taskPage(tasks: Task[], more: boolean): Result<TaskPage> {
  return ok({ tasks, next_page_token: more ? 'next' : null, tenant_id: 'eng' } as TaskPage)
}

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 0, SUCCEEDED: 40, FAILED: 3, DEAD_LETTERED: 0, CANCELLED: 6 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date().toISOString(),
}

/** Three tasks finished minutes ago -- inside today unless the run straddles 00:00 UTC. */
const FINISHED: Task[] = [
  task({ id: 'tsk_done_1', state: 'SUCCEEDED', completed_at: ago(2) }),
  task({ id: 'tsk_done_2', state: 'FAILED', completed_at: ago(3) }),
  task({ id: 'tsk_done_3', state: 'CANCELLED', completed_at: ago(4) }),
]
const todayHasRoom = new Date().getUTCHours() * 60 + new Date().getUTCMinutes() > 5

afterEach(cleanup)

describe('G1-02: Finished today says when it is only the page it read', () => {
  // MUTATION: draw the page's count as an exact figure whatever
  // `next_page_token` says, or drop the partial mark.
  it('draws a page with more behind it as a lower bound, with the partial mark', () => {
    const { container } = render(<LifecycleBand stats={ok(STATS)} tasks={taskPage(FINISHED, true)} />)
    const done = container.querySelectorAll('#ov-band .ov-lc')[2]!
    const n = done.querySelector('.ov-lc-n')!
    if (todayHasRoom) expect(text(n)).toMatch(/^≥3\b/)
    expect(n.querySelector('.ctl-mark.is-partial'), 'a lower bound carries the partial mark').not.toBeNull()
    expect(n.getAttribute('title') ?? '').toMatch(/^At least/)
  })

  // MUTATION: mark every page count partial, even one that read every task.
  it('draws an exact figure when the page held every task', () => {
    const { container } = render(<LifecycleBand stats={ok(STATS)} tasks={taskPage(FINISHED, false)} />)
    const n = container.querySelectorAll('#ov-band .ov-lc')[2]!.querySelector('.ov-lc-n')!
    if (todayHasRoom) expect(text(n)).toBe('3')
    expect(n.querySelector('.ctl-mark')).toBeNull()
  })

  // MUTATION: hide `.ov-lc-foot` again at 390 (overview.css's phone block).
  it('keeps the foot naming the page at 390', () => {
    const { container } = render(<LifecycleBand stats={ok(STATS)} tasks={taskPage(FINISHED, true)} />)
    const foot = container.querySelectorAll('#ov-band .ov-lc')[2]!.querySelector('.ov-lc-foot')!
    expect(text(foot)).toBe('of the 3 newest read')
    expect(painted(foot, 'display', PHONE)).not.toBe('none')
    expect(painted(foot, 'display', WIDE)).not.toBe('none')
  })
})

describe('G1-04: a FAILED task is terminal, whatever attempts it had left', () => {
  const NOW = Date.now()
  function failures(tasks: Task[]): string {
    const c = {
      capacity: empty, tasks: taskPage(tasks, false), leases: empty, providers: empty, accounts: empty, stats: empty, workflows: empty,
    } as unknown as CheckInputs
    const check = deriveChecks(c, NOW).find((x) => x.label === 'Failures')!
    expect(check.status).toBe('found')
    return check.status === 'found' ? check.problems[0]!.detail : ''
  }

  // MUTATION: put back "All still have attempts left and may retry".
  it('never says "may retry" of a failed task with attempts left', () => {
    const d = failures([
      task({ id: 'tsk_f1', state: 'FAILED', attempt_count: 1, max_attempts: 3, completed_at: ago(2), last_error: 'empty_diff' }),
      task({ id: 'tsk_f2', state: 'FAILED', attempt_count: 1, max_attempts: 3, completed_at: ago(3) }),
    ])
    expect(d).not.toMatch(/may retry/)
    expect(d).toMatch(/^Terminal/)
    expect(d).toContain('2 had attempts left but the cause is not retryable')
  })

  // MUTATION: drop the exhausted clause.
  it('names the ones that used every attempt beside the ones that did not', () => {
    const d = failures([
      task({ id: 'tsk_f1', state: 'FAILED', attempt_count: 3, max_attempts: 3, completed_at: ago(2) }),
      task({ id: 'tsk_f2', state: 'FAILED', attempt_count: 1, max_attempts: 3, completed_at: ago(3) }),
    ])
    expect(d).not.toMatch(/may retry/)
    expect(d).toContain('1 used every attempt')
    expect(d).toContain('1 had attempts left but the cause is not retryable')
  })
})

describe('G1-07: on a phone, Recent failures comes before Headroom and the pools', () => {
  function frame(): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="ov-page"><div class="ov-g21 ov-cols">' +
      '<div class="ov-col"><section class="ctl-card ov-card ov-running"></section><section class="ctl-card ov-card ov-waiting"></section><section class="ctl-card ov-card ov-failures"></section></div>' +
      '<div class="ov-col"><section class="ctl-card ov-card ov-spend"></section><section class="ctl-card ov-card ov-headroom"></section></div>' +
      '<section class="ctl-card ov-card ov-pools"></section></div></div>'
    document.body.appendChild(host)
    return host
  }
  const order = (host: HTMLElement, sel: string, env: CascadeEnv) => Number(painted(host.querySelector(sel)!, 'order', env) ?? '0')

  // MUTATION: leave `.ov-failures` at order 6 under 560px.
  it('orders failures ahead of headroom and pools at 390', () => {
    const host = frame()
    expect(order(host, '.ov-failures', PHONE)).toBeLessThan(order(host, '.ov-headroom', PHONE))
    expect(order(host, '.ov-failures', PHONE)).toBeLessThan(order(host, '.ov-pools', PHONE))
    expect(order(host, '.ov-waiting', PHONE)).toBeLessThan(order(host, '.ov-failures', PHONE))
    host.remove()
  })

  // MUTATION: drop the page's phone padding-bottom.
  it('leaves room under the last card for the sticky Top pill', () => {
    const host = frame()
    const pad = painted(host.querySelector('.ov-page')!, 'padding-bottom', PHONE)
    expect(pad, 'no padding-bottom at 390').toMatch(/^\d+px$/)
    expect(parseInt(pad!, 10)).toBeGreaterThanOrEqual(56)
    host.remove()
  })

  it('keeps the tablet order as it was', () => {
    const host = frame()
    expect(order(host, '.ov-failures', TABLET)).toBeGreaterThan(order(host, '.ov-pools', TABLET))
    host.remove()
  })
})

describe('G1-09: a row leads with its task title, then its step', () => {
  // The page is `view=summary` (#168): its rows carry NO metadata. The title
  // comes from one full read per workflow, which `loadTask` answers here.
  const TITLE = 'Fix flaky lease heartbeat test (#248)'
  const full = (row: Task, metadata: Record<string, unknown> | null): Result<Task> => ok({ ...row, metadata })

  async function mount(page: Task[], fulls: Record<string, Result<Task>>): Promise<HTMLElement> {
    api.loadCapacity.mockResolvedValue(empty)
    api.loadTasks.mockResolvedValue(taskPage(page, false))
    api.loadLeases.mockResolvedValue(empty)
    api.loadProviders.mockResolvedValue(empty)
    api.loadWorkflows.mockResolvedValue(empty)
    api.loadStats.mockResolvedValue(ok(STATS))
    api.loadAccountPool.mockResolvedValue(empty)
    api.loadSpend.mockResolvedValue(empty)
    api.loadTask.mockReset()
    api.loadTask.mockImplementation(async (id: string) => fulls[id] ?? { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'gone' } })
    const { container } = render(<OverviewScreen />)
    for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
    return container
  }

  // MUTATION: draw `agentName` alone in either card, or read the title off
  // the summary row (which has none).
  it('in Running now and Recent failures, with one read per workflow', async () => {
    const run = task({ id: 'tsk_g9_run', state: 'RUNNING', step_id: 'review', workflow_id: 'wf_g9', started_at: ago(3), metadata: null })
    const fail = task({ id: 'tsk_g9_fail', state: 'FAILED', step_id: 'fix', workflow_id: 'wf_g9', completed_at: ago(2), metadata: null })
    const el = await mount([run, fail], { tsk_g9_run: full(run, { title: TITLE, unit: 'c5h' }), tsk_g9_fail: full(fail, { title: TITLE, unit: 'c5h' }) })
    expect(text(el.querySelector('#ov-running .ov-rows .ov-name'))).toBe(`${TITLE} · review`)
    expect(text(el.querySelector('#ov-running .ov-prun-n'))).toBe(`${TITLE} · review`)
    expect(text(el.querySelector('#ov-failures .ov-name'))).toBe(`${TITLE} · fix`)
    // Two steps of one workflow: one read names both.
    expect(api.loadTask).toHaveBeenCalledTimes(1)
  })

  it('falls back to the workflow label, then to the step alone when nothing names it', async () => {
    const a = task({ id: 'tsk_g9_a', state: 'FAILED', step_id: 'review', workflow_id: 'wf_g9a', completed_at: ago(2), metadata: null })
    const b = task({ id: 'tsk_g9_b', state: 'FAILED', step_id: 'implement', workflow_id: 'wf_g9b', completed_at: ago(3), metadata: null })
    const c = task({ id: 'tsk_g9_c', state: 'CANCELLED', step_id: 'fix', workflow_id: 'wf_g9c', completed_at: ago(4), metadata: null })
    const el = await mount([a, b, c], { tsk_g9_a: full(a, { unit: 'c5h' }), tsk_g9_b: full(b, { title: '   ' }) })
    expect([...el.querySelectorAll('#ov-failures .ov-name')].map(text)).toEqual(['c5h · review', 'implement', 'fix'])
  })

  // MUTATION: put `text-decoration: none` back on `.ov-name`.
  it('draws the row link underlined, like the workflow id beside it', () => {
    const host = document.createElement('div')
    host.innerHTML = '<a class="ov-name" href="#">x</a>'
    document.body.appendChild(host)
    expect(painted(host.querySelector('.ov-name')!, 'text-decoration-line', WIDE)).toBe('underline')
    host.remove()
  })
})

describe('G1-03 and G1-10: the card heads say what they hold and link to it', () => {
  const SPEND = {
    tenantId: 'eng', tasksOnPage: 189, tasksWithAttempts: 189, tasksSampled: 12, failedReads: 0, failedDetail: null,
    attempts: 12, attemptsWithCost: 12, attemptsWithTokens: 12, costUsd: 8.51,
    inputTokens: 1, outputTokens: 1, cacheReadTokens: 0, cacheCreationTokens: 0, from: null, to: null,
  } as unknown as SpendRollup

  async function mount(): Promise<HTMLElement> {
    api.loadCapacity.mockResolvedValue(empty)
    api.loadTasks.mockResolvedValue(taskPage(FINISHED, false))
    api.loadLeases.mockResolvedValue(empty)
    api.loadProviders.mockResolvedValue(empty)
    api.loadWorkflows.mockResolvedValue(empty)
    api.loadStats.mockResolvedValue(ok(STATS))
    api.loadAccountPool.mockResolvedValue(empty)
    api.loadSpend.mockResolvedValue(ok(SPEND))
    api.loadTask.mockResolvedValue(empty)
    const { container } = render(<OverviewScreen />)
    for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
    return container
  }

  // MUTATION: call the card "Cost so far" again, or drop its note.
  it('calls a rolling sample "Recent token cost", noted with its size', async () => {
    const el = await mount()
    const head = el.querySelector('#ov-spend .ctl-card-head')!
    const title = head.querySelector('.ctl-card-title')!.cloneNode(true) as HTMLElement
    for (const n of [...title.querySelectorAll('[data-help-description], button')]) n.remove()
    expect(text(title)).toBe('Recent token cost')
    expect(text(head.querySelector('.ctl-card-note'))).toBe('last 12 attempts')
    expect(text(head.querySelector('.ctl-card-note'))).not.toMatch(/so far/)
    expect(text(title)).not.toMatch(/so far/)
  })

  // MUTATION: point "All recent" back at `?state=failed`.
  it('opens the whole Recent tab from Recent failures, not only the failed filter', async () => {
    const el = await mount()
    const a = el.querySelector<HTMLAnchorElement>('#ov-failures .ov-link')!
    expect(a.getAttribute('href')).toBe('/agents/recent')
    expect(text(a)).toMatch(/^All recent/)
  })
})
