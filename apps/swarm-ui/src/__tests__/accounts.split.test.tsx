// ACCOUNTS IS A SPLIT, LIKE AGENTS (capacity.html A1, frames 7-10; audit #503).
//
// What the audit measured at 1440, dark: the right pane drew a dashed
// "Choose an account." placeholder instead of an account, `Add account` floated
// alone under the table, and the list was a five-column table rather than A1's
// compact list. And the state column drew account states with task-state
// colours -- an account is not a task (brand.html §3).
//
// What this file pins:
//   * with no choice made, no account is open and no placeholder is drawn: the
//     ranked table is the page (#2222, mock-up A F2), and an account opens as
//     a side sheet only when its row is chosen (F4);
//   * choosing a row shows that one, and only that one; Escape and the
//     sheet's close button close it, and focus goes back to the row;
//   * `Add account` sits in the list's header row;
//   * the list is a list: one item per account, no table and no column heads;
//   * available and draining are grey with no hue mark, paused is the park
//     hue's pause bars, and reauth required is the amber warning triangle --
//     the words lower case with spaces;
//   * on a phone the list is the page and a chosen account is a page of its
//     own with a way back.
//
// BREAK IT: restore the `chosen.length === 0` placeholder, or open the
// default account unasked -- the first case finds a pane. Or put `Add account` back in its own bar under the
// list -- the header case finds it outside `.acct-list-head`.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import STYLES from '../styles.css?raw'
import CAPACITY from '../styles/capacity.css?raw'
import RANKED from '../styles/accounts-ranked.css?raw'
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
const SHEETS = `${STYLES}\n${CAPACITY}\n${RANKED}`

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

/** An account's row in the ranked table (#2222), by the name on its button. */
function item(label: string): HTMLElement {
  const li = [...document.querySelectorAll('tr.acct-rank-row')].find(
    (el) => el.querySelector('button.acct-open')?.textContent === label,
  )
  expect(li, `no row for ${label}`).toBeTruthy()
  return li as HTMLElement
}

const TWO = [
  account({ account_id: 'eng:a', label: 'alpha' }),
  account({ account_id: 'eng:b', label: 'bravo' }),
]

describe('an account opens as a side sheet, and only when asked (#503, #2222)', () => {
  it('opens no account until a row is chosen, and draws no placeholder', async () => {
    await mount(TWO)
    expect(screen.queryByText('Choose an account.')).toBeNull()
    expect(document.querySelector('.acct-pane-empty')).toBeNull()
    expect(document.querySelector('.acct-sheet')).toBeNull()
    expect(shown()).toEqual([])
    expect(document.querySelectorAll('tr.acct-rank-row.is-chosen').length).toBe(0)
  })

  it('closes on Escape and on its close button, and gives focus back to the row', async () => {
    await mount(TWO)
    const open = () => fireEvent.click(item('bravo').querySelector('button.acct-open')!)
    open()
    expect(shown()).toEqual(['Account bravo'])
    expect(document.activeElement?.classList.contains('acct-sheet-close')).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(shown()).toEqual([])
    expect((document.activeElement as HTMLElement | null)?.dataset.account).toBe('eng:b')

    open()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(document.querySelector('.acct-sheet')).toBeNull()
  })

  it('shows the row a reader chooses, and only that one', async () => {
    await mount(TWO)
    fireEvent.click(item('bravo').querySelector('button.acct-open')!)
    expect(shown()).toEqual(['Account bravo'])
    expect(item('bravo').classList.contains('is-chosen')).toBe(true)
    // Choosing the chosen row again keeps it open.
    fireEvent.click(item('bravo').querySelector('button.acct-open')!)
    expect(shown()).toEqual(['Account bravo'])
  })
})

