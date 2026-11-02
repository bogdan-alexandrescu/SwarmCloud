// CAPACITY › ACCOUNTS NAMES EACH ACCOUNT'S PROVIDER (part of #2688). The pool
// holds Claude subscriptions (`anthropic`) and ChatGPT plans (`openai`).
//
// What this file pins:
//   * every row of the table, and every card on a phone, names its provider in
//     words: "Claude subscription", "ChatGPT plan", and an unknown provider as
//     its own id, never a guess;
//   * the account sheet's head chip does the same;
//   * a plan's sign-in section never posts a sign-in (least of all one for
//     `anthropic`): a plan's redirect is the person's own machine, so it shows
//     the exact `sc account add --provider openai` command, loans included;
//   * a Claude account's sign-in is unchanged, and a provider the API cannot
//     sign in is still refused as a mismatch;
//   * the dev fixture carries a plan, so the treatment renders in development.
//
// BREAK IT: print `account.provider` in the chip again, or drop the plan
// branch from `Reauth` -- the head and sign-in cases fail.

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
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
  setAccountState: vi.fn(),
  setAccountMaxAgents: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', () => api)

const reads = vi.hoisted(() => ({ read: vi.fn() }))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: reads.read,
}))

const { AccountsScreen, planSignInCommand } = await import('../Accounts')
const { AccountsTable } = await import('../AccountsTable')

const WAIT = { timeout: 5000 } as const
const HOUR = 3_600_000

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function account(label: string, over: Partial<PoolAccount> = {}): PoolAccount {
  const now = Date.now()
  return {
    account_id: `eng:${label}`,
    owner_tenant: 'eng',
    label,
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: ['smoke', 'u-bogdan'],
    assigned: 0,
    observed_at: new Date(now - 5 * 60_000).toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: new Date(now - 26 * 60_000).toISOString(),
    windows: {
      five_hour: { utilization: 0.4, resets_at: new Date(now + HOUR).toISOString(), reset: false },
      seven_day: { utilization: 0.3, resets_at: new Date(now + 50 * HOUR).toISOString(), reset: false },
    },
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: { name: 'Example', plan: 'Team' },
    ...over,
  }
}

const CLAUDE = account('devops-team')
const PLAN = account('codex-plan', { provider: 'openai', organisation: { name: 'ChatGPT', plan: 'Plus' } })
const ODD = account('odd-one', { provider: 'harbor-vendor' })

async function open(accounts: PoolAccount[], id: string): Promise<HTMLElement> {
  api.loadAccountsBoard.mockResolvedValue(
    ok({
      page: { accounts: accounts as Account[], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: Date.now(),
    } as AccountsBoard),
  )
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
  fireEvent.click(document.querySelector<HTMLButtonElement>(`button.acct-open[data-account="${id}"]`)!)
  return document.querySelector<HTMLElement>('.acct-sheet')!
}

function rowOf(container: HTMLElement, id: string): HTMLElement {
  return container.querySelector<HTMLElement>(`button.acct-open[data-account="${id}"]`)!.closest<HTMLElement>('tr, article')!
}

beforeEach(() => {
  reads.read.mockReset()
  reads.read.mockResolvedValue(ok({ account_id: 'eng:devops-team', viewer: 'owner', total: 0, holders: [], others: 0 }))
  api.beginAccountSignIn.mockReset()
  api.beginAccountSignIn.mockResolvedValue({ status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'no' } })
})

