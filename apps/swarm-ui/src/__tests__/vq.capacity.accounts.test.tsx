// VISUAL QA PASS 1 (2026-10-26, #2391), LANE VQ-CAPACITY: VQ-035, Capacity ›
// Accounts' Timeline and side sheet.
//
//   * A pool-row segment said `7`, a figure with no noun, where `N open` did
//     not fit, and the slivers too thin for that were unlabelled. A segment
//     now says `N open` or nothing, and the line under the bar names every
//     step whenever one is silent.
//   * `1 agent hold it` -> `1 agent holds it`.
//   * The legend names the held-slot cells and the bar to the latest hold
//     deadline, the two lane marks it left out.
//   * The sheet's `last given out` was the browser's clock beside `resets
//     05:30Z`: every clock on Accounts is UTC with its Z.
//
// BREAK IT: put the bare `String(n)` back in `Steps`, the plural `hold` back
// for one agent, drop the hold entries from the legend, or `givenAt` back to
// `toLocaleTimeString` -- a case turns red.

import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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

const { AccountsScreen, givenAt } = await import('../Accounts')

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
  window.history.replaceState(null, '', '/#capacity/accounts?view=timeline')
  // Midday UTC, so the clocks below fall on the day `now` is.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-10-25T12:00:00Z'))
})
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
  window.history.replaceState(null, '', '/')
})

async function mount(accounts: PoolAccount[], waiting = 0) {
  api.loadAccountsBoard.mockResolvedValue(board(accounts))
  api.loadTasksInState.mockResolvedValue(waiting === 0 ? { status: 'empty', fetchedAt: Date.now() } : parked(waiting))
  const r = render(<AccountsScreen />)
  await screen.findByRole('region', { name: 'Can the pool take a new agent' }, WAIT)
  return r
}

async function timeline(accounts: PoolAccount[]): Promise<HTMLElement> {
  await mount(accounts)
  return screen.findByRole('region', { name: 'Subscription accounts, timeline' }, WAIT)
}

describe('VQ-035: the Timeline’s pool rows', () => {
  it('never prints a bare count in a segment, and names every step under the bar when one is silent', async () => {
    const tl = await timeline(b3(Date.now()))
    const rows = [...tl.querySelectorAll('.acct-tl-steps')]
    expect(rows.length).toBeGreaterThan(0)
    let silent = 0
    for (const r of rows) {
      for (const s of r.querySelectorAll('.acct-tl-step')) {
        const text = s.textContent ?? ''
        if (text === '') silent++
        else expect(text).toMatch(/^\d+ open$/)
      }
    }
    // B3's 15:00Z and 15:10Z are minutes apart: at least one segment is silent,
    // so its row carries the list that says it.
    expect(silent).toBeGreaterThan(0)
    for (const r of rows) {
      const silentHere = [...r.querySelectorAll('.acct-tl-step')].some((s) => s.textContent === '')
      if (silentHere) expect(r.nextElementSibling?.classList.contains('acct-tl-steplist')).toBe(true)
    }
  })
})

describe('VQ-035: the lanes’ words and legend', () => {
  it('says one agent holds it, and several hold it', async () => {
    const now = Date.now()
    const tl = await timeline([acct('solo', { assigned: 1 }, now), acct('busy', { assigned: 3 }, now)])
    const lane = (id: string) => tl.querySelector<HTMLElement>(`.acct-tl-lane[data-account="${id}"]`)!
    expect(lane('eng:solo').textContent).toContain('1 agent holds it')
    expect(lane('eng:solo').textContent).not.toContain('1 agent hold it')
    expect(lane('eng:busy').textContent).toContain('3 agents hold it')
  })

  it('names the held-slot cells and the hold-deadline bar in the legend', async () => {
    const now = Date.now()
    const tl = await timeline([acct('solo', { assigned: 1 }, now)])
    const legend = tl.querySelector('.acct-tl-legend')!
    expect(legend.querySelector('.lg-held')).not.toBeNull()
    expect(legend.textContent).toContain('an agent slot held')
    expect(legend.querySelector('.lg-holdbar')).not.toBeNull()
    expect(legend.textContent).toContain('held until the latest hold deadline')
  })

  it('leaves the hold entries out when no account holds an agent', async () => {
    const tl = await timeline([acct('idle', { assigned: 0 })])
    await waitFor(() => expect(tl.querySelector('.acct-tl-legend')).not.toBeNull(), WAIT)
    expect(tl.querySelector('.acct-tl-legend .lg-held')).toBeNull()
  })
})

describe('VQ-035: one time style on Accounts', () => {
  it('writes when an account was last given out in UTC with its Z, as the reset times are', () => {
    const now = Date.UTC(2026, 9, 25, 18, 37)
    expect(givenAt(new Date(Date.UTC(2026, 9, 25, 17, 46)).toISOString(), now)).toBe('17:46Z')
    expect(givenAt(new Date(Date.UTC(2026, 9, 24, 23, 5)).toISOString(), now)).toBe('Oct 24 23:05Z')
    expect(givenAt('not a time', now)).toBe('—')
  })
})
