// LANE QAFIX6-CAPACITY (console visual QA run 6, wf_31aca00c977e4d37a935,
// 2026-10-23; part of #1680). One block per item, each pinning the result the
// finding asked for rather than the markup that happens to produce it:
//
//   a  6:capacity-009  Accounts' Model classes put each checkbox ~40 px from its
//                      own label, nearly the gap to the next pair
//   b  6:capacity-011  Pools' summary left out the `too small` pool Needs action
//                      lists
//   c  6:capacity-012  a 0 % Use bar drew a ~5 px dark cap, not G5-20's 1 px tick
//   d  6:capacity-010  Runtime cards in one row were ragged (box 6314114353)
//   e  box 6298212195  `← AUTO` sits beside its resolved backend, in one value
//   f  box 6314114637  a capacity table's sticky head leaves its first row in view
//
// Everything goes through `cascade` (cssgate.ts) via `painted`, not
// `getComputedStyle`: jsdom applies no `@media` and orders by source alone.

import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import CAPACITY_SHEET from '../styles/capacity.css?raw'
import type { AccountsBoard, ResourceClasses, RuntimeTopology, ServedAccount } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage, Capacity, Pool, Runtime } from '../types'
import { declarations, flatRules, type CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadAccountsBoard: vi.fn(),
  loadRuntimeTopology: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { AccountsScreen, MODEL_CLASSES } = await import('../Accounts')
const { RuntimesScreen } = await import('../Runtimes')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 5000 } as const

afterEach(() => {
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-23T10:00:00Z' }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-23T10:00:00Z', ...over,
  }
}

function capacity(pools: Pool[]): Capacity {
  return { tenant_id: 'eng', generated_at: '2026-10-23T10:00:00Z', pools_complete: true, pools, runner_profiles: {} } as Capacity
}

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
  large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
}

/** px of a length the sheet wrote as a literal, or of the token it names. */
function px(value: string | null): number {
  const m = /^(\d+(?:\.\d+)?)px$/.exec((value ?? '').trim())
  if (m === null) throw new Error(`not a px length: ${JSON.stringify(value)}`)
  return Number(m[1])
}

/** The column part of a `gap` (`row col`, or one value for both). */
function columnGap(value: string | null): number {
  const parts = (value ?? '').trim().split(/\s+/)
  return px(parts[parts.length - 1] ?? null)
}

// ---------------------------------------------------------------------------
// a. 6:capacity-009
// ---------------------------------------------------------------------------

function account(): ServedAccount {
  return {
    account_id: 'eng:primary', owner_tenant: 'eng', label: 'primary', provider: 'anthropic-subscription',
    state: 'AVAILABLE', reason: '', lend_to: [], assigned: 0, windows: {}, observed_at: null, stale: false,
    unreadable_by: [], unreadable_now: [], last_assigned_at: null, model_classes: [],
  }
}

