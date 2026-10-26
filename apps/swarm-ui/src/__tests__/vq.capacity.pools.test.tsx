// VISUAL QA PASS 1 (2026-10-26, #2391), LANE VQ-CAPACITY: Pools and Provider quota.
//
//   VQ-031  `anthropic · eng` at 46 of a lowered 50 read `ok` in the grey an
//           idle pool takes, and the four lowered pools the head counted sat
//           at the foot of Providers. A pool near its ceiling is now drawn
//           `near ceiling` in the full mark's amber (chip, row and track), a
//           lowered one `lowered`, and both lead their family. Needs action
//           keeps the owner's rule (2026-10-01): neither moves into it.
//   VQ-032  at 390 each pool was a ~220px record of six keyed lines. It is
//           one compact row: name and figure, then the use bar and state;
//           Set by only when something other than the configured limit sets
//           the ceiling.
//   VQ-036  Provider quota's Tenant column took 16% beside a Feeds pool that
//           broke every name over three lines and cut its ceiling note.
//
// BREAK IT: drop `nearCeiling` from `classifyPool` (the 46/50 row reads `ok`
// again), put `poolProblemRank` back to four ranks (the lowered pool drops
// under the idle one), drop the phone grid on `.cap-pools.is-stacked` rows
// or the `cap-by-cfg` class, or put Tenant back to 16% -- a case turns red.

import { cleanup, render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn(), loadLeases: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { POOL_NEAR_CEILING } = await import('../capacityPoll')
const { NEAR_CEILING } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const hosts: HTMLElement[] = []

afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  window.history.replaceState(null, '', '/')
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-26T01:00:00Z' }
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
    updated_at: '2026-10-26T01:00:00Z',
    ...over,
  }
}

/** The live page's Providers family, as pass 1 read it. */
function board(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-26T01:00:00Z',
    pools: [
      pool('global', 10, 100),
      pool('provider:anthropic', 0, 200),
      pool('provider:anthropic:tenant:a-idle', 0, 50),
      // 46 of 50, lowered from 100 by the provider's quota.
      pool('provider:anthropic:tenant:eng', 46, 100, { quota_derived_limit: 50, effective_limit: 50, available: 4 }),
      // Lowered, with little in use.
      pool('provider:anthropic:tenant:u-bogdan', 2, 50, { quota_derived_limit: 20, effective_limit: 20, available: 18 }),
    ],
    runner_profiles: {},
  }
}

async function renderPools(data: Capacity = board()): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(data))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

function row(name: string): HTMLElement {
  const r = document.getElementById(`pool-${name}`)
  if (r === null) throw new Error(`no row for ${name}`)
  return r
}

const marks = (name: string) => [...row(name).querySelectorAll('.cap-marks > *')].map((m) => m.textContent?.trim())

function familyOrder(title: string): string[] {
  const card = [...document.querySelectorAll('.cap-families .ctl-card')].find(
    (c) => c.querySelector('.ctl-card-title')?.textContent === title,
  )
  if (card === undefined) throw new Error(`no ${title} family`)
  return [...card.querySelectorAll('tbody tr')].map((r) => r.id.replace(/^pool-/, ''))
}

