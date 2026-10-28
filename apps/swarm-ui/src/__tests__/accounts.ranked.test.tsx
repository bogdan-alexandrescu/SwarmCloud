// #2222, ACCOUNTS-A-LIST: CAPACITY › ACCOUNTS ANSWERS FIRST (proposal A).
//
// On 2026-10-24 the page read "7 accounts · 7 available" while 28 agents
// waited: six accounts were under their weekly floor and every 5-hour figure
// it showed with `~` belonged to a window that had already reset. The page now
// opens on "N of M accounts can take a new agent", the account that frees
// first and when, the agents waiting on the pool; it ranks the accounts Can
// take now -> Clears at a known time -> Frees when an agent finishes (BGD,
// "Known clock first"); a reset window reads "reset HH:MMZ, nothing read
// since"; and the forecast says when the pool goes dark at this pace.
//
// BREAK IT: rank full accounts before clocked ones, show a reset window's old
// figure, count a failed waiting read as 0, name another workspace's account
// in the duplicate banner, or put "available" back in the head -- a case below
// turns red.

import { render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { AccountsPage, Task } from '../types'
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
const pool = await import('../accountsPool')
const { duplicatePairs } = await import('../AccountsVerdict')
const tableModule = await import('../AccountsTable')

const WAIT = { timeout: 5000 } as const
const H = 3_600_000
const D = 24 * H

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

const iso = (t: number) => new Date(t).toISOString()

/** Under its 7-day floor until `resets` (#2222's table). */
function weekly(label: string, used: number, resets: number, now: number, over: Partial<PoolAccount> = {}): PoolAccount {
  return acct(
    label,
    {
      can_take_new_agent: false,
      blocked_by: [{ window: 'seven_day', used, floor: 0.05, resets_at: iso(resets) }],
      next_eligible_at: iso(resets),
      windows: {
        five_hour: { utilization: 0.4, resets_at: iso(now - 4 * H), reset: true },
        seven_day: { utilization: used, resets_at: iso(resets), reset: false },
      },
      ...over,
    },
    now,
  )
}

function board(accounts: PoolAccount[], shortfall = 0): Result<AccountsBoard> {
  const now = Date.now()
  return {
    status: 'ok',
    fetchedAt: now,
    serverAt: iso(now),
    data: {
      page: {
        accounts,
        tenant_id: 'eng',
        unreadable_documents: [],
        unreadable_document_count: shortfall,
      } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: now,
    },
  }
}

function parked(id: string, reason: string, summary: string, workflow: string | null): Task {
  return { task_id: id, state: 'PARKED', workflow_id: workflow, waiting: { reason, summary } } as unknown as Task
}

// 2026-10-24 09:04Z, the reading #2222 took (its F1, under the floors as deployed since).
const AT = Date.parse('2026-10-24T09:04:00Z')

describe('the ranking: Known clock first', () => {
  it('ranks Can take now, then a known clock soonest first, then full, then what a person holds', () => {
    const accounts = [
      acct('paused', { state: 'PAUSED', can_take_new_agent: false }, AT),
      acct('full', { assigned: 6, can_take_new_agent: false, blocked_by: [{ cap: 6, max_agents: 6 }] }, AT),
      weekly('week-late', 0.97, AT + 3 * D, AT),
      weekly('week-soon', 0.98, AT + D, AT),
      acct('two-free', { assigned: 4 }, AT),
      acct('six-free', {}, AT),
    ]
    const groups = pool.rankAccounts(accounts, 'eng')
    expect(groups.map((g) => [g.title, g.accounts.map((a) => a.label)])).toEqual([
      ['Can take now', ['six-free', 'two-free']],
      ['Clears at a known time', ['week-soon', 'week-late']],
      ['Frees when an agent finishes', ['full']],
      ['Waits for a person', ['paused']],
    ])
  })

  it('takes a tenant the account is unreadable for out of Can take now, though the broker says yes', () => {
    const a = acct('lent', { unreadable_now: ['eng'] }, AT)
    expect(pool.canTake(a, 'eng')).toBe(false)
    expect(pool.groupOf(a, 'eng')).toBe('person')
  })

  it('gives a never-measured account one slot until its first reading', () => {
    expect(pool.freeSlots(acct('new', { observed_at: null }, AT))).toBe(1)
    expect(pool.freeSlots(acct('old', {}, AT))).toBe(6)
  })
})

describe('the verdict', () => {
  it('names the account that frees first, and when, when none can take one', () => {
    const accounts = [
      acct(
        'aieng-claude1',
        {
          can_take_new_agent: false,
          blocked_by: [{ window: 'five_hour', used: 0.99, floor: 0.15, resets_at: '2026-10-24T10:10:00Z' }],
          next_eligible_at: '2026-10-24T10:10:00Z',
        },
        AT,
      ),
      weekly('ai-eng-3', 0.97, Date.parse('2026-10-27T10:59:00Z'), AT),
      weekly('home-personal', 0.98, Date.parse('2026-10-26T07:59:00Z'), AT),
    ]
    const v = pool.verdictOf(accounts, 'eng', 0, AT)
    expect(v.n).toBe(0)
    expect(v.next?.account.label).toBe('aieng-claude1')
    expect(pool.utcAt(v.next!.at, AT)).toBe('10:10Z')
    expect(v.next?.window).toBe('five_hour')
    expect(v.then?.account.label).toBe('home-personal')
    expect(pool.utcAt(v.then!.at, AT)).toBe('Oct 26 07:59Z')
    expect(v.underBy).toEqual([
      { window: 'seven_day', count: 2 },
      { window: 'five_hour', count: 1 },
    ])
  })

  it('counts free slots over the accounts that can take one', () => {
    const v = pool.verdictOf([acct('a', {}, AT), acct('b', { assigned: 4 }, AT), weekly('c', 0.99, AT + D, AT)], 'eng', 0, AT)
    expect([v.n, v.m, v.free]).toEqual([2, 3, 8])
  })

  it('renders "N of M" with the sentence, and the head no longer says available', async () => {
    const now = Date.now()
    api.loadTasksInState.mockResolvedValue({
      status: 'ok',
      fetchedAt: now,
      data: {
        tasks: [
          parked('t1', 'PROVIDER_QUOTA_EXHAUSTED', 'No pool account can take an agent on anthropic: every account is under its floor', 'wf_1'),
          parked('t2', 'PROVIDER_QUOTA_EXHAUSTED', 'No pool account can take an agent on anthropic: every account is under its floor', null),
          parked('t3', 'DEPENDENCY_INCOMPLETE', 'Waiting on 1 upstream steps', 'wf_1'),
          parked('t4', 'DEPENDENCY_INCOMPLETE', 'Waiting on 1 upstream steps', 'wf_other'),
          parked('t5', 'PROVIDER_QUOTA_EXHAUSTED', 'Provider quota exhausted on openai', null),
        ],
      },
    })
    api.loadAccountsBoard.mockResolvedValue(board([weekly('ai-eng-2', 0.86, now + 34 * H, now), weekly('team', 0.98, now + 47 * H, now)]))
    render(<AccountsScreen />)
    const verdict = await screen.findByRole('region', { name: 'Can the pool take a new agent' }, WAIT)
    expect(verdict.querySelector('.acct-verdict-big')?.textContent).toBe('0 of 2')
    expect(verdict.textContent).toContain('No account can take a new agent. Next: ai-eng-2 at')
    expect(verdict.textContent).toContain('2 of the 2 are under the floor on their 7-day window.')
    await waitFor(() => expect(verdict.querySelector('.acct-verdict-wn')?.textContent).toBe('2'), WAIT)
    expect(verdict.textContent).toContain('and 1 later step waits on them')
    expect(verdict.querySelector('a')?.getAttribute('href')).toBe('#work/running/waiting')
    expect(document.body.textContent).not.toMatch(/\bavailable\b/)
    expect(screen.getByText(/Floors:/).textContent).toContain('7-day 5% left')
  })

  it('says a failed waiting read as a dash with its reason, never 0', async () => {
    api.loadTasksInState.mockResolvedValue({ status: 'error', error: { kind: 'network', message: 'offline' } })
    api.loadAccountsBoard.mockResolvedValue(board([acct('a')]))
    render(<AccountsScreen />)
    const verdict = await screen.findByRole('region', { name: 'Can the pool take a new agent' }, WAIT)
    await waitFor(() => expect(verdict.textContent).toContain('could not read the waiting list'), WAIT)
    expect(verdict.querySelector('.acct-verdict-wn')?.textContent).not.toMatch(/0/)
  })

  it('says the empty pool first (F5)', async () => {
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadAccountsBoard.mockResolvedValue(board([]))
    render(<AccountsScreen />)
    const verdict = await screen.findByRole('region', { name: 'Can the pool take a new agent' }, WAIT)
    expect(verdict.textContent).toContain('No account is registered, so no agent that needs the pool can start.')
    expect(verdict.querySelector('.acct-verdict-big')?.textContent).toBe('0 accounts')
  })

  it('says which organisation to sign in to once on the empty pool, not once per merged copy', async () => {
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadAccountsBoard.mockResolvedValue(board([]))
    render(<AccountsScreen />)
    const panel = await waitFor(() => {
      const el = document.querySelector('.acct-empty')
      expect(el).not.toBeNull()
      return el as HTMLElement
    }, WAIT)
    const says = (panel.textContent ?? '').split('Sign in to the organisation you mean').length - 1
    expect(says).toBe(1)
  })

  it('never says no account is registered when a document could not be read', async () => {
    // Rendered 2026-10-25: the head said "1 document unreadable, so this list
    // is short" while the verdict said "No account is registered".
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadAccountsBoard.mockResolvedValue(board([], 1))
    render(<AccountsScreen />)
    const verdict = await screen.findByRole('region', { name: 'Can the pool take a new agent' }, WAIT)
    expect(verdict.textContent).not.toContain('No account is registered')
    expect(verdict.querySelector('.acct-verdict-big')?.textContent).toBe('0 of at least 1')
    expect(verdict.querySelector('.acct-verdict-head')?.textContent).toBe(
      'No account here could be read, so no agent that needs the pool can start.',
    )
    // The empty panel under it says the same, never the zero's "No accounts registered".
    const panel = document.querySelector('.acct-empty') as HTMLElement
    // The heading carries its `?` card after the words.
    expect(panel.querySelector('h3')?.firstChild?.textContent).toBe('No account could be read')
    expect(panel.textContent).not.toContain('No accounts registered')
    expect(panel.textContent).toContain('1 registered account could not be read')
    expect(panel.querySelector('.ctl-mark.is-zero')).toBeNull()
  })
})

describe('the table', () => {
  it('reads a reset window as reset, never its old figure, and draws each floor as a tick', async () => {
    const now = Date.now()
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: now })
    const reset = now - 4 * H
    api.loadAccountsBoard.mockResolvedValue(board([weekly('devops-team', 0.86, now + 4 * D, now)]))
    render(<AccountsScreen />)
    const row = (await screen.findByRole('button', { name: 'devops-team' }, WAIT)).closest('tr') as HTMLElement
    const five = row.querySelectorAll('.acct-fig')[0] as HTMLElement
    expect(five.textContent).toContain(`reset ${pool.utcAt(reset, now)}`)
    expect(five.textContent).toContain('nothing read since')
    expect(five.textContent).not.toMatch(/40%|60%|~/)
    const seven = row.querySelectorAll('.acct-fig')[1] as HTMLElement
    expect(seven.textContent).toContain('14% left')
    expect((seven.querySelector('.acct-floor > s') as HTMLElement).style.left).toBe('95%')
    expect(row.querySelector('.acct-rk-why')?.textContent).toBe('7-day window: 14% left, under its 5% floor')
    expect(row.textContent).toContain('0 of 6')
    expect(row.textContent).toContain('organisation not recorded')
  })

  it('says an aged reading counts at half, and clears at the next reading', () => {
    const now = AT
    const a = acct('ai-eng-2', {
      stale: true,
      observed_at: iso(now - 52 * 60_000),
      can_take_new_agent: false,
      blocked_by: [{ window: 'seven_day', used: 0.91, floor: 0.05, resets_at: iso(now + D), aged_reading: true }],
      next_eligible_at: iso(now + D),
      windows: { seven_day: { utilization: 0.91, resets_at: iso(now + D), reset: false } },
    })
    const { clearsOf, whyOf } = tableModule
    const why = render(<p>{whyOf(a, 'eng', now)}</p>).container.textContent
    expect(why).toBe('An aged reading: 7-day 9% left, read 52m ago, counts at half (4.5%), under its 5% floor')
    expect(clearsOf(a, 'eng', now).head).toBe('at the next reading')
  })
})

