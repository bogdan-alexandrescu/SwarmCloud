// WHAT AN ACCOUNT'S STATE IS DRAWN AS, NOW THAT IT IS NOT A PILL.
//
// WHY THIS FILE EXISTS, STATED AS A GAP RATHER THAN AS A FEATURE.
//
// The B4.6 restraint pass replaced `.tag.acct-state` -- a bordered, uppercase,
// hue-coloured pill -- with `.ctl-chip`, the product's own status primitive:
// a 10px mark whose SILHOUETTE differs per state, and the state word beside it
// at `--t-body` in `--text` (design-system.md §6.6). Before making that change
// the existing suite was mutated to find out what it could see of the old
// encoding, and the answer was NOTHING: duplicating the state word inside the
// chip -- `{account.state}{account.state}` -- left
// `prose.budget.capacity.test.tsx` and `honesty.prose.test.tsx` both green.
// The word budget did not notice, and no assertion anywhere named the element
// the state is drawn in.
//
// So there was no assertion to re-point, and re-pointing nothing is how an
// encoding change gets called "covered". This file is the assertion that was
// missing. It pins the ENCODING, not the markup: the mark is present, the word
// is present, and the two never collapse into each other.
//
// THE CASE IT EXISTS FOR IS `DRAINING`, AND IT IS A HONESTY CASE.
//
// `accountTone` (types.ts) returns five tones -- ok / paused / wait / bad /
// unknown -- and `.ctl-chip` ships five modifiers -- is-ok / is-paused /
// is-warn / is-bad / is-unknown. THEY ARE NOT THE SAME VOCABULARY: `wait` has
// no `is-wait`. The obvious implementation is `is-${tone}`, and it is wrong in
// a way that does not announce itself, because an unmatched modifier on
// `.ctl-chip` does not fail loudly -- it falls through to the primitive's own
// default, `--chip-tone: var(--text-faint)`, which is the UNKNOWN mark: a
// hollow ring meaning "nobody derived this state".
//
// A DRAINING account would then render as an account whose state nobody
// derived. That is an absence presented as a measurement with the operands
// swapped -- a derived state presented as underived -- and §8.1 forbids the
// class. `CHIP_MOD` in Accounts.tsx is the lookup table that makes it
// unwritable; this is the test that makes it stay one.
//
// WHAT IS DELIBERATELY NOT PINNED: the colours, the sizes and the shapes.
// Those belong to `.ctl-chip` and are held by `styles.css`, by
// `test_state_colour_discriminability.py` and by
// `test_every_chip_state_has_its_own_silhouette`. This file asserts only what
// THIS SCREEN is responsible for -- that each state reaches the right modifier
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

/** The chip drawn in the STATE column of the row whose label is `label`. */
function stateChip(label: string): HTMLElement {
  const row = [...document.querySelectorAll('tr')].find((tr) =>
    tr.querySelector('.acct-open')?.textContent?.includes(label),
  )
  expect(row, `no row rendered for ${label}`).toBeTruthy()
  const chip = row!.querySelector('.acct-statecell > .ctl-chip')
  expect(chip, `the ${label} row drew no state chip`).toBeTruthy()
  return chip as HTMLElement
}

/**
 * Every state this screen can be handed, and the modifier it must reach.
 *
 * `DRAINING` is the row this table exists for: see the header. `SOMETHING_NEW`
 * stands for a state the API grows that this client has never heard of --
 * `accountTone` answers `unknown` for it, and `is-unknown` is the correct,
 * honest drawing of a state nobody here derived. That row is not a placeholder;
 * it is the assertion that an unrecognised state is drawn as unrecognised
 * rather than defaulted into a healthy one.
 */
const STATES = [
  { state: 'AVAILABLE', modifier: 'is-ok' },
  { state: 'PAUSED', modifier: 'is-paused' },
  { state: 'DRAINING', modifier: 'is-warn' },
  { state: 'REAUTH_REQUIRED', modifier: 'is-bad' },
  { state: 'SOMETHING_NEW', modifier: 'is-unknown' },
] as const

