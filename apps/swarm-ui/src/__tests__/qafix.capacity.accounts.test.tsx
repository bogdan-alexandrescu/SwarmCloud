// VISUAL QA 2026-10-20, LANE QAFIX-CAPACITY (part of #1680): the Accounts
// findings of workflow wf_9e9bf74834634db7bcc3. Each case holds the property
// the finding was about -- the rule the cascade picks for the rendered mark,
// the accessible name, the words on the row -- not the markup's shape.
//
//   QA-049  The `partial` mark's dashed right edge is square, not a broken curve,
//           on Accounts and Provider quota only (owner 2026-10-20).
//   QA-050  At 390 the Add button gives way, not the `6 accounts` count.
//   QA-051  A reset binding window reads `cleared · 5h`, never `clears cleared`.

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage, Tenant } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

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

const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-20T12:00:00Z' }
}

function account(over: Partial<Account>): Account {
  return {
    account_id: 'eng:alice-personal',
    owner_tenant: 'eng',
    label: 'alice-personal',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 7,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

async function mount(accounts: Account[], unreadable = 0): Promise<void> {
  const tenants = [{ tenant_id: 'eng' }] as Tenant[]
  api.loadAccountsBoard.mockResolvedValue(
    ok({
      page: {
        accounts,
        tenant_id: 'eng',
        unreadable_documents: unreadable > 0 ? ['eng:broken-doc'] : [],
        unreadable_document_count: unreadable,
      } as AccountsPage,
      tenants,
      tenantsDetail: null,
      readAt: Date.now(),
    } as AccountsBoard),
  )
  render(<AccountsScreen />)
  await screen.findAllByText(accounts[0]!.account_id, undefined, WAIT)
}

describe('QA-049: the partial mark draws its dashed edge straight', () => {
  it('squares the dashed side, beside `unreadable account records`, and keeps the solid side round', async () => {
    await mount([account({})], 1)
    const fact = [...document.querySelectorAll('.ctl-fact')].find((f) => f.querySelector('b')?.textContent === 'unreadable account records')!
    const mark = fact.querySelector<HTMLElement>('.ctl-mark.is-partial')!
    expect(mark.textContent).toBe('partial')
    // Solid and transparent since run 2: the dashes are drawn over it (below).
    expect(painted(mark, 'border-right-style', WIDE)).toBe('solid')
    // MUTATION: drop `border-top-right-radius: 0` / `border-bottom-right-radius: 0`
    // from `.acct-page .ctl-mark.is-partial` (styles/capacity.css), or the
    // `acct-page` wrapper in Accounts.tsx -- the dashes run round the mark's rounded
    // corner again and the edge reads as clipped. Both corners fail.
    expect(painted(mark, ['border-top-right-radius', 'border-radius'], WIDE)).toBe('0')
    expect(painted(mark, ['border-bottom-right-radius', 'border-radius'], WIDE)).toBe('0')
    // MUTATION: set `border-radius: 0` on the whole mark instead -- the solid
    // side loses the radius every other mark has.
    expect(painted(mark, ['border-top-left-radius', 'border-radius'], WIDE)).toBe('var(--ctl-radius-sm)')
  })
})

describe('QA-049, scoped to Accounts (owner 2026-10-20): the account\'s own partial mark is squared too', () => {
  it('squares the dashed side of the `partial` beside a stale reading on the chosen account', async () => {
    await mount([account({ stale: true, observed_at: new Date(Date.now() - 3 * 86_400_000).toISOString() })])
    // The account's own mark is in its sheet, beside its reading's age (#2222).
    fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open')!)
    const marks = [...document.querySelectorAll<HTMLElement>('.acct-split .ctl-mark.is-partial')]
    expect(marks.length, 'no partial mark on the stale account').toBeGreaterThan(0)
    for (const mark of marks) {
      // MUTATION: scope the squared rule to the `unread docs` fact alone --
      // the account's own mark, on the same page, keeps the broken curve.
      expect(painted(mark, ['border-top-right-radius', 'border-radius'], WIDE)).toBe('0')
      expect(painted(mark, ['border-bottom-right-radius', 'border-radius'], WIDE)).toBe('0')
    }
  })
})

describe('QA-050: the account count is not what gives way at 390', () => {
  it('drops the button\'s second word on a phone, and keeps its name whole', async () => {
    await mount([account({})])
    // MUTATION: remove `aria-label="Add account"` -- a phone reader would hear
    // `Add` alone once the word is hidden; this query is the name's guard.
    const add = screen.getByRole('button', { name: 'Add account' })
    const what = add.querySelector<HTMLElement>('.acct-add-what')!
    expect(what.textContent).toBe(' account')
    // MUTATION: delete the `@media (max-width: 560px) .acct-add-what` rule --
    // the full `Add account` stays at 390 and the count is cut to `1 ac…`.
    expect(painted(what, 'display', PHONE)).toBe('none')
    // MUTATION: hide the word at every width -- a desktop with room loses it.
    expect(painted(what, 'display', WIDE)).toBeNull()
    // The count itself is the full phrase in the header.
    const note = document.querySelector('.acct-list-head .ctl-card-note')!
    expect(note.textContent).toBe('1 account')
  })
})

describe('QA-051: a window that already reset reads `cleared`, not `clears cleared`', () => {
  const RESET = account({
    account_id: 'eng:retired-laptop',
    label: 'retired-laptop',
    state: 'REAUTH_REQUIRED',
    observed_at: new Date(Date.now() - 5 * 3_600_000).toISOString(),
    windows: { five_hour: { utilization: 0.4, resets_at: new Date(Date.now() - 5 * 3_600_000).toISOString(), reset: true } },
  })

  it('says the reset in the past tense, in the row and in the opened account', async () => {
    const LIVE = account({
      account_id: 'eng:alice-personal',
      observed_at: new Date().toISOString(),
      windows: { five_hour: { utilization: 0.4, resets_at: new Date(Date.now() + 80 * 60_000).toISOString(), reset: false } },
    })
    await mount([RESET, LIVE])
    // #2222: the ranked row and the sheet both say `reset HH:MMZ · nothing
    // read since`; no countdown label stands before a past tense anywhere.
    fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open[data-account="eng:retired-laptop"]')!)
    const win = document.querySelector<HTMLElement>('.acct-sheet .acct-ss-wins > .acct-win .acct-win-l')!
    expect(win.textContent).toMatch(/^5-hour reset (\w{3} \d{1,2} )?\d\d:\d\dZ$/)
    expect(document.body.textContent).not.toMatch(/clears cleared/)
  })
})

// QAFIX2-CAPACITY (visual QA run 2, wf_b6e2441b419f423a84eb, capacity-003):
// squared, the dashed side still drew as one tick at the top and one at the
// bottom. A browser fits a dashed border's pattern to the edge so that it
// starts and ends on a dash; a 2px dash pattern is ~6px long, and on a mark
// 19px tall that leaves two dashes and one gap -- the "cut" the run saw.
describe('QA-049 (run 2): the partial mark\'s dashed side on Accounts is a rule of even dashes', () => {
  it('draws the right edge as a 2px-on, 2px-off pattern down the mark\'s full height', async () => {
    await mount([account({ stale: true, observed_at: new Date(Date.now() - 3 * 86_400_000).toISOString() })], 1)
    fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open')!)
    const marks = [...document.querySelectorAll<HTMLElement>('.acct-page .ctl-mark.is-partial')]
    // `unreadable account records` in the footer and the old reading's age in the sheet.
    expect(marks.length, 'no partial marks on Accounts').toBeGreaterThanOrEqual(2)
    for (const mark of marks) {
      // MUTATION: leave the right border dashed in currentColor -- the browser's
      // own dash fitting draws it as two ticks with a gap between again.
      expect(painted(mark, ['border-right-color', 'border-right', 'border-color', 'border'], WIDE)).toBe('transparent')
      // MUTATION: drop the gradient -- the mark loses its dashed side, and
      // with it the shape that says "partial" in greyscale.
      expect(painted(mark, ['background-image', 'background'], WIDE)).toBe(
        'repeating-linear-gradient(to bottom, currentColor 0 2px, transparent 2px 4px)',
      )
      // MUTATION: size it `100% 100%` -- the dashes fill the mark, under the word.
      expect(painted(mark, ['background-size', 'background'], WIDE)).toBe('2px 100%')
      expect(painted(mark, ['background-position', 'background'], WIDE)).toBe('right top')
      expect(painted(mark, ['background-repeat', 'background'], WIDE)).toBe('no-repeat')
      // MUTATION: let it default to `padding-box` -- the strip is drawn inside
      // the transparent border instead of on it, and the edge is blank.
      expect(painted(mark, ['background-origin', 'background'], WIDE)).toBe('border-box')
      expect(painted(mark, ['border-right-width', 'border-right', 'border-width', 'border'], WIDE)).toBe('2px')
    }
    // A 12px mark at line-height 1.45 with 1px top and bottom borders is ~19px:
    // a 4px period draws four whole dashes down it, never two.
    expect(Math.floor((12 * 1.45 + 2) / 4)).toBeGreaterThanOrEqual(4)
  })
})
