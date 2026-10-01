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
// `unset` operand in AdminSettings' `arithmetic`. The chip, the card's figure
// or the editor's field then falls back to `ok`, `0` or "null", and the case
// below that reads it fails by name.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
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
  return raw[0].closest('tr') as HTMLElement
}

describe('Capacity draws a pool with no limit set as that, not as ok or limit 0', () => {
  it('marks the row `no limit set` in the person-acts tone, and never `ok`', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<CapacityScreen />)
    const row = await capacityRow('tenant:eng')
    const chips = [...row.querySelectorAll('.ctl-chip')].map((c) => c.textContent?.trim())
    expect(chips).toContain('no limit set')
    expect(chips).not.toContain('ok')
    expect(chips).not.toContain('limit 0')
    expect(row.querySelector('.ctl-chip.is-paused')?.textContent).toContain('no limit set')
    expect(row.className).toContain('is-paused')
  })

  it('prints no number for the ceiling or the headroom', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<CapacityScreen />)
    const row = await capacityRow('tenant:eng')
    const ceiling = row.querySelector('td[data-label="Ceiling (units)"]')!.textContent ?? ''
    expect(ceiling).toBe('not set')
    expect(row.querySelector('td[data-label="Headroom"]')!.textContent).toBe('—')
  })

  it('says `no limit set` on the card too, with the not-measured track', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<CapacityScreen />)
    await capacityRow('tenant:eng')
    fireEvent.click(screen.getByRole('button', { name: 'Cards' }))
    const card = [...document.querySelectorAll('.cap-pool')].find(
      (c) => c.querySelector('.cap-pool-name')?.getAttribute('title') === 'tenant:eng',
    ) as HTMLElement
    expect(card).toBeDefined()
    expect(card.querySelector('.cap-pool-figure')!.textContent).toContain('no limit set')
    expect(card.querySelector('.cap-pool-figure')!.textContent).not.toContain('/ 0')
    expect(card.querySelector('.ctl-util-track.is-unknown')).not.toBeNull()
    expect(within(card).getByText('no limit set', { selector: '.ctl-chip' })).toBeDefined()
  })
})

describe('Pool limits draws a pool with no limit set as that, not as 0 agents', () => {
  it('says the profile has no limit set rather than computing a ceiling', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    const title = await screen.findByText('claude-code', { selector: '.ctl-card-title' }, WAIT)
    const card = title.closest('.ctl-card') as HTMLElement
    expect(card.querySelector('.ctl-figure')!.textContent).toContain('no limit set')
    expect(card.querySelector('.ctl-figure')!.textContent).not.toMatch(/\d/)
    const operand = card.querySelector('li[title="tenant:eng"]') as HTMLElement
    expect(operand.className).toContain('is-binding')
    expect(operand.querySelector('.adm-value')!.textContent).toBe('no limit set')
    expect(card.querySelector('.ctl-card-foot')!.textContent).toContain('no limit set')
  })

  it('opens the editor on an empty field, not "null", and will not save an empty field as 0', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await screen.findByText('claude-code', { selector: '.ctl-card-title' }, WAIT)
    const row = document.getElementById('limit-tenant:eng') as HTMLElement
    expect(row.querySelector('.adm-ceiling')!.textContent).toBe('no limit set')
    fireEvent.click(within(row).getByRole('button', { name: 'Edit ceiling for tenant:eng' }))
    const field = screen.getByLabelText('Hard limit for tenant:eng', { exact: false }) as HTMLInputElement
    expect(field.value).toBe('')
    expect((within(row).getByRole('button', { name: /save/i }) as HTMLButtonElement).disabled).toBe(true)
  })
})