describe('an account state is a mark and a word', () => {
  it('sends each state to its own chip modifier, and never to the unknown mark by accident', async () => {
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

    for (const [i, { state, modifier }] of STATES.entries()) {
      const chip = stateChip(`s${i}`)
      expect(
        chip.classList.contains(modifier),
        `${state} did not reach ${modifier}; it drew ${chip.className}`,
      ).toBe(true)
    }

    // THE ONE THAT WOULD HAVE BEEN SILENT. Exactly one row may carry the
    // unknown mark, and it is the unrecognised state -- not DRAINING, which is
    // what `is-${tone}` would have produced. Asserted as a COUNT as well as
    // per-row, because a bug that sends every state to `is-unknown` satisfies
    // "the unknown row is unknown" and fails this.
    const unknowns = document.querySelectorAll('.acct-statecell > .ctl-chip.is-unknown')
    expect(unknowns.length, 'more than the unrecognised state drew the unknown mark').toBe(1)
  })

  it('keeps the word on the surface and the mark beside it, neither standing in for the other', async () => {
    api.loadAccountsBoard.mockResolvedValue(
      ok(board([account({ account_id: 'eng:broken', label: 'broken', state: 'REAUTH_REQUIRED' })])),
    )
    render(<AccountsScreen />)
    expect((await screen.findAllByText('eng:broken', undefined, WAIT)).length).toBeGreaterThan(0)

    const chip = stateChip('broken')

    // THE WORD IS THE DATUM AND IT IS UNTRANSFORMED IN THE DOM. §6.6 puts the
    // state word at full ink and lowercases it in CSS, the way `.wf-state`
    // does on Workflows. Lowercasing in CSS rather than in the DOM is the part
    // that matters here: what the screen reader announces, what a `getByText`
    // finds and what someone retypes is still the platform's own spelling.
    expect(chip.textContent).toContain('REAUTH_REQUIRED')

    // THE MARK IS A SECOND CHANNEL, NOT A SUBSTITUTE. It is the `<i>` the
    // silhouette is drawn on, it carries no text, and it is `aria-hidden` --
    // the word already said it, and a screen reader announcing a bare shape
    // learns nothing (the failure `Overview.tsx:1452` still has).
    const mark = chip.querySelector('i')
    expect(mark, 'the chip drew no mark').toBeTruthy()
    expect(mark!.getAttribute('aria-hidden')).toBe('true')
    expect(mark!.textContent).toBe('')

    // AND THE WORD IS NOT INSIDE THE MARK. A regression that renders the state
    // into the `<i>` would satisfy both assertions above separately.
    expect(chip.textContent!.replace(mark!.textContent!, '')).toContain('REAUTH_REQUIRED')
  })

  it('separates the state from the mark beside it, which the pill used to do with its border', async () => {
    // WHAT MOVED. `.tag.acct-state` was a bordered box, so the border was the
    // gap between the state and the `skipped here` mark. `.ctl-chip` has no
    // border by design (§6.6), and with nothing between them the two rendered
    // as `availableSKIPPED HERE` -- one word to anyone skimming the column.
    // The separation is now the cell's, declared on `.acct-statecell`, and it
    // is asserted structurally: both marks are present as SEPARATE elements
    // under the cell. jsdom has no layout engine and cannot measure the gap
    // itself (design-system.md §0.3), so what is pinned is the thing jsdom
    // CAN see -- that the cell carries the class the rule is written against.
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

    const cell = document.querySelector('td.acct-statecell')
    expect(cell, 'the state cell lost the class its spacing rule is written against').toBeTruthy()
    expect(cell!.querySelector('.ctl-chip.is-ok'), 'the state itself went missing').toBeTruthy()
    expect(
      cell!.querySelector('.ctl-mark.is-unread'),
      'the skipped-here mark went missing, which is the fact the state does NOT carry',
    ).toBeTruthy()
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
    expect(document.querySelector('.count-chip')).toBeNull()
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
