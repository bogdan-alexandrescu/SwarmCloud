// #2222, ACCOUNTS-TIMELINE: THE TABLE / TIMELINE TOGGLE AND THE TIMELINE VIEW.
//
// BGD, 2026-10-24 17:33Z: build proposal A with a Table / Timeline toggle
// whose Timeline draws every account the way proposal B does -- one lane per
// account on a split axis (the next 12 hours, then the week), each window a
// band against its floor, a marker where it clears, a next-free-slot line
// through every lane, the pool row counting who can take an agent over time.
// "Table default, remembered": the page opens on Table, the last choice is
// remembered per browser and in the address (`?view=timeline`).
//
// BREAK IT: open on Timeline by default, forget the choice, drop `view` from
// the address, draw the pace row from a second forecast that disagrees with
// the tiles, count a full account into the pool row, name another
// workspace's account on a duplicate lane, or open a lane into anything but
// the detail the table opens -- a case below turns red.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { AccountsPage } from '../types'
import type { PoolAccount } from '../accountsPool'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  loadTasksInState: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
}))
vi.mock('../api', () => api)

const { AccountsScreen } = await import('../Accounts')
const pool = await import('../accountsPool')
const tl = await import('../accountsTimeline')
const { VIEW_KEY, AccountsTimeline } = await import('../AccountsTimeline')
const { canonical, fromAddress } = await import('../App')

const WAIT = { timeout: 5000 } as const
const H = 3_600_000
const D = 24 * H
const iso = (t: number) => new Date(t).toISOString()

function acct(label: string, over: Partial<PoolAccount> = {}, now = Date.now()): PoolAccount {
  return {
    account_id: `eng:${label}`,
    owner_tenant: 'eng',
    label,
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {
      five_hour: { utilization: 0.3, resets_at: iso(now + 3 * H), reset: false },
      seven_day: { utilization: 0.5, resets_at: iso(now + 3 * D), reset: false },
    },
    observed_at: new Date(now - 5 * 60_000).toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: new Date(now - H).toISOString(),
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: null,
    ...over,
  }
}

/** Under its 7-day floor until `resets`. */
function weekly(label: string, used: number, resets: number, now: number, over: Partial<PoolAccount> = {}): PoolAccount {
  return acct(
    label,
    {
      can_take_new_agent: false,
      blocked_by: [{ window: 'seven_day', used, floor: 0.05, resets_at: iso(resets) }],
      next_eligible_at: iso(resets),
      windows: {
        five_hour: { utilization: 0.4, resets_at: iso(now - 4 * H), reset: true },
        seven_day: { utilization: used, resets_at: iso(resets), reset: false },
      },
      ...over,
    },
    now,
  )
}

function full(label: string, now: number): PoolAccount {
  return acct(label, { assigned: 6, can_take_new_agent: false, blocked_by: [{ cap: 6, max_agents: 6 }] }, now)
}

function board(accounts: PoolAccount[]): Result<AccountsBoard> {
  const now = Date.now()
  return {
    status: 'ok',
    fetchedAt: now,
    serverAt: iso(now),
    data: {
      page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: now,
    },
  }
}

// 2026-10-24 09:04Z, the reading #2222 took.
const AT = Date.parse('2026-10-24T09:04:00Z')

describe('the split axis: the next 12 hours, then the week', () => {
  it('gives the first 26% of a track to the next 12 hours and the rest to the week', () => {
    expect(tl.axisX(AT, AT)).toBe(0)
    expect(tl.axisX(AT - H, AT)).toBe(0)
    expect(tl.axisX(AT + 6 * H, AT)).toBeCloseTo(13, 5)
    expect(tl.axisX(AT + 12 * H, AT)).toBeCloseTo(26, 5)
    expect(tl.axisX(AT + 90 * H, AT)).toBeCloseTo(26 + (78 / 156) * 74, 5)
    expect(tl.axisX(AT + 7 * D, AT)).toBe(100)
    expect(tl.axisX(AT + 9 * D, AT)).toBe(100)
  })

  it('labels now, the break and each midnight of the week, and nothing past the end', () => {
    const ticks = tl.axisTicks(AT)
    expect(ticks[0]).toMatchObject({ x: 0, label: 'now 09:04Z' })
    const labels = ticks.map((t) => t.label)
    expect(labels).toContain('Sun 25')
    expect(labels).toContain('Fri 30')
    expect(ticks.every((t) => t.x >= 0 && t.x <= 100)).toBe(true)
    // Ticks in the near zone sit inside it; the week's start after it.
    expect(ticks.filter((t) => t.label === 'Mon 26')[0]!.x).toBeGreaterThan(26)
  })
})

