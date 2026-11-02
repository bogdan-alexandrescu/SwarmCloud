// VISUAL QA PASS 1, FIX LANE QA1-FIX-04 (part of #2596): Admin › Pool limits.
//
//   * admin-shell-03: the lead named the pool by its key, "Pool
//     provider:anthropic:tenant:u-bogdan is at 100%", and its button "Open
//     provider:anthropic:tenant:u-bogdan", over a row named `anthropic ·
//     u-bogdan`. It names the pool as the row does, the key as a mono line.
//   * admin-shell-13: the eng row's Set by read `AIMD back-off`, the broker's
//     algorithm, beside rows that said `configured`. It says what happened.
//   * admin-shell-14: global read 7 in use over Resource classes adding up to
//     5, and nothing said where the other 2 were.
//   * chrome-C23: Last changed listed `100 → 100`, a save that changed
//     nothing drawn as a change.
//
// The api module is replaced so the page renders from pools built here.
// Nothing here is token-shaped.

import SHEET from '../styles.css?raw'
import ADMIN from '../styles/admin.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AdminPool, Capacity, Pool, RunnerProfile } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { serve } from './repofixture'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { AdminSettingsScreen, familyNote } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const STYLES = `${SHEET}\n${ADMIN}`

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-28T10:00:00Z' }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active,
    available: Math.max(0, limit - active),
    enabled: true,
    updated_at: '2026-10-28T09:00:00Z',
    ...over,
  }
}

function profile(pools: string[]): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools }
}

function capacity(pools: Pool[], over: Partial<Capacity> = {}): Capacity {
  return {
    pools,
    runner_profiles: { 'claude-code': profile(pools.map((p) => p.name)) },
    tenant_id: 'u-bogdan',
    generated_at: '2026-10-28T10:00:00Z',
    ...over,
  }
}

const text = (el: Element) => (el.textContent ?? '').replace(/\s+/g, ' ').trim()

beforeEach(() => {
  serve(() => null)
  api.loadCapacity.mockReset()
  api.loadAdminPools.mockReset()
  api.loadMe.mockReset()
  api.loadAdminPools.mockResolvedValue(ok({ pools: [] }))
  api.loadMe.mockResolvedValue(undefined)
})
afterEach(() => cleanup())

async function screenOf(cap: Capacity): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(cap))
  render(<AdminSettingsScreen />)
  await waitFor(() => expect(document.getElementById(`limit-${cap.pools[0]!.name}`)).not.toBeNull(), WAIT)
}

// The fixture console's shape (api.ts `fixtureCapacity`): global 7, the
// provider-tenant pool at 5 of 5, Resource classes adding up to 5.
const FIXTURE = capacity([
  pool('global', 7, 22),
  pool('tenant:u-bogdan', 7, 40),
  pool('tenant:eng', 0, 10, { adaptive_target: 6, effective_limit: 6, available: 6 }),
  pool('provider:anthropic:tenant:u-bogdan', 5, 5),
  pool('resource:standard', 5, 20),
  pool('resource:browser', 0, 4),
  pool('runner:claude-code', 7, 10),
  pool('backend:CLOUD_RUN_JOB', 7, 20),
])

describe('admin-shell-03: the lead names the pool as its row does', () => {
  it('says "anthropic · u-bogdan" in the headline and the button, with the key only as a mono line', async () => {
    await screenOf(FIXTURE)
    const lead = await waitFor(() => document.querySelector<HTMLElement>('.adm-lead')!, WAIT)
    // MUTATION: title the lead with `lead.pool` again -> the key is the headline and this fails.
    expect(text(lead.querySelector('b')!)).toBe('Pool anthropic · u-bogdan is at 100% of its ceiling')
    // MUTATION: drop the sub-line -> the key is nowhere on the lead and this fails.
    const key = lead.querySelector<HTMLElement>('.ws-lead-s.id')
    expect(key?.textContent).toBe('provider:anthropic:tenant:u-bogdan')
    // The key is said once, on its own line, and nowhere else on the lead.
    expect(text(lead).split('provider:anthropic:tenant:u-bogdan')).toHaveLength(2)
    // MUTATION: label the button `Open ${lead.pool}` again -> fails.
    const button = within(lead).getByRole('button', { name: 'Open anthropic · u-bogdan' })
    fireEvent.click(button)
    expect(document.querySelector('aside.adm-side h3')?.textContent).toContain('anthropic · u-bogdan')
  })

  it('draws no key line where the label is the key (global)', async () => {
    await screenOf(capacity([pool('global', 46, 50)]))
    const lead = await waitFor(() => document.querySelector<HTMLElement>('.adm-lead')!, WAIT)
    expect(text(lead)).toContain('Pool global is at 92% of its ceiling')
    // MUTATION: draw the sub-line whatever the label -> "global" twice and this fails.
    expect(lead.querySelector('.ws-lead-s.id')).toBeNull()
  })
})

