// VISUAL QA 2026-10-20, LANE QAFIX-CAPACITY (part of #1680): the Pools,
// Profile headroom and Provider quota findings of workflow
// wf_9e9bf74834634db7bcc3. Each case holds the property the finding was
// about -- the words on the row, the rule the cascade picks, the colour a
// reader sees against the row behind it -- not the markup's shape.
//
//   QA-052  A pool filed under Needs action for being below one task says so.
//   QA-053  The Use track shows on a tinted (Needs action) row, in both themes.
//   QA-054  The summary wraps between clauses, never inside `tenant u-bogdan`.
//   QA-056  The matrix caption says `cells` when a tie outlines several.
//   QA-057  Provider quota's Feeds pool column takes the table's slack.

import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ResourceClasses } from '../api'
import type { Result } from '../fetch'
import type { Capacity, Pool, QuotaState, RunnerProfile } from '../types'
import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted, resolveColour, tokensIn } from './marks'
import { contrast, over } from './spaceprobe'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadLeases: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadAdminQuota: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { ProfileMatrix } = await import('../ProfileMatrix')
const { QuotaDetailScreen } = await import('../QuotaDetail')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 5000 } as const

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  const now = Date.now()
  return { status: 'ok', data, fetchedAt: now, serverAt: new Date(now).toISOString() }
}

function pool(name: string, active: number, limit: number | null, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: limit === null ? null : Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-20T03:00:00Z', ...over,
  } as Pool
}

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
  large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
}

/** The preview's shape: `resource:large` at 0 / 2 under a class of 4 units. */
function board(): Capacity {
  return {
    tenant_id: 'u-bogdan',
    generated_at: '2026-10-20T03:00:00Z',
    pools: [
      pool('global', 3, 40),
      pool('tenant:u-bogdan', 0, 10),
      pool('tenant:eng', 0, 6, { enabled: false }),
      pool('resource:large', 0, 2),
      pool('resource:standard', 1, 20),
    ],
    runner_profiles: {},
  }
}

async function renderPools(): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(board()))
  api.loadLeases.mockResolvedValue({ status: 'error', error: { message: 'admin only' } })
  api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

function rowOf(name: string): HTMLElement {
  const sub = [...document.querySelectorAll('.cap-families tbody .ctl-sub')].find((s) => s.textContent === name)
  return sub!.closest('tr') as HTMLElement
}

describe('QA-052: a pool too small for one task of its class says so on its row', () => {
  it('marks `resource:large` (limit 2, a large task is 4) `too small`, not `ok`', async () => {
    await renderPools()
    await waitFor(() => expect(rowOf('resource:large').closest('.cap-needs')).not.toBeNull(), WAIT)
    const row = rowOf('resource:large')
    const state = within(row).getByRole('cell', { name: /too small/ })
    // MUTATION: drop the `small` chip from classifyPool -- the row reads `ok`
    // under Needs action with nothing saying why it is there.
    expect(state.textContent).toContain('too small')
    expect(state.textContent).not.toMatch(/\bok\b/)
    const mark = [...state.querySelectorAll<HTMLElement>('[title]')].find((m) => m.textContent?.includes('too small'))!
    // MUTATION: title the chip without the weight -- the reason is the
    // comparison, `2` against `4`, and the title states both.
    expect(mark.getAttribute('title')).toMatch(/limit is 2 units, below the 4 units one large task weighs/)
    // MUTATION: leave the row's class undefined for `small` -- the row loses
    // the wash every other Needs-action row carries.
    expect(row.classList.contains('is-paused')).toBe(true)
    // A pool of the same family that fits its class stays `ok` in its family.
    const fine = rowOf('resource:standard')
    expect(fine.closest('.cap-needs')).toBeNull()
    expect(fine.textContent).not.toContain('too small')
  })
})

describe('QA-053: the Use track shows on a tinted row', () => {
  /** The edge a reader sees the track by: its hairline if it has one, else its fill. */
  function edge(track: HTMLElement, theme: 'dark' | 'light') {
    const shadow = painted(track, 'box-shadow', WIDE)
    if (shadow !== null && shadow !== 'none') {
      const t = tokensIn(shadow, theme)
      if (t.length > 0) return t[0]!.rgba
    }
    return resolveColour(painted(track, ['background', 'background-color'], WIDE)!, theme)
  }

  for (const tone of ['is-paused', 'is-warn', 'is-bad'] as const) {
    it(`holds a 0% track on a ${tone} row apart from the wash, at least as well as a plain row's`, () => {
      const tinted = build(`div.ctl-table.cap-pools > table > tbody > tr.${tone} > td > span.cap-use > span.ctl-util-track.is-zero`, hosts)
      const plain = build('div.ctl-table.cap-pools > table > tbody > tr > td > span.cap-use > span.ctl-util-track.is-zero', hosts)
      const row = tinted.closest('tr')!
      for (const theme of THEMES) {
        const surface = resolveColour('var(--surface)', theme)
        const wash = over(resolveColour(painted(row, ['background', 'background-color'], WIDE)!, theme), surface)
        const plainSeen = contrast(over(edge(plain, theme), surface), surface)
        const tintedSeen = contrast(over(edge(tinted, theme), wash), wash)
        // MUTATION: delete the `.cap-pools tr.is-paused .ctl-util-track` rule
        // -- the track is `--surface-2` on a wash ~1.0:1 from it, and only the
        // 0% tick shows (the eng row of the crop).
        expect(tintedSeen, `${theme}: track ${tintedSeen}:1 on the ${tone} wash, plain ${plainSeen}:1`).toBeGreaterThanOrEqual(plainSeen)
      }
    })
  }
})

