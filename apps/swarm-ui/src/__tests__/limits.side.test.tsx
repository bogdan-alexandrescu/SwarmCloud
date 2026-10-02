// POOL LIMITS L2: THE SIDE EDITOR, AS BEHAVIOUR (admin-help.html, the owner's
// pick 2026-10-01).
//
// Picking a pool opens a side editor with the new ceiling, its impact, a typed
// confirmation for a drastic change -- to 0, or a cut of half or more -- and
// the pool's history. Who changed a ceiling, and when, needs admin_changed_by
// and admin_changed_at, which the API does not serve: until it does, Last
// changed and History read "not recorded". A non-admin reads every ceiling
// with Edit locked and one line saying why.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Me, Pool, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen, isDrasticCut } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

// Each test starts with no call history and no answers: the session read
// answers nothing (Edit open) unless a test says who is asking.
beforeEach(() => {
  api.loadCapacity.mockReset()
  api.setPoolLimit.mockReset()
  api.loadMe.mockReset()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-01T10:00:00Z' }
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 10,
    active: 4,
    available: 6,
    enabled: true,
    updated_at: '2026-10-01T10:00:00Z',
    ...over,
  }
}

function profile(pools: string[], units = 1): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units, pools }
}

/**
 * `tenant:eng` at 10 binds claude-code (global is 40); codex takes only
 * global, so no change to tenant:eng moves it.
 */
function capacity(): Capacity {
  return {
    pools: [pool('global', { hard_limit: 40, effective_limit: 40, available: 36 }), pool('tenant:eng')],
    runner_profiles: {
      'claude-code': profile(['global', 'tenant:eng']),
      codex: profile(['global']),
    },
    tenant_id: 'eng',
    generated_at: '2026-10-01T10:00:00Z',
  }
}

function session(isAdmin: boolean): Result<Me> {
  return ok({
    tenant: { tenant_id: 'eng' },
    principal: { email: 'a@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: isAdmin },
    environment: 'dev',
    environment_declared: false,
  } as unknown as Me)
}

async function open(name: string): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(capacity()))
  render(<AdminSettingsScreen />)
  const row = await waitFor(() => {
    const r = document.getElementById(`limit-${name}`)
    expect(r).not.toBeNull()
    return r as HTMLElement
  }, WAIT)
  fireEvent.click(within(row).getByRole('button', { name: `Edit ceiling for ${name}` }))
  const side = document.querySelector<HTMLElement>('aside.adm-side')
  expect(side, 'edit opened no side editor').not.toBeNull()
  return side!
}

function type(side: HTMLElement, value: string): void {
  fireEvent.change(within(side).getByLabelText('Hard limit for tenant:eng'), { target: { value } })
}

function confirmField(side: HTMLElement): HTMLInputElement | null {
  return within(side).queryByLabelText('Type tenant:eng to confirm') as HTMLInputElement | null
}

describe('the drastic-change rule', () => {
  it('is 0, or a cut of half or more, and nothing else', () => {
    expect(isDrasticCut(10, 0)).toBe(true)
    expect(isDrasticCut(10, 5)).toBe(true)
    expect(isDrasticCut(10, 4)).toBe(true)
    expect(isDrasticCut(10, 6)).toBe(false)
    expect(isDrasticCut(10, 10)).toBe(false)
    expect(isDrasticCut(10, 20)).toBe(false)
    expect(isDrasticCut(1, 0)).toBe(true)
  })
})

