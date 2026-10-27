// VISUAL QA WAVE 2, LANE R17 (part of #1427), step r17-admin-gates: V064.
//
//   V064  every admin-only gate shows the page's refresh control (owner
//         decision Q1, 2026-10-16). Tenants drew its gate through `Screen`,
//         which offers no refresh on a 403, so a person who was added to an
//         admin group had to leave the page to read it again. People's gate
//         already drew one (`UrRefresh`); it is held here beside Tenants so
//         the two gates cannot drift apart again.
//
// Each case names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { ApiError, Result } from '../fetch'
import type { Capacity, Tenant } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTenants: vi.fn(),
  loadPeople: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TenantsScreen } = await import('../Activity')
const { PeopleScreen } = await import('../People')

const WAIT = { timeout: 5000 } as const

const GATE: ApiError = {
  kind: 'admin_required',
  httpStatus: 403,
  code: 'forbidden',
  message: 'Admin group membership is required.',
}

function refused<T>(): Result<T> {
  return { status: 'error', error: GATE }
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-27T10:00:00Z' }
}

function tenant(over: Partial<Tenant> = {}): Tenant {
  return {
    tenant_id: 'eng',
    kind: 'group',
    principal: 'eng@example.com',
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 45,
    capacity_units: 40,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic'],
    service_account: 'swarm-agent-worker-eng@example-project.iam.gserviceaccount.com',
    gcs_prefix: null,
    namespace: null,
    ...over,
  }
}

function capacity(): Capacity {
  return { pools: [], runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-27T10:00:00Z' } as Capacity
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  api.loadCapacity.mockResolvedValue(ok(capacity()))
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('V064: the Tenants gate shows the page refresh control', () => {
  it('draws the refresh in the head beside the gate, under the one Tenants title', async () => {
    api.loadTenants.mockResolvedValue(refused())
    const { container } = render(<TenantsScreen />)
    const gate = await screen.findByRole('status', undefined, WAIT)
    expect(gate.classList.contains('admin-gate')).toBe(true)
    expect(container.querySelectorAll('.c-phead')).toHaveLength(1)
    expect(container.querySelector('.c-phead h1')?.textContent).toBe('Tenants')
    // MUTATION: draw the Tenants gate through `Screen` alone again, which
    // hides its refresh on `admin_required`.
    await waitFor(() => expect(container.querySelector('.c-phead .c-acts .c-refresh'), 'the Tenants gate drew no refresh').not.toBeNull(), WAIT)
    // Nothing was read, so it says so rather than claiming an age.
    expect(container.querySelector('.c-phead .c-refresh')?.getAttribute('aria-label')).toBe('Refresh · not read')
    // The gate itself still offers no Try again: the head's control is the one press.
    expect(within(gate).queryByRole('button')).toBeNull()
  })

  it('re-reads on a press, keeps focus on the press while still refused, and draws the roster once allowed', async () => {
    api.loadTenants.mockResolvedValue(refused())
    const { container } = render(<TenantsScreen />)
    await waitFor(() => expect(container.querySelector('.state.admin-gate')).not.toBeNull(), WAIT)
    await waitFor(() => expect(container.querySelector('.c-phead .c-refresh'), 'the Tenants gate drew no refresh').not.toBeNull(), WAIT)
    const calls = api.loadTenants.mock.calls.length

    // Still refused: the same button stays, focused, and the gate stays.
    const press = container.querySelector<HTMLButtonElement>('.c-phead .c-refresh')!
    press.focus()
    fireEvent.click(press)
    await waitFor(() => expect(api.loadTenants.mock.calls.length).toBe(calls + 1), WAIT)
    await waitFor(() => expect(container.querySelector('.c-phead .c-refresh')?.getAttribute('aria-label')).toBe('Refresh · not read'), WAIT)
    expect(container.querySelector('.c-phead .c-refresh')).toBe(press)
    expect(document.activeElement).toBe(press)
    expect(container.querySelector('.state.admin-gate')).not.toBeNull()

    // Now allowed: the press's answer is the roster, read once, not twice.
    api.loadTenants.mockResolvedValue(ok({ tenants: [tenant()] }))
    fireEvent.click(press)
    await screen.findByText('eng@example.com', { exact: false }, WAIT)
    expect(container.querySelector('.state.admin-gate')).toBeNull()
    // MUTATION: let `Screen` read again on top of the press's read.
    expect(api.loadTenants.mock.calls.length).toBe(calls + 2)
    expect(container.querySelectorAll('.c-phead')).toHaveLength(1)
  })
})

describe('V064: the People gate shows the page refresh control', () => {
  it('draws the refresh in the head beside the gate, and a press reads again', async () => {
    api.loadPeople.mockResolvedValue(refused())
    const { container } = render(<PeopleScreen />)
    await waitFor(() => expect(container.querySelector('.state.admin-gate')).not.toBeNull(), WAIT)
    // MUTATION: hide `UrRefresh` while the region is an admin gate.
    const press = container.querySelector<HTMLButtonElement>('.pp-page > .c-phead .c-acts .c-refresh')
    expect(press, 'the People gate drew no refresh').not.toBeNull()
    const calls = api.loadPeople.mock.calls.length
    fireEvent.click(press!)
    await waitFor(() => expect(api.loadPeople.mock.calls.length).toBeGreaterThan(calls), WAIT)
    expect(container.querySelector('.state.admin-gate')).not.toBeNull()
  })
})
