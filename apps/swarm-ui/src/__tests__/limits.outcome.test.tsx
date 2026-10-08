// A SAVE IS A SUCCESS ONLY WHEN THE POOL MOVED TO WHAT WAS ASKED.
//
// Measured live 2026-10-07: the owner raised tenant smoke's ceiling 8 -> 20
// here. PUT /v1/admin/limits/tenant/smoke answered 200 with the pool still at
// 8 (capacity_units held it), and this screen said saved, because a 200 was
// all it read. The editor now compares the response's pool with the request:
// equal is the normal success, anything else is a warning that says the
// ceiling it is still at and why -- never the `saved` tag.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen, saveOutcome } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  api.loadCapacity.mockReset()
  api.setPoolLimit.mockReset()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-07T23:40:00Z' }
}

function pool(name: string, limit: number): Pool {
  return {
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active: 0,
    available: limit,
    enabled: true,
    updated_at: '2026-10-07T23:40:00Z',
  }
}

function capacity(smoke: number, global = 100): Capacity {
  return {
    pools: [pool('global', global), pool('tenant:smoke', smoke)],
    runner_profiles: {},
    tenant_id: 'smoke',
    generated_at: '2026-10-07T23:40:00Z',
  }
}

function row(name: string): HTMLElement {
  const found = document.getElementById(`limit-${name}`)
  expect(found, `no row with id limit-${name}`).not.toBeNull()
  return found as HTMLElement
}

async function save(name: string, value: string): Promise<void> {
  await waitFor(() => expect(document.getElementById(`limit-${name}`)).not.toBeNull(), WAIT)
  fireEvent.click(within(row(name)).getByRole('button', { name: `Edit ceiling for ${name}` }))
  fireEvent.change(screen.getByLabelText(`Hard limit for ${name}`, { exact: false }), { target: { value } })
  fireEvent.click(within(document.querySelector<HTMLElement>('aside.adm-side')!).getByRole('button', { name: 'Save' }))
}

describe('a pool-limit save says what the response says the ceiling became', () => {
  it('reads the normal success when the returned pool is at the requested limit', async () => {
    api.loadCapacity.mockResolvedValueOnce(ok(capacity(8))).mockResolvedValueOnce(ok(capacity(20)))
    api.setPoolLimit.mockResolvedValue(ok({ pool: pool('tenant:smoke', 20), capped_by: null }))
    render(<AdminSettingsScreen />)

    await save('tenant:smoke', '20')

    await waitFor(() => expect(within(row('tenant:smoke')).getByRole('status').textContent).toBe('tenant:smoke ceiling 8 → 20'), WAIT)
    expect(within(row('tenant:smoke')).getByText('saved')).toBeTruthy()
    expect(within(row('tenant:smoke')).queryByText(/Saved, but/)).toBeNull()
    expect(api.setPoolLimit).toHaveBeenCalledWith('tenant:smoke', 20)
  })

  it('warns, with the reason the response gave, when the pool did not move -- never a plain success', async () => {
    // What the live route answered on 2026-10-07 before the API fix.
    api.loadCapacity.mockResolvedValueOnce(ok(capacity(8))).mockResolvedValueOnce(ok(capacity(8)))
    api.setPoolLimit.mockResolvedValue(
      ok({ tenant: { max_active: 20, capacity_units: 8 }, pool: pool('tenant:smoke', 8), capped_by: 'capacity_units' }),
    )
    render(<AdminSettingsScreen />)

    await save('tenant:smoke', '20')

    await waitFor(
      () =>
        expect(within(row('tenant:smoke')).getByRole('status').textContent).toBe(
          "Saved, but the ceiling is still 8: capped by the tenant's capacity_units (8)",
        ),
      WAIT,
    )
    expect(within(row('tenant:smoke')).queryByText('saved'), 'a pool that did not move wore the success tag').toBeNull()
    expect(within(row('tenant:smoke')).getByRole('status').className).toContain('warn-text')
  })

  it('checks every pool kind, not only a tenant: a global save that did not move warns too', async () => {
    api.loadCapacity.mockResolvedValueOnce(ok(capacity(8, 100))).mockResolvedValueOnce(ok(capacity(8, 100)))
    api.setPoolLimit.mockResolvedValue(ok({ pool: pool('global', 100) }))
    render(<AdminSettingsScreen />)

    await save('global', '150')

    await waitFor(
      () =>
        expect(within(row('global')).getByRole('status').textContent).toBe(
          'Saved, but the ceiling is still 100: the response gave no reason',
        ),
      WAIT,
    )
    expect(within(row('global')).queryByText('saved')).toBeNull()
  })
})

describe('saveOutcome', () => {
  it('is a success only for a returned pool at the asked limit', () => {
    expect(saveOutcome('runner:mock', 4, 6, { pool: pool('runner:mock', 6) })).toEqual({
      moved: true,
      text: 'runner:mock ceiling 4 → 6',
    })
    expect(saveOutcome('runner:mock', null, 6, { pool: pool('runner:mock', 6) }).text).toBe(
      'runner:mock ceiling no limit set → 6',
    )
  })

  it('does not call a response with no pool a success', () => {
    const outcome = saveOutcome('backend:CLOUD_RUN_JOB', 10, 20, {})
    expect(outcome.moved).toBe(false)
    expect(outcome.text).toBe('Saved, but the response did not return backend:CLOUD_RUN_JOB, so the new ceiling is not confirmed')
  })

  it('names a capped_by value it has no words for rather than dropping it', () => {
    expect(saveOutcome('tenant:smoke', 8, 20, { pool: pool('tenant:smoke', 8), capped_by: 'global' }).text).toBe(
      'Saved, but the ceiling is still 8: capped by global',
    )
  })
})
