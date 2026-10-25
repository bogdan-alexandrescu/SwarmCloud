// CAPACITY › SUBSCRIPTION ACCOUNTS, ONE ACCOUNT'S HEALTH (#2302,
// ACCOUNT-HEALTH-SHEET; docs/web-ui/mockups/account-health-a.html F2 and F4,
// account-health-d.html P2 at 390).
//
// What this file pins, in the account sheet's Health section:
//   * F2 (A): the served counts by kind for 1 h, 24 h and 7 d, grouped by
//     family; "Per hour, 7 days" with the limited stretches shaded; the last
//     50 events with time, kind, window, the redacted message, tenant and a
//     task link;
//   * F4 (A): no events (a measured zero in words, no chart); not served (a
//     404 is a dash and its reason, and no 0 anywhere); re-auth needed (the fix
//     first); an unreadable read (a dash, what went wrong, Try again);
//   * ONE read when the sheet opens, none while it stays open (the page's poll
//     re-rendering it), one more per Refresh; nothing polls;
//   * at 390 (BGD, D's P2): the three counts stacked, the series, then the
//     incidents, the open one expanded and the closed ones folded.
//
// BREAK IT: key the read's effect on the account object rather than its id --
// the poll case reads twice. Draw `0` for a span the record did not serve --
// the not-served case finds a zero. Drop the realzero branch -- the empty
// case finds a chart. Expand every incident on the phone -- the folded case
// finds the closed one's events.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountHealth, AccountHealthCounts, AccountHealthEvent, AccountHealthKind, AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage } from '../types'
import type { PoolAccount } from '../accountsPool'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  loadAccountHealth: vi.fn(),
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
const { SheetHealth } = await import('../AccountSheet')

const WAIT = { timeout: 5000 } as const
const MIN = 60_000
const HOUR = 60 * MIN

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

const KINDS: AccountHealthKind[] = [
  'rate_limit',
  'exhausted',
  'auth',
  'overloaded',
  'server_error',
  'network',
  'assign_refused',
  'reauth',
  'refresh_failed',
]

function counts(over: Partial<AccountHealthCounts> = {}): AccountHealthCounts {
  const c = {} as AccountHealthCounts
  for (const k of KINDS) c[k] = 0
  return { ...c, ...over }
}

const ago = (m: number) => new Date(Date.now() - m * MIN).toISOString()
const taskId = () => `task_${'a1b2c3d4e5'.repeat(2)}`

function ev(minutesAgo: number, kind: AccountHealthKind, over: Partial<AccountHealthEvent> = {}): AccountHealthEvent {
  return {
    at: ago(minutesAgo),
    kind,
    tenant_id: 'eng',
    task_id: taskId(),
    window: null,
    reset_at: null,
    message: `${kind} in this test`,
    own: true,
    ...over,
  }
}

/** aieng-claude1 in mock-up A's F2: a 5-hour limit now, and a 529 pair two days ago. */
function limited(): AccountHealth {
  const reset = ago(-120)
  const events = [
    ev(20, 'assign_refused', { window: 'five_hour', message: 'not assigned: window under its floor' }),
    ev(50, 'exhausted', { message: 'PROVIDER_QUOTA_EXHAUSTED · parked at checkpoint 4, capacity released' }),
    ev(60, 'rate_limit', { window: 'five_hour', reset_at: reset, message: 'rate_limit_event status=rejected' }),
    ev(2 * 24 * 60, 'overloaded', { message: '529 overloaded_error: Overloaded' }),
    ev(2 * 24 * 60 + 5, 'overloaded', { message: '529 overloaded_error: Overloaded' }),
  ]
  // Hours back from the hour that holds now: four distinct bars whatever the minute.
  const hour = (h: number) => {
    const t = Date.now()
    return new Date(t - (t % HOUR) - h * HOUR).toISOString()
  }
  return {
    account_id: 'eng:aieng-claude1',
    status: 'limited',
    limited_until: reset,
    counts: {
      last_1h: counts({ rate_limit: 1, exhausted: 1, assign_refused: 1 }),
      last_24h: counts({ rate_limit: 1, exhausted: 1, assign_refused: 1 }),
      // 7 d counts more than the events list holds: a hit older than the last 50.
      last_7d: counts({ rate_limit: 2, exhausted: 1, assign_refused: 1, overloaded: 2 }),
    },
    hourly: [
      { hour: hour(1), counts: { rate_limit: 1, exhausted: 1 }, total: 2 },
      { hour: hour(0), counts: { assign_refused: 1 }, total: 1 },
      { hour: hour(48), counts: { overloaded: 2 }, total: 2 },
      { hour: hour(120), counts: { rate_limit: 1 }, total: 1 },
    ],
    events,
    observed_at: new Date().toISOString(),
  }
}

