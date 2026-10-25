// CAPACITY › SUBSCRIPTION ACCOUNTS, ONE ACCOUNT OPENED (#2222, ACCOUNTS-A-DETAIL;
// docs/web-ui/mockups/accounts-a.html F4 and P3).
//
// What this file pins:
//   * the sheet leads with the row's verdict, then both windows, the agents on
//     it with the cap, the account (organisation and plan, owner, lent to,
//     id), and its own forecast, before the actions -- mock-up A's order;
//   * Change the cap writes `PUT /v1/accounts/{id}/max-agents` through
//     `setAccountMaxAgents`, only for a whole number 1 to 50, only for the
//     owner; a borrower reads the cap and has no control;
//   * a duplicate says what to do, naming the other side only when it is in
//     this read (this workspace), and never another workspace's account;
//   * the forecast is the pool forecast's rule for one subscription: its 7-day
//     share over the hours since its week opened, an aged reading at half.
//
// BREAK IT: drop `AgentCap`'s owner check, or let it send 0 or 51 -- a cap case
// fails. Name `possible_duplicate.of` when the read does not hold it -- the
// other-workspace case finds a label. Count an aged reading whole in
// `accountPaceOf` -- the pace case moves.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage } from '../types'
import { accountPaceOf, slotsLine, type PoolAccount } from '../accountsPool'

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

const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const
const HOUR = 3_600_000
const DAY = 24 * HOUR

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function account(over: Partial<PoolAccount> = {}): PoolAccount {
  const now = Date.now()
  return {
    account_id: 'eng:devops-team',
    owner_tenant: 'eng',
    label: 'devops-team',
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
      five_hour: { utilization: 0.4, resets_at: new Date(now - HOUR).toISOString(), reset: true },
      seven_day: { utilization: 0.86, resets_at: new Date(now + 3 * DAY).toISOString(), reset: false },
    },
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: { name: 'Acme', plan: 'Team' },
    ...over,
  }
}

