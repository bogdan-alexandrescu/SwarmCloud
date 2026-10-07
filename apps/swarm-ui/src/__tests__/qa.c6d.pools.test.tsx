// QA G5 (2026-10-07), Pools and Pool limits: two findings.
//
//   G5-11  A pool's raw key was cut ("provider:anthropic:tenant:smoke ·
//          tenant …") on Pools, and broke mid-word ("…tenant:smok / e") on
//          Pool limits. Pools drops the scope clause when the key already
//          names that tenant and gives the Pool column more of the row; Pool
//          limits breaks a key only after a colon.
//   G5-19  Set by said "configured" on 28 of 29 rows (capacity.html §G: Set by
//          only when it is not "configured"). The configured case is a faint
//          dot, named "configured" for a screen reader and in its title.
//
// BREAK IT: put `· {scope}` back unconditionally -- the first case fails; drop
// the `<wbr>` or the keep-all rule -- the second fails; draw the word again --
// the third fails.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Me, Pool } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import CAPACITY_CSS from '../styles/capacity.css?raw'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn(), loadAdminPools: vi.fn(), loadMe: vi.fn(), setPoolLimit: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { AdminSettingsScreen, AdmSetBy } = await import('../AdminSettings')

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const

afterEach(() => {
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-07T10:00:00Z' }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-07T10:00:00Z', ...over,
  }
}

const SMOKE = 'provider:anthropic:tenant:smoke'
const MINE = 'provider:anthropic:tenant:eng'

function capacity(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-07T10:00:00Z',
    pools_complete: true,
    pools: [
      pool('global', 1, 40),
      pool('tenant:smoke', 0, 5),
      pool('provider:anthropic', 1, 30),
      pool(SMOKE, 0, 5),
      pool(MINE, 1, 5),
      pool('backend:CLOUD_RUN_JOB', 1, 30, { hard_limit: 40, quota_derived_limit: 30 }),
    ],
    runner_profiles: {},
  } as Capacity
}

function rowOf(name: string): HTMLElement {
  const th = [...document.querySelectorAll<HTMLElement>('.cap-families th[scope="row"]')].find(
    (t) => t.getAttribute('title') === name,
  )
  expect(th, `no Pools row for ${name}`).toBeTruthy()
  return th!
}

describe('G5-11: a pool key is never cut by a clause that repeats it', () => {
  it('drops "tenant X" under a key that already names tenant X, and keeps the scope the key cannot say', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<CapacityScreen />)
    await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
    expect(rowOf(SMOKE).querySelector('.cap-sub')!.textContent).toBe(SMOKE)
    expect(rowOf('tenant:smoke').querySelector('.cap-sub')!.textContent).toBe('tenant:smoke')
    // The viewer's own slice: "this tenant" is not in the key, so it stays.
    expect(rowOf(MINE).querySelector('.cap-sub')!.textContent).toBe(`${MINE} · this tenant`)
    expect(rowOf('global').querySelector('.cap-sub')!.textContent).toBe('global · platform')
  })

  it('gives the Pool column the widest share of the fixed row', () => {
    const width = (cls: string) => Number(new RegExp(`\\.${cls}\\s*\\{\\s*width:\\s*(\\d+)%`).exec(CAPACITY_CSS)?.[1])
    const cols = ['cap-c-pool', 'cap-c-num', 'cap-c-num', 'cap-c-use', 'cap-c-state', 'cap-c-by', 'cap-c-links']
    expect(cols.reduce((n, c) => n + width(c), 0)).toBe(100)
    expect(width('cap-c-pool')).toBeGreaterThanOrEqual(28)
    expect(width('cap-c-pool')).toBeGreaterThan(2 * width('cap-c-use') - 2)
  })

  it('breaks a Pool limits key only after a colon, never mid-word', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    api.loadMe.mockResolvedValue(ok({
      tenant: { tenant_id: 'eng' },
      principal: { email: 'a@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: true },
      environment: 'dev', environment_declared: false,
    } as unknown as Me))
    render(<AdminSettingsScreen />)
    await waitFor(() => expect(document.getElementById(`limit-${SMOKE}`)).not.toBeNull(), WAIT)
    const sub = document.getElementById(`limit-${SMOKE}`)!.querySelector('th .ctl-sub')!
    expect(sub.textContent).toBe(SMOKE)
    // One break opportunity after each colon, and none elsewhere.
    expect(sub.querySelectorAll('wbr')).toHaveLength(3)
    for (const w of sub.querySelectorAll('wbr')) expect(w.previousSibling?.textContent?.endsWith(':')).toBe(true)
    expect(painted(sub, 'overflow-wrap', WIDE)).toBe('normal')
    expect(painted(sub, 'word-break', WIDE)).toBe('keep-all')
  })
})

describe('G5-19: Set by draws nothing loud for "configured"', () => {
  it('is a faint dot named configured on Pools, and the term in ink otherwise', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<CapacityScreen />)
    await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
    const cells = [...document.querySelectorAll('.cap-families td[data-label="Set by"]')]
    const texts = cells.map((c) => (c.textContent ?? '').trim())
    expect(texts).not.toContain('configured')
    expect(texts).toContain('·')
    expect(texts).toContain('provider quota')
    const dot = cells.find((c) => c.textContent === '·')!.querySelector('.cap-setby-cfg')!
    expect(dot.getAttribute('aria-label')).toBe('configured')
  })

  it('is the same dot on a Pool limits row', () => {
    const { container } = render(<AdmSetBy pool={pool('global', 1, 40)} />)
    expect(container.textContent).toBe('·')
    expect(container.querySelector('.adm-setby-cfg')?.getAttribute('aria-label')).toBe('configured')
  })
})