function emptyRecord(): AccountHealth {
  return {
    account_id: 'eng:devops-team',
    status: 'healthy',
    limited_until: null,
    counts: { last_1h: counts(), last_24h: counts(), last_7d: counts() },
    hourly: [],
    events: [],
    observed_at: new Date().toISOString(),
  }
}

function account(over: Partial<PoolAccount> = {}): PoolAccount {
  const now = Date.now()
  return {
    account_id: 'eng:aieng-claude1',
    owner_tenant: 'eng',
    label: 'aieng-claude1',
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    observed_at: new Date(now - 5 * MIN).toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: new Date(now - 26 * MIN).toISOString(),
    windows: {
      five_hour: { utilization: 0.98, resets_at: new Date(now + 2 * HOUR).toISOString(), reset: false },
      seven_day: { utilization: 0.59, resets_at: new Date(now + 3 * 24 * HOUR).toISOString(), reset: false },
    },
    max_agents: 6,
    can_take_new_agent: false,
    blocked_by: [],
    next_eligible_at: null,
    organisation: { name: 'Acme', plan: 'Team' },
    ...over,
  }
}

function viewport(width: number): void {
  vi.stubGlobal('matchMedia', (query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query)
    const min = /min-width:\s*(\d+)px/.exec(query)
    const matches = (max === null || width <= Number(max[1])) && (min === null || width >= Number(min[1]))
    return { matches, media: query, onchange: null, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false }
  })
}

async function section(): Promise<HTMLElement> {
  const s = await screen.findByRole('region', { name: 'Health' }, WAIT)
  await waitFor(() => expect(s.getAttribute('aria-busy')).toBeNull(), WAIT)
  return screen.getByRole('region', { name: 'Health' })
}