describe('a window as a band', () => {
  it('draws the share left over its floor, blocked when the broker says it blocks', () => {
    const a = weekly('ai-eng-3', 0.97, AT + 3 * D, AT)
    const [week, five] = tl.bandsOf(a, AT)
    expect(week).toMatchObject({ key: 'seven_day', kind: 'reading', blocked: true, aged: false, floor: 0.05 })
    expect(week!.left).toBeCloseTo(0.03, 5)
    expect(week!.x).toBeCloseTo(tl.axisX(AT + 3 * D, AT), 5)
    // A reset window is full, with its reset time, never its old figure.
    expect(five).toMatchObject({ key: 'five_hour', kind: 'reset' })
    expect(five!.left).toBeUndefined()
  })

  it('counts an aged reading at half and says so', () => {
    const a = acct(
      'old',
      {
        stale: true,
        can_take_new_agent: false,
        blocked_by: [{ window: 'seven_day', used: 0.92, floor: 0.05, resets_at: iso(AT + D), aged_reading: true }],
        windows: { seven_day: { utilization: 0.92, resets_at: iso(AT + D), reset: false } },
      },
      AT,
    )
    const [week] = tl.bandsOf(a, AT)
    expect(week).toMatchObject({ blocked: true, aged: true })
  })

  it('draws no band for a never-measured account, and a window the provider does not serve as absent', () => {
    expect(tl.bandsOf(acct('new', { observed_at: null }, AT), AT).map((b) => b.kind)).toEqual(['pending', 'pending'])
    const fiveOnly = acct('five', { windows: { five_hour: { utilization: 0.2, resets_at: iso(AT + H), reset: false } } }, AT)
    expect(tl.bandsOf(fiveOnly, AT).map((b) => [b.key, b.kind])).toEqual([
      ['seven_day', 'absent'],
      ['five_hour', 'reading'],
    ])
  })

  it('keeps a window the console does not know by name, after the two it does', () => {
    const a = acct(
      'team',
      {
        windows: {
          seven_day: { utilization: 0.5, resets_at: iso(AT + D), reset: false },
          five_hour: { utilization: 0.1, resets_at: iso(AT + H), reset: false },
          harbor_lantern: { utilization: 0.2, resets_at: iso(AT - 9 * D), reset: true },
        },
      },
      AT,
    )
    expect(tl.bandsOf(a, AT).map((b) => [b.key, b.known])).toEqual([
      ['seven_day', true],
      ['five_hour', true],
      ['harbor_lantern', false],
    ])
  })
})

describe('the pool row and the next free slot', () => {
  it('counts the accounts that can take an agent over time, by the clock, and never a full one', () => {
    const accounts = [
      acct('open', {}, AT),
      weekly('soon', 0.98, AT + 2 * H, AT),
      weekly('later', 0.97, AT + 2 * D, AT),
      full('full', AT),
    ]
    expect(tl.openRow(accounts, 'eng', AT)).toEqual([
      { from: AT, to: AT + 2 * H, open: 1 },
      { from: AT + 2 * H, to: AT + 2 * D, open: 2 },
      { from: AT + 2 * D, to: AT + 7 * D, open: 3 },
    ])
  })

  it('puts the next free slot at now when any account can take one, else at the first clock', () => {
    expect(tl.nextSlotAt([acct('open', {}, AT), full('full', AT)], 'eng', AT)).toBe(AT)
    expect(tl.nextSlotAt([weekly('later', 0.97, AT + 2 * D, AT), weekly('soon', 0.98, AT + 2 * H, AT)], 'eng', AT)).toBe(AT + 2 * H)
    expect(tl.nextSlotAt([full('full', AT)], 'eng', AT)).toBeNull()
  })

  it('draws the pace row from the one forecast the tiles read: dark exactly when forecastOf says', () => {
    // fast: 90% of a week opened 120h ago, so 0.75%/h, 5% above its floor lasts 6h40m.
    const fast = acct('fast', { windows: { seven_day: { utilization: 0.9, resets_at: iso(AT + 2 * D), reset: false } } }, AT)
    const spent = weekly('spent', 0.97, AT + D, AT)
    const f = pool.forecastOf([fast, spent], 'eng', AT)
    const row = tl.paceRow(f, AT)!
    const dark = row.find((s) => s.open === 0)!
    expect(dark.from).toBe(f.darkAt)
    expect(dark.to).toBe(f.backAt)
    expect(row[0]).toMatchObject({ from: AT, open: 1 })
    // Unmeasured: no pace to draw, so no row rather than a guessed one.
    expect(tl.paceRow(pool.forecastOf([acct('new', { observed_at: null }, AT)], 'eng', AT), AT)).toBeNull()
  })
})

