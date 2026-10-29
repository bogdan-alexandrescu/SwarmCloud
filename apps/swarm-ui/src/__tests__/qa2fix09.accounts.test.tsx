// VISUAL QA PASS 2, LANE QA2-FIX-09 (part of #2694): the account sheet and
// the add-account sheet, as the QA2-CAPACITY sweep found them on the fixture
// console on 2026-10-29.
//
//   * cap2-02: once the sign-in starts, the add sheet still has its close and
//     Escape still folds it; the sign-in is kept, and focus goes to the new
//     step's first control rather than <body>;
//   * cap2-03: Escape in the Rename editor gives focus back to `Rename`;
//   * cap2-04: Escape in the cap editor closes the editor only, focus back on
//     `Change the cap`; a second Escape closes the sheet;
//   * cap2-05: a 7-day reset already past is not forecast as ahead;
//   * cap2-13: the disabled Remove is described by why it is off;
//   * cap2-14: the Pause confirm is a modal over the shared scrim (this one
//     held on main already; it is pinned so it stays);
//   * cap2-15: the health chart's ticks carry the month at 390 too.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard, AccountHealth } from '../api'
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
  setAccountName: vi.fn(),
  loadAccountHealth: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', () => api)

const reads = vi.hoisted(() => ({ read: vi.fn() }))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: reads.read,
}))

const { AccountsScreen } = await import('../Accounts')
const { SheetForecast, HealthSeries } = await import('../AccountSheet')

const WAIT = { timeout: 5000 } as const
const HOUR = 3_600_000
const DAY = 24 * HOUR

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function account(over: Partial<PoolAccount> = {}): PoolAccount {
  const now = Date.now()
  return {
    account_id: 'eng:primary',
    owner_tenant: 'eng',
    label: 'primary',
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
    organisation: { name: 'Example', plan: 'Team' },
    ...over,
  }
}

async function mount(): Promise<void> {
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
}

async function open(): Promise<HTMLElement> {
  await mount()
  fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open[data-account="eng:primary"]')!)
  return document.querySelector<HTMLElement>('.acct-sheet')!
}

const sheet = () => document.querySelector<HTMLElement>('.acct-sheet')
const escape = (on: Element) => fireEvent.keyDown(on, { key: 'Escape' })

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  api.loadAccountHealth.mockReturnValue(new Promise(() => {}))
  reads.read.mockReset()
  reads.read.mockResolvedValue(ok({ account_id: 'eng:primary', viewer: 'owner', total: 0, holders: [], others: 0 }))
})

describe('cap2-03: Escape in Rename gives focus back to Rename', () => {
  it('cancels the rename and focuses the Rename button, not <body>', async () => {
    const s = await open()
    fireEvent.click(within(s).getByRole('button', { name: 'Rename' }))
    const field = within(s).getByRole('textbox', { name: 'Name for eng:primary' })
    field.focus()
    escape(field)
    // MUTATION: drop the `back`/focus effect in NameHead -- focus is <body>.
    expect(document.activeElement).toBe(within(s).getByRole('button', { name: 'Rename' }))
    expect(sheet(), 'the rename Escape closed the sheet').not.toBeNull()
  })
})

describe('cap2-04: Escape in the cap editor closes the editor, not the sheet', () => {
  it('closes only the editor first, then the sheet on a second Escape', async () => {
    const s = await open()
    fireEvent.click(within(s).getByRole('button', { name: 'Change the cap' }))
    const field = within(s).getByRole('spinbutton', { name: 'Agents at once' })
    escape(field)
    // MUTATION: drop the form's onKeyDown in AgentCap -- the sheet closes.
    expect(sheet(), 'the cap editor Escape closed the whole sheet').not.toBeNull()
    expect(within(sheet()!).queryByRole('spinbutton', { name: 'Agents at once' })).toBeNull()
    const change = within(sheet()!).getByRole('button', { name: 'Change the cap' })
    expect(document.activeElement).toBe(change)
    escape(change)
    expect(sheet()).toBeNull()
  })
})

