// A POOL WITH NO LIMIT SET, ON THE POOL SCREENS (#374).
//
// `pool_to_api` serves `hard_limit`, `effective_limit` and `available` as null
// for a pool document that never had a `hard_limit`. Before this the pool
// screens only knew numbers: on Capacity a null limit is neither `=== 0` nor
// `> 0`, so the pool drew no `limit 0` chip and a healthy `ok` beside a ceiling
// cell with nothing in it -- a pool that admits nothing, looking idle. Pool
// limits divided null by a weight and printed `0` agents, and filled the
// edit field with the text "null".
//
// MUTATION: drop the `unset` branch in Capacity's `classifyPool`, or the
// editor's empty-field handling in AdminSettings. The chip, the ceiling cell
// or the editor's field then falls back to `ok`, `0` or "null", and the case
// below that reads it fails by name. (The per-profile card that drew `no limit
// set` as a profile's ceiling was removed, owner decision 2026-10-01.)
//
// The pool card and the Headroom column this file once also read were removed
// with the Capacity redesign; the table row's Ceiling and Use cells carry the
// same facts now.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { CapacityScreen } = await import('../Capacity')
const { AdminSettingsScreen } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

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
    active: 0,
    available: 10,
    enabled: true,
    updated_at: '2026-10-01T10:00:00Z',
    ...over,
  }
}

/** `tenant:eng` exactly as `pool_to_api` serves a document with no `hard_limit`. */
const UNSET = pool('tenant:eng', { hard_limit: null, effective_limit: null, available: null })

function profile(pools: string[]): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools }
}

function capacity(): Capacity {
  return {
    pools: [pool('global', { hard_limit: 50, effective_limit: 50, available: 50 }), UNSET],
    runner_profiles: { 'claude-code': profile(['global', 'tenant:eng']) },
    tenant_id: 'eng',
    generated_at: '2026-10-01T10:00:00Z',
  }
}

async function capacityRow(name: string): Promise<HTMLElement> {
  const raw = await screen.findAllByText(name, { selector: '.ctl-sub' }, WAIT)
  const row = raw[0]?.closest('tr')
  if (!row) throw new Error(`no capacity row for ${name}`)
  return row
}

describe('Capacity draws a pool with no limit set as that, not as ok or limit 0', () => {
  it('marks the row `no limit set` in the person-acts tone, and never `ok`', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<CapacityScreen />)
    const row = await capacityRow('tenant:eng')
    const chips = [...row.querySelectorAll('.sk-st')].map((c) => c.textContent?.trim())
    expect(chips).toContain('no limit set')
    expect(chips).not.toContain('ok')
    expect(chips).not.toContain('limit 0')
    expect(row.querySelector('.sk-st[data-tone="paused"]')?.textContent).toContain('no limit set')
    expect(row.className).toContain('is-paused')
  })

  it('prints no number for the ceiling, and a not-measured track', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<CapacityScreen />)
    const row = await capacityRow('tenant:eng')
    const ceiling = row.querySelector('td[data-label="Ceiling (units)"]')!.textContent ?? ''
    expect(ceiling).toBe('no limit set')
    expect(ceiling).not.toMatch(/\d/)
    expect(row.querySelector('.cap-use-pct')!.textContent).toBe('no limit set')
    expect(row.querySelector('.ctl-util-track.is-unknown')).not.toBeNull()
  })
})

describe('Pool limits draws a pool with no limit set as that, not as 0 agents', () => {
  it('opens the editor on an empty field, not "null", and will not save an empty field as 0', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    const row = await waitFor(() => {
      const el = document.getElementById('limit-tenant:eng')
      expect(el).not.toBeNull()
      return el as HTMLElement
    }, WAIT)
    // The figure's slot holds a dash with its reason, and the words follow
    // `edit` so they cannot widen the slot and move it (#503). Never a 0.
    const slot = row.querySelector('.adm-ceiling')!
    expect(slot.textContent).toBe('—')
    expect(slot.getAttribute('aria-label') ?? '').toMatch(/^No limit set/)
    expect(row.querySelector('.adm-unset')!.textContent).toBe('no limit set')
    expect(row.querySelector('td[data-label="Ceiling (units)"]')!.textContent ?? '').not.toMatch(/\d/)
    fireEvent.click(row.querySelector('button[aria-label^="Edit ceiling for tenant:eng"]') as HTMLElement)
    const field = (await screen.findByLabelText('Hard limit for tenant:eng', { exact: false })) as HTMLInputElement
    expect(field.value).toBe('')
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
