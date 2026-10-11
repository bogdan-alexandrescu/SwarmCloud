// POOL LIMITS: ONE SET OF COLUMNS IN EVERY FAMILY, AND THE L2 GUARDS (#503).
//
// Measured at 1440 for #503: in Providers the extra `Set by` column pushed
// `edit` to x≈1068 while every other family table had it at x≈828, and no
// table had L2's `Last changed` column. jsdom does no layout, so the x is
// pinned through what decides it: the same head, the same colgroup, the
// control in the same column, and the fixed-layout widths in the Admin sheet.
//
// Also here, from the same audit: the typed confirmation for a drastic change
// lives in the side editor and no shortcut skips it (frame adm1), and a
// non-admin reads every ceiling of every family with Edit locked (adm2).
//
// MUTATION: draw `Set by` only where it says something again and the first
// case goes red; print `updated_at` as Last changed and the second does; drop
// `table-layout: fixed` from `.adm-limits` and the third does.

import SHEET from '../styles.css?raw'
import ADMIN from '../styles/admin.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Me, Pool, RunnerProfile } from '../types'
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
const PHONE: CascadeEnv = { width: 390 }

beforeEach(() => {
  api.loadCapacity.mockReset()
  api.setPoolLimit.mockReset()
  api.loadMe.mockReset()
})
afterEach(() => {
  cleanup()
  window.localStorage.clear()
  delete (window as unknown as Record<string, unknown>).SWARM_ASSUME_YES
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-02T10:00:00Z' }
}

function pool(name: string, over: Partial<Pool> & Record<string, unknown> = {}): Pool {
  return {
    name,
    hard_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 10,
    active: 4,
    available: 6,
    enabled: true,
    updated_at: '2026-10-02T09:59:00Z',
    ...over,
  } as Pool
}

function profile(pools: string[], units = 1): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units, pools }
}

/**
 * One pool in every family, and the Providers pool held by quota -- the case
 * that drew a `Set by` column in Providers alone.
 */
