// ACCOUNTS IS A SPLIT, LIKE AGENTS (capacity.html A1, frames 7-10; audit #503).
//
// What the audit measured at 1440, dark: the right pane drew a dashed
// "Choose an account." placeholder instead of an account, `Add account` floated
// alone under the table, and the list was a five-column table rather than A1's
// compact list. And the state column drew account states with task-state
// colours -- an account is not a task (brand.html §3).
//
// What this file pins:
//   * with no choice made, the pane opens the account that needs a person, else
//     the first in list order -- never a placeholder while there are accounts;
//   * choosing a row shows that one, and only that one;
//   * `Add account` sits in the list's header row;
//   * the list is a list: one item per account, no table and no column heads;
//   * available and draining are grey with no hue mark, paused is the park
//     hue's pause bars, and reauth required is the amber warning triangle --
//     the words lower case with spaces;
//   * on a phone the list is the page and a chosen account is a page of its
//     own with a way back.
//
// BREAK IT: restore the `chosen.length === 0` placeholder -- the first case
// finds "Choose an account." Or put `Add account` back in its own bar under the
// list -- the header case finds it outside `.acct-list-head`.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import STYLES from '../styles.css?raw'
import CAPACITY from '../styles/capacity.css?raw'
import { cascade } from './cssgate'
import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage } from '../types'
import { ACCOUNTS_POLL_MS } from '../capacityPoll'

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
const SHEETS = `${STYLES}\n${CAPACITY}`

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

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