describe('one subscription twice', () => {
  it('names the pair inside this workspace, and never another workspace’s account', () => {
    const extra = acct('team', {
      can_take_new_agent: false,
      blocked_by: [{ duplicate_of: 'eng:home-personal', reason: 'registered on the same organisation as eng:home-personal' }],
      duplicate_of: 'eng:home-personal',
    })
    const older = acct('home-personal', { duplicate_of: 'eng:team' })
    const pairs = duplicatePairs([extra, older])
    expect(pairs.map((p) => [p.extra.label, p.other?.label])).toEqual([['team', 'home-personal']])

    const foreign = acct('lone', {
      can_take_new_agent: false,
      blocked_by: [{ duplicate_of: null, reason: 'this subscription is already registered in another workspace' }],
      duplicate_of: null,
    })
    expect(duplicatePairs([foreign]).map((p) => p.other)).toEqual([null])
  })

  it('draws the banner with what to do', async () => {
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const foreign = acct('lone', {
      can_take_new_agent: false,
      blocked_by: [{ duplicate_of: null, reason: 'this subscription is already registered in another workspace' }],
      duplicate_reason: 'this subscription is already registered in another workspace',
    })
    api.loadAccountsBoard.mockResolvedValue(board([foreign, acct('other')]))
    render(<AccountsScreen />)
    // The head as read: the name is its own `acct-nm` span, so match the whole head.
    const banner = await screen.findByText(
      (_, el) =>
        el?.classList.contains('acct-dup-head') === true &&
        el.textContent === 'lone is a subscription another workspace already registered',
      undefined,
      WAIT,
    )
    const box = banner.closest('.acct-dup') as HTMLElement
    expect(box.textContent).toContain('To fix it:')
    expect(screen.getByRole('button', { name: 'Open lone' })).toBeTruthy()
    // Mock-up A's second action: the fix the banner names, one click away.
    expect(within(box).getByRole('button', { name: 'Replace the credential' })).toBeTruthy()
  })
})