beforeEach(() => {
  api.loadAccountHealth.mockReset()
  reads.read.mockReset()
  reads.read.mockResolvedValue(ok({ account_id: 'eng:aieng-claude1', viewer: 'owner', total: 0, holders: [], others: 0 }))
  viewport(1440)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('F2 (A): one account’s health', () => {
  it('draws the served counts by kind for 1 h, 24 h and 7 d, grouped by family', async () => {
    api.loadAccountHealth.mockResolvedValue(ok(limited()))
    render(<SheetHealth a={account()} owned />)
    const s = await section()
    expect(within(s).getByText(/^Limited · 5-hour until /)).toBeTruthy()
    expect(s.textContent).toContain('Its 5-hour window refused at')
    const table = within(s).getByRole('table', { name: 'Events by kind, last hour, day and week' })
    const row = (name: string) =>
      within(table)
        .getByRole('rowheader', { name })
        .closest('tr')!
        .querySelectorAll('td')
    const cells = (name: string) => [...row(name)].map((td) => td.textContent)
    expect(cells('Rate-limit hit')).toEqual(['1', '1', '2'])
    expect(cells('Limits')).toEqual(['3', '3', '4'])
    expect(cells('Overloaded (529)')).toEqual(['0', '0', '2'])
    expect(cells('Provider errors')).toEqual(['0', '0', '2'])
    expect(cells('All kinds')).toEqual(['3', '3', '6'])
    // A kind with none in 7 days folds into the caption rather than a row of zeros.
    expect(within(table).queryByRole('rowheader', { name: 'Network error' })).toBeNull()
    expect(s.textContent).toContain('None in 7 days, so not drawn as rows: server error (5xx), network error,')
  })

  it('draws "Per hour, 7 days" with the limited stretch shaded, and the events with their message, tenant and task', async () => {
    api.loadAccountHealth.mockResolvedValue(ok(limited()))
    render(<SheetHealth a={account()} owned />)
    const s = await section()
    expect(within(s).getByText('Per hour, 7 days')).toBeTruthy()
    const chart = within(s).getByRole('img', { name: /^Events per hour over 7 days: 6 events, at most 2 in one hour; 1 limited stretch shaded$/ })
    expect(chart.querySelectorAll('.acct-hs-band').length).toBe(1)
    expect(chart.querySelector('.acct-hs-band')!.getAttribute('data-window')).toBe('five_hour')
    expect(chart.querySelectorAll('.acct-hs-bar').length).toBe(4)
    expect(s.textContent).toContain('limited: from the hit to the window’s reset')

    const list = within(s).getByRole('list', { name: 'Events, newest first' })
    const items = within(list).getAllByRole('listitem')
    expect(items.length).toBe(5)
    expect(items[0]!.textContent).toContain('Refused at assignment')
    expect(items[0]!.textContent).toContain('not assigned: window under its floor')
    expect(items[2]!.textContent).toContain('Rate-limit hit')
    expect(items[2]!.textContent).toContain('5-hour · resets')
    expect(items[2]!.textContent).toContain('tenant eng')
    const task = within(items[2]!).getByRole('link')
    expect(task.getAttribute('href')).toBe(`/agents/waiting/${taskId()}`)
    // The 7 d column counts one more event than the list holds: the note says which.
    expect(s.textContent).toContain('drawn here are 5 of the 6 in 7 days')
  })

  it('says another workspace’s event without its tenant, task or message', async () => {
    const h = limited()
    h.events = [ev(10, 'network', { tenant_id: null, task_id: null, message: null, own: false })]
    api.loadAccountHealth.mockResolvedValue(ok(h))
    render(<SheetHealth a={account()} owned={false} />)
    const s = await section()
    const item = within(within(s).getByRole('list', { name: 'Events, newest first' })).getByRole('listitem')
    expect(item.textContent).toContain('Another workspace’s event')
    expect(within(item).queryByRole('link')).toBeNull()
  })
})

describe('F4 (A): empty and honest states', () => {
  it('says a read and empty record once, in words, with no chart', async () => {
    api.loadAccountHealth.mockResolvedValue(ok(emptyRecord()))
    render(<SheetHealth a={account({ account_id: 'eng:devops-team' })} owned />)
    const s = await section()
    expect(s.textContent).toContain('No events in 7 days: no limits, errors or credential trouble.')
    expect(s.textContent).toContain('That is a measured zero, so no chart is drawn.')
    expect(within(s).queryByRole('img')).toBeNull()
    expect(within(s).queryByRole('table')).toBeNull()
  })

  it('draws a 404 as the record not served: a dash with its reason, never a zero', async () => {
    api.loadAccountHealth.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: null, message: 'Not Found' },
    })
    render(<SheetHealth a={account()} owned />)
    const s = await section()
    expect(s.textContent).toContain('— Not served')
    expect(s.textContent).toContain('GET /v1/accounts/eng:aieng-claude1/health answered 404')
    expect(s.textContent).toContain('are unknown here, not zero')
    expect(s.textContent).not.toMatch(/\b0\b/)
    expect(within(s).queryByRole('img')).toBeNull()
  })

  it('draws a span the record did not serve as a dash, not a 0', async () => {
    const h = limited()
    h.counts.last_1h = null
    api.loadAccountHealth.mockResolvedValue(ok(h))
    render(<SheetHealth a={account()} owned />)
    const s = await section()
    const row = within(s).getByRole('rowheader', { name: 'Rate-limit hit' }).closest('tr')!
    expect([...row.querySelectorAll('td')].map((td) => td.textContent)).toEqual(['—', '1', '2'])
  })

  it('leads a re-auth with the fix, for the owner and for a borrower', async () => {
    const h: AccountHealth = {
      ...emptyRecord(),
      account_id: 'eng:ai-eng-3',
      status: 'reauth_needed',
      counts: { last_1h: counts({ assign_refused: 2, reauth: 1, auth: 1, refresh_failed: 2 }), last_24h: counts({ assign_refused: 2, reauth: 1, auth: 1, refresh_failed: 2 }), last_7d: counts({ assign_refused: 2, reauth: 1, auth: 1, refresh_failed: 2 }) },
      events: [ev(10, 'assign_refused'), ev(30, 'reauth'), ev(31, 'auth'), ev(40, 'refresh_failed'), ev(50, 'refresh_failed')],
    }
    api.loadAccountHealth.mockResolvedValue(ok(h))
    const { unmount } = render(<SheetHealth a={account({ account_id: 'eng:ai-eng-3', state: 'REAUTH_REQUIRED' })} owned />)
    let s = await section()
    expect(within(s).getByText('Needs re-auth', { selector: '.acct-hs-st' })).toBeTruthy()
    const callout = within(s).getByRole('note')
    expect(callout.textContent).toContain('The broker assigns it nothing until someone signs in again')
    expect(callout.textContent).toContain('Sign in again is below')
    // The fix comes before the counts.
    expect(callout.compareDocumentPosition(within(s).getByRole('table')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    unmount()
    render(<SheetHealth a={account({ account_id: 'eng:ai-eng-3', owner_tenant: 'ops', state: 'REAUTH_REQUIRED' })} owned={false} />)
    s = await section()
    expect(within(s).getByRole('note').textContent).toContain('Its owner, ops, signs in again.')
  })

  it('says an unreadable read as unknown, with what went wrong and Try again', async () => {
    api.loadAccountHealth.mockResolvedValueOnce({
      status: 'error',
      error: { kind: 'server_error', httpStatus: 503, code: null, message: 'the broker did not answer' },
    })
    render(<SheetHealth a={account()} owned />)
    const s = await section()
    expect(s.querySelector('.acct-hs-unread')!.textContent).toContain('The health record could not be read: the broker did not answer.')
    expect(s.textContent).toContain('unknown until a read succeeds, not zero')
    expect(s.textContent).not.toMatch(/\b0\b/)
    api.loadAccountHealth.mockResolvedValueOnce(ok(limited()))
    fireEvent.click(within(s).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(within(screen.getByRole('region', { name: 'Health' })).queryByRole('table')).toBeTruthy(), WAIT)
    expect(api.loadAccountHealth).toHaveBeenCalledTimes(2)
  })
})

describe('one read when the sheet opens, none while it stays open', () => {
  it('reads once on open, not again when the page’s poll re-renders the sheet, and once per Refresh', async () => {
    api.loadAccountHealth.mockResolvedValue(ok(limited()))
    const { rerender } = render(<SheetHealth a={account()} owned />)
    await section()
    expect(api.loadAccountHealth).toHaveBeenCalledTimes(1)
    expect(api.loadAccountHealth).toHaveBeenCalledWith('eng:aieng-claude1')
    // The poll hands the sheet a new object for the same account, twice.
    rerender(<SheetHealth a={account({ assigned: 1 })} owned />)
    rerender(<SheetHealth a={account({ assigned: 2 })} owned />)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })
    expect(api.loadAccountHealth).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(api.loadAccountHealth).toHaveBeenCalledTimes(2), WAIT)
  })

  it('is in the opened account’s sheet, read once for that account', async () => {
    api.loadAccountHealth.mockResolvedValue(ok(limited()))
    api.loadAccountsBoard.mockResolvedValue(
      ok({
        page: { accounts: [account()] as Account[], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
        tenants: [],
        tenantsDetail: null,
        readAt: Date.now(),
      } as AccountsBoard),
    )
    render(<AccountsScreen />)
    await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
    expect(api.loadAccountHealth).not.toHaveBeenCalled()
    fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open[data-account="eng:aieng-claude1"]')!)
    const sheet = document.querySelector<HTMLElement>('.acct-sheet')!
    const s = await within(sheet).findByRole('region', { name: 'Health' }, WAIT)
    await waitFor(() => expect(within(s).queryByRole('table')).toBeTruthy(), WAIT)
    expect(api.loadAccountHealth).toHaveBeenCalledTimes(1)
    expect(api.loadAccountHealth).toHaveBeenCalledWith('eng:aieng-claude1')
  })
})

