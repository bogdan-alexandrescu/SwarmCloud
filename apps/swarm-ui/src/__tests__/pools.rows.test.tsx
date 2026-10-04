// THE POOLS TABLE, AT 3AM (#125).
//
// Scan twenty pools for the one that is full, and from that row reach who holds
// it and where its ceiling is set. Already true before this file, and pinned
// here so it stays true: every row draws a utilisation track and a `%` cell
// (`.cap-use`), and `Set by` is blank where the configured limit is what
// applies. What this file added:
//
//   * ABNORMAL ROWS FIRST, inside each family: a full pool is at the top of
//     its family, then the rest by used/ceiling, highest first -- not by name.
//   * THE POOL NAME LINKS TO HOLDERS FILTERED TO THAT POOL
//     (`#capacity/holders?pool=<name>`), and `limit` to that pool's Pool
//     limits row (`#admin/limits?pool=<name>`), which #134 already lands on.
//   * A ROW A LINK NAMED (`#capacity/pools?pool=<name>`, from Provider quota)
//     is outlined, the way Pool limits outlines its linked row.
//
// BREAK IT: sort a family by name again -- `runner:zeta` (full) drops below
// `runner:alpha`. Or drop `?pool=` from the holders link -- the href no
// longer names the pool. Or keep the router stripping `?pool=` off
// `capacity/pools` / `capacity/holders` -- `canonical` loses it.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool } from '../types'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { canonical, fromHash } = await import('../App')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-02T10:00:00Z' }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-02T10:00:00Z', ...over,
  }
}

function board(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-02T10:00:00Z',
    pools: [
      pool('global', 10, 40),
      pool('runner:alpha', 1, 10),
      pool('runner:beta', 7, 10),
      pool('runner:zeta', 10, 10),
      pool('runner:gamma', 3, 10),
    ],
    runner_profiles: {},
  }
}

async function renderPools(data: Capacity = board()): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(data))
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

/** The pool names a family table draws, in order. */
function familyOrder(title: string): string[] {
  const card = [...document.querySelectorAll('.cap-families .ctl-card')].find(
    (c) => c.querySelector('.ctl-card-title')?.textContent === title,
  )
  expect(card, `no ${title} family`).toBeTruthy()
  return [...card!.querySelectorAll('tbody th[title]')].map((th) => th.getAttribute('title') ?? '?')
}

function rowOf(name: string): HTMLElement {
  const th = [...document.querySelectorAll('.cap-families tbody th[title]')].find((t) => t.getAttribute('title') === name)
  expect(th, `no row for ${name}`).toBeTruthy()
  return th!.closest('tr') as HTMLElement
}

describe('Pools puts the abnormal row first and measures every row (#125)', () => {
  it('sorts a family full first, then by used/ceiling, never by name', async () => {
    await renderPools()
    expect(familyOrder('Runner profiles')).toEqual(['runner:zeta', 'runner:beta', 'runner:gamma', 'runner:alpha'])
  })

  it('draws a utilisation track and a % on every row, and Set by on every row', async () => {
    const data = board()
    data.pools.push(pool('tenant:eng', 2, 4, { hard_limit: 10, adaptive_target: 4 }))
    await renderPools(data)
    for (const name of ['global', 'runner:zeta', 'tenant:eng']) {
      const r = rowOf(name)
      expect(r.querySelector('.cap-use .ctl-util-track'), `${name} draws no track`).not.toBeNull()
      expect(r.querySelector('.cap-use-pct')!.textContent).toMatch(/%$/)
    }
    expect(rowOf('runner:zeta').querySelector('.cap-use-pct')!.textContent).toBe('100%')
    // Never blank since browser QA D32 (2026-10-04): the configured case says so, faint.
    expect(rowOf('global').querySelector('td[data-label="Set by"]')!.textContent).toBe('configured')
    expect(rowOf('tenant:eng').querySelector('td[data-label="Set by"]')!.textContent).toBe('AIMD back-off')
  })
})

describe('a Pools row is one click from its holders and its limit (#125)', () => {
  it('links the pool name to Holders filtered by that pool', async () => {
    await renderPools()
    const link = rowOf('runner:zeta').querySelector('th a')
    expect(link, 'the pool name is not a link').not.toBeNull()
    expect(link!.getAttribute('href')).toBe(`#capacity/holders?pool=${encodeURIComponent('runner:zeta')}`)
  })

  it('links limit to that pool\'s Pool limits row', async () => {
    await renderPools()
    const links = [...rowOf('runner:zeta').querySelectorAll('td.cap-links a')]
    const limit = links.find((a) => a.textContent === 'limit')
    expect(limit).toBeTruthy()
    expect(limit!.getAttribute('href')).toBe(`#admin/limits?pool=${encodeURIComponent('runner:zeta')}`)
    // `N holders` when the lease read could count them, `holders` when not.
    const holders = links.find((a) => /holders?$/.test(a.textContent ?? ''))
    expect(holders!.getAttribute('href')).toBe(`#capacity/holders?pool=${encodeURIComponent('runner:zeta')}`)
  })

  it('outlines the row a link named, and only that row', async () => {
    window.history.replaceState(null, '', `/capacity/pools?pool=${encodeURIComponent('runner:beta')}`)
    await renderPools()
    await waitFor(() => expect(document.querySelectorAll('.cap-families tr.is-target').length).toBe(1), WAIT)
    expect(document.querySelector('.cap-families tr.is-target th')!.getAttribute('title')).toBe('runner:beta')
  })
})

describe('the router keeps ?pool= on Pools and Holders (#125, #128)', () => {
  it('canonicalises the linked pool onto both addresses', () => {
    window.location.hash = `#capacity/holders?pool=${encodeURIComponent('runner:zeta')}`
    expect(canonical(fromHash())).toBe('capacity/holders?pool=runner%3Azeta')
    window.location.hash = `#capacity/pools?pool=${encodeURIComponent('provider:anthropic:tenant:eng')}`
    expect(canonical(fromHash())).toBe('capacity/pools?pool=provider%3Aanthropic%3Atenant%3Aeng')
    // Control: a tab that reads no pool still drops it.
    window.location.hash = `#capacity/accounts?pool=x`
    expect(canonical(fromHash())).toBe('capacity/accounts')
    window.location.hash = ''
  })
})