afterEach(() => {
  cleanup()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('the table names each account’s provider in words', () => {
  for (const phone of [false, true]) {
    it(`a plan reads "ChatGPT plan", a Claude account "Claude subscription", an unknown one its id (${phone ? 'phone' : 'desktop'})`, () => {
      const { container } = render(
        <AccountsTable accounts={[CLAUDE, PLAN, ODD]} scope="eng" now={Date.now()} chosen={null} phone={phone} onChoose={() => {}} />,
      )
      const prov = (id: string) => rowOf(container, id).querySelector<HTMLElement>('.acct-prov')!
      expect(prov('eng:codex-plan').textContent).toBe('ChatGPT plan')
      expect(prov('eng:devops-team').textContent).toBe('Claude subscription')
      // Never a guess: the unknown provider is its own id, drawn as an id.
      expect(prov('eng:odd-one').textContent).toBe('harbor-vendor')
      expect(prov('eng:odd-one').querySelector('.mono')!.textContent).toBe('harbor-vendor')
      // The raw provider id is not printed on a known provider's row.
      expect(rowOf(container, 'eng:codex-plan').textContent).not.toMatch(/\bopenai\b/)
      expect(rowOf(container, 'eng:devops-team').textContent).not.toMatch(/\banthropic\b/)
    })
  }

  it('leaves the rest of a Claude row as it was: id, organisation, windows, agents, loans', () => {
    const { container } = render(
      <AccountsTable accounts={[CLAUDE, PLAN]} scope="eng" now={Date.now()} chosen={null} phone={false} onChoose={() => {}} />,
    )
    const row = rowOf(container, 'eng:devops-team')
    const cells = row.querySelectorAll('td')
    expect(cells).toHaveLength(9)
    expect(cells[1]!.querySelector('.raw')!.textContent).toBe('eng:devops-team')
    expect(cells[1]!.textContent).toContain('Example · Team')
    expect(cells[7]!.textContent).toBe('0 of 6')
    expect(cells[8]!.textContent).toBe('smoke, u-bogdan')
  })
})

describe('the sheet’s head names the provider in words', () => {
  it('a plan’s chip reads "ChatGPT plan · lending on"', async () => {
    const sheet = await open([CLAUDE, PLAN], 'eng:codex-plan')
    const chip = [...sheet.querySelectorAll('.acct-dhead-row *')].find((e) => /lending/.test(e.textContent ?? '') && e.children.length <= 1)!
    expect(chip.textContent).toBe('ChatGPT plan · lending on')
  })

  it('a Claude account’s chip reads "Claude subscription · lending on"', async () => {
    const sheet = await open([CLAUDE, PLAN], 'eng:devops-team')
    expect(sheet.querySelector('.acct-dhead-row')!.textContent).toContain('Claude subscription · lending on')
    expect(sheet.querySelector('.acct-dhead-row')!.textContent).not.toContain('anthropic')
  })

  it('an unknown provider’s chip shows its own id', async () => {
    const sheet = await open([CLAUDE, ODD], 'eng:odd-one')
    expect(sheet.querySelector('.acct-dhead-row')!.textContent).toContain('harbor-vendor · lending on')
  })
})

describe('a plan’s sign-in section', () => {
  it('posts no sign-in and shows the exact `sc account add --provider openai` command, loans included', async () => {
    const sheet = await open([CLAUDE, PLAN], 'eng:codex-plan')
    const section = sheet.querySelector<HTMLElement>('[data-replace="eng:codex-plan"]')!
    expect(section).not.toBeNull()
    expect(section.querySelector('h4')!.textContent).toBe('Replace the credential')
    // Not refused as a mismatch: the API signs a plan in, on the person's own machine.
    expect(section.textContent).not.toContain('Provider mismatch')
    expect(sheet.textContent).not.toContain('Provider mismatch')
    expect(section.querySelector('[data-cmd]')!.textContent).toBe(
      'sc account add --label codex-plan --provider openai --lend-to smoke --lend-to u-bogdan',
    )
    expect(section.textContent).toContain('localhost:1455')
    // No control in it starts a browser sign-in, so nothing can post one.
    expect(within(section).queryAllByRole('button')).toHaveLength(0)
    for (const b of within(sheet).queryAllByRole('button')) {
      if (/sign in|sign-in/i.test(b.textContent ?? '')) fireEvent.click(b)
    }
    expect(api.beginAccountSignIn).not.toHaveBeenCalled()
    for (const call of api.beginAccountSignIn.mock.calls) expect(call[0]?.provider).not.toBe('anthropic')
  })

  it('on a plan that needs a person, says so and that it leaves REAUTH_REQUIRED by itself', async () => {
    const broken = { ...PLAN, state: 'REAUTH_REQUIRED', reason: 'refresh failed: reauth_required', can_take_new_agent: false }
    const sheet = await open([CLAUDE, broken], 'eng:codex-plan')
    const section = sheet.querySelector<HTMLElement>('[data-replace="eng:codex-plan"]')!
    expect(section.querySelector('h4')!.textContent).toBe('Sign in again')
    expect(section.textContent).toContain('Only a person can replace this.')
    expect(section.textContent).toContain('leaves REAUTH_REQUIRED as soon as it registers')
    expect(within(section).queryAllByRole('button')).toHaveLength(0)
    expect(api.beginAccountSignIn).not.toHaveBeenCalled()
  })

  it('quotes a word a shell would not read as itself', () => {
    expect(planSignInCommand({ label: 'plan', lend_to: [] })).toBe('sc account add --label plan --provider openai')
    expect(planSignInCommand({ label: 'plan', lend_to: ["o'brien team"] })).toBe(
      `sc account add --label plan --provider openai --lend-to 'o'\\''brien team'`,
    )
  })
})

describe('a Claude account’s sign-in is unchanged, and an unknown provider is still refused', () => {
  it('starts the browser sign-in with the label and loans, and no provider', async () => {
    const sheet = await open([CLAUDE, PLAN], 'eng:devops-team')
    const section = sheet.querySelector<HTMLElement>('[data-replace="eng:devops-team"]')!
    expect(section.querySelector('[data-cmd]')).toBeNull()
    fireEvent.click(within(section).getByRole('button', { name: 'Start a replacement sign-in' }))
    expect(api.beginAccountSignIn).toHaveBeenCalledTimes(1)
    expect(api.beginAccountSignIn.mock.calls[0]![0]).toEqual({ label: 'devops-team', lend_to: ['smoke', 'u-bogdan'] })
  })

  it('refuses a provider the API cannot sign in as a mismatch, naming both', async () => {
    const sheet = await open([CLAUDE, ODD], 'eng:odd-one')
    expect(sheet.textContent).toContain('Provider mismatch')
    expect(sheet.textContent).toContain('harbor-vendor')
    expect(sheet.querySelector('[data-cmd]')).toBeNull()
  })
})

describe('the dev fixture carries a ChatGPT plan', () => {
  it('serves one `openai` account beside the Claude ones', async () => {
    const real = await vi.importActual<typeof import('../api')>('../api')
    const r = await real.loadAccountPool()
    if (r.status !== 'ok') throw new Error('the fixture accounts did not load')
    const providers = r.data.accounts.map((a) => a.provider)
    expect(providers.filter((p) => p === 'openai')).toHaveLength(1)
    expect(providers).toContain('anthropic')
  })
})