describe('the forecast: from today’s readings', () => {
  it('takes each subscription’s pace over the hours its week has run, and finds when the pool goes dark and comes back', () => {
    const now = AT
    // Week opened 72h ago (resets in 4 days): 36% used is 0.5% an hour; 59% above its 5% floor lasts 118h.
    const slow = acct('slow', { windows: { seven_day: { utilization: 0.36, resets_at: iso(now + 4 * D), reset: false } } }, now)
    // Week opened 120h ago (resets in 2 days): 90% used is 0.75% an hour; 5% above the floor lasts 6h40m.
    const fast = acct('fast', { windows: { seven_day: { utilization: 0.9, resets_at: iso(now + 2 * D), reset: false } } }, now)
    const f = pool.forecastOf([slow, fast], 'eng', now)
    expect(f.paces.map((p) => Math.round(p.perHour * 10000) / 100)).toEqual([0.5, 0.75])
    // fast closes at +6h40m and reopens at +48h; slow closes at +118h, so the pool is never dark this week.
    expect(f.darkAt).toBeNull()

    const spent = weekly('spent', 0.97, now + D, now)
    const g = pool.forecastOf([fast, spent], 'eng', now)
    // spent is under its floor until +24h; fast closes at +6h40m: dark then, back when spent's week resets.
    expect(g.darkAt).toBe(now + (0.05 / 0.0075) * H)
    expect(g.lastOpen?.label).toBe('fast')
    expect(g.backAt).toBe(now + D)
    expect(g.back?.label).toBe('spent')
  })

  it('counts a withheld duplicate once, and says no pace for an unmeasured account', () => {
    const now = AT
    const dup = acct('copy', {
      can_take_new_agent: false,
      blocked_by: [{ duplicate_of: 'eng:a', reason: 'x' }],
      windows: { seven_day: { utilization: 0.5, resets_at: iso(now + D), reset: false } },
    })
    const a = acct('a', { windows: { seven_day: { utilization: 0.5, resets_at: iso(now + D), reset: false } } })
    expect(pool.forecastOf([a, dup], 'eng', now).paces.map((p) => p.account.label)).toEqual(['a'])
    const f = pool.forecastOf([a, acct('new', { observed_at: null })], 'eng', now)
    expect(f.unmeasured.map((x) => x.label)).toEqual(['new'])
    expect(f.darkAt).toBeNull()
  })
})

describe('who waits on the pool', () => {
  it('counts the pool’s parks, not a provider’s own refusal, and the later steps of their workflows', () => {
    const w = pool.poolWaiting(
      [
        parked('a', 'PROVIDER_QUOTA_EXHAUSTED', 'No pool account can take an agent on anthropic: full', 'wf'),
        parked('b', 'PROVIDER_QUOTA_EXHAUSTED', 'Provider quota exhausted on anthropic (account eng:x)', 'wf2'),
        parked('c', 'DEPENDENCY_INCOMPLETE', 'Waiting on 1 upstream steps', 'wf'),
        parked('d', 'DEPENDENCY_INCOMPLETE', 'Waiting on 1 upstream steps', 'wf2'),
      ],
      true,
    )
    expect(w).toEqual({ agents: 1, later: 1, atLeast: true })
  })
})
