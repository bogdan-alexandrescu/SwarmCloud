// CAPACITY › ACCOUNTS: THE VOICE IT POOL (the owner, 2026-11-02: ChatGPT plan
// accounts can be reserved for Voice it in a separate pool, and an account can
// be in the workspace pool and the Voice it pool at once).
//
// What this file pins:
//   * each row says its pools -- `Workspace`, `Voice it`, `Workspace · Voice
//     it` -- and an older broker's row (no `pools`) says nothing;
//   * a "Voice it pool" section lists only the accounts in it: provider, both
//     windows, who may use it (read-only), and the session holding it or `free`;
//   * a platform admin's `Pools…` opens a dialog with a box per pool, the people
//     read-only, and the label typed back; save stays off until the label is
//     typed, and off with no pool ticked; it PUTs `/v1/accounts/{id}/pools`;
//   * the broker's 409 `pool_in_use` is said inside the dialog;
//   * the section renders in both themes, at 1440 and at 390.
//
// BREAK IT: drop the `willSave.length === 0` check -- the last-pool case saves;
// drop `armed` -- the typed-label case saves; filter the section on `workspace`
// -- the section lists the wrong account.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage } from '../types'
import type { PoolAccount } from '../accountsPool'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountModelClasses: vi.fn(),
  setAccountName: vi.fn(),
  setAccountState: vi.fn(),
  setAccountMaxAgents: vi.fn(),
  loadAccountHealth: vi.fn(),
  loadTask: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

const { AccountsScreen, VoiceItPool, ACCOUNT_POOLS_ROUTE, servesWorkspace } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const
const CONFIRM = { name: (_: string, el: Element) => el.classList.contains('c-dlg') }
const HOUR = 3_600_000
const DAY = 24 * HOUR
const PERSON = 'person@example.com'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function account(over: Partial<PoolAccount> & Record<string, unknown> = {}): PoolAccount {
  const now = Date.now()
  return {
    account_id: 'eng:team-a',
    owner_tenant: 'eng',
    label: 'team-a',
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    observed_at: new Date(now - 5 * 60_000).toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: new Date(now - 26 * 60_000).toISOString(),
    windows: {
      five_hour: { utilization: 0.4, resets_at: new Date(now + HOUR).toISOString(), reset: false },
      seven_day: { utilization: 0.3, resets_at: new Date(now + 3 * DAY).toISOString(), reset: false },
    },
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    ...over,
  } as PoolAccount
}

const WORKSPACE_ONLY = account({ pools: ['workspace'], voice_it_people: [], voice_it_holder: null })
const BOTH = account({
  account_id: 'eng:plan-both',
  label: 'plan-both',
  provider: 'openai',
  pools: ['workspace', 'voice-it'],
  voice_it_people: [PERSON],
  voice_it_holder: { voice_session_id: 'vs-0001', person: PERSON, since: new Date(Date.now() - 4 * 60_000).toISOString() },
})
const VOICE_ONLY = account({
  account_id: 'eng:plan-voice',
  label: 'plan-voice',
  provider: 'openai',
  pools: ['voice-it'],
  voice_it_people: [PERSON],
  voice_it_holder: null,
})
const OLD_BROKER = account({ account_id: 'eng:old-row', label: 'old-row' })

function board(accounts: PoolAccount[]): AccountsBoard {
  return {
    page: { accounts: accounts as Account[], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

async function screenWith(accounts: PoolAccount[]): Promise<void> {
  api.loadAccountsBoard.mockResolvedValue(ok(board(accounts)))
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
}

async function openSheet(id: string): Promise<HTMLElement> {
  fireEvent.click(document.querySelector<HTMLButtonElement>(`button.acct-open[data-account="${id}"]`)!)
  return document.querySelector<HTMLElement>('.acct-sheet')!
}

function setWidth(width: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width })
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(max-width: 560px)' ? width <= 560 : false,
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }))
}

beforeEach(() => {
  setWidth(1440)
  fetchMock.read.mockReset()
  fetchMock.read.mockResolvedValue(ok({ account_id: 'eng:team-a', viewer: 'owner', total: 0, holders: [], others: 0 }))
  fetchMock.write.mockReset()
  api.loadMe.mockReset()
  api.loadMe.mockResolvedValue(ok({ principal: { is_admin: true } }))
  api.loadAccountHealth.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no health' } })
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.documentElement.removeAttribute('data-theme')
})

