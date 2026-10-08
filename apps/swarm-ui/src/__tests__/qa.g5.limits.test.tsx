// ADMIN › POOL LIMITS, THE 2026-10-07 QA PASS (G5-21).
//
// mock-provider · eng read "50 · provider quota" on Pool limits while Pools
// read "50/100" for the same pool, and the editor said "Ceiling 50 units ·
// Hard limit 100" -- no unit on the second -- then prefilled 100 under a field
// labelled "New ceiling", beside the 50 it had just shown. The table now
// carries Pools' `/100`, and the editor names the hard limit it is editing.

import SHEET from '../styles.css?raw'
import ADMIN from '../styles/admin.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Me, Pool } from '../types'
import { cascade, type CascadeEnv } from './cssgate'

const STYLES = `${SHEET}\n${ADMIN}`

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-07T10:00:00Z' }
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 10,
    active: 0,
    available: 10,
    enabled: true,
    updated_at: '2026-10-07T09:59:00Z',
    ...over,
  }
}

const QUOTA_HELD = 'provider:mock:tenant:eng'

function capacity(): Capacity {
  return {
    pools: [
      pool('global', { hard_limit: 40, effective_limit: 40 }),
      pool(QUOTA_HELD, { hard_limit: 100, quota_derived_limit: 50, effective_limit: 50, available: 50 }),
    ],
    runner_profiles: {},
    tenant_id: 'eng',
    generated_at: '2026-10-07T10:00:00Z',
  } as Capacity
}

beforeEach(() => {
  api.loadCapacity.mockReset()
  api.loadAdminPools.mockReset()
  api.loadMe.mockReset()
  api.loadCapacity.mockResolvedValue(ok(capacity()))
  api.loadAdminPools.mockResolvedValue(ok({ pools: [] }))
  api.loadMe.mockResolvedValue(
    ok({
      tenant: { tenant_id: 'eng' },
      principal: { email: 'a@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: true },
      environment: 'dev',
      environment_declared: false,
    } as unknown as Me),
  )
})
afterEach(() => cleanup())

async function rowOf(name: string): Promise<HTMLElement> {
  render(<AdminSettingsScreen />)
  await waitFor(() => expect(document.getElementById(`limit-${name}`)).not.toBeNull(), WAIT)
  return document.getElementById(`limit-${name}`)!
}

describe('G5-21: Pool limits shows the configured hard limit beside the effective one', () => {
  it('prints Pools’ `50 /100` in the Ceiling cell, the figure slot still the effective limit', async () => {
    const row = await rowOf(QUOTA_HELD)
    const cell = row.querySelector('td[data-label^="Ceiling"]')!
    // The figure slot stays the one number every other test reads.
    expect(cell.querySelector('.adm-ceiling')!.textContent).toBe('50')
    // MUTATION: drop the `/hard` span and this reads nothing.
    const was = cell.querySelector('.adm-was .cap-was')
    expect(was, 'the configured hard limit is not beside the effective one').not.toBeNull()
    expect(was!.textContent).toBe('/100')
    expect(was!.getAttribute('title')).toBe('Configured hard limit is 100')
    // A pool at its hard limit draws no `/N`, as on Pools -- but keeps the slot,
    // so `edit` starts at one x in every row.
    const plain = document.getElementById('limit-global')!.querySelector('td[data-label^="Ceiling"]')!
    expect(plain.querySelector('.adm-was')).not.toBeNull()
    expect(plain.querySelector('.cap-was')).toBeNull()
    expect(cascade(STYLES, plain.querySelector('.adm-was')!, 'width', WIDE).winner?.value).toMatch(/^\d+ch$/)
    expect(cascade(STYLES, plain.querySelector('.adm-was')!, 'display', WIDE).winner?.value).toBe('inline-block')
  })

  it('says the hard limit in units, and labels the field with what it prefills', async () => {
    const row = await rowOf(QUOTA_HELD)
    fireEvent.click(within(row).getByRole('button', { name: `Edit ceiling for ${QUOTA_HELD}` }))
    const side = document.querySelector<HTMLElement>('aside.adm-side')!
    const facts = [...side.querySelectorAll('.adm-side-facts dt')].map((dt) => [dt.textContent, dt.nextElementSibling?.textContent])
    expect(facts).toContainEqual(['Ceiling', '50 units'])
    // MUTATION: print the bare number again.
    expect(facts).toContainEqual(['Hard limit', '100 units'])
    const input = within(side).getByLabelText(`Hard limit for ${QUOTA_HELD}`) as HTMLInputElement
    expect(input.value).toBe('100')
    const label = side.querySelector(`label[for="${input.id}"]`)!
    expect(label.textContent).toBe('New hard limit (now 100)')
  })
})
