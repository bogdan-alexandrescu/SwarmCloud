// WHICH MODEL CLASSES AN ACCOUNT SERVES (owner decision R5, 2026-10-17).
//
// The broker's account gains `model_classes`: the classes it takes steps of,
// a subset of `opus`, `fable`, `sonnet`, `haiku` in that order, and EMPTY --
// or absent, from a broker that predates it -- for every class. The scheduler
// never hands a step to an account that does not serve its class, so the list
// is a capacity decision and the screen has to show it to every reader and let
// the owner, and only the owner, change it.
//
// What this file pins:
//   * `['opus']` reads as opus alone, and `[]` and an absent field read as
//     "every class" -- never as none;
//   * the owner's edit sends the chosen subset in vocabulary order, and all
//     four ticked (or none) sends `[]`;
//   * a borrower sees the list and no editor;
//   * the writer itself PUTs `{"model_classes": [...]}` to
//     `/v1/accounts/{id}/model-classes`, and the fixture mode records it.
//
// MUTATION: make `classesToSave` return the ticked list unchanged and the
// all-four case sends four names; drop `?? []` from `servedClasses` and the
// absent-field case renders an empty fact instead of "every class".

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { AccountsBoard, ServedAccount } from '../api'
import type { Account, AccountsPage } from '../types'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountModelClasses: vi.fn(),
  setAccountState: vi.fn(),
}))
vi.mock('../api', () => api)

const { AccountsScreen, classesToSave, servedClasses } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-19T12:00:00Z' }
}

function account(over: Partial<ServedAccount>): ServedAccount {
  return {
    account_id: 'eng:primary',
    owner_tenant: 'eng',
    label: 'primary',
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
    model_classes: [],
    ...over,
  }
}