function accountsBoard(accounts: Account[]): AccountsBoard {
  return {
    page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [{ tenant_id: 'eng' }],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

describe('a. 6:capacity-009: each Model classes checkbox sits beside its own label', () => {
  it('wraps each input and its words in one label, 6-8 px apart, with a wider gap between pairs', async () => {
    window.history.replaceState(null, '', '/capacity/accounts?account=eng%3Aprimary')
    api.loadAccountsBoard.mockResolvedValue(ok(accountsBoard([account()])))
    render(<AccountsScreen />)
    const pane = await screen.findByRole('region', { name: 'Account primary' }, WAIT)
    const set = pane.querySelector('fieldset.acct-classes')!
    expect(set, 'no Model classes editor drawn').not.toBeNull()

    for (const c of MODEL_CLASSES) {
      const box = within(pane).getByRole('checkbox', { name: c })
      const label = box.closest('label')!
      expect(label, `${c}: the checkbox is not inside a label`).not.toBeNull()
      expect(label.querySelectorAll('input').length, `${c}: one label holds two inputs`).toBe(1)
      expect(label.textContent?.trim(), `${c}: the label's words are not the class`).toBe(c)
    }

    // MUTATION: drop `width: auto` from `.acct-classes > .acct-class > input`
    // in styles/capacity.css and `.limit-edit input`'s 74px wins again -- the
    // ~40 px the finding measured between a box and its own word.
    const input = set.querySelector('input')!
    const label = input.closest('label')!
    for (const env of [WIDE, PHONE]) {
      expect(painted(input, 'width', env), 'the checkbox takes a text field\'s width').toBe('auto')
      const inside = columnGap(painted(label, ['gap', 'column-gap'], env))
      expect(inside, 'box to its own label').toBeGreaterThanOrEqual(6)
      expect(inside, 'box to its own label').toBeLessThanOrEqual(8)
      expect(columnGap(painted(set, ['gap', 'column-gap'], env)), 'between pairs').toBeGreaterThanOrEqual(2 * inside)
    }
  })
})

// ---------------------------------------------------------------------------
// b. 6:capacity-011
// ---------------------------------------------------------------------------

describe('b. 6:capacity-011: the Pools summary counts the too-small pool Needs action lists', () => {
  it('reads `1 too small` for resource:large below one large task', async () => {
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
    api.loadCapacity.mockResolvedValue(
      ok(capacity([pool('global', 1, 40), pool('tenant:eng', 1, 5), pool('resource:standard', 0, 8), pool('resource:large', 0, 2)])),
    )
    render(<CapacityScreen />)
    await waitFor(() => expect(document.querySelector('.cap-needs')?.textContent ?? '').toContain('too small'), WAIT)
    // MUTATION: delete the `too small` clause from `PoolsSummary` (Capacity.tsx)
    // and the summary reads `4 pools in 3 families · tenant eng` again.
    const clauses = [...document.querySelectorAll('.cap-clause')].map((c) => c.textContent)
    expect(clauses).toContain('1 too small')
  })
})

// ---------------------------------------------------------------------------
// c. 6:capacity-012
// ---------------------------------------------------------------------------

describe('c. 6:capacity-012: a 0 % Use bar is a 1 px tick and nothing more', () => {
  it('draws no 3 px axis border beside the tick, at either width', async () => {
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
    api.loadCapacity.mockResolvedValue(ok(capacity([pool('global', 1, 40), pool('tenant:eng', 0, 5)])))
    render(<CapacityScreen />)
    await waitFor(() => expect(document.querySelector('.cap-use .ctl-util-track.is-zero')).not.toBeNull(), WAIT)
    const zero = document.querySelector('.cap-use .ctl-util-track.is-zero')!
    const tick = zero.querySelector('.ctl-util-zero')!
    for (const env of [WIDE, PHONE]) {
      expect(painted(tick, 'width', env)).toBe('1px')
      // MUTATION: drop `border-left-width: 0` from `.cap-use
      // .ctl-util-track.is-zero` and the shared track's 3px `--track-axis`
      // wins: 3px of axis plus the 1px tick is the ~5 px dark cap run 6 saw.
      const axis = painted(zero, ['border-left-width', 'border-left', 'border-width', 'border'], env)
      expect(axis, 'the zero track keeps its 3px axis').toMatch(/^0(px)?$/)
    }
  })
})

// ---------------------------------------------------------------------------
// d, e. Runtimes
// ---------------------------------------------------------------------------

function runtime(name: string, over: Partial<Runtime> = {}): Runtime {
  return {
    name, available: true, disabled_reason: '', image: 'agent-runtime-base', backend: 'CLOUD_RUN_JOB',
    resolved_backend: 'CLOUD_RUN_JOB', provider: null, secrets: [], secrets_any_of: false, timeout_seconds: 600,
    resource_class: 'standard', resources: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
    ...over,
  } as Runtime
}

async function renderRuntimes(): Promise<void> {
  api.loadRuntimeTopology.mockResolvedValue(
    ok({
      runtimes: { alpha: runtime('alpha', { backend: 'AUTO' }), beta: runtime('beta') },
      classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
      classesDetail: null,
      pools: [],
      poolsDetail: null,
      profilePools: { alpha: ['global'], beta: ['global'] },
      profileUncapped: { alpha: [], beta: [] },
    } as unknown as RuntimeTopology),
  )
  render(<RuntimesScreen />)
  await screen.findByText('Sizing', undefined, WAIT)
}

describe('d. 6:capacity-010 (box 6314114353): runtime cards in a row share its height', () => {
  it('stretches every card to its grid row', async () => {
    await renderRuntimes()
    const grid = document.querySelector('.rt-cards > .ctl-cards')!
    const cards = [...grid.querySelectorAll(':scope > .ctl-card')]
    expect(cards.length).toBe(2)
    // MUTATION: put `align-items: start` on `.rt-cards > .ctl-cards` (the rule
    // QAFIX6-CAPACITY-2 retired) and each card is as tall as its own facts
    // again -- the ragged row.
    expect(painted(grid, 'align-items', WIDE)).toBe('stretch')
    // MUTATION: give a card an `align-self` of its own (`start`, `center`) and
    // that card stops at its facts while its neighbour fills the row.
    for (const card of cards) expect(painted(card, 'align-self', WIDE) ?? 'auto').toMatch(/^(auto|stretch)$/)
  })
})

describe('e. box 6298212195: `← AUTO` reads beside its resolved backend', () => {
  it('holds the declared and the resolved backend in one value element, beside `runs on`', async () => {
    await renderRuntimes()
    // In words since QA2-FIX-21 (capacity-14, cap2-10).
    const declared = screen.getByText('chosen automatically')
    const value = declared.parentElement!
    expect(value.querySelector('.rt-backend')?.textContent).toBe('Cloud Run')
    const fact = value.closest('li.ctl-fact')!
    expect(fact.querySelector(':scope > b')?.textContent).toBe('runs on')
    expect(value, 'the declared backend is its own item of the fact').not.toBe(fact)
    // MUTATION: put `flex-wrap: wrap` back on `.rt-runs-on` (styles/capacity.css)
    // and the whole value can drop under `runs on` again.
    expect(painted(fact, 'flex-wrap', WIDE)).toBe('nowrap')
    expect(painted(value, 'flex-wrap', WIDE)).toBe('wrap')
  })
})

// ---------------------------------------------------------------------------
// f. box 6314114637
// ---------------------------------------------------------------------------

describe('f. box 6314114637: a capacity table\'s sticky head leaves its first row in view', () => {
  it('stops a scroll one head-height below the bar, and the head is that one height', () => {
    const hosts: HTMLElement[] = []
    const tr = build('div.cap-families > div.ctl-table.is-scroll > table > thead > tr', hosts)
    const thead = tr.parentElement!
    expect(painted(thead, 'position', WIDE)).toBe('sticky')
    expect(painted(tr, 'height', WIDE)).toBe('var(--row-h)')
    // MUTATION: delete the `.ctl-scroll:has(.cap-families ...)` rule's
    // `scroll-padding-top` (styles/capacity.css) and a row scrolled into view
    // stops under the stuck head again, as run 3's capacity-007 measured.
    const padded = flatRules(CAPACITY_SHEET).filter(
      (r) =>
        r.conditions.some((c) => /min-width:\s*900px/.test(c)) &&
        r.selector.includes('.ctl-scroll:has(.cap-families') &&
        declarations(r.body).some(
          (d) => d.property === 'scroll-padding-top' && d.value.replace(/\s+/g, ' ') === 'calc(var(--ctl-bar-h, 0px) + var(--row-h))',
        ),
    )
    expect(padded.length, 'no scroller padding clears the families\' sticky head').toBe(1)
    // The matrix is its own scroller, padded by the same token.
    const mx = build('div.cap-mx > div.ctl-table.is-scroll', hosts)
    expect(painted(mx, 'scroll-padding-top', WIDE)).toBe('var(--row-h)')
    for (const h of hosts) h.remove()
  })
})