describe('Pool limits asks for the pool name before a drastic change (L2)', () => {
  it('requires the typed name to set a ceiling to 0, and writes once it matches', async () => {
    api.setPoolLimit.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    const side = await open('tenant:eng')
    type(side, '0')

    const field = confirmField(side)
    expect(field, 'setting a ceiling to 0 asked for no confirmation').not.toBeNull()
    const go = within(side).getByRole('button', { name: 'Set to 0' }) as HTMLButtonElement
    expect(go.disabled, 'Set to 0 is pressable before the name is typed').toBe(true)

    // A prefix is not the name.
    fireEvent.change(field!, { target: { value: 'tenant:en' } })
    expect(go.disabled).toBe(true)
    fireEvent.click(go)
    expect(api.setPoolLimit).not.toHaveBeenCalled()

    fireEvent.change(field!, { target: { value: 'tenant:eng' } })
    expect(go.disabled).toBe(false)
    fireEvent.click(go)
    await waitFor(() => expect(api.setPoolLimit).toHaveBeenCalledWith('tenant:eng', 0), WAIT)
  })

  it('requires it for a cut of exactly half', async () => {
    const side = await open('tenant:eng')
    type(side, '5')
    expect(confirmField(side), 'a 50% cut asked for no confirmation').not.toBeNull()
    expect((within(side).getByRole('button', { name: 'Set to 5' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not ask for a smaller cut, and Save writes on its own', async () => {
    api.setPoolLimit.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    const side = await open('tenant:eng')
    type(side, '6')
    expect(confirmField(side), 'a 40% cut asked for the name').toBeNull()
    const save = within(side).getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    await waitFor(() => expect(api.setPoolLimit).toHaveBeenCalledWith('tenant:eng', 6), WAIT)
  })

  it('does not ask for a raise', async () => {
    const side = await open('tenant:eng')
    type(side, '20')
    expect(confirmField(side), 'a raise asked for the name').toBeNull()
    expect((within(side).getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('drops the confirmation when the value is taken back to a small cut', async () => {
    const side = await open('tenant:eng')
    type(side, '0')
    expect(confirmField(side)).not.toBeNull()
    type(side, '8')
    expect(confirmField(side)).toBeNull()
    expect((within(side).getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('never treats a cleared field as 0', async () => {
    const side = await open('tenant:eng')
    type(side, '')
    expect(within(side).queryByRole('button', { name: 'Set to 0' })).toBeNull()
    expect((within(side).getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('Pool limits says what a change does, from figures it has (L2)', () => {
  it('names the profile whose ceiling moves, the one that does not, and that nothing is stopped', async () => {
    const side = await open('tenant:eng')
    type(side, '2')
    const impact = side.querySelector('.adm-impact')?.textContent ?? ''
    // claude-code is bound by tenant:eng: its ceiling follows it down.
    expect(impact).toContain('claude-code 10 → 2')
    // Below the 4 units in use: nothing is stopped, new work waits.
    expect(impact).toMatch(/Nothing running is stopped: the 4 units in use finish/)
    // The delta, with its sign.
    expect(side.querySelector('.adm-delta')?.textContent).toBe('10 → 2 · −80%')
  })

  it('says a raise frees units, and draws no impact before anything is typed', async () => {
    const side = await open('tenant:eng')
    expect(side.querySelector('.adm-impact')).toBeNull()
    type(side, '20')
    expect(side.querySelector('.adm-impact')?.textContent ?? '').toContain('16 units free on this pool')
  })
})

describe('Pool limits says "not recorded" where the API serves no history (L2)', () => {
  it('reads not recorded for Last changed and for History, with the reason on hover', async () => {
    const side = await open('tenant:eng')
    const marks = [...side.querySelectorAll('.adm-not-recorded')]
    expect(marks.map((m) => m.textContent)).toEqual(['not recorded', 'not recorded'])
    for (const m of marks) expect(m.getAttribute('title') ?? '').toContain('admin_changed_by')
    // updated_at moves on every lease; it must not be printed as a change time.
    expect(side.textContent ?? '').not.toContain('2026-10-01')
  })
})

describe('Pool limits locks Edit for a non-admin (decided 2026-10-01)', () => {
  it('keeps every ceiling readable, disables Edit, and says why once', async () => {
    api.loadMe.mockResolvedValue(session(false))
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await screen.findByText(/Admins only/, undefined, WAIT)
    for (const tr of document.querySelectorAll('tbody tr')) {
      const edit = within(tr as HTMLElement).getByRole('button', { name: /^Edit ceiling for / }) as HTMLButtonElement
      expect(edit.disabled).toBe(true)
      expect(edit.getAttribute('aria-label')).toMatch(/Admins only/)
      expect(tr.querySelector('.adm-ceiling')?.textContent).toMatch(/^\d+$/)
    }
    expect(screen.getAllByText(/Admins only/)).toHaveLength(1)
  })

  it('leaves Edit open for an admin', async () => {
    api.loadMe.mockResolvedValue(session(true))
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await waitFor(() => expect(api.loadMe).toHaveBeenCalled(), WAIT)
    const row = await waitFor(() => {
      const r = document.getElementById('limit-tenant:eng')
      expect(r).not.toBeNull()
      return r as HTMLElement
    }, WAIT)
    expect((within(row).getByRole('button', { name: 'Edit ceiling for tenant:eng' }) as HTMLButtonElement).disabled).toBe(false)
    expect(screen.queryByText(/Admins only/)).toBeNull()
  })
})