function board(accounts: Account[]): AccountsBoard {
  return {
    page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [{ tenant_id: 'eng' }],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

/** Mount the screen with `chosen` open in the detail pane. */
async function mount(accounts: Account[], chosen: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', `/capacity/accounts?account=${encodeURIComponent(chosen)}`)
  api.loadAccountsBoard.mockResolvedValue(ok(board(accounts)))
  render(<AccountsScreen />)
  const label = accounts.find((a) => a.account_id === chosen)!.label
  return screen.findByRole('region', { name: `Account ${label}` }, WAIT)
}

/** The `classes` fact's value in the open detail. */
function classesFact(pane: HTMLElement): HTMLElement {
  const fact = [...pane.querySelectorAll('.acct-facts > .ctl-fact')].find(
    (li) => li.querySelector('b')?.textContent === 'classes',
  )
  expect(fact, 'the detail drew no classes fact').toBeTruthy()
  return fact!.querySelector('span') as HTMLElement
}

function box(pane: HTMLElement, cls: string): HTMLInputElement {
  return within(pane).getByRole('checkbox', { name: cls }) as HTMLInputElement
}

describe('an account shows which model classes it serves', () => {
  it('reads [opus] as opus alone', async () => {
    const pane = await mount([account({ model_classes: ['opus'] })], 'eng:primary')
    expect(classesFact(pane).textContent).toBe('opus')
    expect(box(pane, 'opus').checked).toBe(true)
    for (const c of ['fable', 'sonnet', 'haiku']) expect(box(pane, c).checked, c).toBe(false)
  })

  it('reads [] as every class, with every box ticked', async () => {
    const pane = await mount([account({ model_classes: [] })], 'eng:primary')
    expect(classesFact(pane).textContent).toBe('every class')
    for (const c of ['opus', 'fable', 'sonnet', 'haiku']) expect(box(pane, c).checked, c).toBe(true)
  })

  it('reads an absent field, from an older broker, as every class', async () => {
    const old = account({})
    delete old.model_classes
    expect(servedClasses(old)).toEqual([])
    const pane = await mount([old], 'eng:primary')
    expect(classesFact(pane).textContent).toBe('every class')
  })

  it('shows a borrower the list and no editor', async () => {
    const lent = account({ account_id: 'other:lent', owner_tenant: 'other', label: 'lent', lend_to: ['eng'], model_classes: ['sonnet', 'haiku'] })
    const pane = await mount([lent], 'other:lent')
    expect(classesFact(pane).textContent).toBe('sonnet, haiku')
    expect(within(pane).queryByRole('checkbox')).toBeNull()
    expect(within(pane).queryByRole('button', { name: 'Save classes' })).toBeNull()
  })
})

describe('the owner edits the served classes', () => {
  it('sends the chosen subset in vocabulary order', async () => {
    api.setAccountModelClasses.mockResolvedValue(ok({ account: account({ model_classes: ['opus', 'haiku'] }) }))
    const pane = await mount([account({ model_classes: ['haiku'] })], 'eng:primary')
    const save = within(pane).getByRole('button', { name: 'Save classes' }) as HTMLButtonElement
    expect(save.disabled, 'an unchanged list armed the save').toBe(true)
    fireEvent.click(box(pane, 'opus'))
    expect(save.disabled).toBe(false)
    // The reload after the write reads what the broker now holds.
    api.loadAccountsBoard.mockResolvedValue(ok(board([account({ model_classes: ['opus', 'haiku'] })])))
    fireEvent.click(save)
    await waitFor(() => expect(api.setAccountModelClasses).toHaveBeenCalledWith('eng:primary', ['opus', 'haiku']), WAIT)
    await waitFor(() => {
      const now = screen.getByRole('region', { name: 'Account primary' })
      expect(classesFact(now).textContent).toBe('opus, haiku')
    }, WAIT)
  })

  it('sends [] when all four are ticked, and says so on the screen', async () => {
    api.setAccountModelClasses.mockResolvedValue(ok({ account: account({ model_classes: [] }) }))
    const pane = await mount([account({ model_classes: ['opus', 'fable', 'sonnet'] })], 'eng:primary')
    expect(pane.textContent).toContain('All four or none saves as every class.')
    fireEvent.click(box(pane, 'haiku'))
    fireEvent.click(within(pane).getByRole('button', { name: 'Save classes' }))
    await waitFor(() => expect(api.setAccountModelClasses).toHaveBeenCalledWith('eng:primary', []), WAIT)
  })

  it('treats none ticked as every class too, and a no-op as nothing to send', () => {
    expect(classesToSave([])).toEqual([])
    expect(classesToSave(['haiku', 'opus', 'sonnet', 'fable'])).toEqual([])
    expect(classesToSave(['haiku', 'opus'])).toEqual(['opus', 'haiku'])
  })

  it('keeps the list and says so when the write is refused', async () => {
    api.setAccountModelClasses.mockResolvedValue({
      status: 'error',
      error: { kind: 'invalid', httpStatus: 422, code: 'validation_failed', message: 'unknown model class.' },
    })
    const pane = await mount([account({ model_classes: [] })], 'eng:primary')
    fireEvent.click(box(pane, 'fable'))
    fireEvent.click(within(pane).getByRole('button', { name: 'Save classes' }))
    const alert = await within(pane).findByRole('alert', undefined, WAIT)
    expect(alert.textContent).toContain('The class list was not changed.')
    expect(api.setAccountModelClasses).toHaveBeenCalledWith('eng:primary', ['opus', 'sonnet', 'haiku'])
  })
})

describe('the writer', () => {
  it('PUTs the list to /v1/accounts/{id}/model-classes', async () => {
    vi.resetModules()
    vi.stubEnv('VITE_LIVE', '1')
    const real = await vi.importActual<typeof import('../api')>('../api')
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        new Response(JSON.stringify({ account: account({ model_classes: ['sonnet'] }) }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const res = await real.setAccountModelClasses('eng:primary', ['sonnet'])
    expect(res.status, JSON.stringify(res)).toBe('ok')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toMatch(/\/v1\/accounts\/eng%3Aprimary\/model-classes$/)
    expect(init?.method).toBe('PUT')
    expect(JSON.parse(String(init?.body))).toEqual({ model_classes: ['sonnet'] })
  })

  it('in fixture mode, starts every account at every class and records the write', async () => {
    vi.resetModules()
    vi.stubEnv('VITE_LIVE', '')
    const real = await vi.importActual<typeof import('../api')>('../api')
    expect(real.USE_FIXTURES).toBe(true)
    const before = await real.loadAccountsBoard()
    if (before.status !== 'ok') throw new Error(JSON.stringify(before))
    const first = before.data.page.accounts[0] as ServedAccount
    expect(first.model_classes).toEqual([])
    const res = await real.setAccountModelClasses(first.account_id, ['opus'])
    expect(res.status).toBe('ok')
    const after = await real.loadAccountsBoard()
    if (after.status !== 'ok') throw new Error(JSON.stringify(after))
    const again = after.data.page.accounts.find((a) => a.account_id === first.account_id) as ServedAccount
    expect(again.model_classes).toEqual(['opus'])
  })
})
