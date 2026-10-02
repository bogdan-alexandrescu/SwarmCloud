// POOL LIMITS #133: WHO CHANGED IT, OVER ITS CEILING, AND WHICH POOL BINDS.
//
// The L2 side editor already previews the impact and asks for the pool's name
// before a drastic cut (limits.side.test.tsx). What #133 still asked for:
//
//   * LAST CHANGED BY, AND WHEN, once the API serves the `admin_changed_by` /
//     `admin_changed_at` the store already writes. Until a pool carries them
//     the editor says "not recorded" (pinned in limits.side.test.tsx); once it
//     does, the editor prints them.
//   * A NEUTRAL OVER-CEILING MARK on a row whose units in use exceed its
//     ceiling. Help calls that a legal state (`ceiling-change-evicts-nothing`),
//     so the mark must not wear the fault or warning style.
//   * THE LIVE PREVIEW NAMES THE BINDING POOL, and each operand it names is a
//     link to that pool's row.
//
// MUTATION: drop `changeOf` and the first case reads "not recorded"; give the
// mark `warn-text` and the second goes red; print the pool name as text and
// the third does.

import STYLES from '../styles.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile } from '../types'
import { cascade } from './cssgate'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

beforeEach(() => {
  api.loadCapacity.mockReset()
  api.setPoolLimit.mockReset()
  api.loadMe.mockReset()
})
afterEach(() => {
  cleanup()
  window.location.hash = ''
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
    updated_at: '2026-10-02T10:00:00Z',
    ...over,
  } as Pool
}

function profile(pools: string[], units = 1): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units, pools }
}

function capacity(pools: Pool[]): Capacity {
  return {
    pools,
    runner_profiles: {
      'claude-code': profile(['global', 'runner:claude-code', 'tenant:eng']),
      codex: profile(['global']),
    },
    tenant_id: 'eng',
    generated_at: '2026-10-02T10:00:00Z',
  }
}

const POOLS = () => [
  pool('global', { hard_limit: 40, effective_limit: 40, available: 36 }),
  pool('runner:claude-code', { hard_limit: 20, effective_limit: 20, available: 16 }),
  pool('tenant:eng', { hard_limit: 30, effective_limit: 30, available: 26 }),
]

async function rows(cap: Capacity): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(cap))
  render(<AdminSettingsScreen />)
  await waitFor(() => expect(document.getElementById(`limit-${cap.pools[0]!.name}`)).not.toBeNull(), WAIT)
}

function open(name: string): HTMLElement {
  const row = document.getElementById(`limit-${name}`)!
  fireEvent.click(within(row).getByRole('button', { name: `Edit ceiling for ${name}` }))
  return document.querySelector<HTMLElement>('aside.adm-side')!
}

describe('Pool limits says who last changed a ceiling, once the API serves it (#133)', () => {
  it('prints the admin and the time in Last changed and History', async () => {
    const at = new Date(Date.now() - 3 * 3_600_000).toISOString()
    const cap = capacity(POOLS())
    cap.pools[2] = pool('tenant:eng', {
      hard_limit: 30,
      effective_limit: 30,
      admin_changed_by: 'ops@saga.xyz',
      admin_changed_at: at,
    })
    await rows(cap)
    const side = open('tenant:eng')
    expect(side.querySelector('.adm-not-recorded')).toBeNull()
    const changed = [...side.querySelectorAll('.adm-changed')]
    expect(changed).toHaveLength(2)
    for (const c of changed) {
      expect(c.textContent).toContain('ops@saga.xyz')
      expect(c.textContent).toContain('3h ago')
      // The exact instant rides on the element, for a reader who needs it.
      expect(c.querySelector('time')?.getAttribute('dateTime')).toBe(at)
    }
  })

  it('still reads not recorded for a pool that carries neither field', async () => {
    await rows(capacity(POOLS()))
    const side = open('tenant:eng')
    expect([...side.querySelectorAll('.adm-not-recorded')].map((m) => m.textContent)).toEqual(['not recorded', 'not recorded'])
    expect(side.querySelector('.adm-changed')).toBeNull()
  })
})

describe('a pool over its ceiling carries a neutral mark (#133)', () => {
  it('marks the row whose units in use exceed its ceiling, and only that row', async () => {
    const cap = capacity(POOLS())
    cap.pools[1] = pool('runner:claude-code', { hard_limit: 5, effective_limit: 5, active: 8, available: 0 })
    await rows(cap)
    const over = document.getElementById('limit-runner:claude-code')!.querySelector('.adm-over')
    expect(over, 'a pool over its ceiling has no mark').not.toBeNull()
    expect(over!.textContent).toBe('over ceiling')
    expect(over!.getAttribute('title')).toMatch(/evicts nothing/)
    // At its ceiling is not over it.
    expect(document.querySelectorAll('.adm-over')).toHaveLength(1)

    // NOT THE FAULT STYLE: no warn/bad class, and the sheet paints it neutral.
    expect(over!.className).not.toMatch(/warn|bad|is-unread/)
    for (const theme of ['dark', 'light'] as const) {
      const colour = cascade(STYLES, over!, 'color', { width: 1440, theme }).winner?.value ?? ''
      expect(colour, `over-ceiling is painted ${colour} (${theme})`).not.toMatch(/--(warn|bad|s-bad|s-warn)/)
      expect(colour).not.toBe('')
    }
  })

  it('draws no mark on a pool with no limit set', async () => {
    const cap = capacity(POOLS())
    cap.pools[2] = pool('tenant:eng', { hard_limit: null, effective_limit: null, available: null, active: 3 })
    await rows(cap)
    expect(document.querySelector('.adm-over')).toBeNull()
  })
})

describe('the live preview names the binding pool and links it to its row (#133)', () => {
  it('says which pool the moved ceiling binds on, as a link to that row', async () => {
    await rows(capacity(POOLS()))
    const side = open('runner:claude-code')
    fireEvent.change(within(side).getByLabelText('Hard limit for runner:claude-code'), { target: { value: '10' } })
    const impact = side.querySelector('.adm-impact')!
    expect(impact.textContent).toContain('claude-code 20 → 10, binds on')
    const link = within(impact as HTMLElement).getByRole('link', { name: 'runner:claude-code' })
    expect(link.getAttribute('href')).toBe('#admin/limits?pool=runner:claude-code')
  })

  it('links the pool that holds an unchanged profile, and following it marks that row', async () => {
    await rows(capacity(POOLS()))
    const side = open('tenant:eng')
    // tenant:eng 30 -> 25 moves nothing: runner:claude-code at 20 still binds.
    fireEvent.change(within(side).getByLabelText('Hard limit for tenant:eng'), { target: { value: '25' } })
    const impact = side.querySelector('.adm-impact')!
    expect(impact.textContent).toContain('Unchanged: claude-code (')
    const link = within(impact as HTMLElement).getByRole('link', { name: 'runner:claude-code' })
    expect(link.getAttribute('href')).toBe('#admin/limits?pool=runner:claude-code')

    window.location.hash = link.getAttribute('href')!
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await waitFor(() =>
      expect(document.getElementById('limit-runner:claude-code')!.classList.contains('is-target')).toBe(true),
    )
  })
})
