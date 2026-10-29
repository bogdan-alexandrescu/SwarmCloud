// ADMIN › POOL LIMITS, ITS LEAD (ADMIN-LEADS; part of #2157, proposal E
// S10b and S10g).
//
// "Every Admin page opens with what needs an admin now, in one sentence with
// one button, and a green 'nothing waiting' lead when that is the truth."
// Pool limits is "the table as built, led by the pool closest to its
// ceiling".
//
// WHAT EACH CASE HOLDS:
//   * a pool at 80% of its ceiling or more leads, named, with its units in use
//     of its ceiling, and its one button opens THAT pool's side editor;
//   * a pool with no ceiling set (#374: admission refuses on it) leads ahead
//     of any busy pool;
//   * nothing near a ceiling is the green "Nothing waiting" lead, with no
//     button;
//   * a capacity read that failed is a dash with its reason, never green;
//   * the lead reads only the page's own reads: no route is added.
//
// The api module is replaced so the page renders from pools built here.
// Nothing here is token-shaped.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'

import type { ApiError, Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile } from '../types'
import { serve } from './repofixture'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { AdminSettingsScreen, poolLead } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

const FAILED: ApiError = { kind: 'server_error', httpStatus: 500, code: null, message: 'Internal Server Error' }

function pool(name: string, active: number, limit: number | null, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active,
    available: limit === null ? 0 : Math.max(0, limit - active),
    enabled: true,
    updated_at: '2026-10-24T10:00:00Z',
    ...over,
  }
}

function profile(pools: string[]): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools }
}

function capacity(pools: Pool[]): Capacity {
  return {
    pools,
    runner_profiles: { 'claude-code': profile(pools.map((p) => p.name)) },
    tenant_id: 'example-team',
    generated_at: '2026-10-24T10:00:00Z',
  }
}

const BUSY = capacity([
  pool('global', 46, 50),
  pool('tenant:example-team', 18, 30),
  pool('resource:browser', 7, 12),
])
const QUIET = capacity([pool('global', 12, 50), pool('tenant:example-team', 18, 30), pool('resource:browser', 0, 12)])

beforeEach(() => {
  // Every other read on the page (the folded cards' own) answers 404.
  serve(() => null)
  api.loadCapacity.mockReset()
  api.loadAdminPools.mockReset()
  api.loadMe.mockReset()
  api.loadAdminPools.mockResolvedValue(ok({ pools: [] }))
  api.loadMe.mockResolvedValue(undefined)
})

async function lead(answer: Result<Capacity>): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(answer)
  render(<AdminSettingsScreen />)
  return waitFor(() => {
    const l = document.querySelector<HTMLElement>('.adm-lead')
    expect(l, 'Pool limits drew no lead').not.toBeNull()
    return l as HTMLElement
  }, WAIT)
}

const text = (el: Element) => (el.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('Pool limits leads with the pool closest to its ceiling (S10b)', () => {
  it('names the pool and its units in use of its ceiling', async () => {
    const l = await lead(ok(BUSY))
    expect(l.dataset.lead).toBe('warn')
    expect(text(l)).toContain('Pool global is at 92% of its ceiling')
    expect(text(l)).toContain('46 of 50 units in use')
    // One button, and only one.
    expect(within(l).getAllByRole('button')).toHaveLength(1)
  })

  it('opens that pool’s side editor from its one button', async () => {
    const l = await lead(ok(BUSY))
    expect(document.querySelector('aside.adm-side')).toBeNull()
    fireEvent.click(within(l).getByRole('button', { name: 'Open global' }))
    const side = document.querySelector<HTMLElement>('aside.adm-side')
    expect(side, 'the lead opened no side editor').not.toBeNull()
    expect(within(side!).getByLabelText('Hard limit for global')).toBeTruthy()
  })

  it('sits above the table, which is drawn as built', async () => {
    const l = await lead(ok(BUSY))
    const table = document.querySelector('.adm-ceilings') as HTMLElement
    expect(table).not.toBeNull()
    expect(l.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(document.getElementById('limit-global')).not.toBeNull()
  })

  it('leads with a pool that has no ceiling set ahead of a busy one', async () => {
    const l = await lead(ok(capacity([pool('global', 46, 50), pool('tenant:example-labs', 0, null)])))
    expect(l.dataset.lead).toBe('warn')
    // Named as its row names it, the key on its own line (admin-shell-03).
    expect(text(l)).toContain('Pool example-labs has no ceiling set')
    expect(l.querySelector('.ws-lead-s.id')?.textContent).toBe('tenant:example-labs')
    fireEvent.click(within(l).getByRole('button', { name: 'Open example-labs' }))
    expect(within(document.querySelector('aside.adm-side') as HTMLElement).getByLabelText('Hard limit for example-labs (workspace)')).toBeTruthy()
  })
})

describe('nothing near a ceiling reads the green lead (S10b)', () => {
  it('says nothing is waiting, with no button', async () => {
    const l = await lead(ok(QUIET))
    expect(l.dataset.lead).toBe('ok')
    expect(text(l)).toContain('Nothing waiting')
    expect(text(l)).toContain('No pool is near its ceiling')
    expect(within(l).queryAllByRole('button')).toHaveLength(0)
  })
})

describe('a read that failed is a dash with its reason, never green', () => {
  it('draws the dash and the reason when the capacity read failed', async () => {
    const l = await lead({ status: 'error', error: FAILED })
    expect(l.dataset.lead).toBe('unread')
    expect(text(l)).toContain('—, pool use could not be read')
    expect(text(l)).toContain('The API failed on this request')
    expect(document.querySelector('.adm-lead[data-lead="ok"]')).toBeNull()
    expect(within(l).queryAllByRole('button')).toHaveLength(0)
  })
})

describe('poolLead, the judgement', () => {
  it('ranks by share of the ceiling, and calls under 80% clear', () => {
    expect(poolLead(BUSY.pools)).toMatchObject({ kind: 'near', pool: 'global', active: 46, ceiling: 50 })
    expect(poolLead([pool('a', 7, 10), pool('b', 79, 100)])).toMatchObject({ kind: 'clear' })
    expect(poolLead([pool('a', 8, 10), pool('b', 99, 100)])).toMatchObject({ kind: 'near', pool: 'b' })
  })

  it('leaves a paused pool and a ceiling of 0 with nothing in use out of the ranking', () => {
    expect(poolLead([pool('a', 10, 10, { enabled: false }), pool('b', 0, 0)])).toMatchObject({ kind: 'clear' })
  })
})
