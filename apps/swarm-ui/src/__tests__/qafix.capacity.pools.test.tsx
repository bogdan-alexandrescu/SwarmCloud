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
    expect(mark.getAttribute('title')).toMatch(/limit is 2 units, below the 4 units one large agent weighs/)
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

  /** One `Npx`, `N%` or `min(...)` of them, in px, inside a `table` px table. */
  function px(w: string, table: number): number {
    const m = /^min\((.*)\)$/.exec(w.trim())
    if (m) return Math.min(...m[1]!.split(',').map((a) => px(a, table)))
    return w.trim().endsWith('%') ? (parseFloat(w) / 100) * table : parseFloat(w)
  }

  /** The px a fixed-layout table of `table` px gives the one auto column. */
  function feedsAt(cols: HTMLElement[], table: number): number {
    let used = 0
    for (const c of cols) {
      if (c.classList.contains('quota-c-feeds')) continue
      used += px(painted(c, 'width', WIDE)!, table)
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

  it('keeps the pool name its old 15% in a table narrower than the px columns fit', async () => {
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota()] }))
    render(<QuotaDetailScreen />)
    await screen.findAllByRole('rowheader', { name: 'u-bogdan' }, WAIT)
    const cols = [...document.querySelectorAll<HTMLElement>('.quota-table col')]
    // A 1280px viewport with the nav open is a ~894px table; 900px, the
    // narrowest viewport the fixed layout holds at (the auto layout stops at
    // 899px), is a ~514px table. Bounded columns in bare px (724px) left
    // Feeds pool ~27px at 894 and less than nothing at 514, where the fixed
    // table outgrew its card and pushed Reported past its edge.
    // MUTATION: drop the `min(..., N%)` from any bounded column -- Feeds pool
    // falls below 15% at 894, and the columns overflow the table at 514.
    for (const table of [514, 894]) {
      expect(feedsAt(cols, table), `${table}px table`).toBeGreaterThanOrEqual(0.15 * table - 0.5)
    }
  })
})

describe('QA-049 on Provider quota: a partial mark there has the squared dashed side (owner 2026-10-20)', () => {
  it('carries the Accounts-section scope, so a partial mark inside it is drawn as on Accounts', async () => {
    const row = {
      provider: 'anthropic', tenant_id: 'u-bogdan', state: 'AVAILABLE', updated_at: new Date().toISOString(),
      configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
      tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
      retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50,
    } as QuotaState
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [row] }))
    render(<QuotaDetailScreen />)
    await screen.findAllByRole('rowheader', { name: 'u-bogdan' }, WAIT)
    // MUTATION: drop the `quota-page` wrapper from QuotaDetail.tsx -- the
    // view loses the scope the squared rule is written against.
    const page = document.querySelector<HTMLElement>('.quota-page')
    expect(page, 'Provider quota carries no local scope').not.toBeNull()
    // Provider quota draws no partial mark of its own today; one placed in the
    // view is what any future one would be.
    const mark = document.createElement('span')
    mark.className = 'ctl-mark is-partial'
    page!.appendChild(mark)
    // MUTATION: drop `.quota-page` from the scoped rule in styles/capacity.css.
    expect(painted(mark, ['border-top-right-radius', 'border-radius'], WIDE)).toBe('0')
    expect(painted(mark, ['border-bottom-right-radius', 'border-radius'], WIDE)).toBe('0')
  })
})

// QAFIX2-CAPACITY (visual QA run 2, wf_b6e2441b419f423a84eb, capacity-002):
// the slack PR 1714 gave Feeds pool does not exist at 1440 -- the table is
// ~1054px and the column still ~160px -- so the middle cut ran: `…opic:tenant:eng`,
// and `…:ten… u-bogdan` with two ellipses once the tail split too.
describe('QA-057 (run 2): the Feeds pool name reads whole, wrapping at its colons', () => {
  function quota(tenant: string, provider = 'anthropic'): QuotaState {
    return {
      provider, tenant_id: tenant, state: 'AVAILABLE', updated_at: new Date().toISOString(),
      configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
      tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
      retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50,
    }
  }

  it('prints every pool whole, with a break offered only after each colon', async () => {
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota('eng'), quota('u-bogdan'), quota('eng', 'openai'), quota('u-bogdan', 'openai')] }))
    render(<QuotaDetailScreen />)
    await screen.findAllByRole('rowheader', { name: 'u-bogdan' }, WAIT)
    const names = ['provider:anthropic:tenant:eng', 'provider:anthropic:tenant:u-bogdan', 'provider:openai:tenant:eng', 'provider:openai:tenant:u-bogdan']
    for (const name of names) {
      const link = screen.getByRole('link', { name })
      expect(link.textContent).toBe(name)
      // MUTATION: put MiddleCut back on the pool -- its head and tail boxes
      // each ellipsize, which is the double cut `…:ten… u-bogdan`.
      expect(link.querySelector('.quota-cut'), `${name} is still cut`).toBeNull()
      // MUTATION: drop the `<wbr>`s -- the name has no break but its hyphen.
      expect(link.querySelectorAll('wbr')).toHaveLength(3)
      for (const env of [WIDE, PHONE]) {
        // MUTATION: leave the link `nowrap` with `text-overflow: ellipsis` --
        // at 1440 the ~160px column cuts the name to `…opic:tenant:eng`.
        expect(painted(link, 'white-space', env)).toBe('normal')
        expect(painted(link, 'text-overflow', env)).not.toBe('ellipsis')
        expect(painted(link, ['overflow-x', 'overflow'], env)).not.toBe('hidden')
        // MUTATION: `overflow-wrap: normal` -- a tenant id wider than the
        // column overflows into Requests left instead of wrapping.
        expect(painted(link, 'overflow-wrap', env)).toBe('anywhere')
      }
      // MUTATION: keep `text-decoration: none` (the underline was the cut
      // box's border) -- the wrapped name is a link with no underline.
      expect(painted(link, 'text-decoration', WIDE)).toBe('underline')
    }
  })
})
