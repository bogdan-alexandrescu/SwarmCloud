// #2222, ACCOUNTS-TIMELINE: CAPACITY › ACCOUNTS GETS A TABLE / TIMELINE TOGGLE.
//
// BGD, 2026-10-24 17:33Z: proposal A, with a toggle beside the verdict whose
// Timeline draws every account the way proposal B does -- one lane per account
// on a shared axis, each window a band against its floor, the next free slot
// through every lane with the waiting agents queued against it, the pool row
// counting how many can take an agent over time. Table by default; the last
// choice remembered per browser and in the address (`?view=timeline`).
//
// BREAK IT: open on Timeline with nothing chosen, forget the choice, drop
// `view` from the router's kept keys, count a past clock as open now (the
// pool row then disagrees with the verdict), take the pace from anywhere but
// `forecastOf`, queue more agents than the free slots, name another
// workspace's account on a duplicate lane, or open a different detail from a
// lane -- a case below turns red.
//
// ACCOUNTS-TIMELINE-MERGE (BGD, 2026-10-25 10:05Z): a lane opens the Table's
// side sheet (the whole screen on a phone), not a detail under the lane; a
// held lane draws its latest hold deadline as the broker serves it, and a dash
// with its reason when it does not; the kept half of a duplicate pair is
// marked on its lane; a Timeline restored from the browser's memory writes
// `?view=timeline` to the address. BREAK IT: draw the old `.acct-tl-detail`,
// drop `latest_hold_deadline`, mark only the withheld half, or leave a
// restored address bare -- a case below turns red.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'

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
const lanes = await import('../accountsLanes')
const { canonical, fromHash } = await import('../App')

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
    observed_at: new Date(now - 3 * 60_000).toISOString(),
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

const win = (used: number, resets: number, reset = false) => ({ utilization: used, resets_at: iso(resets), reset })

/** Under one window's floor until `resets`. */
function under(label: string, window: 'five_hour' | 'seven_day', used: number, resets: number, now: number, over: Partial<PoolAccount> = {}) {
  return acct(
    label,
    {
      can_take_new_agent: false,
      blocked_by: [{ window, used, floor: window === 'five_hour' ? 0.15 : 0.05, resets_at: iso(resets) }],
      next_eligible_at: iso(resets),
      windows: { [window]: win(used, resets), ...(window === 'five_hour' ? { seven_day: win(0.5, now + 5 * D) } : {}) },
      ...over,
    },
    now,
  )
}