describe('each row says its pools', () => {
  it('draws Workspace, Voice it and Workspace · Voice it, and nothing for an older broker', async () => {
    await screenWith([WORKSPACE_ONLY, BOTH, VOICE_ONLY, OLD_BROKER])
    const pools = (id: string) =>
      document.querySelector(`button.acct-open[data-account="${id}"]`)!.closest('tr')!.querySelector('.acct-pools')?.textContent ?? null
    expect(pools('eng:team-a')).toBe('Pools: Workspace')
    expect(pools('eng:plan-both')).toBe('Pools: Workspace · Voice it')
    expect(pools('eng:plan-voice')).toBe('Pools: Voice it')
    expect(pools('eng:old-row')).toBeNull()
  })
})

describe('the Voice it pool section', () => {
  it('lists only the accounts in the Voice it pool, with provider, windows, people and holder', async () => {
    await screenWith([WORKSPACE_ONLY, BOTH, VOICE_ONLY])
    const section = screen.getByRole('region', { name: 'Voice it pool' })
    const rows = within(section).getAllByRole('row').slice(1)
    expect(rows.map((r) => r.querySelector('.acct-nm')?.textContent)).toEqual(['plan-both', 'plan-voice'])
    expect(section.textContent).not.toContain('team-a')
    const both = rows[0]!
    expect(both.querySelector('[data-provider="openai"]')).not.toBeNull()
    expect(both.textContent).toContain(PERSON)
    expect(both.querySelector('[data-holder="vs-0001"]')!.textContent).toContain('vs-0001')
    expect(rows[1]!.querySelector('[data-holder="free"]')!.textContent).toBe('free')
    // The windows are the table's own cells.
    expect(both.querySelectorAll('.acct-win').length).toBe(2)
  })

  it('keeps a space between its heading and the account count', () => {
    render(<VoiceItPool accounts={[BOTH, VOICE_ONLY] as Account[]} now={Date.now()} phone={false} />)
    const section = screen.getByRole('region', { name: 'Voice it pool' })
    expect(section.querySelector('h3')!.textContent).toBe('Voice it pool 2 accounts')
  })

  it('counts a Voice-it-only account toward no workspace capacity', () => {
    // The verdict, the rule line and the forecast read only these.
    expect([WORKSPACE_ONLY, BOTH, VOICE_ONLY, OLD_BROKER].filter(servesWorkspace).map((a) => a.label))
      .toEqual([WORKSPACE_ONLY.label, BOTH.label, OLD_BROKER.label])
  })

  it('is not drawn when no account is in the Voice it pool', async () => {
    await screenWith([WORKSPACE_ONLY, OLD_BROKER])
    expect(screen.queryByRole('region', { name: 'Voice it pool' })).toBeNull()
  })

  for (const theme of ['light', 'dark'] as const) {
    for (const width of [1440, 390] as const) {
      it(`renders under the ${theme} theme at ${width} wide`, () => {
        setWidth(width)
        document.documentElement.setAttribute('data-theme', theme)
        render(<VoiceItPool accounts={[WORKSPACE_ONLY, BOTH, VOICE_ONLY] as Account[]} now={Date.now()} phone={width <= 560} />)
        const section = screen.getByRole('region', { name: 'Voice it pool' })
        expect(within(section).getByRole('heading', { name: /Voice it pool/ })).toBeTruthy()
        if (width <= 560) {
          expect(section.querySelectorAll('article.acct-card').length).toBe(2)
          expect(section.querySelector('table')).toBeNull()
        } else {
          expect(within(section).getByRole('table')).toBeTruthy()
          expect(section.querySelectorAll('article.acct-card').length).toBe(0)
        }
        expect(section.textContent).toContain('plan-both')
        expect(section.textContent).toContain('plan-voice')
        expect(section.textContent).toContain('free')
        expect(section.textContent).not.toContain('team-a')
      })
    }
  }
})

async function openPools(acct: PoolAccount): Promise<HTMLElement> {
  await screenWith([WORKSPACE_ONLY, BOTH, VOICE_ONLY])
  const sheet = await openSheet(acct.account_id)
  const button = await within(sheet).findByRole('button', { name: 'Pools…' }, WAIT)
  fireEvent.click(button)
  return screen.getByRole('dialog', CONFIRM)
}

