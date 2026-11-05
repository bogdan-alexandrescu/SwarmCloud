// CAPACITY › ACCOUNTS CARRIES NO EXPLANATORY PROSE ON ITS SURFACE (BGD's
// standing rule, 5 November 2026; part of #2688).
//
// The audit of that day listed this page's sentences: the floors line, the
// duplicate banner's why and its how-to, the sign-in callout's "gives out
// nothing until then", the table's "Ranked by ..." note, the forecast's notes
// and its "it does not go dark", and an arrow link to the waiting agents. Each
// fact is still on the page, now as the tooltip of the label it explained or a
// status tick's reason; the arrow is a button. Only what must stay visible --
// the lent-to-you line, the unreadable-documents count -- stays.
//
// BREAK IT: put a sentence back on the surface, or drop the fact from its
// tooltip, or put the arrow back -- a case below turns red.

import { act, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { AccountsPage } from '../types'
import type { PoolAccount } from '../accountsPool'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  loadTasksInState: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
}))
vi.mock('../api', () => api)

const { AccountsScreen } = await import('../Accounts')
const { DuplicateBanner, Forecast, Verdict, WAITING_HREF } = await import('../AccountsVerdict')

const WAIT = { timeout: 5000 } as const
const H = 3_600_000
const D = 24 * H
const iso = (t: number) => new Date(t).toISOString()

function acct(label: string, over: Partial<PoolAccount> = {}, now = Date.now()): PoolAccount {
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
    observed_at: new Date(now - 5 * 60_000).toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: new Date(now - H).toISOString(),
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: null,
    ...over,
  }
}

function board(accounts: PoolAccount[]): Result<AccountsBoard> {
  const now = Date.now()
  return {
    status: 'ok',
    fetchedAt: now,
    serverAt: iso(now),
    data: {
      page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: now,
    },
  }
}

/** What a reader sees: the text without the hidden descriptions or an open tooltip. */
function surface(el: Element): string {
  const copy = el.cloneNode(true) as Element
  copy.querySelectorAll('[role="tooltip"], [hidden]').forEach((t) => t.remove())
  return copy.textContent ?? ''
}

/**
 * The tooltip an (i) opens on focus, read the way a keyboard reaches it. It is
 * the button's description too, and none is drawn until it opens.
 */
function tipOf(button: HTMLElement): string {
  const host = button.closest('.c-tip-host') as HTMLElement
  expect(host.querySelector('[role="tooltip"]'), 'a tooltip is drawn while closed').toBeNull()
  const described = document.getElementById(button.getAttribute('aria-describedby')!)!.textContent
  act(() => button.focus())
  const tip = host.querySelector('[role="tooltip"]')
  expect(tip, 'the tooltip opens on focus').not.toBeNull()
  expect(tip!.textContent).toBe(described)
  act(() => button.blur())
  return described ?? ''
}

const waiting = (agents: number) => ({ kind: 'ok' as const, waiting: { agents, later: 0, atLeast: false } })

describe('the waiting agents: a button, not an arrow link', () => {
  it('draws the theme button with the same target and no arrow', () => {
    const { container } = render(<Verdict accounts={[acct('a')]} scope="eng" shortfall={0} now={Date.now()} waiting={waiting(2)} />)
    const go = screen.getByRole('link', { name: 'agents wait on the pool' })
    expect(go.getAttribute('href')).toBe(WAITING_HREF)
    expect(go.classList.contains('c-btn')).toBe(true)
    expect(container.textContent).not.toMatch(/→/)
  })

  it('keeps the button when the waiting list could not be read', () => {
    const { container } = render(
      <Verdict accounts={[acct('a')]} scope="eng" shortfall={0} now={Date.now()} waiting={{ kind: 'error', message: 'offline' }} />,
    )
    const go = screen.getByRole('link', { name: 'agents waiting on the pool' })
    expect(go.classList.contains('c-btn')).toBe(true)
    expect(container.textContent).not.toMatch(/→/)
  })
})

describe('the rule: an (i) beside "accounts can take a new agent"', () => {
  it('moves the floors line into the tooltip, every fact kept', () => {
    const { container } = render(<Verdict accounts={[acct('a')]} scope="eng" shortfall={0} now={Date.now()} waiting={waiting(0)} />)
    expect(surface(container)).not.toContain('Floors:')
    const tip = tipOf(screen.getByRole('button', { name: 'About which accounts can take a new agent' }))
    expect(tip).toContain('Floors: 5-hour 15% left')
    expect(tip).toContain('7-day 5% left')
    expect(tip).toContain('a reading older than 30 min counts at half')
    expect(tip).toContain('at most 6 agents per account')
    expect(tip).toContain('a new account takes one agent until its first reading')
  })
})