function board(accounts: Account[]): AccountsBoard {
  return {
    page: {
      accounts,
      tenant_id: 'eng',
      unreadable_documents: [],
      unreadable_document_count: 0,
    } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

async function mount(accounts: Account[]): Promise<void> {
  api.loadAccountsBoard.mockResolvedValue(ok(board(accounts)))
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', undefined, WAIT)
}

/** The accounts the pane is showing, by the label in their accessible name. */
function shown(): string[] {
  return [...document.querySelectorAll('.acct-pane-detail')].map((s) => s.getAttribute('aria-label') ?? '')
}

function item(label: string): HTMLElement {
  const li = [...document.querySelectorAll('.acct-li')].find(
    (el) => el.querySelector('.acct-li-name')?.textContent === label,
  )
  expect(li, `no list item for ${label}`).toBeTruthy()
  return li as HTMLElement
}

const TWO = [
  account({ account_id: 'eng:a', label: 'alpha' }),
  account({ account_id: 'eng:b', label: 'bravo' }),
]

describe('the pane opens an account without being asked (#503)', () => {
  it('opens the first account in list order, never the placeholder', async () => {
    await mount(TWO)
    expect(screen.queryByText('Choose an account.')).toBeNull()
    expect(document.querySelector('.acct-pane-empty')).toBeNull()
    expect(shown()).toEqual(['Account alpha'])
    expect(item('alpha').classList.contains('is-chosen')).toBe(true)
  })

  it('opens the account that needs a sign-in first, wherever it sits', async () => {
    await mount([TWO[0]!, account({ account_id: 'eng:b', label: 'bravo', state: 'REAUTH_REQUIRED' })])
    expect(shown()).toEqual(['Account bravo'])
  })

  it('shows the row a reader chooses, and only that one', async () => {
    await mount(TWO)
    fireEvent.click(item('bravo').querySelector('button.acct-open')!)
    expect(shown()).toEqual(['Account bravo'])
    // Choosing the chosen row again keeps it: there is no empty pane to fall to.
    fireEvent.click(item('bravo').querySelector('button.acct-open')!)
    expect(shown()).toEqual(['Account bravo'])
  })
})

describe('the list is A1’s compact list, with Add account in its header (#503)', () => {
  it('puts Add account in the list header row, beside the title', async () => {
    await mount(TWO)
    const head = document.querySelector('.acct-list .acct-list-head')
    expect(head, 'the list has no header row').not.toBeNull()
    expect(head!.textContent).toContain('Subscription accounts')
    const add = screen.getByRole('button', { name: 'Add account' })
    expect(head!.contains(add), 'Add account sits outside the list header').toBe(true)
    expect(document.querySelector('.acct-add-bar')).toBeNull()
  })

  it('is one item per account, not a table with five column heads', async () => {
    await mount(TWO)
    expect(document.querySelector('.acct-list table')).toBeNull()
    expect(document.querySelectorAll('.acct-list [role="columnheader"]').length).toBe(0)
    expect(document.querySelectorAll('.acct-list li.acct-li').length).toBe(2)
    // The foot says what the tilde means, as the frame's does.
    expect(document.querySelector('.acct-list .provenance')).not.toBeNull()
  })

  it('draws the 5h figure on the item: an em dash unmeasured, ~ when projected', async () => {
    const t = Date.now()
    await mount([
      account({ account_id: 'eng:a', label: 'alpha' }),
      account({
        account_id: 'eng:b',
        label: 'bravo',
        observed_at: new Date(t - 60_000).toISOString(),
        stale: true,
        windows: { five_hour: { utilization: 0.64, resets_at: new Date(t + 3_600_000).toISOString(), reset: false } },
      }),
    ])
    const a = item('alpha').querySelector('.acct-window')!
    expect(a.classList.contains('acct-unmeasured')).toBe(true)
    expect(a.textContent).toBe('—')
    const b = item('bravo').querySelector('.acct-window')!
    expect(b.textContent).toBe('~64%')
    expect(b.querySelector('.acct-tilde')).not.toBeNull()
  })
})

describe('an account state is not a task state (brand §3, #503)', () => {
  const STATES = [
    account({ account_id: 'eng:1', label: 's1', state: 'AVAILABLE' }),
    account({ account_id: 'eng:2', label: 's2', state: 'PAUSED' }),
    account({ account_id: 'eng:3', label: 's3', state: 'DRAINING' }),
    account({ account_id: 'eng:4', label: 's4', state: 'REAUTH_REQUIRED' }),
  ]
  const mark = (label: string) => item(label).querySelector<HTMLElement>('.acct-state')!

  it('draws available and draining grey with no hue mark', async () => {
    await mount(STATES)
    for (const label of ['s1', 's3']) {
      const m = mark(label)
      expect(m.getAttribute('data-hue'), `${label} took a hue`).toBe('neu')
      expect(m.querySelector('svg'), `${label} drew a task-state glyph`).toBeNull()
    }
    expect(mark('s1').textContent).toBe('available')
    expect(mark('s3').textContent).toBe('draining')
  })

  it('draws paused with the park hue’s pause bars', async () => {
    await mount(STATES)
    expect(mark('s2').getAttribute('data-hue')).toBe('park')
    expect(mark('s2').getAttribute('data-mark')).toBe('parked')
    expect(mark('s2').textContent).toBe('paused')
  })

  it('draws reauth required as the amber warning triangle, in words', async () => {
    await mount(STATES)
    expect(mark('s4').getAttribute('data-mark')).toBe('warn')
    expect(mark('s4').textContent).toBe('reauth required')
  })

  it('says a sign-in is needed in a warning callout that opens the account', async () => {
    await mount(STATES)
    const callout = document.querySelector('.acct-callout')
    expect(callout, 'no callout for the account that needs a sign-in').not.toBeNull()
    expect(callout!.querySelector('[data-mark="warn"]')).not.toBeNull()
    fireEvent.click(item('s1').querySelector('button.acct-open')!)
    expect(shown()).toEqual(['Account s1'])
    fireEvent.click(screen.getByRole('button', { name: 'Sign in again to s4' }))
    expect(shown()).toEqual(['Account s4'])
  })
})

describe('on a phone the account is a page (frame 10, #503)', () => {
  const display = (el: Element, width: number) => {
    const r = cascade(SHEETS, el, 'display', { width })
    return r.winner?.value ?? null
  }

  it('shows the list as the page until a row is chosen, then the account with a way back', async () => {
    await mount(TWO)
    const split = document.querySelector('.acct-split')!
    const pane = document.querySelector('.acct-pane')!
    const list = document.querySelector('.acct-list')!
    // Nothing chosen yet: the phone shows the list and not the pane.
    expect(split.classList.contains('is-detail')).toBe(false)
    expect(display(pane, 390)).toBe('none')
    expect(display(pane, 1440)).not.toBe('none')

    fireEvent.click(item('bravo').querySelector('button.acct-open')!)
    expect(split.classList.contains('is-detail')).toBe(true)
    expect(display(list, 390)).toBe('none')
    expect(display(pane, 390)).not.toBe('none')
    expect(display(list, 1440)).not.toBe('none')

    const back = document.querySelector<HTMLButtonElement>('.acct-pane > button.acct-back')!
    expect(back, 'the account page has no way back').not.toBeNull()
    expect(back.textContent).toBe('‹ Accounts')
    expect(display(back, 1440)).toBe('none')
    fireEvent.click(back)
    expect(split.classList.contains('is-detail')).toBe(false)
  })

  it('stacks the split into one column at 390', async () => {
    await mount(TWO)
    const split = document.querySelector('.acct-split')!
    const cols = cascade(SHEETS, split, 'grid-template-columns', { width: 390 }).winner?.value ?? null
    expect(cols).toBe('minmax(0, 1fr)')
  })
})

describe('the head says how many accounts can serve (frame 7)', () => {
  it('counts accounts, the available ones and the ones needing a sign-in', async () => {
    await mount([
      account({ account_id: 'eng:1', label: 's1' }),
      account({ account_id: 'eng:2', label: 's2', state: 'PAUSED' }),
      account({ account_id: 'eng:3', label: 's3', state: 'REAUTH_REQUIRED' }),
    ])
    expect(document.body.textContent).toContain('3 accounts · 1 available · 1 needs a sign-in')
  })
})

describe('Accounts re-reads every 60s and not in a hidden tab (#117)', () => {
  afterEach(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    vi.useRealTimers()
  })

  it('passes its cadence to Screen', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    api.loadAccountsBoard.mockReset()
    api.loadAccountsBoard.mockImplementation(async () => ok(board(TWO)))
    render(<AccountsScreen />)
    const advance = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    }
    await advance(0)
    expect(api.loadAccountsBoard).toHaveBeenCalledTimes(1)
    await advance(ACCOUNTS_POLL_MS - 1_000)
    expect(api.loadAccountsBoard, 'Accounts re-read before 60s').toHaveBeenCalledTimes(1)
    await advance(1_000)
    expect(api.loadAccountsBoard, 'Accounts did not re-read after 60s').toHaveBeenCalledTimes(2)

    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
    await advance(ACCOUNTS_POLL_MS * 3)
    expect(api.loadAccountsBoard, 'a hidden tab kept reading').toHaveBeenCalledTimes(2)
  })
})
