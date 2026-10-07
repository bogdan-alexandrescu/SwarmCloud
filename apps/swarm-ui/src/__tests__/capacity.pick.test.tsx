// THE PICKED CAPACITY FRAMES, AND THE #503 AUDIT OF THEM (lane U5, 2026-10-02).
//
// capacity.html C1 (Ceilings by family, frames 0-1), the profile matrix
// (frames 3-4), Runtimes (frame 5), Holders (frame 6) and Provider quota
// (frame 11). Each block pins one audit finding:
//
//   * Pools and By runner profile are ONE page, "Pools", with an in-page
//     `Ceilings | By runner profile` strip; the strip used to exist only in the
//     panel, and By runner profile was headed as a page of its own.
//   * The family tables carry the frame's seven columns -- no Scope column
//     (the scope rides under the pool's name, CP-2 kept), each head on one
//     line (CP-24's `(units)` kept) -- and the holders link carries its count
//     when the lease read can vouch for one.
//   * The Use percentage sits in its own grid track and cannot paint over the
//     state chip (it overflowed a fixed 12.5% column by ~25px).
//   * By runner profile marks a disabled profile with the amber condition
//     triangle, not the red FAILED diamond (brand §3).
//   * Runtimes draws the runtime cards first and Sizing last.
//   * Holders draws its table first, with the All / per-tenant filter even
//     for one tenant, and drift as a per-pool table under it; the Class mix
//     card and the drift summary card are gone. Dispatched and awaiting are
//     the half disc of the holding-capacity family.
//   * Provider quota draws `available` one way on every row; a stale reading
//     keeps the same word and mark and carries its stale mark beside it.
//
// BREAK IT: put the Scope column back -- "draws the frame's seven columns"
// fails. Drop `CapSeg` from ProfilesScreen -- the strip test fails. Draw the
// disabled chip as `is-bad` again -- "a condition, not a failed task" fails.
// Move `<Sizing>` above the cards -- the order test fails.

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import CAPACITY_CSS from '../styles/capacity.css?raw'
import type { HoldersBoard, RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Capacity, LeasePage, LeaseRow, Pool, QuotaState, RunnerProfile } from '../types'
import { POOLS_POLL_MS } from '../capacityPoll'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadLeases: vi.fn(),
  loadHolders: vi.fn(),
  loadRuntimeTopology: vi.fn(),
  loadAdminQuota: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { ProfilesScreen } = await import('../Profiles')
const { HoldersScreen } = await import('../Holders')
const { RuntimesScreen } = await import('../Runtimes')
const { QuotaDetailScreen } = await import('../QuotaDetail')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  window.history.replaceState(null, '', '/')
  vi.useRealTimers()
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

