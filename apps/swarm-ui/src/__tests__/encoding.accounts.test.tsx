// WHAT AN ACCOUNT'S STATE IS DRAWN AS.
//
// HISTORY. B4.6 replaced `.tag.acct-state` -- a bordered, uppercase,
// hue-coloured pill -- with `.ctl-chip`, and this file was written then
// because mutating the suite showed nothing could see the encoding at all.
// Its case was DRAINING: `accountTone` has a `wait` tone and `.ctl-chip` has no
// `is-wait`, so interpolating the tone drew DRAINING as the unknown mark.
//
// A1 (#503) REPLACED THE CHIP, because the chip drew account states in task
// colours, and an account is not a task (brand.html §3). The marks now are:
// available and draining grey with no mark (both healthy), paused the park
// hue's pause bars, reauth required the amber warning triangle -- a condition,
// so `WarnMark` -- and a state this console does not know faint and unmarked,
// in the platform's own word. The DRAINING case survives in a new form: a
// healthy state must never reach a hue, and an unknown one must never pass for
// a healthy one.
//
// WHAT IS DELIBERATELY NOT PINNED: the colours, the sizes and the shapes.
// Those belong to `.sk-st` and `MarkGlyph`. This file asserts only what THIS
// SCREEN is responsible for -- that each state reaches the right mark and hue
// and carries its word.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AccountsBoard } from '../api'
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

const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-23T10:00:00Z' }
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

/** The state mark on the list item whose label is `label`. */
function stateChip(label: string): HTMLElement {
  const li = [...document.querySelectorAll('.acct-li')].find(
    (el) => el.querySelector('.acct-li-name')?.textContent === label,
  )
  expect(li, `no item rendered for ${label}`).toBeTruthy()
  const chip = li!.querySelector('.acct-li-line > .acct-state')
  expect(chip, `the ${label} item drew no state mark`).toBeTruthy()
  return chip as HTMLElement
}

/**
 * Every state this screen can be handed, and what it must reach.
 *
 * `SOMETHING_NEW` stands for a state the API grows that this client has never
 * heard of: it is drawn as unrecognised, never defaulted into a healthy one.
 */
const STATES = [
  { state: 'AVAILABLE', hue: 'neu', mark: 'none', word: 'available' },
  { state: 'PAUSED', hue: 'park', mark: 'parked', word: 'paused' },
  { state: 'DRAINING', hue: 'neu', mark: 'none', word: 'draining' },
  { state: 'REAUTH_REQUIRED', hue: 'warn', mark: 'warn', word: 'reauth required' },
  { state: 'SOMETHING_NEW', hue: 'unknown', mark: 'none', word: 'something new' },
] as const