describe('one subscription twice: the head, its tooltip, and the must-stay line', () => {
  it('puts the why and the fix of a proven pair behind the head', () => {
    const extra = acct('team-copy', {
      can_take_new_agent: false,
      organisation: { name: 'Example', plan: 'Team' },
      blocked_by: [{ duplicate_of: 'eng:primary', reason: 'one subscription' }],
      duplicate_of: 'eng:primary',
    } as Partial<PoolAccount>)
    const { container } = render(<DuplicateBanner accounts={[extra, acct('primary')]} scope="eng" onOpen={() => {}} />)
    const box = container.querySelector('.acct-dup') as HTMLElement
    expect(surface(box)).not.toMatch(/To fix it|One claude\.ai login|hands out only/)
    const tip = tipOf(within(box).getByRole('button', { name: 'About team-copy and primary are one subscription' }))
    expect(tip).toContain('The pool hands out only primary, so team-copy adds no capacity.')
    expect(tip).toContain('One claude.ai login can hold two organisations')
    expect(tip).toContain('replace the credential')
    expect(tip).toContain('Or remove team-copy.')
    expect(within(box).getByRole('button', { name: 'Replace the credential' }).classList.contains('c-btn')).toBe(true)
  })

  it('keeps "lent to you" visible and puts the polls behind the head', () => {
    const shared = acct('shared', { owner_tenant: 'other', possible_duplicate: { of: 'x:hidden', reason: 'read identically' } } as Partial<PoolAccount>)
    const { container } = render(<DuplicateBanner accounts={[shared]} scope="eng" onOpen={() => {}} />)
    const box = container.querySelector('.acct-dup') as HTMLElement
    expect(surface(box)).toContain('shared is lent to you; other can fix it.')
    expect(surface(box)).not.toContain('read identically')
    const tip = tipOf(
      within(box).getByRole('button', { name: 'About shared may be a subscription another workspace registered' }),
    )
    expect(tip).toContain('They read identically on two polls. The pool still hands out both; if they are one, shared adds no capacity.')
  })
})

describe('the forecast: labels and ticks, no notes', () => {
  it('says "it does not go dark" as the Back tile’s tick', () => {
    const now = Date.parse('2026-10-24T09:04:00Z')
    const slow = acct('slow', { windows: { seven_day: { utilization: 0.05, resets_at: iso(now + 4 * D), reset: false } } }, now)
    const { container } = render(<Forecast accounts={[slow]} scope="eng" now={now} />)
    const back = [...container.querySelectorAll('.acct-fc-tile')].find((t) => t.querySelector('.acct-fc-k')?.textContent === 'Back')!
    expect(surface(back)).not.toContain('it does not go dark')
    // The tick alone is decoration (aria-hidden): its reason must reach a
    // screen reader and a keyboard through the (i) beside it.
    expect(back.querySelector('[data-tone="ok"]')).not.toBeNull()
    const about = within(back as HTMLElement).getByRole('button', { name: 'About Back' })
    expect(tipOf(about)).toBe('it does not go dark')
  })

  it('keeps "Left out" and "Laptop sessions" as labels and moves their reasons into tooltips', () => {
    const now = Date.parse('2026-10-24T09:04:00Z')
    const slow = acct('slow', { windows: { seven_day: { utilization: 0.36, resets_at: iso(now + 4 * D), reset: false } } }, now)
    const never = acct('never-polled', { windows: {} }, now)
    const { container } = render(<Forecast accounts={[slow, never]} scope="eng" now={now} />)
    const text = surface(container)
    expect(text).toContain('Left out')
    expect(text).toContain('never-polled')
    expect(text).not.toContain('stay open longer')
    expect(text).toContain('Laptop sessions spend the same windows')
    expect(text).not.toContain('go dark sooner')
    expect(tipOf(screen.getByRole('button', { name: 'About Left out' }))).toContain(
      'no 7-day reading to take a pace from, so the pool can stay open longer than this says',
    )
    expect(tipOf(screen.getByRole('button', { name: 'About Laptop sessions spend the same windows' }))).toContain(
      'they sign in to these subscriptions too, so the pool can go dark sooner than this says',
    )
  })
})

describe('the page: the sign-in tick and the ranking (i)', () => {
  it('ticks an account that needs a sign-in, with its reason, beside a real button', async () => {
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadAccountsBoard.mockResolvedValue(
      board([acct('retired-laptop', { state: 'REAUTH_REQUIRED', can_take_new_agent: false }), acct('ok')]),
    )
    render(<AccountsScreen />)
    const go = await screen.findByRole('button', { name: 'Sign in again to retired-laptop' }, WAIT)
    expect(go.classList.contains('c-btn')).toBe(true)
    const callout = go.closest('.acct-callout') as HTMLElement
    expect(surface(callout)).not.toContain('gives out nothing')
    const tick = callout.querySelector('[data-tone="warn"]') as HTMLElement
    expect(tick.textContent).toContain('retired-laptop')
    expect(tick.getAttribute('title')).toBe('needs someone to sign in again; it gives out nothing until then')
  })

  it('puts "Ranked by ..." behind an (i) beside Subscription accounts', async () => {
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadAccountsBoard.mockResolvedValue(board([acct('a'), acct('b')]))
    render(<AccountsScreen />)
    const list = await screen.findByRole('region', { name: 'Subscription accounts' }, WAIT)
    expect(surface(list)).not.toContain('Ranked by')
    expect(tipOf(within(list).getByRole('button', { name: 'About Subscription accounts' }))).toBe(
      'Ranked by when each can take a new agent. Times are UTC.',
    )
  })
})