describe('at 390 (BGD, D’s P2)', () => {
  it('stacks the three counts, then the series, then the incidents: the open one expanded, the closed ones folded', async () => {
    viewport(390)
    api.loadAccountHealth.mockResolvedValue(ok(limited()))
    render(<SheetHealth a={account()} owned />)
    const s = await section()
    const stats = [...s.querySelectorAll('.acct-hs-stat')].map((x) => x.textContent)
    expect(stats).toEqual([
      'Last hour31 rate-limit hit · 1 exhausted report · 1 refusal at assignment',
      'Last 24 h31 rate-limit hit · 1 exhausted report · 1 refusal at assignment',
      'Last 7 days62 overloaded (529) · 2 rate-limit hits · 1 exhausted report · 1 refusal at assignment',
    ])
    expect(within(s).queryByRole('table')).toBeNull()
    const chart = within(s).getByRole('img', { name: /^Events per hour over 7 days/ })
    const cards = [...s.querySelectorAll('article.acct-hs-inc')] as HTMLElement[]
    expect(cards.map((c) => [c.querySelector('h5')!.textContent, c.classList.contains('is-open')])).toEqual([
      ['Rate-limited on its 5-hour window', true],
      ['Erroring: 2 provider errors', false],
    ])
    // The order: counts, series, incidents.
    expect(s.querySelector('.acct-hs-stats')!.compareDocumentPosition(chart) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(chart.compareDocumentPosition(cards[0]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // Open: every event. Closed: its latest message and a button for the rest.
    expect(within(cards[0]!).getAllByRole('listitem').length).toBe(3)
    expect(within(cards[1]!).queryAllByRole('listitem').length).toBe(0)
    expect(cards[1]!.querySelector('.acct-hs-msg')!.textContent).toBe('529 overloaded_error: Overloaded')
    fireEvent.click(within(cards[1]!).getByRole('button', { name: '2 events ▾' }))
    expect(within(cards[1]!).getAllByRole('listitem').length).toBe(2)
  })
})