describe('cap2-05: a 7-day reset already past is not forecast as ahead', () => {
  it('says the week should have reset, in the Windows block words', () => {
    const now = Date.parse('2026-10-29T09:00:00Z')
    const a = account({
      observed_at: '2026-10-21T09:00:00Z',
      windows: {
        five_hour: { utilization: 0.2, resets_at: '2026-10-21T12:00:00Z', reset: false },
        seven_day: { utilization: 0.94, resets_at: '2026-10-22T10:00:00Z', reset: false },
      },
    })
    const { container } = render(<SheetForecast a={a} now={now} />)
    const text = container.textContent ?? ''
    // MUTATION: drop the `passed` branch in SheetForecast -- it says "stays above ... until its week resets".
    expect(text).not.toContain('until its week resets')
    expect(text).toContain('should have reset')
    expect(text).toMatch(/reset Oct 22 10:00Z passed/)
    expect(text).not.toContain('Pace:')
  })
})

describe('cap2-13: the disabled Remove says why it is off', () => {
  it('is described by the line that names what arms it', async () => {
    const s = await open()
    const remove = within(s).getByRole('button', { name: 'Remove primary' }) as HTMLButtonElement
    expect(remove.disabled).toBe(true)
    const why = remove.getAttribute('aria-describedby')
    // MUTATION: drop aria-describedby from Remove's Button -- the reason is not tied to it.
    expect(why).not.toBeNull()
    expect(document.getElementById(why!)?.textContent).toContain('type primary')
    fireEvent.change(within(s).getByRole('textbox', { name: 'Type primary to confirm removal' }), { target: { value: 'primary' } })
    expect(remove.getAttribute('aria-describedby')).toBeNull()
  })
})

describe('cap2-14: the Pause confirm is a modal over the scrim', () => {
  it('opens as an aria-modal dialog inside the shared scrim', async () => {
    const s = await open()
    fireEvent.click(within(s).getByRole('button', { name: 'Pause' }))
    const d = await screen.findByRole('dialog', { name: /Move primary to paused/ })
    expect(d.getAttribute('aria-modal')).toBe('true')
    expect(d.parentElement?.classList.contains('c-scrim')).toBe(true)
  })
})

describe('cap2-15: the health chart ticks name their month on a phone', () => {
  it('writes Oct 23, not a bare 23, at 390', () => {
    const now = Date.parse('2026-10-29T09:00:00Z')
    const h = { hourly: [], events: [], limited_until: null } as unknown as AccountHealth
    const { container } = render(<HealthSeries h={h} now={now} width={350} />)
    // The day ticks are the texts on the axis line under the plot (y = 126).
    const ticks = [...container.querySelectorAll('text')].filter((t) => t.getAttribute('y') === '126').map((t) => t.textContent)
    expect(ticks.length, 'no day ticks drawn').toBeGreaterThan(0)
    // MUTATION: put back `narrow ? String(dt.getUTCDate())` -- the ticks read `23 25 27`.
    for (const w of ticks) expect(w).toMatch(/^[A-Z][a-z]{2} \d{1,2}$/)
  })
})

describe('cap2-02: the add sheet closes at every step of the sign-in', () => {
  it('keeps its close and Escape once the sign-in starts, and moves focus to the step', async () => {
    api.beginAccountSignIn.mockResolvedValue(
      ok({ authorize_url: 'https://claude.com/cai/oauth/authorize?state=s', state: 's'.repeat(43), expires_in_seconds: 900 }),
    )
    await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Add account' }))
    const s = sheet()!
    const label = within(s).getByRole('textbox', { name: /label/i })
    fireEvent.change(label, { target: { value: 'second' } })
    await act(async () => {
      fireEvent.click(within(s).getByRole('button', { name: 'Start the sign-in' }))
    })
    await waitFor(() => expect(sheet()?.textContent).toContain('Sign in to Claude'), WAIT)
    // MUTATION: put back `closable && (` around the sheet's Close -- it is gone.
    const close = within(sheet()!).getByRole('button', { name: 'Close' })
    expect(close).toBeTruthy()
    // MUTATION: drop the step-focus effect in AddAccount -- focus is <body>.
    expect(document.activeElement).not.toBe(document.body)
    expect(sheet()!.contains(document.activeElement)).toBe(true)
    // MUTATION: put back `ui.adding || ui.signin.kind !== 'idle'` in addOpen -- Escape leaves it open.
    escape(document.activeElement!)
    expect(sheet(), 'Escape did not fold the add sheet mid-sign-in').toBeNull()
    // The sign-in is kept: Add account opens it again where it was.
    const add = screen.getByRole('button', { name: 'Add account' }) as HTMLButtonElement
    expect(add.disabled).toBe(false)
    fireEvent.click(add)
    expect(sheet()?.textContent).toContain('Sign in to Claude')
    expect(api.beginAccountSignIn).toHaveBeenCalledTimes(1)
  })
})
