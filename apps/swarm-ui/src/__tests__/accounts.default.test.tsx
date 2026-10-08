// #503: THE ACCOUNTS PAGE OPENED ON AN ARBITRARY ACCOUNT -- the first by its
// opaque `<tenant>:<label>` id. It opens on the account that needs attention
// (sign-in expired, then unreadable for this tenant, then exhausted), else the
// most used (most agents holding it now), else the first by label.
//
// BREAK IT: drop the exhausted rank from `attentionRank`, sort the fallback by
// `account_id`, or skip the `assigned` step -- a case below turns red.

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage } from '../types'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
}))
vi.mock('../api', () => api)

const { AccountsScreen, attentionRank, defaultAccount } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const
const NOW = Date.now()
const LATER = new Date(NOW + 3_600_000).toISOString()

function account(over: Partial<Account>): Account {
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

/** A measured account whose five-hour window is at `utilization` (0-1). */
function used(id: string, label: string, utilization: number, over: Partial<Account> = {}): Account {
  return account({
    account_id: id,
    label,
    observed_at: new Date(NOW - 60_000).toISOString(),
    windows: { five_hour: { utilization, resets_at: LATER, reset: false } },
    ...over,
  })
}

const label = (a: Account | null) => a?.label ?? null

describe('attentionRank: who needs a person, most urgent first', () => {
  it('ranks an expired sign-in, then unreadable, then exhausted; a healthy account is null', () => {
    expect(attentionRank(account({ state: 'REAUTH_REQUIRED' }), 'eng')).toBe(0)
    expect(attentionRank(account({ unreadable_now: ['eng'] }), 'eng')).toBe(1)
    expect(attentionRank(used('eng:x', 'x', 1), 'eng')).toBe(2)
    expect(attentionRank(used('eng:x', 'x', 0.99), 'eng')).toBeNull()
    expect(attentionRank(account({ state: 'PAUSED' }), 'eng')).toBeNull()
  })

  it('does not call a window that has already reset exhausted', () => {
    const refilled = used('eng:x', 'x', 1)
    refilled.windows.five_hour!.reset = true
    expect(attentionRank(refilled, 'eng')).toBeNull()
  })

  it('reads unreadable for this tenant only', () => {
    expect(attentionRank(account({ unreadable_now: ['ops'] }), 'eng')).toBeNull()
  })
})

describe('defaultAccount: the rule the page opens by', () => {
  it('opens on the account that needs attention before the most used', () => {
    const busy = account({ account_id: 'eng:a', label: 'alpha', assigned: 4 })
    const spent = used('eng:z', 'zulu', 1)
    expect(label(defaultAccount([busy, spent], 'eng'))).toBe('zulu')
  })

  it('puts an expired sign-in before an exhausted account, whatever the names', () => {
    const spent = used('eng:a', 'alpha', 1)
    const expired = account({ account_id: 'eng:z', label: 'zulu', state: 'REAUTH_REQUIRED' })
    expect(label(defaultAccount([spent, expired], 'eng'))).toBe('zulu')
  })

  it('takes the first by label among equally urgent accounts', () => {
    const a = used('eng:2', 'bravo', 1)
    const b = used('eng:1', 'charlie', 1)
    expect(label(defaultAccount([b, a], 'eng'))).toBe('bravo')
  })

  it('else opens on the most used: the most agents holding it', () => {
    const accounts = [
      account({ account_id: 'eng:a', label: 'alpha', assigned: 1 }),
      account({ account_id: 'eng:b', label: 'bravo', assigned: 3 }),
      account({ account_id: 'eng:c', label: 'charlie', assigned: 0 }),
    ]
    expect(label(defaultAccount(accounts, 'eng'))).toBe('bravo')
  })

  it('else the first by label, not by the opaque id', () => {
    const accounts = [
      account({ account_id: 'eng:a', label: 'zulu' }),
      account({ account_id: 'eng:z', label: 'alpha' }),
    ]
    expect(label(defaultAccount(accounts, 'eng'))).toBe('alpha')
  })

  it('is null for an empty pool', () => {
    expect(defaultAccount([], 'eng')).toBeNull()
  })
})

describe('the page opens on that account', () => {
  it('shows the most used account in the pane when nothing needs attention', async () => {
    const accounts = [
      account({ account_id: 'eng:a', label: 'alpha' }),
      account({ account_id: 'eng:b', label: 'bravo', assigned: 2 }),
    ]
    const board = {
      page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: NOW,
    } as AccountsBoard
    const ok: Result<AccountsBoard> = { status: 'ok', data: board, fetchedAt: NOW, serverAt: new Date(NOW).toISOString() }
    api.loadAccountsBoard.mockResolvedValue(ok)
    render(<AccountsScreen />)
    await screen.findByText('Subscription accounts', undefined, WAIT)
    const shown = [...document.querySelectorAll('.acct-pane-detail')].map((s) => s.getAttribute('aria-label'))
    expect(shown).toEqual(['Account bravo'])
  })
})
