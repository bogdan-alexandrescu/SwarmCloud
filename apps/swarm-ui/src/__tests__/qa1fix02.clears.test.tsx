// VISUAL QA PASS 1, LANE QA1-FIX-02, capacity-03 (part of #2596): a window
// reset already in the past, with nothing read since, was said as the time the
// account clears. On the fixture console on 2026-10-28 the five-hour-floor row
// read "Clears Oct 19 12:40Z · now", the verdict "five-hour-floor frees first,
// at Oct 19 12:40Z", and the Timeline drew "clears Oct 19 12:40Z" right of
// "now 09:35Z" -- while the same row said No.
//
// What this file pins, at the table, the verdict, the sheet and the Timeline:
// a passed clock says it passed and that the next reading is when it clears;
// the verdict names only a time still to come as when capacity frees.
//
// MUTATION: make `clockPassed` return false -- every case finds the past time
// said as a clearing time again. Drop the `filter` in `verdictOf` -- the
// verdict case finds "frees first, at <a past time>". Drop the passed branch
// in `bandWords` -- the Timeline case finds "clears <a past time>".

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  setAccountMaxAgents: vi.fn(),
}))
vi.mock('../api', () => api)

const { AccountsScreen } = await import('../Accounts')
const pool = await import('../accountsPool')

const WAIT = { timeout: 5000 } as const
const H = 3_600_000
const D = 24 * H
// Pinned so the dates below are fixed words: the fixture clock was Oct 19
// 12:00Z and the sweep read the page on Oct 28.
const NOW = Date.parse('2026-10-28T09:35:00Z')
const iso = (t: number) => new Date(t).toISOString()
const READ = NOW - 9 * D // the fixture's last reading: Oct 19 11:56Z
const GONE = Date.parse('2026-10-19T12:40:00Z')
const LATER = NOW + 2 * H // 11:35Z, still to come

function acct(label: string, over: Partial<PoolAccount> = {}): PoolAccount {
  return {
    account_id: `eng:${label}`,
    owner_tenant: 'eng',
    label,
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: iso(NOW - 3 * 60_000),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: iso(NOW - H),
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: null,
    ...over,
  }
}

/** five-hour-floor as the fixture serves it on a running clock. */
const passed = acct('five-hour-floor', {
  observed_at: iso(READ - 4 * 60_000),
  can_take_new_agent: false,
  blocked_by: [{ window: 'five_hour', used: 0.92, floor: 0.15, resets_at: iso(GONE), aged_reading: false }],
  next_eligible_at: iso(GONE),
  windows: { five_hour: { utilization: 0.92, resets_at: iso(GONE), reset: false } },
})
/** A floor whose reset is still to come, read just now. */
const coming = acct('later-floor', {
  can_take_new_agent: false,
  blocked_by: [{ window: 'five_hour', used: 0.9, floor: 0.15, resets_at: iso(LATER), aged_reading: false }],
  next_eligible_at: iso(LATER),
  windows: { five_hour: { utilization: 0.9, resets_at: iso(LATER), reset: false } },
})

function board(accounts: PoolAccount[]): Result<AccountsBoard> {
  return {
    status: 'ok',
    fetchedAt: NOW,
    serverAt: iso(NOW),
    data: {
      page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: NOW,
    },
  } as Result<AccountsBoard>
}

async function mount(accounts: PoolAccount[]) {
  api.loadAccountsBoard.mockResolvedValue(board(accounts))
  api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: NOW })
  render(<AccountsScreen />)
  return screen.findByRole('region', { name: 'Can the pool take a new agent' }, WAIT)
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(NOW))
  window.history.replaceState(null, '', '/#capacity/accounts?view=table')
})
afterEach(() => {
  vi.useRealTimers()
  window.localStorage.clear()
})

describe('capacity-03: a reset already past is not when the account clears', () => {
  it('knows a passed clock from one still to come, and from one a later reading stands by', () => {
    expect(pool.clockPassed(passed, NOW)).toBe(true)
    expect(pool.clockPassed(coming, NOW)).toBe(false)
    // Read after the reset and still blocked: the broker's word, so it stands.
    expect(pool.clockPassed({ ...passed, observed_at: iso(GONE + 60_000) }, NOW)).toBe(false)
  })

  it('names in the verdict only a time still to come', async () => {
    const v = pool.verdictOf([passed, coming], 'eng', 0, NOW)
    expect(v.next?.account.label).toBe('later-floor')
    expect(pool.verdictOf([passed], 'eng', 0, NOW).next).toBeNull()
    const verdict = await mount([passed, coming])
    expect(verdict.textContent).not.toContain('Oct 19 12:40Z')
    expect(verdict.textContent).toContain('Next: later-floor at 11:35Z')
  })

  it('says the row and the sheet clear at the next reading, the reset passed', async () => {
    await mount([passed])
    const row = document.querySelector<HTMLElement>('button.acct-open[data-account="eng:five-hour-floor"]')!.closest('tr')!
    expect(row.textContent).not.toMatch(/Oct 19 12:40Z\s*·?\s*now/)
    expect(row.textContent).toContain('at the next reading')
    expect(row.textContent).toContain('reset Oct 19 12:40Z passed')
    fireEvent.click(row.querySelector('button.acct-open')!)
    const sheet = document.querySelector<HTMLElement>('.acct-sheet')!
    const clears = [...sheet.querySelectorAll('.acct-sv-why')].find((p) => p.textContent?.startsWith('Clears:'))!
    expect(clears.textContent).toBe('Clears: at the next reading · reset Oct 19 12:40Z passed, nothing read since')
    // The window itself: a reset that has gone, not one to come.
    expect(sheet.querySelector('.acct-ss-wins')!.textContent).toContain('reset Oct 19 12:40Z passed')
    expect(sheet.querySelector('.acct-ss-wins')!.textContent).not.toContain('resets Oct 19 12:40Z')
  })

  it('draws no "clears <a past time>" on the Timeline', async () => {
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount([passed, coming])
    const tl = await screen.findByRole('region', { name: 'Subscription accounts, timeline' }, WAIT)
    await waitFor(() => expect(tl.querySelector('.acct-tl-lane')).not.toBeNull(), WAIT)
    expect(tl.textContent).not.toContain('clears Oct 19 12:40Z')
    expect(tl.textContent).toContain('reset Oct 19 12:40Z passed, nothing read since')
    expect(within(tl).getAllByText(/clears/).some((el) => el.textContent?.includes('11:35Z'))).toBe(true)
  })
})
