// POOL LIMITS #133: WHO CHANGED A CEILING, AND WHAT THEY CHANGED IT TO, READ
// FROM WHERE THE API SERVES IT.
//
// `admin_changed_by` / `admin_changed_at` / `admin_change` are served on the
// ADMIN pool read (`GET /v1/admin/pools`) and deliberately never on
// `/v1/capacity`, which every tenant member reads. The screen read only
// `/v1/capacity`, so every row said "not recorded" against an API that had the
// record all along. It now reads both and joins them by pool name.
//
//   * An admin sees `ops@saga.xyz · 20 → 10 · 3h ago` in Last changed and in
//     the side editor's History.
//   * A non-admin is refused the admin read: the row says the record is for
//     admins, not that the API has none.
//   * A ceiling written since that change by something that records nothing
//     (scripts/pool-limit.sh writes Firestore directly) is not credited to the
//     last admin: the record's `to` no longer matches, and the row says so.
//   * A save re-reads the record too, so the author of the change just made
//     is on the row once the editor closes.
//
// MUTATION: read `loadCapacity` alone and the first and last cases read "not
// recorded"; drop the `to` comparison and the third credits ops@ with 25.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AdminPool, Capacity, Pool, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

beforeEach(() => {
  api.loadCapacity.mockReset()
  api.loadAdminPools.mockReset()
  api.setPoolLimit.mockReset()
  api.loadMe.mockReset()
})
afterEach(() => {
  cleanup()
  window.location.hash = ''
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-02T10:00:00Z' }
}

function pool(name: string, hard: number, active = 2): Pool {
  return {
    name,
    hard_limit: hard,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: hard,
    active,
    available: hard - active,
    enabled: true,
    updated_at: '2026-10-02T10:00:00Z',
  }
}

function profile(pools: string[]): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools }
}

function capacity(pools: Pool[]): Capacity {
  return {
    pools,
    runner_profiles: { 'claude-code': profile(['global', 'runner:claude-code']) },
    tenant_id: 'eng',
    generated_at: '2026-10-02T10:00:00Z',
  }
}

function admin(p: Pool, by: string | null, at: string | null, change: AdminPool['admin_change']): AdminPool {
  return { ...p, admin_changed_by: by, admin_changed_at: at, admin_change: change }
}

const HOURS_AGO = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()

async function rows(cap: Capacity): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(cap))
  render(<AdminSettingsScreen />)
  await waitFor(() => expect(document.getElementById(`limit-${cap.pools[0]!.name}`)).not.toBeNull(), WAIT)
}

function lastChanged(name: string): HTMLElement {
  return document.getElementById(`limit-${name}`)!.querySelector<HTMLElement>('td[data-label="Last changed"]')!
}

function open(name: string): HTMLElement {
  const row = document.getElementById(`limit-${name}`)!
  fireEvent.click(within(row).getByRole('button', { name: `Edit ceiling for ${name}` }))
  return document.querySelector<HTMLElement>('aside.adm-side')!
}

describe('Pool limits reads who changed a ceiling from the admin pool read (#133)', () => {
  it('prints the admin, the change and the time, in the row and in History', async () => {
    const at = HOURS_AGO(3)
    const pools = [pool('global', 40), pool('runner:claude-code', 10)]
    api.loadAdminPools.mockResolvedValue(
      ok({
        pools: [
          admin(pools[0]!, null, null, null),
          admin(pools[1]!, 'ops@saga.xyz', at, { hard_limit: { from: 20, to: 10 } }),
        ],
      }),
    )
    await rows(capacity(pools))

    await waitFor(() => expect(lastChanged('runner:claude-code').textContent).toContain('ops@saga.xyz'), WAIT)
    const cell = lastChanged('runner:claude-code')
    expect(cell.textContent).toContain('20 → 10')
    expect(cell.textContent).toContain('3h ago')

    const side = open('runner:claude-code')
    const changed = [...side.querySelectorAll('.adm-changed')]
    expect(changed).toHaveLength(2)
    for (const c of changed) {
      expect(c.textContent).toContain('ops@saga.xyz')
      expect(c.textContent).toContain('20 → 10')
    }

    // A pool the admin read serves with no record says so, and says why.
    const none = lastChanged('global').querySelector('.adm-changed-none')!
    expect(none.getAttribute('title')).toMatch(/no admin has changed/i)
  })

  it('names a drain as the switch it flipped', async () => {
    const pools = [pool('resource:standard', 20)]
    pools[0]!.enabled = false
    api.loadAdminPools.mockResolvedValue(
      ok({ pools: [admin(pools[0]!, 'ops@saga.xyz', HOURS_AGO(1), { enabled: { from: true, to: false } })] }),
    )
    await rows(capacity(pools))
    await waitFor(() => expect(lastChanged('resource:standard').textContent).toContain('drained'), WAIT)
  })

  it('tells a non-admin the record is for admins, not that there is none', async () => {
    api.loadAdminPools.mockResolvedValue({
      status: 'error',
      error: { kind: 'admin_required', httpStatus: 403, code: 'admin_required', message: 'admin group membership is required' },
    })
    await rows(capacity([pool('global', 40)]))
    await waitFor(
      () => expect(lastChanged('global').querySelector('.adm-changed-none')?.getAttribute('title')).toMatch(/admins/i),
      WAIT,
    )
    expect(lastChanged('global').textContent).not.toContain('@')
  })

  it('does not credit the last admin with a ceiling written since by something that records nothing', async () => {
    const pools = [pool('global', 25)]
    api.loadAdminPools.mockResolvedValue(
      ok({ pools: [admin(pools[0]!, 'ops@saga.xyz', HOURS_AGO(5), { hard_limit: { from: 40, to: 30 } })] }),
    )
    await rows(capacity(pools))
    await waitFor(() => expect(lastChanged('global').querySelector('.adm-changed-since')).not.toBeNull(), WAIT)
    const since = lastChanged('global').querySelector<HTMLElement>('.adm-changed-since')!
    expect(since.getAttribute('title')).toMatch(/pool-limit\.sh/)
    expect(since.getAttribute('title')).toContain('30')
    expect(since.getAttribute('title')).toContain('25')
  })

  it('re-reads the record after a save, so the change just made is on the row', async () => {
    const before = [pool('global', 40), pool('runner:claude-code', 20)]
    const after = [pool('global', 40), pool('runner:claude-code', 15)]
    api.loadCapacity.mockResolvedValueOnce(ok(capacity(before))).mockResolvedValue(ok(capacity(after)))
    api.loadAdminPools
      .mockResolvedValueOnce(ok({ pools: before.map((p) => admin(p, null, null, null)) }))
      .mockResolvedValue(
        ok({
          pools: [
            admin(after[0]!, null, null, null),
            admin(after[1]!, 'root@saga.xyz', new Date().toISOString(), { hard_limit: { from: 20, to: 15 } }),
          ],
        }),
      )
    api.setPoolLimit.mockResolvedValue(ok({}))
    render(<AdminSettingsScreen />)
    await waitFor(() => expect(document.getElementById('limit-runner:claude-code')).not.toBeNull(), WAIT)

    const side = open('runner:claude-code')
    fireEvent.change(within(side).getByLabelText('Hard limit for runner:claude-code'), { target: { value: '15' } })
    fireEvent.click(within(side).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(lastChanged('runner:claude-code').textContent).toContain('root@saga.xyz'), WAIT)
    expect(lastChanged('runner:claude-code').textContent).toContain('20 → 15')
  })
})
