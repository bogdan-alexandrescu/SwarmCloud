// POOL LIMITS: A PROVIDER-TENANT POOL'S QUOTA TERMS, AND THE TWO CONTROLS
// OVER THEM (POOL-LIMITS-QUOTA, BGD 2026-10-25, part of #2302).
//
// anthropic · eng sat full at 14 while this screen showed its hard limit, 100:
// the effective limit is the least of the hard limit, the broker's configured
// max (50) and the AIMD target (14), and only the hard limit was on screen.
// Each provider-tenant row now draws every term and which binds, from
// `GET /v1/admin/quota`; a platform admin can reset the target and change the
// configured max from the row's editor, each with a required reason, and the
// row redraws from the re-read and says what the answer said. Nobody else
// sees either control.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Me, Pool, QuotaState } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  loadAdminQuota: vi.fn(),
  setPoolLimit: vi.fn(),
  resetAdaptiveTarget: vi.fn(),
  setQuotaHardMax: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen, quotaTerms, poolSpoken } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const
const POOL = 'provider:anthropic:tenant:eng'

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-25T06:03:00Z' }
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 100,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 100,
    active: 0,
    available: 100,
    enabled: true,
    updated_at: '2026-10-25T06:03:00Z',
    ...over,
  }
}

/** The measured case: hard 100, max 50, target 14, 14 of 14 in use. */
function capacity(target: number): Capacity {
  return {
    pools: [
      pool('global', { hard_limit: 200, effective_limit: 200, available: 186, active: 14 }),
      pool(POOL, { adaptive_target: target, quota_derived_limit: target, effective_limit: target, active: 14, available: Math.max(0, target - 14) }),
    ],
    runner_profiles: {
      'claude-code': { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools: ['global', POOL] },
    },
    tenant_id: 'eng',
    generated_at: '2026-10-25T06:03:00Z',
  }
}

function quota(over: Partial<QuotaState> = {}): QuotaState {
  return {
    provider: 'anthropic',
    tenant_id: 'eng',
    state: 'THROTTLED',
    updated_at: '2026-10-25T06:03:00Z',
    configured_hard_max: 50,
    adaptive_target: 14,
    quota_derived_limit: null,
    requests_remaining: null,
    tokens_remaining: null,
    reset_at: null,
    cooldown_until: null,
    last_429_at: '2026-10-25T04:40:57Z',
    retry_after_seconds: null,
    success_count: 16,
    rate_limit_count: 1,
    effective_limit: 14,
    ...over,
  }
}

