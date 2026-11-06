// CONSOLE VISUAL QA PASS 2, LANE QA2-FIX-14 (2026-10-29, part of #2694):
// Capacity › Pools, as the fixture console drew it on main.
//
//   capacity-02  `standard` at 5 of 20 read `0 holders`: the counter holds 5
//                units and no lease row names the pool. The link now says
//                `no lease rows`; a pool nothing holds still reads `0 holders`.
//   capacity-11  The head read `1 full · 2 lowered · 1 paused · 1 too small`
//                (5) over 4 pools: `eng`, paused and lowered, was counted
//                twice. Each pool is counted once, by its worst mark.
//   capacity-12  The compact phone row read `0 /6 /10`. It reads `0 of 6
//                (lowered from 10)`: the configured limit's `/` is its own
//                element, and the phone row's sheet words the figure.
//
// MUTATION: give `HoldersLink` back its bare `${n} holders` -- capacity-02
// fails; count `lowered` and `paused` with their own filters again --
// capacity-11 fails; put `/{pool.hard_limit}` back as one text node, or the
// phone row's `content: '/'` -- capacity-12 fails.

import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, LeasePage, Pool } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn(), loadLeases: vi.fn(), loadResourceClasses: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')

const WAIT = { timeout: 5000 } as const
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }
const hosts: HTMLElement[] = []

afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-29T10:00:00Z' }
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
    updated_at: '2026-10-29T10:00:00Z',
    ...over,
  }
}

// The fixture console's states: one full pool, one paused AND lowered, one a
// provider quota closed at 0 under a configured 5, and one holding 5 units no
// lease row names.
const POOLS: Pool[] = [
  pool('global', 1, 40),
  pool('tenant:eng', 0, 10, { effective_limit: 6, adaptive_target: 6, enabled: false }),
  pool('provider:anthropic:tenant:u-bogdan', 5, 5),
  pool('provider:openai:tenant:eng', 0, 5, { effective_limit: 0, quota_derived_limit: 0 }),
  pool('resource:standard', 5, 20),
]

function capacity(): Capacity {
  return { generated_at: '2026-10-29T10:00:00Z', tenant_id: 'u-bogdan', pools: POOLS, pools_complete: true } as unknown as Capacity
}

function leases(): LeasePage {
  return {
    leases: [{ lease_id: 'l1', task_id: 'task_1', pools: ['global'], units: 1, released: false }],
    thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
    evaluated_at: '2026-10-29T10:00:00Z',
    active_only: true,
    tenant_id: null,
    units_held: 1,
    active_beyond_window: 0,
    truncated: false,
    examined: 1,
  } as unknown as LeasePage
}

async function mount(): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(ok(capacity()))
  api.loadLeases.mockResolvedValue(ok(leases()))
  api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: {} }))
  const { container } = render(<CapacityScreen />)
  await waitFor(() => expect(document.getElementById('pool-resource:standard')).not.toBeNull(), WAIT)
  return container
}

function row(container: HTMLElement, name: string): HTMLElement {
  const th = [...container.querySelectorAll<HTMLElement>('tr > th[scope="row"]')].find(
    (h) => h.querySelector('.ctl-sub')?.textContent === name,
  )
  if (th === undefined) throw new Error(`no row for ${name}`)
  return th.closest('tr') as HTMLElement
}

describe('capacity-02: a pool holding units no lease row names', () => {
  it('says `no lease rows`, never `0 holders`, beside 5 leased units', async () => {
    const c = await mount()
    const links = within(row(c, 'resource:standard')).getByRole('cell', { name: /no lease rows/ })
    expect(links.textContent).not.toMatch(/0 holders/)
    const a = within(links).getByRole('link', { name: 'no lease rows' })
    expect(a.getAttribute('title')).toMatch(/counter holds 5 units.*no unreleased lease names this pool/)
    // A pool the lease rows do name keeps its count, and an idle one its 0.
    expect(within(row(c, 'global')).getByRole('link', { name: '1 holder' })).toBeTruthy()
    expect(within(row(c, 'provider:openai:tenant:eng')).getByRole('link', { name: '0 holders' })).toBeTruthy()
  })
})

describe('capacity-11: the head counts each pool once', () => {
  it('counts `eng`, paused and lowered, as paused only, and the closed quota as limit 0', async () => {
    const c = await mount()
    await waitFor(() => expect(c.textContent).toMatch(/5 pools in \d famil/), WAIT)
    const head = [...c.querySelectorAll('.cap-clause')].map((e) => e.textContent)
    expect(head).toContain('1 full')
    expect(head).toContain('1 paused')
    expect(head).toContain('1 limit 0')
    expect(head.some((t) => /lowered/.test(t ?? ''))).toBe(false)
    // The clauses add up to the pools that carry a mark other than `ok`.
    const sum = head
      .map((t) => /^(\d+) (?!pools)/.exec(t ?? '')?.[1])
      .filter((n): n is string => n !== undefined)
      .reduce((a, n) => a + Number(n), 0)
    expect(sum).toBe(3)
  })
})

describe('capacity-12: the compact row labels the configured limit', () => {
  it('draws the configured limit `/` as its own element, so the phone row can drop it', async () => {
    const c = await mount()
    const was = row(c, 'tenant:eng').querySelector('.cap-was')
    expect(was?.textContent).toBe('/10')
    expect(was?.querySelector('.cap-was-sl')?.textContent).toBe('/')
    // The wide table's figure is unchanged: `6/10`.
    expect(row(c, 'tenant:eng').querySelector('.cap-ceil')?.textContent).toBe('6/10')
    expect(screen.getAllByText('6').length).toBeGreaterThan(0)
  })

  it('words the phone row `of 6 (lowered from 10)`, not `/6 /10`', () => {
    const ceil = build('div.ctl-table.cap-pools.is-stacked > table > tbody > tr > td', hosts)
    ceil.setAttribute('data-label', 'Ceiling (units)')
    expect(painted(ceil, 'content', PHONE, 'before')).toBe("'of '")
    const was = document.createElement('span')
    was.className = 'cap-was'
    ceil.appendChild(was)
    const slash = document.createElement('span')
    slash.className = 'cap-was-sl'
    was.appendChild(slash)
    expect(painted(slash, 'display', PHONE)).toBe('none')
    // MUTATION: drop the leading space from `.cap-was::before` and this goes
    // red: the row read `of 6(lowered from 10)`.
    expect(painted(was, 'content', PHONE, 'before')).toBe("' (lowered from '")
    expect(painted(was, 'content', PHONE, 'after')).toBe("')'")
    // The wide table keeps the frame's `6/10`.
    expect(painted(slash, 'display', WIDE)).not.toBe('none')
    expect(painted(was, 'content', WIDE, 'before')).toBeNull()
  })
})
