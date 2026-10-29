/**
 * POOL LIMITS, VISUAL QA PASS 2 (QA2-FIX-11, part of #2694; the sweep lane
 * QA2-ADMIN-SHELL on the fixture console, 2026-10-29).
 *
 * admin2-19  With -3 typed, Save went disabled and the bare range `0–100000`
 *            turned amber: nothing said the value was wrong or why. The line
 *            under the field says so, and the field points at it.
 * admin2-20  The row shows `eng`, but its Edit button and its editor's field
 *            were named `tenant:eng`, the raw key the visible text dropped.
 *            They are named from the row's label, with the kind beside it.
 * admin2-21  A link to a pool's row was the legacy hash; it is the path. On
 *            arrival the row was marked but focus stayed on the H1; the row
 *            takes focus.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import ADMIN from '../styles/admin.css?raw'
import SHELL from '../styles.css?raw'
import type { Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen, poolSpoken, rowHref } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

beforeEach(() => {
  api.loadCapacity.mockReset()
  api.setPoolLimit.mockReset()
  api.loadMe.mockReset()
})

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-29T10:00:00Z' }
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
    updated_at: '2026-10-29T10:00:00Z',
    ...over,
  }
}

function profile(pools: string[]): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools }
}

function capacity(): Capacity {
  return {
    pools: [
      pool('global', { hard_limit: 40, effective_limit: 40, available: 36 }),
      pool('tenant:eng'),
      pool('provider:anthropic:tenant:u-bogdan'),
    ],
    runner_profiles: { 'claude-code': profile(['global', 'tenant:eng']) },
    tenant_id: 'eng',
    generated_at: '2026-10-29T10:00:00Z',
  }
}

async function row(name: string): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(capacity()))
  render(<AdminSettingsScreen />)
  return waitFor(() => {
    const r = document.getElementById(`limit-${name}`)
    expect(r).not.toBeNull()
    return r as HTMLElement
  }, WAIT)
}

describe('admin2-20: a control is named as its row reads, not by its raw key', () => {
  it('builds the spoken name from the row label and its kind', () => {
    // MUTATION: return `name` from poolSpoken -- `tenant:eng` again.
    expect(poolSpoken('tenant:eng')).toBe('eng (workspace)')
    expect(poolSpoken('provider:anthropic:tenant:u-bogdan')).toBe('anthropic · u-bogdan')
    expect(poolSpoken('global')).toBe('global')
    expect(poolSpoken('resource:browser')).toBe('browser (resource class)')
    expect(poolSpoken('runner:browser')).toBe('browser (runner profile)')
  })

  it('names the Edit button and the editor field from the label', async () => {
    const r = await row('tenant:eng')
    // MUTATION: put `${pool.name}` back in the Edit button's aria-label.
    const edit = within(r).getByRole('button', { name: 'Edit ceiling for eng (workspace)' })
    fireEvent.click(edit)
    const side = document.querySelector<HTMLElement>('aside.adm-side')!
    // MUTATION: put `${pool.name}` back in the field's aria-label.
    expect(within(side).getByLabelText('Hard limit for eng (workspace)')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /tenant:eng/ })).toBeNull()
  })

  it('names a provider tenant pool by its two names', async () => {
    const r = await row('provider:anthropic:tenant:u-bogdan')
    fireEvent.click(within(r).getByRole('button', { name: 'Edit ceiling for anthropic · u-bogdan' }))
    const side = document.querySelector<HTMLElement>('aside.adm-side')!
    expect(within(side).getByLabelText('Hard limit for anthropic · u-bogdan')).toBeTruthy()
  })
})

describe('admin2-19: an out-of-range ceiling says what is wrong', () => {
  it('-3 reads `A ceiling is 0 to 100000 units.`, and the field is invalid and points at it', async () => {
    const r = await row('tenant:eng')
    fireEvent.click(within(r).getByRole('button', { name: /^Edit ceiling for eng/ }))
    const side = document.querySelector<HTMLElement>('aside.adm-side')!
    const field = within(side).getByLabelText('Hard limit for eng (workspace)') as HTMLInputElement
    fireEvent.change(field, { target: { value: '-3' } })
    expect(field.getAttribute('aria-invalid')).toBe('true')
    const id = field.getAttribute('aria-describedby')
    expect(id, 'the field points at no message').not.toBeNull()
    // MUTATION: draw the bare range `0–100000` as the message again.
    expect(document.getElementById(id!)?.textContent).toBe('A ceiling is 0 to 100000 units.')
    expect((within(side).getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('a fraction says the ceiling is a whole number', async () => {
    const r = await row('tenant:eng')
    fireEvent.click(within(r).getByRole('button', { name: /^Edit ceiling for eng/ }))
    const side = document.querySelector<HTMLElement>('aside.adm-side')!
    const field = within(side).getByLabelText('Hard limit for eng (workspace)') as HTMLInputElement
    fireEvent.change(field, { target: { value: '2.5' } })
    // MUTATION: one message for every invalid value -- `0 to 100000` for 2.5, which is in range.
    expect(document.getElementById(field.getAttribute('aria-describedby')!)?.textContent).toBe(
      'A ceiling is a whole number of units, 0 to 100000.',
    )
  })
})

describe('admin2-21: a link to a pool row is a path, and focus lands on the row', () => {
  it('writes the path, its colons encoded, not the legacy hash', () => {
    // MUTATION: return `#admin/limits?pool=${pool}` from rowHref.
    expect(rowHref('tenant:u-bogdan')).toBe('/admin/limits?pool=tenant%3Au-bogdan')
  })

  it('focuses the row the link named once it is drawn', async () => {
    window.history.replaceState(null, '', '/admin/limits?pool=tenant%3Aeng')
    const r = await row('tenant:eng')
    expect(r.classList.contains('is-target')).toBe(true)
    // MUTATION: drop `row.focus(...)` from the landing effect -- focus stays where it was.
    await waitFor(() => expect(document.activeElement).toBe(r), WAIT)
    expect(r.getAttribute('tabindex')).toBe('-1')
    expect(document.getElementById('limit-global')?.hasAttribute('tabindex')).toBe(false)
  })

  it('keeps the target outline while the row holds focus, over the shell\'s no-ring rule', () => {
    // The shell drops the ring on a focused `[id][tabindex='-1']` (0,4,0);
    // the row's own rule must outrank it and draw the is-target outline.
    expect(SHELL).toContain(".ctl-scroll [id][tabindex='-1']:focus")
    expect(SHELL).toMatch(/tr\.is-target \{ outline: 2px solid var\(--info\); outline-offset: -2px; \}/)
    // MUTATION: delete the `.ctl-scroll tr.is-target[id][tabindex='-1']:focus` rule from admin.css.
    expect(ADMIN).toMatch(
      /\.ctl-scroll tr\.is-target\[id\]\[tabindex='-1'\]:focus \{ outline: 2px solid var\(--info\); outline-offset: -2px; \}/,
    )
  })
})
