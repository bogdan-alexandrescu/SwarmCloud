// CAPACITY › HOLDERS, VISUAL QA PASS 1 (QA1-FIX-12; part of #2596).
//
//   chrome-C25   every holder was `task_…4806a6ff` and its lease id: no
//                agent name. The row names the agent the way Accounts'
//                holders do (`agentName` off the task read), the short id
//                staying the link's title and the copy.
//   capacity-16  Accounting drift listed all 20 pools in catalogue order,
//                zeros interleaved with drifted ones; the banner named pools
//                by raw id beside a table that says `anthropic · u-bogdan`;
//                the foot carried `make pool-check`. Drifted pools first by
//                the size of their delta, the agreeing ones folded behind a
//                count, one pool name in banner and table, no Makefile target.
//   capacity-17  with `?tenant=u-bogdan` the head still said `every tenant`.
//
// MUTATION: draw `<TaskRef id={l.task_id} />` with no title again -- the
// first case finds no agent name. Sort drift rows by `held` again, or draw
// the agreeing rows unfolded -- the second finds a zero row before the
// drifted one, or no `pools agree` fold. Put `<b>{r.name}</b>` back in the
// callout -- the third finds the raw id. Put `make pool-check` back in the
// foot -- the fourth finds it. Build the head's scope from `page.tenant_id`
// alone -- the fifth reads `every tenant` under the filter.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LeasePage, LeaseRow, Pool, Task } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn(), loadTask: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { HoldersScreen } = await import('../Holders')

const WAIT = { timeout: 5000 } as const
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  window.history.replaceState(null, '', '/')
  api.loadTask.mockReset()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-02T10:00:00Z' }
}

function lease(n: number, pools: string[], tenant = 'u-bogdan'): LeaseRow {
  return {
    lease_id: `lease_${String(n).padStart(10, '0')}`,
    task_id: `task_${String(n).padStart(10, '0')}`,
    attempt_id: `att_${n}`,
    tenant_id: tenant,
    generation: 1,
    pools,
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-02T09:00:00Z',
    dispatch_deadline: '2026-10-02T09:05:00Z',
    expires_at: '2026-10-02T11:00:00Z',
    heartbeat_at: '2026-10-02T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
  } as LeaseRow
}

function pool(name: string, active: number): Pool {
  return { name, active } as unknown as Pool
}

const RAW = 'provider:anthropic:tenant:u-bogdan'

function board(leases: LeaseRow[] = [lease(1, ['global', 'tenant:u-bogdan']), lease(2, ['global', 'tenant:u-bogdan'])]): HoldersBoard {
  return {
    page: {
      leases,
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-10-02T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: leases.length,
      truncated: false,
      active_beyond_window: 0,
    } as unknown as LeasePage,
    // global and tenant:u-bogdan agree at 2; CLOUD_RUN_JOB is off by 1, the
    // provider pool by 5, and three pools nobody holds agree at 0.
    pools: [
      pool('backend:CLOUD_RUN_JOB', 1),
      pool('backend:GKE_AUTOPILOT', 0),
      pool(RAW, 5),
      pool('global', 2),
      pool('resource:browser', 0),
      pool('resource:standard', 0),
      pool('tenant:u-bogdan', 2),
    ],
    poolsDetail: null,
  }
}

function driftCard(): HTMLElement {
  return screen.getByRole('heading', { name: /Accounting drift/ }).closest('section') as HTMLElement
}

function driftRowNames(): string[] {
  return [...driftCard().querySelectorAll('tbody th')].map((th) => th.getAttribute('title') ?? '?')
}