describe('admin-shell-13: Set by says the back-off in words', () => {
  it('reads "lowered automatically (back-off)" with the sentence why on its title, never "AIMD"', async () => {
    await screenOf(FIXTURE)
    const cell = document.querySelector<HTMLElement>('#limit-tenant\\:eng td[data-label="Set by"]')!
    // MUTATION: draw `setBy(pool).term` in the cell again -> "AIMD back-off" and this fails.
    expect(text(cell)).toBe('lowered automatically (back-off)')
    expect(cell.getAttribute('title')).toBe(
      'Lowered automatically to 6 after the provider rate-limited it (back-off); configured is 10. It climbs back as calls succeed.',
    )
    expect(cell.getAttribute('title')).not.toMatch(/AIMD/)
    expect(text(document.body)).not.toMatch(/AIMD/)
    // The other rows still say `configured`.
    expect(text(document.querySelector('#limit-global td[data-label="Set by"]')!)).toBe('configured')
  })

  it('wraps the words between spaces and never cuts `(back-off)` at its hyphen', async () => {
    await screenOf(FIXTURE)
    const cell = document.querySelector<HTMLElement>('#limit-tenant\\:eng td[data-label="Set by"]')!
    const won = (el: Element, prop: string) => cascade(STYLES, el, prop, WIDE).winner?.value ?? null
    // MUTATION: keep the cell `nowrap` with an ellipsis -> the words are cut at 123px and this fails.
    expect(won(cell, 'white-space')).toBe('normal')
    expect(won(cell, 'overflow-wrap')).toBe('normal')
    const run = cell.querySelector('.adm-setby-nb')!
    expect(run.textContent).toBe('(back-off)')
    // MUTATION: drop `.adm-setby-nb { white-space: nowrap }` -> fails.
    expect(won(run, 'white-space')).toBe('nowrap')
  })
})

describe('admin-shell-14: a family that does not add up to global says where the rest are', () => {
  it('draws one line under Resource classes: 5 of global\'s 7, 2 outside any class', async () => {
    await screenOf(FIXTURE)
    const family = (title: string) =>
      [...document.querySelectorAll<HTMLElement>('.adm-family')].find((f) => f.querySelector('h2')?.textContent === title)!
    // MUTATION: drop the note -> nothing says where the 2 units are and this fails.
    expect(family('Resource classes').querySelector('.adm-family-note')?.textContent).toBe(
      "These pools add up to 5 of global's 7 units in use: 2 are outside any class.",
    )
    // Workspaces add up (7 + 0): no line.
    expect(family('Workspaces').querySelector('.adm-family-note')).toBeNull()
    // Providers count one task twice and are never summed.
    expect(family('Providers').querySelector('.adm-family-note')).toBeNull()
  })

  it('says nothing when the listing was cut short, or when there is no global pool', () => {
    const pools = FIXTURE.pools
    const global = pools.find((p) => p.name === 'global')
    expect(familyNote('resource', pools, global, true)).toMatch(/2 are outside any class/)
    // MUTATION: ignore `pools_complete` -> a guess about pools nobody served and this fails.
    expect(familyNote('resource', pools, global, false)).toBeNull()
    expect(familyNote('resource', pools, undefined, true)).toBeNull()
    expect(familyNote('provider', pools, global, true)).toBeNull()
    expect(familyNote('runner', [pool('global', 3, 10), pool('runner:a', 2, 5)], pool('global', 3, 10), true)).toBe(
      "These pools add up to 2 of global's 3 units in use: 1 is outside any runner profile.",
    )
    expect(familyNote('backend', [pool('backend:a', 5, 5)], pool('global', 3, 10), true)).toBe(
      "These pools add up to 5 units in use, more than global's 3.",
    )
  })
})

describe('chrome-C23: a save that changed nothing is not drawn as a change', () => {
  it('reads "100, unchanged" under Last changed, never "100 → 100"', async () => {
    const at = new Date(Date.now() - 8 * 86_400_000).toISOString()
    const pools = [pool('global', 7, 100), pool('runner:claude-code', 2, 10)]
    const admin = (p: Pool, change: AdminPool['admin_change']): AdminPool => ({
      ...p,
      admin_changed_by: change === null ? null : 'ops@example.com',
      admin_changed_at: change === null ? null : at,
      admin_change: change,
    })
    api.loadAdminPools.mockResolvedValue(
      ok({ pools: [admin(pools[0]!, { hard_limit: { from: 100, to: 100 } }), admin(pools[1]!, { hard_limit: { from: 20, to: 10 } })] }),
    )
    await screenOf(capacity(pools))
    const cell = (name: string) => document.getElementById(`limit-${name}`)!.querySelector<HTMLElement>('td[data-label="Last changed"]')!
    await waitFor(() => expect(text(cell('global'))).toContain('ops@example.com'), WAIT)
    // MUTATION: draw `${from} → ${to}` whatever they are -> "100 → 100" and this fails.
    expect(text(cell('global'))).not.toContain('100 → 100')
    expect(text(cell('global'))).toContain('100, unchanged')
    expect(text(cell('global'))).toContain('8d ago')
    // A real change is still an arrow.
    expect(text(cell('runner:claude-code'))).toContain('20 → 10')
  })
})
