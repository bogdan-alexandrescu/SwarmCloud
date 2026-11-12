// POOLS LINKS TO POOL LIMITS FOR AN ADMIN ONLY (#3015, item 1).
//
// Pool limits is a platform-admin page: Spine refuses `#admin/limits` in place
// to anyone whose session's `principal.is_admin` is not true, workspace admins
// included. Capacity › Pools drew each row's `limit` link, and By runner
// profile's opened row its `Pool limits` link, for everyone -- so a member was
// shown a link to a page they cannot open. Both are drawn now only when the
// session read (`useIsAdmin`, the frame's `loadMe`) says admin; a member, an
// unread session and a failed read draw neither link nor the ` · ` before it.
//
// BREAK IT: drop the `admin &&` in PoolRow -- the member's row has a link to
// admin/limits. Or keep the ` · ` outside the gate -- the member's Open cell
// ends in a dangling separator. Or read `useIsAdmin() !== false` -- the
// unread session draws the link.

import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn(), loadMe: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { ProfileMatrix } = await import('../ProfileMatrix')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  api.loadCapacity.mockReset()
  api.loadMe.mockReset()
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-11-12T10:00:00Z' }
}

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-11-12T10:00:00Z',
  }
}

function profile(over: Partial<RunnerProfile> = {}): RunnerProfile {
  return {
    resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1,
    pools: ['global', 'runner:claude-code'], ...over,
  }
}

function board(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-11-12T10:00:00Z',
    pools: [pool('global', 3, 40), pool('runner:claude-code', 2, 10)],
    runner_profiles: { 'claude-code': profile() },
  }
}

/** The session read: an admin, a member, never answered, or failed. */
type Session = 'admin' | 'member' | 'unread' | 'failed'

function session(who: Session): void {
  if (who === 'unread') api.loadMe.mockReturnValue(new Promise(() => {}))
  else if (who === 'failed') api.loadMe.mockRejectedValue(new Error('session read failed'))
  else api.loadMe.mockResolvedValue(ok({ principal: { email: 'someone@example.test', is_admin: who === 'admin' } }))
}

async function renderPools(who: Session): Promise<void> {
  session(who)
  api.loadCapacity.mockResolvedValue(ok(board()))
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

async function renderMatrix(who: Session): Promise<HTMLElement> {
  session(who)
  render(<ProfileMatrix capacity={board()} />)
  const toggle = document.querySelector<HTMLButtonElement>('.cap-mx tr[data-profile="claude-code"] button')!
  fireEvent.click(toggle)
  const exp = document.querySelector<HTMLElement>('.cap-mx tr.cap-mx-exp')
  expect(exp, 'the profile did not open').not.toBeNull()
  return exp!
}

function openCells(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>('.cap-families tbody .cap-open')]
}

function limitsLinks(root: ParentNode = document): HTMLAnchorElement[] {
  return [...root.querySelectorAll<HTMLAnchorElement>('a[href^="#admin/limits"]')]
}

describe('Capacity › Pools links a row to Pool limits for an admin only (#3015)', () => {
  it('draws each row\'s limit link, after a ` · `, for an admin', async () => {
    await renderPools('admin')
    await waitFor(() => expect(limitsLinks().length).toBe(2), WAIT)
    for (const cell of openCells()) {
      const a = cell.querySelector<HTMLAnchorElement>('a[href^="#admin/limits"]')
      expect(a?.getAttribute('href')).toMatch(/^#admin\/limits\?pool=/)
      expect(a?.textContent).toBe('limit')
      expect(cell.textContent).toContain(' · limit')
    }
  })

  it('draws no limit link and no dangling ` · ` for a member', async () => {
    await renderPools('member')
    // Let the resolved session read land before asserting its absence.
    await new Promise((r) => setTimeout(r, 20))
    expect(openCells().length).toBe(2)
    expect(limitsLinks()).toEqual([])
    for (const cell of openCells()) {
      expect(cell.textContent).not.toContain('·')
      expect(cell.textContent).not.toMatch(/\blimit\b/)
    }
  })

  for (const who of ['unread', 'failed'] as const) {
    it(`draws no limit link while the session is ${who}`, async () => {
      await renderPools(who)
      await new Promise((r) => setTimeout(r, 20))
      expect(openCells().length).toBe(2)
      expect(limitsLinks()).toEqual([])
      for (const cell of openCells()) expect(cell.textContent).not.toContain('·')
    })
  }
})

describe('By runner profile links its opened row to Pool limits for an admin only (#3015)', () => {
  it('draws the Pool limits link for an admin', async () => {
    const exp = await renderMatrix('admin')
    await waitFor(() => expect(limitsLinks(exp).map((a) => a.textContent)).toEqual(['Pool limits']), WAIT)
    expect(limitsLinks(exp)[0]!.getAttribute('href')).toBe('#admin/limits')
  })

  for (const who of ['member', 'unread', 'failed'] as const) {
    it(`draws no Pool limits link for a ${who} session`, async () => {
      const exp = await renderMatrix(who)
      await new Promise((r) => setTimeout(r, 20))
      expect(limitsLinks(exp)).toEqual([])
      expect(exp.textContent).not.toContain('Pool limits')
    })
  }
})