function everyFamily(): Capacity {
  return {
    pools: [
      pool('global', { hard_limit: 40, effective_limit: 40, available: 36 }),
      pool('tenant:eng', { hard_limit: 20, effective_limit: 20, active: 14, available: 6 }),
      pool('resource:browser', { hard_limit: 4, effective_limit: 4, active: 4, available: 0 }),
      pool('runner:claude-code', { hard_limit: 20, effective_limit: 20, available: 16 }),
      pool('backend:cloudrun', { hard_limit: 30, effective_limit: 30, available: 26 }),
      pool('provider:anthropic', { hard_limit: 30, quota_derived_limit: 22, effective_limit: 22, active: 22, available: 0 }),
    ],
    runner_profiles: {
      'claude-code': profile(['global', 'tenant:eng', 'runner:claude-code', 'backend:cloudrun', 'provider:anthropic']),
      browser: profile(['global', 'tenant:eng', 'resource:browser']),
    },
    tenant_id: 'eng',
    generated_at: '2026-10-02T10:00:00Z',
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

async function drawn(cap: Capacity = everyFamily()): Promise<HTMLTableElement[]> {
  api.loadCapacity.mockResolvedValue(ok(cap))
  render(<AdminSettingsScreen />)
  await waitFor(() => expect(document.getElementById(`limit-${cap.pools.at(-1)!.name}`)).not.toBeNull(), WAIT)
  return [...document.querySelectorAll<HTMLTableElement>('.adm-family table')]
}

function won(el: Element, prop: string | readonly string[], env: CascadeEnv): string | null {
  const r = cascade(STYLES, el, prop, env)
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

function open(name: string): HTMLElement {
  const row = document.getElementById(`limit-${name}`)!
  fireEvent.click(within(row).getByRole('button', { name: `Edit ceiling for ${name}` }))
  const side = document.querySelector<HTMLElement>('aside.adm-side')
  expect(side, 'edit opened no side editor').not.toBeNull()
  return side!
}

describe('every family table has the same columns, and edit in the same one (#503)', () => {
  it('draws one head, one colgroup and the edit control at one column index in all six', async () => {
    const tables = await drawn()
    expect(tables).toHaveLength(6)
    const heads = tables.map((t) => [...t.querySelectorAll('thead th')].map((th) => th.textContent))
    // L2's columns, in L2's order, with Last changed.
    expect(heads[0]).toEqual(['Pool', 'In use (units)', 'Ceiling (units)', 'Set by', 'Last changed'])
    for (const h of heads) expect(h, 'a family draws different columns').toEqual(heads[0])

    const cols = tables.map((t) => [...t.querySelectorAll(':scope > colgroup > col')].map((c) => c.className))
    for (const c of cols) expect(c, 'a family sizes different columns').toEqual(cols[0])
    expect(cols[0]).toHaveLength(heads[0]!.length)

    for (const t of tables) {
      for (const tr of t.querySelectorAll('tbody tr')) {
        const cells = [...tr.children]
        expect(cells, 'a row with a different number of cells than its head').toHaveLength(heads[0]!.length)
        const at = cells.findIndex((c) => c.querySelector('button[aria-label^="Edit ceiling for"]') !== null)
        expect(at, `${tr.id}: edit is not in the Ceiling column`).toBe(2)
      }
    }
  })

  it('reads the pool\'s last admin write in Last changed, and a dash with its reason when unknown', async () => {
    // The admin pool read answered and holds no record for these pools: the
    // "nobody has changed it" case, which alone is the dash (VQA V078).
    api.loadAdminPools.mockResolvedValue({ status: 'ok', data: { pools: [] }, fetchedAt: Date.now() })
    const at = new Date(Date.now() - 2 * 3_600_000).toISOString()
    const cap = everyFamily()
    cap.pools[1] = pool('tenant:eng', {
      hard_limit: 20,
      effective_limit: 20,
      active: 14,
      admin_changed_by: 'priya@saga.xyz',
      admin_changed_at: at,
    })
    await drawn(cap)
    const known = document.getElementById('limit-tenant:eng')!.querySelector('td[data-label="Last changed"]')!
    expect(known.textContent).toBe('priya@saga.xyz · 2h ago')
    expect(known.querySelector('time')?.getAttribute('dateTime')).toBe(at)

    const unknown = document.getElementById('limit-global')!.querySelector('td[data-label="Last changed"]')!
    expect(unknown.textContent).toBe('—')
    const dash = unknown.querySelector('.adm-changed-none')!
    expect(dash.getAttribute('title') ?? '').toContain('admin_changed_at')
    // Never `updated_at`, which admission moves on every lease.
    expect(document.body.textContent ?? '').not.toContain('2026-10-02')
  })

  it('fixes the widths from the colgroup in the Admin sheet above 900px, and leaves the phone to scroll', async () => {
    const [table] = await drawn()
    // MUTATION: drop `table-layout: fixed` from `.adm-limits` in admin.css.
    expect(won(table!, 'table-layout', WIDE)).toBe('fixed')
    expect(won(table!, 'width', WIDE)).toBe('100%')
    const widths = [...table!.querySelectorAll(':scope > colgroup > col')].map((c) => won(c, 'width', WIDE) ?? '')
    for (const w of widths) expect(w, 'a column with no width').toMatch(/^\d+%$/)
    // A set of widths over 100% would overflow the panel at 1440.
    expect(widths.reduce((n, w) => n + parseFloat(w), 0)).toBe(100)
    // The figure's slot is a fixed width, so `edit` after it starts at one x.
    const slot = document.getElementById('limit-global')!.querySelector('.adm-ceiling')!
    expect(won(slot, 'width', WIDE)).toMatch(/^\d+ch$/)
    expect(won(slot, 'display', WIDE)).toBe('inline-block')
    // At 390 the table is the scrolling one Capacity's families draw; no col
    // width here fights it.
    for (const col of table!.querySelectorAll(':scope > colgroup > col')) expect(won(col, 'width', PHONE)).toBeNull()
    expect(ADMIN).toMatch(/table\.adm-limits\s*\{[^}]*table-layout:\s*fixed/)
  })
})

describe('a drastic change asks for the pool name inside the side editor (adm1)', () => {
  it('setting resource:browser to 0 asks for the name in the editor, and writes only once it is typed', async () => {
    api.setPoolLimit.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    await drawn()
    const side = open('resource:browser')
    fireEvent.change(within(side).getByLabelText('Hard limit for resource:browser'), { target: { value: '0' } })
    const field = within(side).getByLabelText('Type resource:browser to confirm')
    expect(field.closest('aside'), 'the confirmation is not in the side editor').toBe(side)
    const go = within(side).getByRole('button', { name: 'Set to 0' }) as HTMLButtonElement
    expect(go.disabled).toBe(true)
    // Case and padding are not the name.
    for (const near of ['RESOURCE:BROWSER', ' resource:browser', 'resource:brow']) {
      fireEvent.change(field, { target: { value: near } })
      expect(go.disabled, `"${near}" confirmed`).toBe(true)
    }
    fireEvent.change(field, { target: { value: 'resource:browser' } })
    expect(go.disabled).toBe(false)
    fireEvent.click(go)
    await waitFor(() => expect(api.setPoolLimit).toHaveBeenCalledWith('resource:browser', 0), WAIT)
  })

  it('ignores any assume-yes shortcut: a stored flag, a window flag, or Enter in the field', async () => {
    window.localStorage.setItem('SWARM_ASSUME_YES', '1')
    ;(window as unknown as Record<string, unknown>).SWARM_ASSUME_YES = true
    await drawn()
    const side = open('tenant:eng')
    const input = within(side).getByLabelText('Hard limit for tenant:eng')
    fireEvent.change(input, { target: { value: '10' } })
    expect(within(side).getByLabelText('Type tenant:eng to confirm'), 'a cut of half asked for nothing').toBeTruthy()
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(within(side).getByRole('button', { name: 'Set to 10' }))
    expect(api.setPoolLimit).not.toHaveBeenCalled()
  })
})

describe('a non-admin reads every ceiling in every family, Edit locked, one line why (adm2)', () => {
  it('keeps each figure on screen, locks each Edit, and says why once', async () => {
    api.loadMe.mockResolvedValue(session(false))
    await drawn()
    await screen.findByText(/Admins only/, undefined, WAIT)
    const rows = [...document.querySelectorAll('.adm-family tbody tr')]
    expect(rows).toHaveLength(6)
    for (const tr of rows) {
      expect(tr.querySelector('.adm-ceiling')?.textContent, `${tr.id} hides its ceiling`).toMatch(/^\d+$/)
      const edit = tr.querySelector<HTMLButtonElement>('button[aria-label^="Edit ceiling for"]')!
      expect(edit.disabled, `${tr.id} can be edited by a non-admin`).toBe(true)
    }
    expect(screen.getAllByText(/Admins only/)).toHaveLength(1)
    expect(document.querySelector('aside.adm-side')).toBeNull()
  })
})
