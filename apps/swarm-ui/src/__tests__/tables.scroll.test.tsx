// CH-13, RENDERED: THE TABLES THE OWNER NAMED SCROLL AT 390.
//
// The first CH-13 cascade test asked about a bare `.ctl-table.is-scroll`
// fixture, outside every screen, and it passed while the two tables the
// decision names -- Pools and Profile headroom -- did not scroll at all. CP-18
// gives both `table-layout: fixed` at `width: 100%` and lets their cells wrap
// at any character, and a fixed-layout table at 100% is sized to its wrapper:
// it never overflows, so the held first column has nothing to be held against,
// and at 390 each figure column was about 25px of content with "In use (units)"
// on three lines. These render the screens and ask the cascade (cssgate.ts)
// about the elements they drew, because jsdom applies no `@media` block.
//
// ALSO HERE: an expanded account's detail. It is one cell spanning the whole
// scrolling table -- about 909px of columns in a 358px phone -- so under
// `is-scroll` its prose and controls ran off the right edge, and the held-
// column rule made that one cell sticky as well.
//
// WHAT NONE OF THIS CAN SEE: a pixel. Whether the held column -- 45vw at 390,
// 175.5px, since 20ch of its face is wider there -- reads well under a thumb
// is for the next release's screenshots.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AccountsBoard } from '../api'
import type { Account, AccountsPage, Capacity, Pool, ProfileAdmission, RunnerProfile } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAccountsBoard: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { CapacityScreen } = await import('../Capacity')
const { ProfilesScreen } = await import('../Profiles')
const { AccountsScreen } = await import('../Accounts')

const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-25T10:00:00Z' }
}

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'global', hard_limit: 8, adaptive_target: null, quota_derived_limit: null,
    effective_limit: 8, active: 3, available: 5, enabled: true,
    updated_at: '2026-09-25T10:00:00Z', ...over,
  }
}

