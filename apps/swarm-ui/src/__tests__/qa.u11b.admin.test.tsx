/**
 * BROWSER QA U11b N19 (owner, 2026-10-04; live console at main 69416faf,
 * 1440x900): /admin/limits.
 *
 *   - The side editor's heading read "global / global": the label and the
 *     pool name under it are the same word for `global`. The name is drawn
 *     under the label only when it says something the label does not.
 *   - With the editor open the families column is ~660px, and "In use
 *     (units)" wrapped in its 12% (79px) column. The column holds its head
 *     on one line at that width, and the head does not wrap.
 *
 * MUTATIONS: draw the sub-name unconditionally, put the In use column back to
 * 12%, or let its head wrap -- each turns a case red.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile } from '../types'
import { painted } from './marks'
import { cellStyle, lengthPx, textPx } from './tablefit'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn(), setPoolLimit: vi.fn(), loadMe: vi.fn() }))
vi.mock('../api', () => api)

const { AdminSettingsScreen } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const
const WIDE = { width: 1440 }
/** The families column at 1440 with the editor open (styles/admin.css). */
const FAMILIES_OPEN = 660

beforeEach(() => {
  api.loadCapacity.mockReset()
  api.loadMe.mockReset()
  document.body.innerHTML = ''
})

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-01T10:00:00Z' })
const pool = (name: string, over: Partial<Pool> = {}): Pool => ({
  name, hard_limit: 10, adaptive_target: null, quota_derived_limit: null, effective_limit: 10, active: 4, available: 6, enabled: true,
  updated_at: '2026-10-01T10:00:00Z', ...over,
})
const profile = (pools: string[]): RunnerProfile => ({ resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools })
const CAP: Capacity = {
  pools: [pool('global', { hard_limit: 40, effective_limit: 40 }), pool('tenant:eng')],
  runner_profiles: { 'claude-code': profile(['global', 'tenant:eng']) },
  tenant_id: 'eng',
  generated_at: '2026-10-01T10:00:00Z',
}

async function open(name: string): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(CAP))
  api.loadMe.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  render(<AdminSettingsScreen />)
  const row = await waitFor(() => {
    const r = document.getElementById(`limit-${name}`)
    expect(r).not.toBeNull()
    return r as HTMLElement
  }, WAIT)
  fireEvent.click(within(row).getByRole('button', { name: `Edit ceiling for ${name}` }))
  return document.querySelector<HTMLElement>('aside.adm-side')!
}

describe('N19: the limits editor', () => {
  it('titles the global pool once', async () => {
    const side = await open('global')
    const h3 = side.querySelector('h3.adm-side-title')!
    expect((h3.textContent ?? '').trim()).toBe('global')
    expect(h3.querySelector('.ctl-sub')).toBeNull()
  })

  it('keeps the pool name under a label that shortens it', async () => {
    const side = await open('tenant:eng')
    const h3 = side.querySelector('h3.adm-side-title')!
    expect(h3.querySelector('.ctl-sub')?.textContent).toBe('tenant:eng')
  })

  it('holds "In use (units)" on one line beside the open editor', async () => {
    await open('global')
    const table = document.querySelector<HTMLTableElement>('table.adm-limits')!
    const col = table.querySelector('col.adm-col-use')!
    const th = [...table.querySelectorAll('thead th')].find((h) => (h.textContent ?? '').includes('In use'))!
    const width = lengthPx(painted(col, 'width', WIDE), FAMILIES_OPEN)!
    const st = cellStyle(th, WIDE)
    expect(painted(th, 'white-space', WIDE)).toBe('nowrap')
    expect(width - st.pl - st.pr).toBeGreaterThanOrEqual(textPx('In use (units)', th, WIDE))
    // The five columns still fit the open column.
    const total = [...table.querySelectorAll('col')].reduce((n, c) => n + (lengthPx(painted(c, 'width', WIDE), FAMILIES_OPEN) ?? 0), 0)
    expect(total).toBeLessThanOrEqual(FAMILIES_OPEN)
  })
})