describe('chrome-C25: a holder is named by its agent, not only its ids', () => {
  it('draws the agent name the task read gives, with the whole id kept as the link title', async () => {
    api.loadHolders.mockResolvedValue(ok(board()))
    api.loadTask.mockImplementation(async (id: string) =>
      ok({ id, step_id: id.endsWith('1') ? 'implement' : null, runner_profile: 'claude-code' } as unknown as Task),
    )
    render(<HoldersScreen />)
    const section = (await screen.findByText('Every holder', undefined, WAIT)).closest('section')!
    await waitFor(() => {
      const links = [...section.querySelectorAll('tbody th a.task-ref-link')].map((a) => visible(a))
      expect(links, 'no holder is named by its agent').toContain('implement')
    }, WAIT)
    const named = [...section.querySelectorAll('tbody th a.task-ref-link')].find((a) => visible(a) === 'implement')!
    expect(named.getAttribute('title')).toBe('task_0000000001')
    expect(api.loadTask).toHaveBeenCalledWith('task_0000000001')
  })

  it('keeps the short id when the task read fails: never a blank cell', async () => {
    api.loadHolders.mockResolvedValue(ok(board()))
    api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'gone' } })
    render(<HoldersScreen />)
    const section = (await screen.findByText('Every holder', undefined, WAIT)).closest('section')!
    await waitFor(() => expect(api.loadTask).toHaveBeenCalled(), WAIT)
    const texts = [...section.querySelectorAll('tbody th a.task-ref-link')].map((a) => visible(a))
    expect(texts).toContain('task_…00000001')
  })
})

describe('capacity-16: drifted pools first, agreeing ones folded, one name per pool', () => {
  it('orders drifted pools by the size of their delta and folds the agreeing ones behind a count', async () => {
    api.loadHolders.mockResolvedValue(ok(board()))
    api.loadTask.mockResolvedValue({ status: 'loading', since: Date.now() })
    render(<HoldersScreen />)
    await screen.findByRole('heading', { name: /Accounting drift/ }, WAIT)
    expect(driftRowNames(), 'the drifted pools are not first, largest delta first').toEqual([RAW, 'backend:CLOUD_RUN_JOB'])
    const fold = [...driftCard().querySelectorAll('button')].find((b) => /pools agree/.test(visible(b)))
    expect(fold, 'no fold for the agreeing pools').toBeTruthy()
    expect(visible(fold!)).toMatch(/^5 pools agree/)
    expect(fold!.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(fold!)
    expect(fold!.getAttribute('aria-expanded')).toBe('true')
    expect(driftRowNames()).toHaveLength(7)
    expect(driftRowNames().slice(0, 2)).toEqual([RAW, 'backend:CLOUD_RUN_JOB'])
  })

  it('names a drifted pool in the banner as the table does, raw id in its title', async () => {
    api.loadHolders.mockResolvedValue(ok(board()))
    api.loadTask.mockResolvedValue({ status: 'loading', since: Date.now() })
    render(<HoldersScreen />)
    const callout = await waitFor(() => {
      const el = document.querySelector('.hold-callout')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(callout)).toContain('anthropic · u-bogdan counts 5 more than its leases.')
    expect(visible(callout), 'the banner still names the pool by its raw id').not.toContain(RAW)
    expect(callout.querySelector(`[title="${RAW}"]`)).not.toBeNull()
  })

  it('says no Makefile target in the card foot', async () => {
    api.loadHolders.mockResolvedValue(ok(board()))
    api.loadTask.mockResolvedValue({ status: 'loading', since: Date.now() })
    render(<HoldersScreen />)
    await screen.findByRole('heading', { name: /Accounting drift/ }, WAIT)
    const foot = driftCard().querySelector('.ctl-card-foot')!
    expect(visible(foot)).toMatch(/against 2 leases read/)
    expect(visible(foot)).not.toContain('make ')
  })
})

describe('capacity-17: the head names the tenant filter', () => {
  it('reads the filtered tenant, with its own counts, under ?tenant=', async () => {
    window.history.replaceState(null, '', '/capacity/holders?tenant=u-bogdan')
    const leases = [lease(1, ['global']), lease(2, ['global']), lease(3, ['global'], 'eng')]
    api.loadHolders.mockResolvedValue(ok(board(leases)))
    api.loadTask.mockResolvedValue({ status: 'loading', since: Date.now() })
    const { container } = render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const head = visible(container)
    expect(head, 'the head still claims every tenant under a filter').not.toContain('every tenant')
    expect(head).toContain('2 unreleased leases · 2 units held · tenant u-bogdan')
  })

  it('the control: with no filter the head still says every tenant', async () => {
    const leases = [lease(1, ['global']), lease(2, ['global']), lease(3, ['global'], 'eng')]
    api.loadHolders.mockResolvedValue(ok(board(leases)))
    api.loadTask.mockResolvedValue({ status: 'loading', since: Date.now() })
    const { container } = render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    expect(visible(container)).toContain('3 unreleased leases · 3 units held · every tenant')
  })
})