function admission(over: Partial<ProfileAdmission> = {}): ProfileAdmission {
  return {
    units: 1, headroom: 3, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(over: Partial<RunnerProfile> = {}): RunnerProfile {
  return {
    resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1,
    pools: ['global', 'tenant:eng', 'provider:anthropic:tenant:eng'], admission: admission(), ...over,
  }
}

const CAPACITY: Capacity = {
  pools: [
    pool({ name: 'global' }),
    pool({ name: 'tenant:eng' }),
    pool({ name: 'provider:anthropic:tenant:eng' }),
  ],
  runner_profiles: { 'claude-code': profile() },
  tenant_id: 'eng',
  generated_at: '2026-09-25T10:00:00Z',
}

/** The cells of a rendered table that are NOT its held first column. */
function heldAndRest(table: Element): { held: Element[]; rest: Element[] } {
  const held: Element[] = []
  const rest: Element[] = []
  for (const tr of table.querySelectorAll(':scope > thead > tr, :scope > tbody > tr')) {
    const cells = [...tr.children]
    if (cells[0] !== undefined) held.push(cells[0])
    rest.push(...cells.slice(1))
  }
  expect(held.length, 'the table drew no rows').toBeGreaterThan(1)
  expect(rest.length, 'the table drew one column').toBeGreaterThan(held.length)
  return { held, rest }
}

/**
 * THE PROPERTY THAT MAKES A TABLE SCROLL, on a table a screen drew: its layout
 * is sized by its content, and the cells beside the held column do not wrap,
 * so the table is wider than a 358px phone and the wrapper scrolls it. The
 * held column stays in view, on an opaque fill.
 */
function expectScrolls(table: Element, what: string): void {
  const wrap = table.parentElement!
  expect(wrap.classList.contains('is-scroll'), `${what} is not an is-scroll table`).toBe(true)
  expect(painted(wrap, ['overflow-x', 'overflow'], PHONE), `${what}'s wrapper does not scroll`).toBe('auto')

  // MUTATION: CP-18's `table-layout: fixed` back in charge below 900px.
  expect(painted(table, 'table-layout', PHONE), `${what} is sized to its wrapper, so it never overflows`).toBe('auto')
  expect(painted(table, 'width', PHONE), `${what} is held to its wrapper's width`).toBe('max-content')
  expect(painted(table, 'min-width', PHONE)).toBe('100%')

  const { held, rest } = heldAndRest(table)
  for (const cell of rest) {
    // MUTATION: CP-18's `white-space: normal; overflow-wrap: anywhere` back.
    expect(painted(cell, 'white-space', PHONE), `${what}: "${cell.textContent}" wraps`).toBe('nowrap')
    expect(painted(cell, 'overflow-wrap', PHONE), `${what}: "${cell.textContent}" breaks anywhere`).toBe('normal')
    expect(painted(cell, 'width', PHONE) ?? 'auto', `${what}: "${cell.textContent}" keeps a fixed share`).toBe('auto')
  }
  for (const cell of held) {
    expect(painted(cell, 'position', PHONE), `${what}: the held column is not held`).toBe('sticky')
    expect(painted(cell, 'left', PHONE)).toBe('0')
    expect(painted(cell, ['background-color', 'background'], PHONE) ?? '', `${what}: the held column is see-through`).toMatch(
      /^var\(--surface(-2)?\)$/,
    )
    // Its width is the held column's ceiling, not CP-18's percentage.
    expect(painted(cell, 'width', PHONE) ?? '', `${what}: the held column's width`).toMatch(/^min\(/)
    // AND THE CEILING IS ALSO ITS FLOOR (#222). A table cell's `width` counts
    // only toward its column's max-content width; a table laid out at its
    // min-content width -- any `width: 100%` table whose nowrap columns
    // overflow a phone -- gives the held column one character. These tables
    // are `max-content`, so they never showed it, but the floor is the shared
    // rule's and holds here too (`tables.held.test.tsx` has the tables that
    // did show it). MUTATION: drop `min-width` from the held-column rule.
    expect(painted(cell, 'min-width', PHONE), `${what}: the held column has no floor`).toBe(painted(cell, 'width', PHONE))
  }
}

describe('CH-13: the data tables the owner named scroll at 390', () => {
  it('scrolls every Pools family table with its pool column held, and keeps CP-18 on a wide screen', async () => {
    api.loadCapacity.mockResolvedValue(ok(CAPACITY))
    render(<CapacityScreen />)
    await screen.findAllByText('global', undefined, WAIT)
    const tables = [...document.querySelectorAll('.cap-families .ctl-table > table')]
    expect(tables.length, 'Pools drew no family table').toBeGreaterThan(0)
    for (const t of tables) expectScrolls(t, 'a Pools family table')
    // CP-18 still lines the six tables up where they fit.
    expect(painted(tables[0]!, 'table-layout', WIDE)).toBe('fixed')
  })

  it('scrolls every Profile headroom table with its pool column held, and keeps CP-18 on a wide screen', async () => {
    api.loadCapacity.mockResolvedValue(ok(CAPACITY))
    render(<ProfilesScreen />)
    await screen.findAllByText('claude-code', undefined, WAIT)
    const tables = [...document.querySelectorAll('.section.panel > dl.kv + .table-wrap > table.pools')]
    expect(tables.length, 'Profile headroom drew no headroom table').toBeGreaterThan(0)
    for (const t of tables) expectScrolls(t, 'a Profile headroom table')
    expect(painted(tables[0]!, 'table-layout', WIDE)).toBe('fixed')
  })
})

// ---------------------------------------------------------------------------

function account(over: Partial<Account> = {}): Account {
  return {
    account_id: 'eng:laptop',
    owner_tenant: 'eng',
    label: 'laptop',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

function board(accounts: Account[]): AccountsBoard {
  return {
    page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

describe("CH-13: an expanded account's detail stays inside what the phone shows", () => {
  it('holds the detail at the left of the scrolling table, one scrollport wide, and wraps its prose', async () => {
    // MUTATION: drop the detail's sticky rule or its width, or let the
    // held-column rule claim the detail's spanning cell again.
    api.loadAccountsBoard.mockResolvedValue(ok(board([account()])))
    render(<AccountsScreen />)
    const open = await screen.findByRole('button', { name: /laptop/ }, WAIT)
    if (open.getAttribute('aria-expanded') !== 'true') fireEvent.click(open)

    const cell = document.querySelector('.table-wrap.is-scroll tr.acct-detail-row > td[colspan]')
    expect(cell, 'the account did not open into a detail row').not.toBeNull()
    // The spanning cell is not the held column: a 909px sticky cell covers
    // the scrollport at every offset.
    expect(painted(cell!, 'position', PHONE), 'the detail row became the held column').not.toBe('sticky')

    const detail = cell!.querySelector('.acct-detail')!
    expect(detail, 'no .acct-detail').not.toBeNull()
    expect(painted(detail, 'position', PHONE), 'the detail scrolls away with the columns').toBe('sticky')
    expect(painted(detail, 'left', PHONE)).toBe('0')
    // One scrollport wide: the viewport less the page's two gutters and the
    // wrapper's two 1px borders. MUTATION: any width that is not the scrollport.
    expect(painted(detail, 'width', PHONE)).toBe('calc(100vw - 2 * var(--app-pad) - 2px)')
    // `table.pools td` is nowrap and a cell's descendants inherit it.
    expect(painted(detail, 'white-space', PHONE), 'the detail prose is one line per sentence').toBe('normal')
  })
})