async function open(accounts: PoolAccount[], id: string, tenant = 'eng'): Promise<HTMLElement> {
  api.loadAccountsBoard.mockResolvedValue(
    ok({
      page: { accounts: accounts as Account[], tenant_id: tenant, unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
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

beforeEach(() => {
  reads.read.mockReset()
  reads.read.mockResolvedValue(ok({ account_id: 'eng:devops-team', viewer: 'owner', total: 0, holders: [], others: 0 }))
  api.setAccountMaxAgents.mockReset()
})

describe('the sheet answers first, in mock-up A’s order', () => {
  it('leads with the verdict, then windows, agents, account and forecast, before the actions', async () => {
    const sheet = await open([account()], 'eng:devops-team')
    expect(sheet.querySelector('.acct-sv .acct-chip')!.textContent).toBe('Can take a new agent · 6 of 6 slots free')
    const order = [...sheet.querySelectorAll('.acct-sv, .acct-ss > h4, .acct-action > h4')].map((e) =>
      e.classList.contains('acct-sv') ? 'verdict' : (e.textContent ?? '').trim(),
    )
    const at = (name: string) => order.findIndex((x) => x.startsWith(name))
    expect(at('verdict')).toBe(0)
    expect(at('Windows')).toBeLessThan(at('Agents on it'))
    expect(at('Agents on it')).toBeLessThan(at('Account'))
    expect(at('Account')).toBeLessThan(at('Forecast'))
    expect(at('Forecast')).toBeLessThan(at('State'))
    expect(at('State')).toBeLessThan(at('Replace the credential'))
    // The account's facts, as the broker serves them.
    const kv = [...sheet.querySelectorAll('.acct-kv > dt')].map((d) => [d.textContent, d.nextElementSibling!.textContent])
    expect(kv[0]).toEqual(['Signed in to', 'Acme · Team'])
    expect(kv[1]![1]).toContain('eng')
    expect(kv[2]![1]).toContain('smoke, u-bogdan')
    expect(kv[3]).toEqual(['Id', 'eng:devops-team'])
    // Pause and Drain keep their place and their confirmation.
    fireEvent.click(screen.getByRole('button', { name: 'Drain' }))
    expect(await screen.findByRole('dialog', undefined, WAIT)).toBeTruthy()
    expect(api.setAccountState).not.toHaveBeenCalled()
  })

  it('says an organisation the broker has not recorded as a dash with its reason, not a guess', async () => {
    const sheet = await open([account({ organisation: null })], 'eng:devops-team')
    const dd = [...sheet.querySelectorAll('.acct-kv > dt')].find((d) => d.textContent === 'Signed in to')!.nextElementSibling!
    expect(dd.textContent).toBe('— not recorded yet')
  })

  it('gives the hand-out the same age the row gives it', async () => {
    // 5 d 19 h ago: the row says `given out 5d 19h ago` (agoPastDay); the
    // sheet said `(6d ago)` beside it, one fact rounded two ways (desktop
    // review, dark-desktop-02).
    const given = new Date(Date.now() - (5 * DAY + 19 * HOUR + 10 * 60_000)).toISOString()
    const sheet = await open([account({ last_assigned_at: given })], 'eng:devops-team')
    const fact = [...sheet.querySelectorAll('.acct-facts > li')].find((li) => li.textContent?.startsWith('last given out'))!
    expect(fact.textContent).toContain('(5d 19h ago)')
    const row = document.querySelector('.acct-rank-row')!
    expect(row.textContent).toContain('given out 5d 19h ago')
  })
})

describe('the agent cap and its Change (owner only, 1 to 50)', () => {
  it('writes a valid number through PUT /v1/accounts/{id}/max-agents and refuses 0 and 51 before sending', async () => {
    api.setAccountMaxAgents.mockResolvedValue(ok({}))
    await open([account()], 'eng:devops-team')
    expect(screen.getByText(/At most/).textContent).toContain('At most 6 agents at once.')
    fireEvent.click(screen.getByRole('button', { name: 'Change the cap' }))
    const field = screen.getByRole('spinbutton', { name: 'Agents at once' }) as HTMLInputElement
    const save = () => screen.getByRole('button', { name: 'Save cap' }) as HTMLButtonElement
    for (const bad of ['0', '51', '2.5', '']) {
      fireEvent.change(field, { target: { value: bad } })
      expect(save().disabled, `${bad} armed the save`).toBe(true)
    }
    fireEvent.change(field, { target: { value: '4' } })
    expect(save().disabled).toBe(false)
    fireEvent.click(save())
    await waitFor(() => expect(api.setAccountMaxAgents).toHaveBeenCalledWith('eng:devops-team', 4), WAIT)
    expect(api.setAccountMaxAgents).toHaveBeenCalledTimes(1)
  })

  it('says the broker’s refusal in its words and keeps the editor open', async () => {
    api.setAccountMaxAgents.mockResolvedValue({
      status: 'error',
      error: { kind: 'invalid', httpStatus: 422, code: 'validation_failed', message: 'refused by the broker in this test' },
    })
    await open([account()], 'eng:devops-team')
    fireEvent.click(screen.getByRole('button', { name: 'Change the cap' }))
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Agents at once' }), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save cap' }))
    expect((await screen.findByRole('alert', undefined, WAIT)).textContent).toContain('refused by the broker in this test')
    expect(screen.getByRole('spinbutton', { name: 'Agents at once' })).toBeTruthy()
  })

  it('gives a borrower the number and no control', async () => {
    await open([account({ owner_tenant: 'eng' })], 'eng:devops-team', 'smoke')
    expect(screen.queryByRole('button', { name: 'Change the cap' })).toBeNull()
    expect(screen.getByText(/At most/).textContent).toContain('Its owner sets it.')
  })
})

describe('a duplicate says what to do, naming only this workspace’s accounts', () => {
  it('tells the withheld registration to replace its credential, naming the other by its label', async () => {
    const kept = account({ account_id: 'eng:home-personal', label: 'home-personal', duplicate_of: 'eng:team' })
    const extra = account({
      account_id: 'eng:team',
      label: 'team',
      duplicate_of: 'eng:home-personal',
      duplicate_reason: 'registered on the same organisation as eng:home-personal',
      can_take_new_agent: false,
      blocked_by: [{ duplicate_of: 'eng:home-personal', reason: 'registered on the same organisation as eng:home-personal' }],
    })
    const sheet = await open([kept, extra], 'eng:team')
    const dup = sheet.querySelector('.acct-ss-dup')!
    expect(dup.querySelector('h4')!.textContent).toBe('One subscription, registered twice')
    expect(dup.textContent).toContain('team was signed in to the same organisation as home-personal')
    expect(dup.textContent).toContain('Replace the credential below')
  })

  it('never names another workspace’s account, for a proven or a possible pair', async () => {
    const proven = account({
      account_id: 'eng:team',
      label: 'team',
      duplicate_of: null,
      duplicate_reason: 'this subscription is already registered in another workspace',
      can_take_new_agent: false,
      blocked_by: [{ duplicate_of: null, reason: 'this subscription is already registered in another workspace' }],
    })
    let sheet = await open([proven], 'eng:team')
    expect(sheet.querySelector('.acct-ss-dup')!.textContent).toContain('an account in another workspace')
    expect(sheet.querySelector('.acct-ss-dup')!.textContent).not.toMatch(/\w+:\w+/)

    document.body.innerHTML = ''
    const possible = account({
      possible_duplicate: { of: null, reason: 'identical readings on two polls with an account in another workspace' },
    })
    sheet = await open([possible], 'eng:devops-team')
    const dup = sheet.querySelector('.acct-ss-dup')!
    expect(dup.querySelector('h4')!.textContent).toBe('Possibly one subscription, registered twice')
    expect(dup.textContent).toContain('devops-team and an account in another workspace read identically')
    expect(dup.textContent).toContain('The pool still hands out both')
  })
})

describe('on a phone the opened account is the whole screen (mock-up A P3)', () => {
  /** jsdom has no `matchMedia`: `phone` matches the 560px query. */
  const media = (phone: boolean) =>
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: phone && query.includes('560'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }))
  afterEach(() => {
    vi.unstubAllGlobals()
  })
  const dup = () => [
    account({ account_id: 'eng:home-personal', label: 'home-personal' }),
    account({
      account_id: 'eng:team',
      label: 'team',
      duplicate_of: 'eng:home-personal',
      duplicate_reason: 'registered on the same organisation as eng:home-personal',
      can_take_new_agent: false,
      blocked_by: [{ duplicate_of: 'eng:home-personal', reason: 'registered on the same organisation as eng:home-personal' }],
    }),
  ]

  it('draws no verdict, rule line or banner above the sheet at 390, only ‹ Accounts and the account', async () => {
    media(true)
    const sheet = await open(dup(), 'eng:team')
    expect(sheet.getAttribute('aria-label')).toBe('Account team')
    expect(document.querySelector('.acct-dup'), 'the page’s duplicate banner is still above the sheet').toBeNull()
    expect(document.querySelector('.acct-verdict, .acct-rule')).toBeNull()
    expect(sheet.querySelector('.acct-back')).not.toBeNull()
    // The account's own duplicate advice is in the sheet, so nothing is lost.
    expect(sheet.querySelector('.acct-ss-dup')).not.toBeNull()
    // Back brings the page's lead back.
    fireEvent.click(sheet.querySelector<HTMLButtonElement>('.acct-back')!)
    await waitFor(() => expect(document.querySelector('.acct-dup')).not.toBeNull(), WAIT)
  })

  it('keeps the lead beside the sheet on a desktop, where the table is still the page', async () => {
    media(false)
    await open(dup(), 'eng:team')
    expect(document.querySelector('.acct-dup')).not.toBeNull()
  })
})