function session(isAdmin: boolean): Result<Me> {
  return ok({
    tenant: { tenant_id: 'eng' },
    principal: { email: 'a@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
  } as unknown as Me)
}

function serve(target: number, q: QuotaState = quota({ adaptive_target: target })) {
  api.loadCapacity.mockResolvedValue(ok(capacity(target)))
  api.loadAdminPools.mockResolvedValue(ok({ pools: [] }))
  api.loadAdminQuota.mockResolvedValue(ok({ quota: [q] }))
}

async function row(): Promise<HTMLElement> {
  return await waitFor(() => {
    const el = document.getElementById(`limit-${POOL}`)
    expect(el?.querySelector('[data-quota-line]')).toBeTruthy()
    return el as HTMLElement
  }, WAIT)
}

async function openEditor(): Promise<HTMLElement> {
  const r = await row()
  fireEvent.click(within(r).getByRole('button', { name: `Edit ceiling for ${poolSpoken(POOL)}` }))
  return await waitFor(() => {
    const side = document.querySelector<HTMLElement>(`aside.adm-side[data-pool="${POOL}"]`)
    expect(side?.querySelector('[data-quota-section]')).toBeTruthy()
    return side as HTMLElement
  }, WAIT)
}

describe('a provider-tenant row draws every term of its ceiling', () => {
  it('shows the hard limit, the configured max and the target, and that the target binds', async () => {
    serve(14)
    render(<AdminSettingsScreen />)
    const line = (await row()).querySelector('[data-quota-line]') as HTMLElement
    expect(line.textContent).toContain('hard 100')
    expect(line.textContent).toContain('max 50')
    expect(line.textContent).toContain('target 14 binds')
    expect(line.textContent).toContain('429 ')
    expect(line.querySelectorAll('.adm-quota-binds')).toHaveLength(1)
  })

  it('names every term tied at the least, and a closing state over the terms', () => {
    const p = pool(POOL, { hard_limit: 50 })
    expect(quotaTerms(p, quota({ adaptive_target: null })).binds).toEqual(['hard', 'max'])
    expect(quotaTerms(p, quota({ state: 'EXHAUSTED' }))).toMatchObject({ binds: [], closed: 'exhausted' })
  })

  it('the editor lists the terms, what binds, in use and free, the last 429 and the cooldown', async () => {
    serve(14, quota({ cooldown_until: new Date(Date.now() + 5 * 60_000).toISOString() }))
    render(<AdminSettingsScreen />)
    const side = await openEditor()
    const facts = (side.querySelector('[data-quota-section]') as HTMLElement).textContent ?? ''
    expect(facts).toContain('Configured max50')
    expect(facts).toContain('Adaptive target14')
    expect(facts).toContain('Bindstarget')
    // In use and free are the editor's own first fact, not drawn twice.
    expect(side.textContent).toContain('14 units · 0 free')
    expect(facts).toMatch(/Cooldown\d+m left/)
  })
})

describe('only a platform admin sees the two controls', () => {
  it.each([
    ['a non-admin', () => api.loadMe.mockResolvedValue(session(false))],
    ['an unknown session', () => api.loadMe.mockResolvedValue(undefined)],
  ])('%s sees neither', async (_who, who) => {
    who()
    serve(14)
    render(<AdminSettingsScreen />)
    // A non-admin cannot open the editor at all; an unknown session can, for
    // the hard limit, and the quota section draws its facts without a control.
    const r = await row()
    const edit = within(r).getByRole('button', { name: new RegExp(`^Edit ceiling for ${poolSpoken(POOL)}`) })
    fireEvent.click(edit)
    await new Promise((done) => setTimeout(done, 50))
    expect(screen.queryByRole('button', { name: 'Reset target' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Set max' })).toBeNull()
    expect(api.resetAdaptiveTarget).not.toHaveBeenCalled()
    expect(api.setQuotaHardMax).not.toHaveBeenCalled()
  })
})

describe('Reset target', () => {
  it('sends the target and the reason, re-reads, and redraws from the answer', async () => {
    api.loadMe.mockResolvedValue(session(true))
    serve(14)
    api.resetAdaptiveTarget.mockImplementation(async () => {
      serve(50)
      return ok({ quota: { adaptive_target: 50 }, reset: { from_target: 14, to_target: 50, clamped: false } })
    })
    render(<AdminSettingsScreen />)
    const side = await openEditor()
    const button = within(side).getByRole('button', { name: 'Reset target' }) as HTMLButtonElement
    // The target is prefilled with the configured max; the reason is required.
    expect((within(side).getByLabelText(`New adaptive target for ${poolSpoken(POOL)}`) as HTMLInputElement).value).toBe('50')
    expect(button.disabled).toBe(true)
    fireEvent.change(within(side).getByLabelText(`Reason for resetting the target of ${poolSpoken(POOL)}`), {
      target: { value: 'one account hit its window' },
    })
    fireEvent.click(button)
    await waitFor(() => expect(api.resetAdaptiveTarget).toHaveBeenCalledTimes(1), WAIT)
    expect(api.resetAdaptiveTarget).toHaveBeenCalledWith('anthropic', 'eng', {
      target: 50,
      reason: 'one account hit its window',
      clear_cooldown: false,
    })
    await waitFor(() => {
      const line = document.getElementById(`limit-${POOL}`)?.querySelector('[data-quota-line]')
      expect(line?.textContent).toContain('target 50')
      expect(line?.textContent).toContain('max 50')
    }, WAIT)
    // Said once, on the row, as the hard limit's save is.
    expect(screen.getAllByText(/target 14 → 50$/)).toHaveLength(1)
    expect(document.getElementById(`limit-${POOL}`)?.textContent).toMatch(/target 14 → 50/)
  })

  it('says so when the broker clamped it', async () => {
    api.loadMe.mockResolvedValue(session(true))
    serve(14)
    api.resetAdaptiveTarget.mockResolvedValue(
      ok({ quota: { adaptive_target: 50 }, reset: { from_target: 14, to_target: 50, clamped: true } }),
    )
    render(<AdminSettingsScreen />)
    const side = await openEditor()
    fireEvent.change(within(side).getByLabelText(`New adaptive target for ${poolSpoken(POOL)}`), { target: { value: '80' } })
    expect(side.textContent).toContain('above the max')
    fireEvent.change(within(side).getByLabelText(`Reason for resetting the target of ${poolSpoken(POOL)}`), { target: { value: 'r' } })
    fireEvent.click(within(side).getByRole('button', { name: 'Reset target' }))
    await waitFor(() => expect(screen.getAllByText(/target 14 → 50, clamped to the max/).length).toBeGreaterThan(0), WAIT)
  })

  it('offers "clear cooldown" only when the tenant is EXHAUSTED, and sends it', async () => {
    api.loadMe.mockResolvedValue(session(true))
    serve(14)
    const first = render(<AdminSettingsScreen />)
    let side = await openEditor()
    expect(within(side).queryByLabelText('clear cooldown')).toBeNull()
    first.unmount()

    serve(14, quota({ state: 'EXHAUSTED', effective_limit: 0 }))
    api.resetAdaptiveTarget.mockResolvedValue(ok({ reset: { from_target: 14, to_target: 50, clamped: false } }))
    render(<AdminSettingsScreen />)
    side = await openEditor()
    fireEvent.click(within(side).getByLabelText('clear cooldown'))
    fireEvent.change(within(side).getByLabelText(`Reason for resetting the target of ${poolSpoken(POOL)}`), { target: { value: 'window reset' } })
    fireEvent.click(within(side).getByRole('button', { name: 'Reset target' }))
    await waitFor(() => expect(api.resetAdaptiveTarget).toHaveBeenCalledTimes(1), WAIT)
    expect(api.resetAdaptiveTarget.mock.calls[0]![2]).toEqual({ target: 50, reason: 'window reset', clear_cooldown: true })
  })
})

describe('Set max', () => {
  it('sends the max and the reason and says the broker clamped the target', async () => {
    api.loadMe.mockResolvedValue(session(true))
    serve(40, quota({ adaptive_target: 40 }))
    api.setQuotaHardMax.mockResolvedValue(
      ok({ quota: {}, change: { from_hard_max: 50, to_hard_max: 30, from_target: 40, to_target: 30, target_clamped: true } }),
    )
    render(<AdminSettingsScreen />)
    const side = await openEditor()
    fireEvent.change(within(side).getByLabelText(`New configured max for ${poolSpoken(POOL)}`), { target: { value: '30' } })
    expect(side.textContent).toContain('below the target (40): the broker lowers the target to 30')
    fireEvent.change(within(side).getByLabelText(`Reason for changing the max of ${poolSpoken(POOL)}`), { target: { value: 'eng asked for less' } })
    fireEvent.click(within(side).getByRole('button', { name: 'Set max' }))
    await waitFor(() => expect(api.setQuotaHardMax).toHaveBeenCalledTimes(1), WAIT)
    expect(api.setQuotaHardMax).toHaveBeenCalledWith('anthropic', 'eng', { hard_max: 30, reason: 'eng asked for less' })
    await waitFor(() => expect(screen.getAllByText(/max 50 → 30; target clamped 40 → 30/).length).toBeGreaterThan(0), WAIT)
  })
})