describe('the list is the ranked table, with Add account in its header (#503, #2222)', () => {
  it('puts Add account in the list header row, beside the title', async () => {
    await mount(TWO)
    const head = document.querySelector('.acct-list .acct-list-head')
    expect(head, 'the list has no header row').not.toBeNull()
    expect(head!.textContent).toContain('Subscription accounts')
    const add = screen.getByRole('button', { name: 'Add account' })
    expect(head!.contains(add), 'Add account sits outside the list header').toBe(true)
    expect(document.querySelector('.acct-add-bar')).toBeNull()
  })

  // #2222 (proposal A, BGD 2026-10-24): one table ranked by when each account
  // can take a new agent replaced A1's compact list.
  it('is one ranked row per account, under the group it ranks in', async () => {
    await mount(TWO)
    expect(document.querySelectorAll('.acct-list tr.acct-rank-row').length).toBe(2)
    // These rows carry none of the pool's fields (an older broker), and are said as that.
    expect(document.querySelector('.acct-list tr.acct-rank-grp')?.textContent).toBe('The broker does not say · 2')
    expect(document.querySelector('.acct-list .provenance')).not.toBeNull()
  })

  it('draws a window as an em dash unmeasured, and an old reading pale with its age', async () => {
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
    const a = item('alpha').querySelector('.acct-fig')!
    expect(a.textContent).toBe('5-hour —no reading yet')
    expect(a.querySelector('.acct-bar')).toBeNull()
    const row = item('bravo')
    expect(row.classList.contains('is-stale')).toBe(true)
    expect(row.querySelector('.acct-fig')!.textContent).toContain('36% left')
    expect(row.querySelector('.acct-age')!.textContent).toContain('counts at half')
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

  // AVAILABLE draws no state at all now (#2222): "available" was the word the
  // page read as "can take an agent" while every account was under its floor.
  it('draws draining grey with no hue mark, and available as no state', async () => {
    await mount(STATES)
    expect(item('s1').querySelector('.acct-state')).toBeNull()
    const m = mark('s3')
    expect(m.getAttribute('data-hue')).toBe('neu')
    expect(m.querySelector('svg'), 'draining drew a task-state glyph').toBeNull()
    expect(m.textContent).toBe('draining')
  })

  it('draws paused with the park hue’s pause bars', async () => {
    await mount(STATES)
    expect(mark('s2').getAttribute('data-hue')).toBe('park')
    expect(mark('s2').getAttribute('data-mark')).toBe('parked')
    expect(mark('s2').textContent).toBe('paused')
  })

  it('draws REAUTH_REQUIRED as the amber warning triangle, as needs sign-in', async () => {
    await mount(STATES)
    expect(mark('s4').getAttribute('data-mark')).toBe('warn')
    expect(mark('s4').textContent).toBe('needs sign-in')
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
    const list = document.querySelector('.acct-list')!
    // Nothing chosen yet: no sheet at any width.
    expect(split.classList.contains('is-detail')).toBe(false)
    expect(document.querySelector('.acct-pane')).toBeNull()

    fireEvent.click(item('bravo').querySelector('button.acct-open')!)
    const pane = document.querySelector('.acct-pane.acct-sheet')!
    expect(split.classList.contains('is-detail')).toBe(true)
    // A phone: the account is the page. A desktop: a sheet over the table.
    expect(display(list, 390)).toBe('none')
    expect(display(pane, 390)).not.toBe('none')
    expect(display(list, 1440)).not.toBe('none')
    expect(cascade(SHEETS, pane, 'position', { width: 1440 }).winner?.value).toBe('fixed')
    expect(cascade(SHEETS, pane, 'position', { width: 390 }).winner?.value ?? 'static').not.toBe('fixed')

    const back = document.querySelector<HTMLButtonElement>('.acct-sheet-head > button.acct-back')!
    expect(back, 'the account page has no way back').not.toBeNull()
    expect(back.textContent).toBe('‹ Accounts')
    expect(display(back, 1440)).toBe('none')
    fireEvent.click(back)
    expect(split.classList.contains('is-detail')).toBe(false)
  })

  it('draws the opened account at 390 as the screen, with no card frame round it (mock-up A P3)', async () => {
    await mount(TWO)
    fireEvent.click(item('bravo').querySelector('button.acct-open')!)
    const split = document.querySelector('.acct-split.is-detail')!
    expect(split, 'choosing a row did not open the account').not.toBeNull()
    const won = (prop: string | string[], width: number) => cascade(SHEETS, split, prop, { width }).winner?.value ?? null
    // P3 has the account straight under the app bar: no border, no rounded
    // inset card, no surface laid over the page.
    expect(won(['border', 'border-width', 'border-style'], 390)).toBe('0')
    expect(won(['border-radius'], 390)).toBe('0')
    expect(won(['background', 'background-color'], 390)).toBe('none')
    // With no rounded corners there is nothing to clip, and the clip cut the
    // focus ring of `‹ Accounts`, the screen's first control (dark-phone-01).
    expect(won(['overflow', 'overflow-x', 'overflow-y'], 390)).toBe('visible')
    // The list at 390 and every desktop keep the card.
    expect(won(['border', 'border-width', 'border-style'], 1440)).toBe('1px solid var(--line)')
  })

  it('stacks the split into one column at 390', async () => {
    await mount(TWO)
    const split = document.querySelector('.acct-split')!
    const cols = cascade(SHEETS, split, 'grid-template-columns', { width: 390 }).winner?.value ?? null
    expect(cols).toBe('minmax(0, 1fr)')
  })
})

describe('the head says how many accounts there are (frame 7, #2222)', () => {
  it('counts accounts and the ones needing a sign-in, and no longer says available', async () => {
    await mount([
      account({ account_id: 'eng:1', label: 's1' }),
      account({ account_id: 'eng:2', label: 's2', state: 'PAUSED' }),
      account({ account_id: 'eng:3', label: 's3', state: 'REAUTH_REQUIRED' }),
    ])
    expect(document.body.textContent).toContain('3 accounts · 1 needs a sign-in')
    expect(document.body.textContent).not.toMatch(/\bavailable\b/)
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