describe('the page’s duplicate banner opens the registration to fix', () => {
  // MAIN'S BANNER, THIS SHEET (PR 2309 + PR 2326): the banner sits on the
  // page's surface, so its two actions are buttons with a 3:1 edge, and both
  // open the account's sheet. Replace the credential also brings the sheet's
  // own Replace the credential section into view and focuses its button.
  const dupBoard = () => {
    const kept = account({ account_id: 'eng:home-personal', label: 'home-personal' })
    const extra = account({
      account_id: 'eng:team',
      label: 'team',
      duplicate_of: 'eng:home-personal',
      duplicate_reason: 'registered on the same organisation as eng:home-personal',
      can_take_new_agent: false,
      blocked_by: [{ duplicate_of: 'eng:home-personal', reason: 'registered on the same organisation as eng:home-personal' }],
    })
    api.loadAccountsBoard.mockResolvedValue(
      ok({
        page: { accounts: [kept, extra] as Account[], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
        tenants: [],
        tenantsDetail: null,
        readAt: Date.now(),
      } as AccountsBoard),
    )
  }
  const findBanner = async () =>
    (
      await screen.findByText(
        (_, el) => el?.classList.contains('acct-dup-head') === true && el.textContent === 'team and home-personal are one subscription',
        undefined,
        WAIT,
      )
    ).closest('.acct-dup') as HTMLElement

  it('opens the account’s sheet from Open', async () => {
    dupBoard()
    render(<AccountsScreen />)
    const banner = await findBanner()
    fireEvent.click(within(banner).getByRole('button', { name: 'Open team' }))
    await waitFor(() => expect(document.querySelector('.acct-sheet')?.getAttribute('aria-label')).toBe('Account team'), WAIT)
  })

  it('lands Replace the credential on the sheet’s own section, its button focused', async () => {
    dupBoard()
    render(<AccountsScreen />)
    const banner = await findBanner()
    fireEvent.click(within(banner).getByRole('button', { name: 'Replace the credential' }))
    await waitFor(() => expect(document.querySelector('.acct-sheet')?.getAttribute('aria-label')).toBe('Account team'), WAIT)
    const section = document.querySelector<HTMLElement>('.acct-sheet [data-replace="eng:team"]')
    expect(section, 'the sheet has no section the banner can land on').not.toBeNull()
    await waitFor(() => expect(section!.contains(document.activeElement)).toBe(true), WAIT)
  })
})

describe('one account’s forecast, from today’s reading', () => {
  const now = Date.parse('2026-10-24T09:16:00Z')
  const week = (used: number, resetsIn: number, over: Partial<PoolAccount> = {}) =>
    account({
      observed_at: new Date(now - 5 * 60_000).toISOString(),
      windows: { seven_day: { utilization: used, resets_at: new Date(now + resetsIn).toISOString(), reset: false } },
      ...over,
    })

  it('takes the pace over the hours since its week opened, and finds when it reaches its floor', () => {
    // Opened 4 days ago (resets in 3), 48% used: 0.5% of the week an hour.
    const p = accountPaceOf(week(0.48, 3 * DAY), now)!
    expect(p.perHour).toBeCloseTo(0.005, 6)
    // 52% left, floor 5%: 47% above it, at 0.5% an hour, 94 hours -- after the reset.
    expect(p.floorAt).toBeNull()
    // 86% used over 4 days: 14% left, 9% above the floor at 0.896% an hour, ~10h.
    const q = accountPaceOf(week(0.86, 3 * DAY), now)!
    expect((q.floorAt! - now) / HOUR).toBeCloseTo(0.09 / (0.86 / 96), 3)
    expect(q.underNow).toBe(false)
  })

  it('counts an aged reading at half, as the broker does', () => {
    const fresh = accountPaceOf(week(0.86, 3 * DAY), now)!
    const aged = accountPaceOf(week(0.86, 3 * DAY, { stale: true }), now)!
    // 14% left counts as 7%: 2% above the 5% floor, not 9%.
    expect(aged.floorAt! - now).toBeLessThan(fresh.floorAt! - now)
    expect(accountPaceOf(week(0.92, 3 * DAY, { stale: true }), now)!.underNow).toBe(true)
  })

  it('has no pace with no 7-day reading, and none for a week that reset with nothing read since', () => {
    expect(accountPaceOf(account({ observed_at: null, windows: {} }), now)).toBeNull()
    const reset = accountPaceOf(
      account({ windows: { seven_day: { utilization: 0.9, resets_at: new Date(now - HOUR).toISOString(), reset: true } } }),
      now,
    )!
    expect(reset.read).toBe(false)
    expect(reset.resetsAt).toBe(now - HOUR + 7 * DAY)
  })
})

describe('the head chip says the slots in words', () => {
  it('counts free slots of the cap, one before a first reading, and says full or no', () => {
    expect(slotsLine(account({ assigned: 2 }), 'eng')).toBe('Can take a new agent · 4 of 6 slots free')
    expect(slotsLine(account({ observed_at: null }), 'eng')).toBe('Can take a new agent · 1 of 1 slot free')
    expect(
      slotsLine(account({ assigned: 6, can_take_new_agent: false, blocked_by: [{ cap: 6, max_agents: 6 }] }), 'eng'),
    ).toBe('Full · 6 of 6 agents')
    expect(
      slotsLine(
        account({
          can_take_new_agent: false,
          blocked_by: [{ window: 'seven_day', used: 0.98, floor: 0.05, resets_at: '2026-10-26T08:00:00Z' }],
        }),
        'eng',
      ),
    ).toBe('Cannot take a new agent')
    expect(slotsLine(account({ can_take_new_agent: undefined }), 'eng')).toBeNull()
  })
})