function lease(n: number, pools: string[], over: Partial<LeaseRow> = {}): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`,
    task_id: `task-${String(n).padStart(10, '0')}`,
    attempt_id: `att-${n}`,
    tenant_id: 'eng',
    generation: 1,
    pools,
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-02T09:00:00Z',
    dispatch_deadline: '2026-10-02T09:05:00Z',
    expires_at: '2026-10-02T11:00:00Z',
    heartbeat_at: '2026-10-02T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
    ...over,
  } as LeaseRow
}

function leasePage(leases: LeaseRow[], over: Partial<LeasePage> = {}): LeasePage {
  return {
    leases,
    thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
    evaluated_at: '2026-10-02T10:00:00Z',
    active_only: true,
    tenant_id: null,
    units_held: leases.reduce((n, l) => n + l.units, 0),
    truncated: false,
    active_beyond_window: 0,
    ...over,
  } as unknown as LeasePage
}

function board(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-02T10:00:00Z',
    pools: [
      pool('global', 3, 40),
      pool('tenant:eng', 2, 20),
      pool('tenant:research', 1, 20),
      pool('runner:codex', 10, 10),
      pool('provider:anthropic', 22, 22, { hard_limit: 30, adaptive_target: 22 }),
    ],
    runner_profiles: {},
  }
}

async function renderPools(data: Capacity = board(), leases: Result<LeasePage> = ok(leasePage([]))): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(data))
  api.loadLeases.mockResolvedValue(leases)
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

function rowOf(name: string): HTMLElement {
  const th = [...document.querySelectorAll('.cap-families tbody th[title]')].find((t) => t.getAttribute('title') === name)
  expect(th, `no row for ${name}`).toBeTruthy()
  return th!.closest('tr') as HTMLElement
}

function profile(over: Partial<RunnerProfile> = {}): RunnerProfile {
  return {
    backend: 'CLOUD_RUN_JOB',
    resource_class: 'standard',
    units: 1,
    pools: ['global'],
    headroom: 5,
    available: true,
    disabled_reason: '',
    ...over,
  } as unknown as RunnerProfile
}

// ---------------------------------------------------------------------------

describe('Pools is one page with an in-page Ceilings | By runner profile strip (#503)', () => {
  it('draws the strip on Ceilings, with Ceilings as the current view', async () => {
    await renderPools()
    // The canonical segmented control, in its link form.
    const strip = document.querySelector('.cap-seg nav.c-seg[aria-label="Pools views"]')
    expect(strip, 'Ceilings has no in-page view strip').not.toBeNull()
    const links = [...strip!.querySelectorAll('a')]
    expect(links.map((a) => a.textContent)).toEqual(['Ceilings', 'By runner profile'])
    expect(links[0]!.getAttribute('aria-current')).toBe('page')
    expect(links[1]!.getAttribute('aria-current')).toBeNull()
    expect(links[1]!.getAttribute('href')).toBe('#capacity/profiles')
  })

  it('heads By runner profile "Pools", with the strip on By runner profile', async () => {
    api.loadCapacity.mockResolvedValue(ok({ ...board(), runner_profiles: { codex: profile() } }))
    api.loadLeases.mockResolvedValue(ok(leasePage([])))
    render(<ProfilesScreen />)
    await waitFor(() => expect(document.querySelector('.cap-mx')).not.toBeNull(), WAIT)
    expect(screen.getByRole('heading', { level: 1 }).textContent).toContain('Pools')
    const links = [...document.querySelectorAll('.cap-seg nav.c-seg a')]
    expect(links.map((a) => a.textContent)).toEqual(['Ceilings', 'By runner profile'])
    expect(links[1]!.getAttribute('aria-current')).toBe('page')
    expect(links[0]!.getAttribute('href')).toBe('#capacity/pools')
  })
})

describe('the family tables are the frame’s seven columns (#503)', () => {
  it('draws the frame’s seven columns, with no Scope column and the unit on both figures', async () => {
    await renderPools()
    for (const table of document.querySelectorAll('.cap-families table')) {
      const heads = [...table.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
      // The links column has a head since browser QA D32 (2026-10-04).
      expect(heads).toEqual(['Pool', 'Leased (units)', 'Ceiling (units)', 'Use', 'State', 'Set by', 'Open'])
    }
  })

  it('keeps the scope on every row, under the pool’s name (CP-2)', async () => {
    await renderPools()
    expect(rowOf('global').querySelector('.cap-scope')?.textContent).toBe('platform')
    expect(rowOf('tenant:eng').querySelector('.cap-scope')?.textContent).toBe('this tenant')
    expect(rowOf('tenant:research').querySelector('.cap-scope')?.textContent).toBe('tenant research')
  })

  it('keeps each head on one line, ellipsized rather than wrapped (CP-24 keeps the unit in it)', async () => {
    await renderPools()
    expect(rowOf('global').querySelector('td[data-label="Leased (units)"]')?.textContent).toBe('3')
    const head = [...document.querySelectorAll('.cap-families thead th')].find((th) => th.textContent === 'Leased (units)')
    expect(head?.getAttribute('title') ?? '').toMatch(/units, not agents/)
    expect(CAPACITY_CSS).toMatch(/\.cap-pools thead th\s*\{[^}]*white-space:\s*nowrap[^}]*text-overflow:\s*ellipsis/)
  })

  it('counts the holders on the link when every live lease was read', async () => {
    await renderPools(
      board(),
      ok(leasePage([lease(1, ['global', 'runner:codex']), lease(2, ['global', 'runner:codex']), lease(3, ['global'])])),
    )
    const links = (name: string) => [...rowOf(name).querySelectorAll('.cap-links a')].map((a) => a.textContent)
    expect(links('global')).toEqual(['3 holders', 'limit'])
    expect(links('runner:codex')).toEqual(['2 holders', 'limit'])
    expect(links('tenant:research')).toEqual(['0 holders', 'limit'])
  })

  it('draws no count when the lease read failed or was cut: unknown is not 0', async () => {
    await renderPools(board(), {
      status: 'error',
      error: { kind: 'admin_required', httpStatus: 403, code: 'forbidden', message: 'admin group membership is required' },
    })
    const holders = rowOf('global').querySelector('.cap-links a')!
    expect(holders.textContent).toBe('holders')
    expect(holders.getAttribute('title') ?? '').toMatch(/not counted/)
  })

  it('draws no count over a cut lease window', async () => {
    await renderPools(board(), ok(leasePage([lease(1, ['global'])], { active_beyond_window: 4 })))
    expect(rowOf('global').querySelector('.cap-links a')!.textContent).toBe('holders')
  })

  // The summary is the count note over the first card since #138 (owner
  // ruling 2026-10-07), where it was the line under the title.
  // MUTATION: drop any of the four clauses from Pools' `summary`.
  it('says how many families are full or lowered, and whose pools these are', async () => {
    await renderPools()
    const summary = document.querySelector('p.c-count-note')?.textContent ?? ''
    expect(summary).toContain('5 pools')
    expect(summary).toContain('2 full')
    expect(summary).toContain('1 lowered')
    expect(summary).toContain('tenant eng')
  })
})

describe('the Use percentage cannot paint over the state chip (#503)', () => {
  it('gives the figure its own nowrap track that the bar shrinks around', () => {
    const style = document.createElement('style')
    style.textContent = CAPACITY_CSS
    document.head.appendChild(style)
    try {
      const host = document.createElement('div')
      host.innerHTML = '<span class="cap-use"><span></span><span class="cap-use-pct">no limit set</span></span>'
      document.body.appendChild(host)
      const use = getComputedStyle(host.querySelector('.cap-use')!)
      expect(use.display).toBe('grid')
      // The bar's track can shrink to nothing; the figure's track is its own
      // content width, so the figure is never the thing that overflows.
      expect(use.gridTemplateColumns).toBe('minmax(0, 1fr) max-content')
      expect(['0', '0px']).toContain(use.minWidth)
      expect(getComputedStyle(host.querySelector('.cap-use-pct')!).whiteSpace).toBe('nowrap')
    } finally {
      style.remove()
    }
  })

  it('gives every family table one colgroup, so the Use column is sized on purpose', async () => {
    await renderPools()
    for (const table of document.querySelectorAll('.cap-families table')) {
      const cols = [...table.querySelectorAll('colgroup col')].map((c) => c.className)
      expect(cols).toEqual(['cap-c-pool', 'cap-c-num', 'cap-c-num', 'cap-c-use', 'cap-c-state', 'cap-c-by', 'cap-c-links'])
    }
    // Seven widths that add to the whole table: nothing is left for a column
    // to take from its neighbour.
    const widths = [...CAPACITY_CSS.matchAll(/\.cap-c-(pool|num|use|state|by|links)\s*\{\s*width:\s*([\d.]+)%/g)]
    const by = new Map(widths.map((m) => [m[1], Number(m[2])]))
    const sum = ['pool', 'num', 'num', 'use', 'state', 'by', 'links'].reduce((n, k) => n + (by.get(k) ?? NaN), 0)
    expect(sum).toBe(100)
  })
})

describe('Pools re-reads every 30 s (the hidden-tab pause is Screen’s, pinned in capacity.polling.test.tsx)', () => {
  it('reads again after POOLS_POLL_MS', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await renderPools()
    const before = api.loadCapacity.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POOLS_POLL_MS + 50)
    })
    expect(api.loadCapacity.mock.calls.length).toBeGreaterThan(before)
    expect(POOLS_POLL_MS).toBe(30_000)
  })
})

describe('By runner profile draws a disabled profile as a condition (#503, brand §3)', () => {
  it('uses the amber warning triangle, not the FAILED diamond', async () => {
    api.loadCapacity.mockResolvedValue(
      ok({ ...board(), runner_profiles: { browser: profile({ available: false, disabled_reason: 'no GKE cluster' }) } }),
    )
    render(<ProfilesScreen />)
    await waitFor(() => expect(document.querySelector('.cap-mx tbody tr')).not.toBeNull(), WAIT)
    const cell = document.querySelector('.cap-mx td[data-label="Can start"]')!
    expect(cell.querySelector('.sk-st[data-tone="bad"]'), 'disabled is drawn as a failure').toBeNull()
    const mark = cell.querySelector('[data-mark]')
    expect(mark?.getAttribute('data-mark')).toBe('warn')
    expect(cell.textContent).toContain('disabled')
  })
})

// ---------------------------------------------------------------------------

function topology(): RuntimeTopology {
  return {
    runtimes: {
      mock: {
        name: 'mock',
        available: true,
        disabled_reason: '',
        image: 'agent-runtime-base',
        backend: 'CLOUD_RUN_JOB',
        resolved_backend: 'CLOUD_RUN_JOB',
        provider: null,
        secrets: [],
        secrets_any_of: false,
        timeout_seconds: 600,
        resource_class: 'standard',
        resources: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
      },
    },
    classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
    classesDetail: null,
    pools: [pool('global', 0, 10), pool('backend:CLOUD_RUN_JOB', 0, 10)],
    poolsDetail: null,
    profilePools: { mock: ['global', 'backend:CLOUD_RUN_JOB'] },
  } as unknown as RuntimeTopology
}

describe('Runtimes draws the cards first and Sizing last (#503)', () => {
  it('orders the runtime cards, then Backends, then Sizing', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const cards = document.querySelector('.rt-cards')
    const backends = document.querySelector('.rt-backends')
    const sizing = document.querySelector('.rt-sizing')
    expect(cards && backends && sizing, 'a Runtimes region is missing').toBeTruthy()
    expect(cards!.compareDocumentPosition(backends!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(backends!.compareDocumentPosition(sizing!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('lets Sizing’s Resolves from column wrap instead of clipping at the card edge', () => {
    expect(CAPACITY_CSS).toMatch(/\.rt-sizing td\[data-label="Resolves from"\]\s*\{[^}]*white-space:\s*normal/)
  })
})

// ---------------------------------------------------------------------------

function holders(leases: LeaseRow[], pools: Pool[] | null = [pool('global', 2, 10), pool('runner:codex', 2, 10)]): HoldersBoard {
  return { page: leasePage(leases), pools, poolsDetail: pools === null ? 'not read in this test' : null }
}

describe('Holders leads with the table, its filter, and drift per pool under it (#503)', () => {
  it('draws Every holder first and Accounting drift after it, and no Class mix', async () => {
    api.loadHolders.mockResolvedValue(ok(holders([lease(1, ['global', 'runner:codex']), lease(2, ['global', 'runner:codex'])])))
    render(<HoldersScreen />)
    const table = (await screen.findByText('Every holder', undefined, WAIT)).closest('section')!
    const drift = screen.getByText('Accounting drift').closest('section')!
    expect(table.compareDocumentPosition(drift) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText('Class mix')).toBeNull()
  })

  it('offers All and each tenant even when one tenant holds everything', async () => {
    api.loadHolders.mockResolvedValue(ok(holders([lease(1, ['global'])])))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const seg = document.querySelector('.hold-tenants')
    expect(seg, 'no tenant filter').not.toBeNull()
    expect([...seg!.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['All', 'eng'])
  })

  it('draws drift as a per-pool table of From leases, Counter and Delta, agreeing rows included', async () => {
    api.loadHolders.mockResolvedValue(
      ok(holders([lease(1, ['global', 'runner:codex']), lease(2, ['global'])], [pool('global', 2, 10), pool('runner:codex', 2, 10)])),
    )
    render(<HoldersScreen />)
    const drift = (await screen.findByText('Accounting drift', undefined, WAIT)).closest('section')!
    const heads = [...drift.querySelectorAll('thead th')].map((th) => th.textContent)
    expect(heads).toEqual(['Pool', 'From leases', 'Counter', 'Delta'])
    const row = (name: string) => [...drift.querySelectorAll('tbody tr')].find((tr) => tr.querySelector('th')?.getAttribute('title') === name)!
    expect(row('global').querySelector('td[data-label="Delta"]')!.textContent).toBe('0')
    const off = row('runner:codex')
    expect(off.querySelector('td[data-label="Delta"]')!.textContent).toContain('+1')
    expect(off.querySelector('td[data-label="Delta"] [data-mark="warn"]')).not.toBeNull()
    // And the callout above the table names the pool that disagrees.
    expect(document.querySelector('.hold-callout')?.textContent ?? '').toContain('runner:codex')
  })

  it('draws dispatched and awaiting as the half disc of the holding-capacity family', async () => {
    api.loadHolders.mockResolvedValue(
      ok(holders([lease(1, ['global']), lease(2, ['global'], { dispatch_state: 'LEASED' })], [pool('global', 2, 10)])),
    )
    render(<HoldersScreen />)
    const table = (await screen.findByText('Every holder', undefined, WAIT)).closest('section')!
    const cells = [...table.querySelectorAll('td[data-label="Dispatch"]')]
    expect(cells).toHaveLength(2)
    for (const c of cells) {
      const mark = c.querySelector('[data-mark]')
      expect(mark?.getAttribute('data-mark')).toBe('starting')
      expect(mark?.getAttribute('data-hue')).toBe('live')
      expect(c.querySelectorAll('[data-mark]').length, 'a second mark, the old grey dot chip, is still drawn').toBe(1)
    }
    expect(cells.map((c) => c.textContent).sort()).toEqual(['awaiting', 'dispatched'])
  })

  it('narrows to one tenant when its filter is pressed', async () => {
    api.loadHolders.mockResolvedValue(
      ok(holders([lease(1, ['global']), lease(2, ['global'], { tenant_id: 'research' })], [pool('global', 2, 10)])),
    )
    render(<HoldersScreen />)
    const table = (await screen.findByText('Every holder', undefined, WAIT)).closest('section')!
    fireEvent.click(screen.getByRole('button', { name: 'research' }))
    expect(table.querySelectorAll('tbody tr')).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------

function quota(over: Partial<QuotaState>): QuotaState {
  return {
    provider: 'anthropic',
    tenant_id: 'eng',
    state: 'AVAILABLE',
    updated_at: new Date(Date.now() - 60_000).toISOString(),
    configured_hard_max: 50,
    adaptive_target: null,
    quota_derived_limit: null,
    requests_remaining: 120,
    tokens_remaining: null,
    reset_at: null,
    cooldown_until: null,
    last_429_at: null,
    retry_after_seconds: null,
    success_count: 10,
    rate_limit_count: 0,
    effective_limit: 50,
    ...over,
  } as QuotaState
}

describe('Provider quota draws one word one way (#503)', () => {
  it('draws a fresh and a stale `available` with the same mark, and only the stale one carries its age', async () => {
    api.loadAdminQuota.mockResolvedValue(
      ok({
        quota: [
          quota({ tenant_id: 'eng' }),
          quota({ tenant_id: 'research', updated_at: new Date(Date.now() - 5 * 86_400_000).toISOString() }),
        ],
      }),
    )
    render(<QuotaDetailScreen />)
    await screen.findByRole('rowheader', { name: 'research' }, WAIT)
    const state = (tenant: string) =>
      screen.getByRole('rowheader', { name: tenant }).closest('tr')!.querySelector('td[data-label="State"]')!
    const fresh = state('eng').querySelector('.quota-state')!
    const stale = state('research').querySelector('.quota-state')!
    expect(fresh.outerHTML).toBe(stale.outerHTML)
    expect(fresh.getAttribute('data-tone')).toBe('neu')
    expect(state('eng').querySelector('.ctl-stale-mark')).toBeNull()
    expect(state('research').querySelector('.ctl-stale-mark')?.textContent).toContain('5d')
  })

  it('draws cooldown as the amber condition triangle', async () => {
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota({ state: 'COOLDOWN', effective_limit: 0 })] }))
    render(<QuotaDetailScreen />)
    const th = await screen.findByRole('rowheader', { name: 'eng' }, WAIT)
    expect(th.closest('tr')!.querySelector('td[data-label="State"] [data-mark="warn"]')).not.toBeNull()
  })

  it('fits the table to the page: fixed layout, the long pool name ellipsized', () => {
    expect(CAPACITY_CSS).toMatch(/\.quota-table\s*>\s*table\s*\{[^}]*table-layout:\s*fixed/)
    expect(CAPACITY_CSS).toMatch(/\.quota-feeds a\s*\{[^}]*text-overflow:\s*ellipsis/)
  })
})