describe('a duplicate lane names only this workspace’s accounts', () => {
  it('names the other registration inside the workspace, and never another workspace’s', () => {
    const older = acct('saga-personal', {}, AT)
    const copy = acct('team', { can_take_new_agent: false, blocked_by: [{ duplicate_of: 'eng:saga-personal', reason: 'same organisation' }] }, AT)
    expect(tl.dupMark(copy, [older, copy])).toEqual({ kind: 'withheld', other: 'saga-personal' })
    expect(tl.dupMark(older, [older, copy])).toEqual({ kind: 'handed', other: 'team' })
    const lent = acct('theirs', { owner_tenant: 'ops', account_id: 'ops:theirs' }, AT)
    const mine = acct('mine', { can_take_new_agent: false, blocked_by: [{ duplicate_of: 'ops:theirs', reason: 'x' }] }, AT)
    expect(tl.dupMark(mine, [lent, mine])).toEqual({ kind: 'withheld', other: null })
    const maybe = acct('maybe', { possible_duplicate: { of: 'eng:open', reason: 'identical readings' } }, AT)
    const open = acct('open', {}, AT)
    expect(tl.dupMark(maybe, [maybe, open])).toEqual({ kind: 'possible', other: 'open' })
    // Both lanes of a possible pair are flagged, as B draws the pair.
    expect(tl.dupMark(open, [maybe, open])).toEqual({ kind: 'possible', other: 'maybe' })
  })
})

describe('the toggle: Table default, remembered', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.history.replaceState(null, '', '/capacity/accounts')
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  })
  afterEach(() => {
    window.localStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  it('opens on Table, with the toggle beside the verdict', async () => {
    api.loadAccountsBoard.mockResolvedValue(board([acct('a'), weekly('b', 0.98, Date.now() + 2 * H, Date.now())]))
    render(<AccountsScreen />)
    const toggle = await screen.findByRole('group', { name: 'Show the accounts as' }, WAIT)
    expect(within(toggle).getByRole('button', { name: 'Table' }).getAttribute('aria-pressed')).toBe('true')
    expect(document.querySelector('.acct-rank, .acct-cards')).toBeTruthy()
    expect(document.querySelector('.acct-tl')).toBeNull()
    // Beside the verdict: the next thing after it.
    expect(document.querySelector('.acct-verdict')?.nextElementSibling?.contains(toggle)).toBe(true)
  })

  it('opens on Timeline from the address, and remembers a click in the browser and the address', async () => {
    api.loadAccountsBoard.mockResolvedValue(board([acct('a')]))
    window.history.replaceState(null, '', '/capacity/accounts?view=timeline')
    const { unmount } = render(<AccountsScreen />)
    await screen.findByRole('region', { name: 'Accounts on a time axis' }, WAIT)
    unmount()

    window.history.replaceState(null, '', '/capacity/accounts')
    render(<AccountsScreen />)
    const toggle = await screen.findByRole('group', { name: 'Show the accounts as' }, WAIT)
    expect(document.querySelector('.acct-tl')).toBeNull()
    act(() => {
      fireEvent.click(within(toggle).getByRole('button', { name: 'Timeline' }))
    })
    await screen.findByRole('region', { name: 'Accounts on a time axis' }, WAIT)
    expect(window.localStorage.getItem(VIEW_KEY)).toBe('timeline')
    expect(window.location.hash).toContain('view=timeline')
    act(() => {
      fireEvent.click(within(toggle).getByRole('button', { name: 'Table' }))
    })
    await waitFor(() => expect(document.querySelector('.acct-tl')).toBeNull(), WAIT)
    expect(window.localStorage.getItem(VIEW_KEY)).toBe('table')
    expect(window.location.hash).not.toContain('view=')
  })

  it('opens on the browser’s last choice when the address does not say', async () => {
    window.localStorage.setItem(VIEW_KEY, 'timeline')
    api.loadAccountsBoard.mockResolvedValue(board([acct('a')]))
    render(<AccountsScreen />)
    await screen.findByRole('region', { name: 'Accounts on a time axis' }, WAIT)
  })

  it('keeps `view` on the canonical address, beside `account`', () => {
    expect(canonical(fromAddress('capacity/accounts?account=eng%3Aa&view=timeline&junk=1'))).toBe('capacity/accounts?account=eng%3Aa&view=timeline')
  })
})