/** Proposal B's frame B3, #2222's 14:04Z measurement: 1 of 7, then 15:00Z, 15:10Z, Mon, Tue. */
function b3(now: number): PoolAccount[] {
  // #2222's instants, moved with `now` so a render test reads the same pool on any day.
  const T = (x: string) => Date.parse(x) - AT + now
  return [
    acct('ai-eng-2', { assigned: 6, can_take_new_agent: false, blocked_by: [{ cap: 6, max_agents: 6 }], windows: { seven_day: win(0.9, now + 29 * H) } }, now),
    under('ai-eng-3', 'seven_day', 0.97, T('2026-10-27T10:59:00Z'), now),
    under('aieng-claude1', 'five_hour', 0.91, T('2026-10-24T15:10:00Z'), now),
    acct('devops-main', { assigned: 6, can_take_new_agent: false, blocked_by: [{ cap: 6, max_agents: 6 }], windows: { seven_day: win(0.92, now + 2 * D) } }, now),
    acct('devops-team', { assigned: 5, windows: { seven_day: win(0.89, T('2026-10-28T12:00:00Z')) } }, now),
    under('example-personal', 'seven_day', 0.98, T('2026-10-26T07:59:00Z'), now),
    under('example-team', 'five_hour', 0.95, T('2026-10-24T15:00:00Z'), now),
  ]
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

function parked(n: number): Result<{ tasks: Task[] }> {
  const tasks = Array.from(
    { length: n },
    (_, i) =>
      ({
        task_id: `t${i}`,
        state: 'PARKED',
        workflow_id: `wf_${i}`,
        waiting: { reason: 'PROVIDER_QUOTA_EXHAUSTED', summary: 'No pool account can take an agent on anthropic' },
      }) as unknown as Task,
  )
  return { status: 'ok', fetchedAt: Date.now(), data: { tasks } } as unknown as Result<{ tasks: Task[] }>
}

const AT = Date.parse('2026-10-24T14:04:00Z')

beforeEach(() => {
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
})
afterEach(() => {
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// The pure rules
// ---------------------------------------------------------------------------

describe('which view the page opens on', () => {
  it('opens on Table, then the browser’s last choice, and the address beats both', () => {
    expect(lanes.viewOf(null, null)).toBe('table')
    expect(lanes.viewOf(null, 'timeline')).toBe('timeline')
    expect(lanes.viewOf('table', 'timeline')).toBe('table')
    expect(lanes.viewOf('timeline', null)).toBe('timeline')
    // A value nobody wrote is not a view.
    expect(lanes.viewOf('graph', 'cards')).toBe('table')
  })

  it('keeps ?view= on the accounts address through the router, and drops it elsewhere', () => {
    window.location.hash = '#capacity/accounts?account=eng%3Ab&view=timeline'
    expect(canonical(fromHash())).toBe('capacity/accounts?account=eng%3Ab&view=timeline')
    window.location.hash = '#capacity/pools?view=timeline'
    expect(canonical(fromHash())).toBe('capacity/pools')
    window.location.hash = ''
  })
})

describe('the shared axis', () => {
  it('gives the next 12 hours the split, and the week the rest', () => {
    expect(lanes.xOf(AT, AT, 'split')).toBe(0)
    expect(lanes.xOf(AT + 12 * H, AT, 'split')).toBeCloseTo(lanes.SPLIT_AT)
    expect(lanes.xOf(AT + 7 * D, AT, 'split')).toBe(1)
    expect(lanes.xOf(AT + 9 * D, AT, 'split')).toBe(1)
    expect(lanes.xOf(AT + 6 * H, AT, '12h')).toBeCloseTo(0.5)
    expect(lanes.xOf(AT + 3.5 * D, AT, '7d')).toBeCloseTo(0.5)
  })

  it('never draws two tick labels closer than a label is wide, at either width', () => {
    for (const axis of ['split', '12h', '7d'] as const) {
      for (const phone of [false, true]) {
        const ticks = lanes.ticksOf(AT, axis, phone)
        expect(ticks.length, `${axis} ${phone ? 'phone' : 'desktop'}`).toBeGreaterThan(0)
        const gap = phone ? 0.16 : 0.06
        expect(ticks[0]!.x).toBeGreaterThanOrEqual(phone ? 0.26 : 0.1)
        for (let i = 1; i < ticks.length; i++) expect(ticks[i]!.x - ticks[i - 1]!.x).toBeGreaterThanOrEqual(gap)
        for (const t of ticks) expect(t.x).toBeLessThanOrEqual(1 - gap)
      }
    }
  })
})

describe('the pool row and the pace row', () => {
  it('counts B3’s pool: 1 now, then 15:00Z, 15:10Z, Monday and Tuesday, and starts at the verdict’s N', () => {
    const accounts = b3(AT)
    const steps = lanes.poolSteps(accounts, 'eng', AT)
    expect(steps.map((s) => [s.count, pool.utcAt(s.at, AT)])).toEqual([
      [1, '14:04Z'],
      [2, '15:00Z'],
      [3, '15:10Z'],
      [4, 'Oct 26 07:59Z'],
      [5, 'Oct 27 10:59Z'],
    ])
    expect(steps[0]!.count).toBe(pool.verdictOf(accounts, 'eng', 0).n)
  })

  it('leaves a clock already past out, so the first step is still the verdict’s N', () => {
    const accounts = [acct('open', {}, AT), under('late', 'five_hour', 0.9, AT - 60_000, AT)]
    const steps = lanes.poolSteps(accounts, 'eng', AT)
    expect(steps).toEqual([{ at: AT, count: 1 }])
    expect(lanes.nextFreeAt(accounts, 'eng', AT)).toBe(AT)
  })

  it('reaches 0 at this pace exactly when the forecast says the pool goes dark: one computation', () => {
    // Two subscriptions spending fast: both reach their 7-day floor inside the week.
    const opened = AT - 2 * D
    const a = acct('a', { windows: { seven_day: win(0.6, opened + 7 * D) } }, AT)
    const b = acct('b', { windows: { seven_day: win(0.5, opened + 7 * D + 6 * H) } }, AT)
    const f = pool.forecastOf([a, b], 'eng', AT)
    expect(f.darkAt).not.toBeNull()
    const paced = lanes.paceSteps([a, b], 'eng', AT, f)!
    expect(paced.find((s) => s.count === 0)?.at).toBe(f.darkAt)
    expect(paced.find((s) => s.at > f.darkAt! && s.count > 0)?.at).toBe(f.backAt)
  })

  it('draws no pace row when an account has no 7-day reading to take one from', () => {
    const accounts = [acct('measured', { windows: { seven_day: win(0.3, AT + 3 * D) } }, AT), acct('new', { observed_at: null }, AT)]
    expect(lanes.paceSteps(accounts, 'eng', AT, pool.forecastOf(accounts, 'eng', AT))).toBeNull()
  })
})

describe('the waiting agents, queued against the free slots', () => {
  it('starts what the free slots hold now, queues the rest at each clock, and the remainder waits for an agent', () => {
    const now = Date.parse('2026-10-24T09:04:00Z')
    const accounts = [
      acct('ai-eng-2', {}, now),
      acct('devops-team', {}, now),
      acct('devops-main', {}, now),
      under('aieng-claude1', 'five_hour', 0.99, Date.parse('2026-10-24T10:10:00Z'), now),
      acct('full', { assigned: 6, can_take_new_agent: false, blocked_by: [{ cap: 6, max_agents: 6 }] }, now),
    ]
    const q = lanes.queueOf(accounts, 'eng', 28, now)
    expect(q.now.map((s) => [s.account.label, s.n])).toEqual([
      ['ai-eng-2', 6],
      ['devops-main', 6],
      ['devops-team', 6],
    ])
    expect(q.later.map((s) => [s.account.label, s.n, pool.utcAt(s.at, now)])).toEqual([['aieng-claude1', 6, '10:10Z']])
    expect(q.rest).toBe(4)
    // Never more than wait.
    const few = lanes.queueOf(accounts, 'eng', 2, now)
    expect(few.now.reduce((s, x) => s + x.n, 0) + few.later.length + few.rest).toBe(2)
  })

  it('queues behind a clock already past at the next check, never at a time that has gone', () => {
    const now = Date.parse('2026-10-24T09:04:00Z')
    const q = lanes.queueOf([under('late', 'five_hour', 0.9, now - 60_000, now)], 'eng', 3, now)
    expect(q.later.map((s) => [s.account.label, s.n, s.at])).toEqual([['late', 3, now]])
  })
})

describe('one window’s band', () => {
  it('draws a reset window as full with no figure, and an old reading at half', () => {
    const reset = lanes.bandOf(acct('r', { windows: { five_hour: win(0.97, AT - H, true) } }, AT), 'five_hour')
    expect([reset.kind, reset.left, reset.counted]).toEqual(['reset', null, null])
    const stale = lanes.bandOf(acct('s', { stale: true, windows: { seven_day: win(0.8, AT + D) } }, AT), 'seven_day')
    expect(stale.kind).toBe('stale')
    expect(stale.left).toBeCloseTo(0.2)
    expect(stale.counted).toBeCloseTo(0.1)
    const blocked = lanes.bandOf(under('u', 'seven_day', 0.98, AT + D, AT), 'seven_day')
    expect([blocked.blocks, blocked.floor]).toEqual([true, 0.05])
    expect(lanes.bandOf(acct('n', { observed_at: null }, AT), 'seven_day').kind).toBe('never')
  })
})

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

async function mount(accounts: PoolAccount[], waiting = 0) {
  api.loadAccountsBoard.mockResolvedValue(board(accounts))
  api.loadTasksInState.mockResolvedValue(waiting === 0 ? { status: 'empty', fetchedAt: Date.now() } : parked(waiting))
  const r = render(<AccountsScreen />)
  await screen.findByRole('region', { name: 'Can the pool take a new agent' }, WAIT)
  return r
}

const toggle = () => screen.getByRole('navigation', { name: 'Accounts view' })

describe('the Table / Timeline toggle', () => {
  it('opens on Table, beside the verdict', async () => {
    await mount(b3(Date.now()))
    const row = document.querySelector('.acct-verdict-row')!
    expect(within(row as HTMLElement).getByRole('navigation', { name: 'Accounts view' })).toBeTruthy()
    expect(within(toggle()).getByRole('link', { name: 'Table' }).getAttribute('aria-current')).toBe('page')
    expect(document.querySelector('.acct-tl')).toBeNull()
    expect(document.querySelector('.acct-split')).not.toBeNull()
  })

  it('switches to Timeline, writes it to the address and the browser, and reopens on it', async () => {
    const r = await mount(b3(Date.now()))
    const link = within(toggle()).getByRole('link', { name: 'Timeline' })
    expect(link.getAttribute('href')).toBe('#capacity/accounts?view=timeline')
    fireEvent.click(link)
    await waitFor(() => expect(document.querySelector('.acct-tl')).not.toBeNull(), WAIT)
    expect(window.location.hash).toBe('#capacity/accounts?view=timeline')
    expect(window.localStorage.getItem(lanes.VIEW_KEY)).toBe('timeline')
    r.unmount()
    // A later visit with a bare address: the browser remembers.
    window.history.replaceState(null, '', '/')
    await mount(b3(Date.now()))
    await waitFor(() => expect(document.querySelector('.acct-tl')).not.toBeNull(), WAIT)
  })

  it('lets a followed ?view=table link win over the browser’s memory', async () => {
    window.localStorage.setItem(lanes.VIEW_KEY, 'timeline')
    window.history.replaceState(null, '', '/#capacity/accounts?view=table')
    await mount(b3(Date.now()))
    expect(document.querySelector('.acct-tl')).toBeNull()
    expect(within(toggle()).getByRole('link', { name: 'Table' }).getAttribute('aria-current')).toBe('page')
  })
})

describe('the Timeline', () => {
  it('draws a lane per account in the Table’s order, the pool row from the verdict’s N, and the queue', async () => {
    // Only Date is pinned, to midday UTC: run near midnight, the queue's clocks
    // (an hour or so out) fall on the next day and are drawn with their date
    // ("Oct 26 00:55Z"), which the `at HH:MMZ` reading below does not match.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-25T12:00:00Z'))
    onTestFinished(() => {
      vi.useRealTimers()
    })
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount(b3(Date.now()), 3)
    const tl = await screen.findByRole('region', { name: 'Subscription accounts, timeline' }, WAIT)
    const names = [...tl.querySelectorAll('.acct-tl-lane .acct-tl-open')].map((b) => b.textContent)
    const ranked = pool
      .rankAccounts(b3(Date.now()), 'eng')
      .flatMap((g) => g.accounts)
      .map((a) => a.label)
    expect(names).toEqual(ranked)
    expect(names[0]).toBe('devops-team')
    const first = tl.querySelector('.acct-tl-step')!
    expect(first.getAttribute('title')).toMatch(/^1 from /)
    // 3 wait, 1 slot now: 1 starts now, 2 queue at the clocks (example-team, then aieng-claude1).
    await waitFor(() => expect(tl.textContent).toContain('1 start now'), WAIT)
    expect(tl.textContent).toMatch(/2 queue\s·\sexample-team takes 2 at \d\d:\d\dZ/)
    // The forecast under it is the Table's own.
    expect(within(tl).getByRole('region', { name: 'Forecast' })).toBeTruthy()
  })

  it('draws a full account’s holds up to the latest hold deadline the broker serves', async () => {
    const now = Date.now()
    const last = now + 2 * H + 41 * 60_000
    const accounts = b3(now).map((a) => (a.account_id === 'eng:ai-eng-2' ? { ...a, latest_hold_deadline: iso(last) } : a))
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount(accounts)
    const lane = document.querySelector<HTMLElement>('.acct-tl-lane[data-account="eng:ai-eng-2"]')!
    expect(lane.textContent).toContain('Full: 6 of 6 agents')
    expect(lane.querySelectorAll('.acct-tl-holdcells > i.is-held')).toHaveLength(6)
    // B3: "6 agents · latest hold deadline 16:45Z", labelled latest, never expected.
    expect(lane.textContent).toContain(`6 agents hold it · latest hold deadline ${pool.utcAt(last, now)}`)
    expect(lane.textContent).not.toContain('not in the accounts read')
    // The bar runs from now to that instant on the lane's own axis.
    const bar = lane.querySelector<HTMLElement>('.acct-tl-holdbar')!
    expect(bar).not.toBeNull()
    expect(parseFloat(bar.style.width)).toBeGreaterThan(0)
  })

  it('draws a held but not full account’s latest hold deadline too, and a dash with its reason when unserved', async () => {
    const now = Date.now()
    const last = now + 3 * H
    // B3: devops-team holds 5 of 6 and its lane says "5 agents · latest hold deadline 17:04Z".
    const accounts = b3(now).map((a) =>
      a.account_id === 'eng:devops-team'
        ? { ...a, max_agents: 6, latest_hold_deadline: iso(last) }
        : a.account_id === 'eng:devops-main'
          ? { ...a, latest_hold_deadline: iso(now + H) }
          : a,
    )
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    const first = await mount(accounts)
    const lane = (id: string) => document.querySelector<HTMLElement>(`.acct-tl-lane[data-account="${id}"]`)!
    const team = lane('eng:devops-team')
    expect(team.querySelectorAll('.acct-tl-holdcells > i')).toHaveLength(6)
    expect(team.querySelectorAll('.acct-tl-holdcells > i.is-held')).toHaveLength(5)
    expect(team.textContent).toContain(`5 agents hold it · latest hold deadline ${pool.utcAt(last, now)}`)
    expect(team.querySelector('.acct-tl-holdbar')).not.toBeNull()
    // It still has a free slot, so it does not say it frees only when one finishes.
    expect(team.textContent).not.toContain('frees when one finishes')
    expect(lane('eng:devops-main').textContent).toContain('frees when one finishes')
    // Not served: the same dash and reason a full lane draws.
    first.unmount()
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount(b3(now))
    expect(lane('eng:devops-team').textContent).toContain('latest hold deadline — not reported')
    // An account nobody holds draws no holds at all.
    expect(lane('eng:ai-eng-3').querySelector('.acct-tl-holds')).toBeNull()
  })

  it('draws a hold deadline the broker does not know as a dash with its reason', async () => {
    const now = Date.now()
    const accounts = b3(now).map((a) =>
      a.account_id === 'eng:devops-main' ? { ...a, latest_hold_deadline: null } : a,
    )
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount(accounts)
    const lane = (id: string) => document.querySelector<HTMLElement>(`.acct-tl-lane[data-account="${id}"]`)!
    // No key: a broker older than the field.
    expect(lane('eng:ai-eng-2').textContent).toContain('latest hold deadline — not reported')
    // Null: the broker read no live hold on it.
    expect(lane('eng:devops-main').textContent).toContain('latest hold deadline — the broker read no live hold')
    expect(lane('eng:ai-eng-2').querySelector('.acct-tl-holdbar')).toBeNull()
    expect(lanes.holdDeadlineOf({ latest_hold_deadline: 'soon' } as never, now)).toEqual({
      at: null,
      reason: 'not an instant the broker served',
    })
  })

  it('draws an old reading pale, and names a duplicate’s other half only inside this workspace', async () => {
    const now = Date.now()
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount([
      acct('example-personal', { windows: { seven_day: win(0.5, now + 3 * D) } }, now),
      acct('team', { can_take_new_agent: false, blocked_by: [{ duplicate_of: 'eng:example-personal', reason: 'same organisation' }] }, now),
      acct('copy', { can_take_new_agent: false, blocked_by: [{ duplicate_of: 'other:theirs', reason: 'same organisation' }] }, now),
      acct('old', { stale: true, windows: { seven_day: win(0.4, now + 2 * D) } }, now),
    ])
    const lane = (id: string) => document.querySelector<HTMLElement>(`.acct-tl-lane[data-account="${id}"]`)!
    expect(lane('eng:team').textContent).toContain('Not handed out: one subscription with example-personal')
    expect(lane('eng:copy').textContent).toContain('one subscription with an account in another workspace')
    expect(lane('eng:copy').textContent).not.toContain('theirs')
    expect(lane('eng:team').classList.contains('is-dup')).toBe(true)
    expect(lane('eng:old').classList.contains('is-stale')).toBe(true)
  })

  it('marks the kept half of a duplicate pair on its lane, naming the other only inside this workspace', async () => {
    const now = Date.now()
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount([
      // The kept half: the broker names the pair on both rows, and withholds only the later one.
      acct('example-personal', { duplicate_of: 'eng:team', duplicate_reason: 'registered on the same organisation as eng:team' }, now),
      acct('team', {
        duplicate_of: 'eng:example-personal',
        can_take_new_agent: false,
        blocked_by: [{ duplicate_of: 'eng:example-personal', reason: 'same organisation' }],
      }, now),
      // Kept here, its later twin in another workspace: named by neither side.
      acct('solo', { duplicate_of: null, duplicate_reason: 'this subscription is already registered in another workspace' }, now),
      acct('plain', {}, now),
    ])
    const lane = (id: string) => document.querySelector<HTMLElement>(`.acct-tl-lane[data-account="${id}"]`)!
    expect(lane('eng:example-personal').classList.contains('is-kept')).toBe(true)
    expect(lane('eng:example-personal').textContent).toContain('One subscription with team · older, handed out')
    // The withheld half keeps its own flag, and is not also called kept.
    expect(lane('eng:team').classList.contains('is-kept')).toBe(false)
    expect(lane('eng:solo').classList.contains('is-kept')).toBe(true)
    expect(lane('eng:solo').textContent).toContain('One subscription with an account in another workspace · handed out here')
    expect(lane('eng:plain').classList.contains('is-kept')).toBe(false)
  })

  it('opens the Table’s side sheet from a lane, keeps the view in the address, and closes back to the lanes', async () => {
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount(b3(Date.now()))
    const open = document.querySelector<HTMLButtonElement>('.acct-tl-open[data-account="eng:example-team"]')!
    fireEvent.click(open)
    expect(window.location.hash).toBe('#capacity/accounts?account=eng%3Aexample-team&view=timeline')
    const sheet = await waitFor(() => {
      const d = document.querySelector<HTMLElement>('aside.acct-sheet')
      expect(d).not.toBeNull()
      return d!
    }, WAIT)
    // The same sheet a Table row opens: its label, its head and the detail.
    expect(sheet.getAttribute('aria-label')).toBe('Account example-team')
    expect(sheet.querySelector('.acct-sheet-head .acct-sheet-close')).not.toBeNull()
    expect(sheet.querySelector('.acct-pane-detail .acct-detail')).not.toBeNull()
    expect(document.querySelector('.acct-split.is-timeline.has-sheet > .acct-scrim')).not.toBeNull()
    // Not proposal B's expanded lane: no detail under it, and no lane folds.
    expect(document.querySelector('.acct-tl-detail')).toBeNull()
    expect(document.querySelectorAll('.acct-tl-lane.is-folded')).toHaveLength(0)
    expect(document.querySelectorAll('.acct-tl-lane')).toHaveLength(7)
    // The lanes stay drawn under the sheet, the opened one marked.
    expect(document.querySelector('.acct-tl-lane.is-open')?.getAttribute('data-account')).toBe('eng:example-team')
    fireEvent.click(within(sheet).getByRole('button', { name: 'Close' }))
    expect(window.location.hash).toBe('#capacity/accounts?view=timeline')
    await waitFor(() => expect(document.querySelector('.acct-sheet')).toBeNull(), WAIT)
    expect(document.querySelector('.acct-tl')).not.toBeNull()
  })

  it('opens the sheet as the whole screen on a phone: the lanes and the verdict step aside', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('560'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }))
    try {
      window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
      await mount(b3(Date.now()))
      fireEvent.click(document.querySelector<HTMLButtonElement>('.acct-tl-open[data-account="eng:example-team"]')!)
      const sheet = await waitFor(() => {
        const d = document.querySelector<HTMLElement>('aside.acct-sheet')
        expect(d).not.toBeNull()
        return d!
      }, WAIT)
      expect(document.querySelector('.acct-split.is-timeline.is-detail')).not.toBeNull()
      expect(document.querySelector('.acct-verdict, .acct-view-toggle')).toBeNull()
      expect(sheet.querySelector('.acct-back')).not.toBeNull()
      // The phone block hides the Timeline under an opened account, as capacity.css hides the list.
      const css = readFileSync(resolve(__dirname, '../styles/accounts-timeline.css'), 'utf8')
      expect(css).toMatch(/\.acct-split\.is-timeline\.is-detail > \.acct-tl\s*\{[^}]*display:\s*none/)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('keeps the view in the address an account writes when the Timeline came from the browser’s memory', async () => {
    // Arriving from the sidebar: a bare address, the Timeline from storage.
    window.localStorage.setItem(lanes.VIEW_KEY, 'timeline')
    window.history.replaceState(null, '', '/#capacity/accounts')
    await mount(b3(Date.now()))
    await waitFor(() => expect(document.querySelector('.acct-tl')).not.toBeNull(), WAIT)
    fireEvent.click(document.querySelector<HTMLButtonElement>('.acct-tl-open[data-account="eng:example-team"]')!)
    expect(window.location.hash).toBe('#capacity/accounts?account=eng%3Aexample-team&view=timeline')
  })

  it('writes ?view=timeline to the address when the Timeline is restored from the browser’s memory', async () => {
    window.localStorage.setItem(lanes.VIEW_KEY, 'timeline')
    window.history.replaceState(null, '', '/#capacity/accounts')
    const before = window.history.length
    await mount(b3(Date.now()))
    await waitFor(() => expect(window.location.hash).toBe('#capacity/accounts?view=timeline'), WAIT)
    // Replaced, not pushed: Back does not land on the same page with a bare address.
    expect(window.history.length).toBe(before)
  })

  it('leaves a Table address bare: only a restored Timeline is written', async () => {
    window.history.replaceState(null, '', '/#capacity/accounts')
    await mount(b3(Date.now()))
    expect(window.location.hash).toBe('#capacity/accounts')
  })

  it('draws the empty pool as B6: no lanes, the add form in the first lane, one heading', async () => {
    window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
    await mount([])
    const tl = await screen.findByRole('region', { name: 'Subscription accounts, timeline' }, WAIT)
    expect(tl.textContent).toContain('No lanes yet')
    expect(screen.getAllByRole('heading', { name: /Subscription accounts/ })).toHaveLength(1)
    expect(within(tl).getByRole('heading', { name: 'Add an account' })).toBeTruthy()
    expect(tl.querySelector('.acct-tl-legend')).toBeNull()
  })
})