describe('Pools…, a platform admin’s', () => {
  it('is not offered to a reader who is not a platform admin', async () => {
    api.loadMe.mockResolvedValue(ok({ principal: { is_admin: false } }))
    await screenWith([BOTH])
    const sheet = await openSheet(BOTH.account_id)
    await act(async () => {
      for (let i = 0; i < 5; i += 1) await Promise.resolve()
    })
    expect(within(sheet).queryByRole('button', { name: 'Pools…' })).toBeNull()
  })

  it('needs the label typed back, shows the people read-only, and PUTs the pools route', async () => {
    fetchMock.write.mockResolvedValue(ok({ account: { ...BOTH, pools: ['voice-it'] } }))
    const dlg = await openPools(BOTH)
    expect(dlg.textContent).toContain(PERSON)
    expect(dlg.textContent).toContain('Version one: the platform owner only')
    expect(within(dlg).queryByRole('textbox', { name: /Voice it people/ })).toBeNull()

    const ws = within(dlg).getByRole('checkbox', { name: 'Workspace' }) as HTMLInputElement
    const vi_ = within(dlg).getByRole('checkbox', { name: 'Voice it' }) as HTMLInputElement
    expect(ws.checked).toBe(true)
    expect(vi_.checked).toBe(true)
    fireEvent.click(ws)

    const save = within(dlg).getByRole('button', { name: 'Save pools' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.change(within(dlg).getByLabelText('Type plan-both to confirm the pools'), { target: { value: 'plan-bot' } })
    expect(save.disabled).toBe(true)
    fireEvent.click(save)
    expect(fetchMock.write).not.toHaveBeenCalled()

    fireEvent.change(within(dlg).getByLabelText('Type plan-both to confirm the pools'), { target: { value: 'plan-both' } })
    expect(save.disabled).toBe(false)
    fireEvent.click(save)
    await waitFor(() => expect(fetchMock.write).toHaveBeenCalledTimes(1), WAIT)
    const [target, method, body] = fetchMock.write.mock.calls[0]!
    expect((target as { url: string; template: string }).template).toBe(ACCOUNT_POOLS_ROUTE)
    expect((target as { url: string }).url).toBe('/v1/accounts/eng%3Aplan-both/pools')
    expect(method).toBe('PUT')
    expect(body).toEqual({ pools: ['voice-it'], confirm_label: 'plan-both' })
  })

  it('refuses to remove the last pool', async () => {
    const dlg = await openPools(VOICE_ONLY)
    fireEvent.change(within(dlg).getByLabelText('Type plan-voice to confirm the pools'), { target: { value: 'plan-voice' } })
    fireEvent.click(within(dlg).getByRole('checkbox', { name: 'Voice it' }))
    const save = within(dlg).getByRole('button', { name: 'Save pools' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    expect(dlg.textContent).toContain('keep at least one pool ticked')
    fireEvent.click(save)
    expect(fetchMock.write).not.toHaveBeenCalled()
  })

  it('says the broker’s 409 pool_in_use inside the dialog and keeps it open', async () => {
    fetchMock.write.mockResolvedValue({
      status: 'error',
      error: {
        kind: 'conflict',
        httpStatus: 409,
        code: 'conflict',
        message: 'the account holds a live voice-it lease',
        detail: { reason: 'pool_in_use' },
        method: 'PUT',
      },
    })
    const dlg = await openPools(BOTH)
    fireEvent.click(within(dlg).getByRole('checkbox', { name: 'Voice it' }))
    fireEvent.change(within(dlg).getByLabelText('Type plan-both to confirm the pools'), { target: { value: 'plan-both' } })
    fireEvent.click(within(dlg).getByRole('button', { name: 'Save pools' }))
    const alert = await within(dlg).findByRole('alert', {}, WAIT)
    expect(alert.textContent).toContain('Pool in use')
    expect(alert.textContent).toContain('the account holds a live voice-it lease')
    expect(alert.textContent).toContain('The pools were not changed.')
    expect(screen.getByRole('dialog', CONFIRM)).toBe(dlg)
  })
})