describe('QA-054: the summary wraps between clauses', () => {
  it('holds `tenant u-bogdan` on one line at 390', async () => {
    await renderPools()
    const clauses = [...document.querySelectorAll<HTMLElement>('.cap-clause')]
    const tenant = clauses.find((c) => c.textContent === 'tenant u-bogdan')
    // MUTATION: render the summary as one text run again -- no clause element
    // holds the tenant, and the line can break at its hyphen.
    expect(tenant, clauses.map((c) => c.textContent).join(' | ')).toBeDefined()
    // MUTATION: drop `.cap-clause { white-space: nowrap }`.
    expect(painted(tenant!, 'white-space', PHONE)).toBe('nowrap')
    // The words are unchanged for a reader and for a copy.
    expect(tenant!.parentElement!.textContent).toMatch(/^5 pools in 3 families · 1 paused · tenant u-bogdan$/)
  })
})

describe('QA-056: the matrix caption follows how many cells are outlined', () => {
  function profile(binding: string[]): RunnerProfile {
    return {
      backend: 'CLOUD_RUN_JOB', resource_class: 'standard', units: 1,
      pools: ['global', 'resource:standard', 'backend:CLOUD_RUN_JOB'],
      headroom: 15, available: true, disabled_reason: '',
      admission: { headroom: 15, basis: 'measured', complete: true, binding, blockers: [], unread: [], uncapped: [], counterfactual: [] },
    } as unknown as RunnerProfile
  }
  function capacity(binding: string[]): Capacity {
    return {
      tenant_id: 'u-bogdan',
      generated_at: '2026-10-20T03:00:00Z',
      pools: [pool('global', 5, 20), pool('resource:standard', 5, 20), pool('backend:CLOUD_RUN_JOB', 5, 20)],
      runner_profiles: { mock: profile(binding) },
    }
  }

  it('says `cells` when a tie outlines three, and `cell` when one is', () => {
    render(<ProfileMatrix capacity={capacity(['global', 'resource:standard', 'backend:CLOUD_RUN_JOB'])} />)
    expect(document.querySelectorAll('tr[data-profile="mock"] td.is-binding')).toHaveLength(3)
    const note = document.querySelector('.cap-mx-note')!.textContent ?? ''
    // MUTATION: keep the singular caption for every matrix -- three outlined
    // cells sit over `The outlined cell runs out first`.
    expect(note).toMatch(/The outlined cells run out first/)
    expect(note).not.toMatch(/The outlined cell runs/)
    cleanup()
    render(<ProfileMatrix capacity={capacity(['global'])} />)
    expect(document.querySelectorAll('tr[data-profile="mock"] td.is-binding')).toHaveLength(1)
    // MUTATION: always say `cells` -- one outlined cell is named as several.
    expect(document.querySelector('.cap-mx-note')!.textContent).toMatch(/The outlined cell runs out first\./)
  })
})

describe('QA-057: the Feeds pool column takes the quota table\'s slack', () => {
  function quota(over: Partial<QuotaState> = {}): QuotaState {
    return {
      provider: 'anthropic', tenant_id: 'u-bogdan', state: 'AVAILABLE', updated_at: new Date().toISOString(),
      configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
      tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
      retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50, ...over,
    }
  }

  /** The px a fixed-layout table of `table` px gives the one auto column. */
  function feedsAt(cols: HTMLElement[], table: number): number {
    let used = 0
    for (const c of cols) {
      if (c.classList.contains('quota-c-feeds')) continue
      const w = painted(c, 'width', WIDE)!
      used += w.endsWith('%') ? (parseFloat(w) / 100) * table : parseFloat(w)
    }
    return table - used
  }

  it('gives the pool name what the bounded columns leave, never less than its old 15%', async () => {
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota()] }))
    render(<QuotaDetailScreen />)
    await screen.findAllByRole('rowheader', { name: 'u-bogdan' }, WAIT)
    const cols = [...document.querySelectorAll<HTMLElement>('.quota-table col')]
    const feeds = cols.find((c) => c.classList.contains('quota-c-feeds'))!
    // MUTATION: give `.quota-c-feeds` a percentage again -- every column then
    // takes its share of a wider table and the pool is still head-cut.
    expect(painted(feeds, 'width', WIDE)).toBeNull()
    for (const c of cols) if (c !== feeds) expect(painted(c, 'width', WIDE), c.className).not.toBeNull()
    // At the ~1054px table the widths were measured on, no narrower than 15%.
    expect(feedsAt(cols, 1054)).toBeGreaterThanOrEqual(0.15 * 1054)
    // At the capture's ~1300px table the whole `provider:anthropic:tenant:u-bogdan`
    // (34 characters of 14px mono, 0.6em each) fits inside the 10px gutters.
    // MUTATION: put every bounded column back in percentages -- the slack is
    // shared out and the name does not fit.
    const name = 'provider:anthropic:tenant:u-bogdan'
    expect(feedsAt(cols, 1300) - 2 * 10).toBeGreaterThanOrEqual(name.length * 14 * 0.6)
  })
})