describe('an account state is a mark and a word', () => {
  it('sends each state to its own mark and hue, and never to the unknown one by accident', async () => {
    api.loadAccountsBoard.mockResolvedValue(
      ok(
        board(
          STATES.map(({ state }, i) =>
            account({ account_id: `eng:s${i}`, label: `s${i}`, state }),
          ),
        ),
      ),
    )
    render(<AccountsScreen />)
    expect(await screen.findByText('eng:s0', undefined, WAIT)).toBeTruthy()

    for (const [i, { state, hue, mark, word }] of STATES.entries()) {
      const chip = stateChip(`s${i}`)
      expect(chip.getAttribute('data-hue'), `${state} reached the wrong hue`).toBe(hue)
      expect(chip.getAttribute('data-mark'), `${state} reached the wrong mark`).toBe(mark)
      expect(chip.textContent).toBe(word)
      // A mark is drawn only where the state has one.
      expect(chip.querySelector('svg') !== null, `${state} glyph`).toBe(mark !== 'none')
    }

    // THE ONE THAT WOULD HAVE BEEN SILENT, asserted as a COUNT: a bug that
    // sends every state to `unknown` satisfies "the unknown row is unknown".
    const unknowns = document.querySelectorAll('.acct-li-line > .acct-state[data-hue="unknown"]')
    expect(unknowns.length, 'more than the unrecognised state drew the unknown mark').toBe(1)
  })

  it('keeps the word on the surface and the mark beside it, neither standing in for the other', async () => {
    api.loadAccountsBoard.mockResolvedValue(
      ok(board([account({ account_id: 'eng:broken', label: 'broken', state: 'REAUTH_REQUIRED' })])),
    )
    render(<AccountsScreen />)
    expect((await screen.findAllByText('eng:broken', undefined, WAIT)).length).toBeGreaterThan(0)

    const chip = stateChip('broken')

    // THE WORD IS LOWER CASE WITH SPACES, in the DOM (#503), the way every
    // other state word in the console reads; the platform's own spelling is
    // the title, so what someone retypes into a command is still at hand.
    expect(chip.textContent).toBe('reauth required')
    expect(chip.getAttribute('title')).toBe('REAUTH_REQUIRED')

    // THE MARK IS A SECOND CHANNEL, NOT A SUBSTITUTE: aria-hidden, no text.
    const mark = chip.querySelector('svg')
    expect(mark, 'the state drew no mark').toBeTruthy()
    expect(mark!.getAttribute('aria-hidden')).toBe('true')
    expect(mark!.textContent).toBe('')
  })

  it('keeps the state and the skipped-here mark apart, as two elements', async () => {
    // The state says AVAILABLE and is right; the pool still will not hand the
    // account to this tenant. Two facts, so two elements: the state on the
    // item's line, the mark in its meta line -- never one run of text.
    api.loadAccountsBoard.mockResolvedValue(
      ok(
        board([
          account({
            account_id: 'eng:skipped',
            label: 'skipped',
            state: 'AVAILABLE',
            unreadable_now: ['eng'],
          }),
        ]),
      ),
    )
    render(<AccountsScreen />)
    expect(await screen.findByText('eng:skipped', undefined, WAIT)).toBeTruthy()

    const chip = stateChip('skipped')
    expect(chip.getAttribute('data-hue'), 'the state itself went missing').toBe('neu')
    const li = chip.closest('.acct-li')!
    const skipped = li.querySelector('.acct-li-meta > .ctl-mark.is-unread')
    expect(skipped, 'the skipped-here mark went missing, which is the fact the state does NOT carry').toBeTruthy()
    expect(chip.contains(skipped)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Visual QA 2026-09-25, Accounts (#85). Pushed before the fixes they demand.
// ---------------------------------------------------------------------------

describe('the state buttons speak the case the state chip does (CP-23)', () => {
  it('labels every "move to" button in lowercase, beside a lowercase chip', async () => {
    api.loadAccountsBoard.mockResolvedValue(
      ok(board([account({ account_id: 'eng:held', label: 'held', state: 'PAUSED' })])),
    )
    render(<AccountsScreen />)
    expect(await screen.findByText('eng:held', undefined, WAIT)).toBeTruthy()
    // Open the row: the controls live in its detail.
    const open = document.querySelector<HTMLButtonElement>('.acct-open')!
    if (open.getAttribute('aria-expanded') !== 'true') fireEvent.click(open)
    const moves = await screen.findAllByRole('button', { name: /^move to /i }, WAIT)
    expect(moves.length, 'no state buttons were drawn, so this checked nothing').toBeGreaterThan(0)
    for (const b of moves) {
      const label = b.textContent ?? ''
      expect(label, `"${label}" shouts`).toBe(label.toLowerCase())
    }
  })
})

describe('the pool’s count sits in the card-note slot (CP-22)', () => {
  it('is a .ctl-card-note, not the retired .count-chip', async () => {
    api.loadAccountsBoard.mockResolvedValue(ok(board([account({})])))
    render(<AccountsScreen />)
    expect(await screen.findByText('eng:laptop', undefined, WAIT)).toBeTruthy()
    expect(document.querySelector('.count-chip, .c-chip.is-n')).toBeNull()
    const note = [...document.querySelectorAll('.ctl-card-note')].find((n) =>
      /\baccounts?\b/.test(n.textContent ?? ''),
    )
    expect(note, 'the account count is not a card note').toBeTruthy()
    expect(note!.textContent).toBe('1 account')
  })
})

describe('the add form states its isolation rule where it stays visible (CP-20)', () => {
  it('describes the lending field with a line of text, not only a placeholder', async () => {
    api.loadAccountsBoard.mockResolvedValue(ok(board([account({})])))
    render(<AccountsScreen />)
    expect(await screen.findByText('eng:laptop', undefined, WAIT)).toBeTruthy()
    // The form is behind `Add account` once the pool has an account (#127).
    fireEvent.click(screen.getByRole('button', { name: 'Add account' }))
    const lend = document.getElementById('acct-lend')
    expect(lend, 'no lending field on the add form').not.toBeNull()
    const ids = (lend!.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
    const hint = ids.map((id) => document.getElementById(id)).find((el) => el !== null)
    expect(hint, 'the lending rule is only a placeholder, which vanishes on typing').toBeTruthy()
    // WHOSE account it is when the field is left empty -- the rule itself.
    expect(hint!.textContent).toContain('eng')
    expect(hint!.textContent).toMatch(/only/)
  })
})
