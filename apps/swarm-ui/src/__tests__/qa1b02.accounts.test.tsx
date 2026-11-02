// QA1B-02 (part of #2596): pass 1b of the 2026-10-28 visual QA applies the
// patches pass 1's lanes wrote for Accounts.tsx, accountsPool.ts and
// AccountHealthPage.tsx, which sat outside their territory.
//
//   capacity-03  A reset already past, with no reading since it, is not when
//                the account clears: the verdict no longer names it as the
//                account that frees first (`clockPassed`, `verdictOf`'s `now`).
//   capacity-21  Accounts' pool footer says `unreadable account records`, not
//                the internal `unread docs`.
//   capacity-13  Account health ends in no `Reading this screen:` footer; its
//                two topics sit behind the title's one `?`.
//
// BREAK IT: drop the `clockPassed` filter from `verdictOf`, restore `unread
// docs`, or put `<HelpLinks topics={HEALTH_TOPICS} />` back / drop `help` from
// the Account health `Screen` -- a case below turns red.

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountHealthBoard, AccountsBoard } from '../api'
import type { Result } from '../fetch'
import { HELP, type TopicId } from '../help'
import type { PoolAccount } from '../accountsPool'
import type { AccountsPage, Tenant } from '../types'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  loadAccountHealthBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const pool = await import('../accountsPool')
const { AccountsScreen } = await import('../Accounts')
const { AccountHealthScreen } = await import('../AccountHealthPage')

const WAIT = { timeout: 5000 } as const
const MIN = 60_000
const H = 60 * MIN
const NOW = Date.parse('2026-10-28T09:35:00Z')
const iso = (t: number) => new Date(t).toISOString()

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-28T09:35:00Z' }
}

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
    observed_at: iso(NOW - 5 * MIN),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: null,
    ...over,
  }
}

/** Under its 5-hour floor until `resets`, last read at `read`. */
function floored(label: string, resets: number, read: number): PoolAccount {
  return acct(label, {
    can_take_new_agent: false,
    observed_at: iso(read),
    blocked_by: [{ window: 'five_hour', used: 0.92, floor: 0.15, resets_at: iso(resets) }],
    next_eligible_at: iso(resets),
    windows: { five_hour: { utilization: 0.92, resets_at: iso(resets), reset: false } },
  })
}

describe('capacity-03: a reset already past is not when the account clears', () => {
  // The fixture's frozen clock: reset Oct 19, read before it, seen on Oct 28.
  const gone = () => floored('five-hour-floor', Date.parse('2026-10-19T12:40:00Z'), Date.parse('2026-10-19T09:00:00Z'))

  it('calls a clock gone when its reset is past and no reading came in since it', () => {
    expect(pool.clockPassed(gone(), NOW)).toBe(true)
  })

  it('keeps a clock still ahead, and one a reading after the reset still stands by', () => {
    expect(pool.clockPassed(floored('ahead', NOW + 2 * H, NOW - 5 * MIN), NOW)).toBe(false)
    // Read after the reset and still blocked: the broker's own word.
    expect(pool.clockPassed(floored('reread', NOW - 2 * H, NOW - 5 * MIN), NOW)).toBe(false)
    expect(pool.clockPassed(acct('open'), NOW)).toBe(false)
  })

  it('names no gone clock as the account that frees first', () => {
    // MUTATION: drop the `clockPassed` filter from `verdictOf` -- the Oct 19
    // reset comes first and `next` names five-hour-floor.
    const ahead = floored('ahead', NOW + 2 * H, NOW - 5 * MIN)
    const v = pool.verdictOf([gone(), ahead], 'eng', 0, NOW)
    expect(v.n).toBe(0)
    expect(v.next?.account.label).toBe('ahead')
    expect(v.then).toBeNull()
  })

  it('says no clock frees anything when the only clock has gone', () => {
    const v = pool.verdictOf([gone()], 'eng', 0, NOW)
    expect(v.next).toBeNull()
  })
})

describe('capacity-21: Accounts says unreadable account records, not unread docs', () => {
  it('names the unreadable records in the pool footer in plain words', async () => {
    // MUTATION: restore `<b>unread docs</b>` in PoolFindings.
    const tenants = [{ tenant_id: 'eng' }] as Tenant[]
    api.loadAccountsBoard.mockResolvedValue(
      ok({
        page: {
          accounts: [acct('alice-personal')],
          tenant_id: 'eng',
          unreadable_documents: ['eng:broken-doc'],
          unreadable_document_count: 1,
        } as AccountsPage,
        tenants,
        tenantsDetail: null,
        readAt: Date.now(),
      } as AccountsBoard),
    )
    render(<AccountsScreen />)
    await screen.findAllByText('eng:alice-personal', undefined, WAIT)
    const keys = [...document.querySelectorAll('.ctl-fact > b')].map((b) => b.textContent)
    expect(keys).toContain('unreadable account records')
    expect(keys).not.toContain('unread docs')
  })
})

const HEALTH_TOPICS: readonly TopicId[] = ['account-health', 'absent-vs-zero']

function healthBoard(): AccountHealthBoard {
  return {
    page: { accounts: [acct('example-team')], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 },
    health: { kind: 'not_served', reason: 'GET /v1/accounts/health answers 404.' },
    readAt: NOW,
  } as unknown as AccountHealthBoard
}

async function health(): Promise<void> {
  api.loadAccountHealthBoard.mockResolvedValue(ok(healthBoard()))
  render(<AccountHealthScreen />)
  await screen.findAllByText('example-team', undefined, WAIT)
}

describe('capacity-13: Account health ends in no help-link footer; its topics sit behind the title’s ?', () => {
  it('draws no `Reading this screen:` row', async () => {
    // MUTATION: restore `<HelpLinks topics={HEALTH_TOPICS} />` at the foot of
    // either layout and its label is found.
    await health()
    expect(screen.queryByText('Reading this screen:')).toBeNull()
    const head = document.querySelector('.c-phead')!
    const strays = [...document.querySelectorAll('a[href^="#help/"]')].filter((a) => !head.contains(a))
    expect(strays.map((a) => a.getAttribute('href'))).not.toContain(`#${HELP['account-health'].anchor}`)
  })

  it('opens both topics, in order, from the one ? by the title', async () => {
    // MUTATION: drop `help={HEALTH_TOPICS}` from the `Screen` -- the section's
    // lone question is left and this reads no topic links.
    await health()
    const head = document.querySelector<HTMLElement>('.c-phead')!
    const glyphs = within(head).getAllByRole('button', { name: /^Help:/ })
    expect(glyphs.length).toBe(1)
    fireEvent.click(glyphs[0]!)
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    const hrefs = [...dialog.querySelectorAll('a[href^="#help/"]')].map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual(HEALTH_TOPICS.map((id) => `#${HELP[id].anchor}`))
  })
})