describe('the timeline as drawn', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.history.replaceState(null, '', '/capacity/accounts?view=timeline')
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  })
  afterEach(() => {
    window.localStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  it('draws one lane per account in the table’s order, the pool row, and the forecast the table shows', async () => {
    const now = Date.now()
    api.loadAccountsBoard.mockResolvedValue(
      board([full('full', now), weekly('later', 0.97, now + 2 * D, now), acct('open', {}, now), weekly('soon', 0.98, now + 2 * H, now)]),
    )
    render(<AccountsScreen />)
    const region = await screen.findByRole('region', { name: 'Accounts on a time axis' }, WAIT)
    const lanes = [...region.querySelectorAll<HTMLElement>('.acct-tl-lane')].map((l) => l.dataset.account)
    expect(lanes).toEqual(['eng:open', 'eng:soon', 'eng:later', 'eng:full'])
    expect(within(region).getByText('Accounts that can take an agent')).toBeTruthy()
    // The full lane says its holds and that no clock frees it.
    const fullLane = region.querySelector('[data-account="eng:full"]') as HTMLElement
    expect(fullLane.textContent).toContain('Full: 6 of 6 agents')
    expect(fullLane.textContent).toContain('6 of 6 agents hold it')
    expect(fullLane.textContent).toContain('no clock')
    // The Forecast section is the table's, drawn once.
    expect(screen.getAllByRole('region', { name: 'Forecast' })).toHaveLength(1)
  })

  it('opens the same detail the table opens when a lane’s name is chosen', async () => {
    const now = Date.now()
    api.loadAccountsBoard.mockResolvedValue(board([acct('a', {}, now), weekly('b', 0.98, now + 2 * H, now)]))
    render(<AccountsScreen />)
    const region = await screen.findByRole('region', { name: 'Accounts on a time axis' }, WAIT)
    act(() => {
      fireEvent.click(within(region).getByRole('button', { name: 'b' }))
    })
    await screen.findByRole('region', { name: 'Account b' }, WAIT)
    expect(window.location.hash).toContain('account=eng%3Ab')
    expect(window.location.hash).toContain('view=timeline')
  })

  it('draws a stale reading pale and says how old it is', async () => {
    const now = Date.now()
    api.loadAccountsBoard.mockResolvedValue(
      board([acct('old', { stale: true, observed_at: new Date(now - 41 * 60_000).toISOString() }, now)]),
    )
    render(<AccountsScreen />)
    const region = await screen.findByRole('region', { name: 'Accounts on a time axis' }, WAIT)
    const lane = region.querySelector('[data-account="eng:old"]') as HTMLElement
    expect(lane.classList.contains('is-stale')).toBe(true)
    expect(lane.textContent).toContain('41 min')
  })

  it('draws the empty pool as the axis with no lanes, keeping the empty state’s words', async () => {
    api.loadAccountsBoard.mockResolvedValue(board([]))
    render(<AccountsScreen />)
    const region = await screen.findByRole('region', { name: 'Accounts on a time axis' }, WAIT)
    expect(region.querySelectorAll('.acct-tl-lane')).toHaveLength(0)
    expect(within(region).getByText('No accounts registered')).toBeTruthy()
  })
})

describe('the waiting agents, queued against the next free slot', () => {
  const ok = (agents: number) => ({ kind: 'ok' as const, waiting: { agents, later: 0, atLeast: false } })
  const draw = (accounts: PoolAccount[], waiting: Parameters<typeof AccountsTimeline>[0]['waiting']) =>
    render(<AccountsTimeline accounts={accounts} scope="eng" now={AT} waiting={waiting} chosen={null} phone={false} onChoose={() => {}} />)

  it('starts as many as the free slots now and queues the rest at the first clock', () => {
    const { container } = draw([acct('open', { assigned: 2 }, AT), weekly('soon', 0.98, AT + 2 * H, AT)], ok(10))
    const pills = [...container.querySelectorAll('.acct-tl-qq')].map((p) => p.textContent)
    expect(pills[0]).toContain('4 can start now')
    expect(pills[1]).toContain('6 queue here')
    expect(pills[1]).toContain('soon opens at 11:04Z')
  })

  it('says a failed waiting read as a dash, never 0, and an empty pool as no account registered', () => {
    const failed = draw([acct('a', {}, AT)], { kind: 'error', message: 'offline' })
    expect(failed.container.querySelector('.acct-tl-qq')?.textContent).toContain('could not read the waiting list')
    failed.unmount()
    const empty = draw([], ok(3))
    expect(empty.container.querySelector('.acct-tl-qq')?.textContent).toContain('3 wait')
    expect(empty.container.querySelector('.acct-tl-qq')?.textContent).toContain('no account is registered')
  })
})