describe('VQ-031: a pool near its ceiling, or lowered, is drawn so', () => {
  it('draws 46 of 50 `near ceiling` in amber, never `ok`', async () => {
    await renderPools()
    expect(marks('provider:anthropic:tenant:eng')).toEqual(['near ceiling'])
    // CP-14: the chip, the row and the track are one answer.
    expect(row('provider:anthropic:tenant:eng').className).toContain('is-warn')
    expect(row('provider:anthropic:tenant:eng').querySelector('.ctl-util-track > .is-warn')).not.toBeNull()
  })

  it('draws a lowered pool `lowered`, with what lowered it in the title', async () => {
    await renderPools()
    expect(marks('provider:anthropic:tenant:u-bogdan')).toEqual(['lowered'])
    const mark = within(row('provider:anthropic:tenant:u-bogdan')).getByText('lowered')
    expect(mark.closest('[title]')?.getAttribute('title')).toMatch(/Provider quota caps this at 20; configured is 50/)
    // An idle pool still reads ok, so `ok` keeps meaning something.
    expect(marks('provider:anthropic:tenant:a-idle')).toEqual(['ok'])
  })

  it('leads the family with them, near before lowered, idle pools after', async () => {
    await renderPools()
    expect(familyOrder('Providers')).toEqual([
      'provider:anthropic:tenant:eng',
      'provider:anthropic:tenant:u-bogdan',
      'provider:anthropic',
      'provider:anthropic:tenant:a-idle',
    ])
  })

  it('counts near ceiling in the head by the marks’ own rule', async () => {
    await renderPools()
    await waitFor(() => expect(document.body.textContent).toContain('1 near ceiling'), WAIT)
    expect(document.body.textContent).toContain('2 lowered')
  })

  it('keeps Needs action to the owner’s rule: neither near nor lowered moves there', async () => {
    await renderPools()
    expect(document.querySelector('.cap-needs')).toBeNull()
  })

  it('takes Pool limits’ near-ceiling share, so the two screens cannot disagree', () => {
    expect(POOL_NEAR_CEILING).toBe(NEAR_CEILING)
  })
})

describe('VQ-032: at 390 a pool is one compact row', () => {
  it('marks a configured Set by cell so the phone row can drop it, and keeps an override', async () => {
    await renderPools()
    const cfg = row('provider:anthropic:tenant:a-idle').querySelector('td[data-label="Set by"]')!
    expect(cfg.classList.contains('cap-by-cfg')).toBe(true)
    const quota = row('provider:anthropic:tenant:eng').querySelector('td[data-label="Set by"]')!
    expect(quota.classList.contains('cap-by-cfg')).toBe(false)
    expect(quota.textContent).toBe('provider quota')
  })

  const cell = (label: string, cls = '') => {
    const td = build(`div.ctl-table.cap-pools.is-stacked > table > tbody > tr > td${cls}`, hosts)
    td.setAttribute('data-label', label)
    return td
  }

  it('lays the record out as a grid of name and figure, then use and state', () => {
    const tr = build('div.ctl-table.cap-pools.is-stacked > table > tbody > tr', hosts)
    expect(painted(tr, 'display', PHONE)).toBe('grid')
    expect(painted(tr, 'grid-template-areas', PHONE)).toMatch(/'name leased ceil'\s*'name open open'\s*'use use state'/)
    // 1440 is a table row, untouched.
    expect(painted(tr, 'display', WIDE)).not.toBe('grid')
  })

  it('drops the field keys, prints the ceiling as `/50`, and hides a configured Set by', () => {
    expect(painted(cell('Leased (units)'), 'content', PHONE, 'before')).toBe('none')
    expect(painted(cell('Ceiling (units)'), 'content', PHONE, 'before')).toBe("'/'")
    expect(painted(cell('Set by', '.cap-by-cfg'), 'display', PHONE)).toBe('none')
    expect(painted(cell('Set by'), 'display', PHONE)).not.toBe('none')
    expect(painted(cell('State'), 'grid-area', PHONE)).toBe('state')
    expect(painted(cell('Links'), 'grid-area', PHONE)).toBe('open')
  })
})

describe('VQ-036: Provider quota gives the slack to Feeds pool', () => {
  it('holds Tenant to a tenant id’s width, and lets the ceiling note wrap rather than cut', () => {
    const col = build('div.quota-table > table > colgroup > col.quota-c-tenant', hosts)
    expect(painted(col, 'width', WIDE)).toBe('min(120px, 16%)')
    const note = build('div.quota-table > table > tbody > tr > td.quota-feeds > span.quota-ceiling', hosts)
    expect(painted(note, 'white-space', WIDE)).toBe('normal')
    expect(painted(note, 'text-overflow', WIDE)).not.toBe('ellipsis')
  })
})
