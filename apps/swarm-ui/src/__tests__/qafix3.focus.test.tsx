/**
 * LANE QAFIX3-FOCUS (part of #1680; visual QA run 3, wf_d598fa7bffdb41fc9d76,
 * 2026-10-21, the fixture build of main at 51d99c53).
 *
 *   a11y-keyboard-001  Activating a refresh control dropped keyboard focus to
 *                      <body> (a DOM measurement at 1440 light), on Accounts,
 *                      the Shell refresh, Platform counts and Submit a
 *                      workflow. A focused button that becomes `disabled`
 *                      loses focus in a browser, and one that unmounts takes
 *                      focus with it: either way the next Tab starts again
 *                      from the top of the page.
 *
 * What holds now: a refresh in flight is `aria-disabled`, not `disabled`, so
 * the control keeps focus and a second press does nothing; a `Try again` that
 * unmounts with its failure hands focus to the head's refresh, which stays;
 * and the Accounts refresh, which the reload it causes remounts, is focused
 * again when it is back.
 *
 * jsdom runs no focus fix-up when a focused button turns `disabled`, so the
 * cases below assert the attribute that makes a browser drop focus as well as
 * `document.activeElement`.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { ApiError, Result } from '../fetch'
import type { Account, AccountsPage, RefreshResponse, Stats } from '../types'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  refreshAccount: vi.fn(),
  loadStats: vi.fn(),
  loadMe: vi.fn(),
  loadCapacity: vi.fn(),
  loadModelClasses: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  ...api,
}))

const { Screen } = await import('../Shell')
const { AccountsScreen } = await import('../Accounts')
const { PlatformCountsScreen } = await import('../PlatformCounts')
const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

function failure(kind: ApiError['kind'] = 'unreachable', retryAfterSeconds?: number): ApiError {
  return { kind, httpStatus: null, code: null, message: 'the read failed', retryAfterSeconds }
}

function failed<T>(error: ApiError = failure()): Result<T> {
  return { status: 'error', error }
}

/** The press, as a keyboard user makes it: focus the control, then activate it. */
function press(el: HTMLElement): void {
  el.focus()
  expect(document.activeElement, 'the control could not take focus to begin with').toBe(el)
  fireEvent.click(el)
}

describe('a11y-keyboard-001: the Shell refresh keeps focus through its read', () => {
  it('keeps focus on the head refresh while the read it started is in flight', async () => {
    const second = deferred<Result<number[]>>()
    const load = vi.fn<() => Promise<Result<number[]>>>()
    load.mockResolvedValueOnce(failed()).mockReturnValueOnce(second.promise)
    render(
      <Screen<number[]> title="Agents" load={load}>
        {(d) => <span>{d.length} rows</span>}
      </Screen>,
    )
    const refresh = await screen.findByRole('button', { name: 'Refresh · not read' }, WAIT)
    press(refresh)
    const reading = await screen.findByRole('button', { name: 'Reading' }, WAIT)
    // MUTATION: `disabled={reading}` back on the not-read branch of
    // `RefreshControl` (Shell.tsx) -- a browser blurs the focused control to
    // <body> the moment it is disabled.
    expect(reading.hasAttribute('disabled')).toBe(false)
    expect(reading.getAttribute('aria-disabled')).toBe('true')
    // MUTATION: the not-read branch drawn as a different element (a <span>),
    // so the focused button unmounts under the press.
    expect(document.activeElement).toBe(reading)
    // MUTATION: `onClick={onRefresh}` kept while reading: a second press
    // starts a second read over the first.
    fireEvent.click(reading)
    expect(load).toHaveBeenCalledTimes(2)
    await act(async () => second.resolve({ status: 'ok', data: [1, 2], fetchedAt: Date.now() }))
    await screen.findByText('2 rows', undefined, WAIT)
    // MUTATION: the answered read drawn on a new button (a `key` on the
    // control), so focus falls when the rows land.
    expect(document.activeElement?.classList.contains('c-refresh')).toBe(true)
  })

  it('keeps focus on the head refresh when its read comes back rate-limited', async () => {
    const load = vi.fn<() => Promise<Result<number[]>>>()
    load.mockResolvedValueOnce(failed()).mockResolvedValueOnce(failed(failure('rate_limited', 30)))
    render(
      <Screen<number[]> title="Agents" load={load}>
        {(d) => <span>{d.length} rows</span>}
      </Screen>,
    )
    press(await screen.findByRole('button', { name: 'Refresh · not read' }, WAIT))
    const paused = await screen.findByText(/^paused \d+ s$/, { selector: 'button' }, WAIT)
    // MUTATION: `disabled` back on the paused branch of `RefreshControl`.
    expect(paused.hasAttribute('disabled')).toBe(false)
    expect(paused.getAttribute('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(paused)
  })

  it('draws one retry on a failed page, the head refresh, which keeps focus (QA2 automate2-11)', async () => {
    const second = deferred<Result<number[]>>()
    const load = vi.fn<() => Promise<Result<number[]>>>()
    load.mockResolvedValueOnce(failed()).mockReturnValueOnce(second.promise)
    render(
      <Screen<number[]> title="Agents" load={load}>
        {(d) => <span>{d.length} rows</span>}
      </Screen>,
    )
    const refresh = await screen.findByRole('button', { name: 'Refresh · not read' }, WAIT)
    // MUTATION: the panel's `Try again` drawn on a Screen again: two controls for one press.
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    press(refresh)
    const reading = await screen.findByRole('button', { name: 'Reading' }, WAIT)
    expect(document.activeElement).toBe(reading)
    await act(async () => second.resolve({ status: 'ok', data: [1], fetchedAt: Date.now() }))
  })
})

// ---------------------------------------------------------------------------
// Submit a workflow: its refresh is the Shell's, on the form's catalogue read
// ---------------------------------------------------------------------------

describe('a11y-keyboard-001: Submit a workflow keeps focus on its refresh', () => {
  it('keeps focus on the head refresh while the catalogue is re-read', async () => {
    const second = deferred<Result<never>>()
    api.loadCapacity.mockResolvedValueOnce(failed()).mockReturnValueOnce(second.promise)
    api.loadStats.mockResolvedValue(failed())
    api.loadModelClasses.mockResolvedValue(failed())
    render(<SubmitWorkflowScreen />)
    press(await screen.findByRole('button', { name: 'Refresh · not read' }, WAIT))
    const reading = await screen.findByRole('button', { name: 'Reading' }, WAIT)
    // MUTATION: `disabled={reading}` back on `RefreshControl`'s not-read branch.
    expect(reading.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(reading)
    await act(async () => second.resolve(failed()))
  })
})

// ---------------------------------------------------------------------------
// Platform counts: the run is the page's read control
// ---------------------------------------------------------------------------

function stats(): Result<Stats> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'eng',
      tasks_by_state: { READY: 1, RUNNING: 2 },
      dispatch_paused: false,
      limits: {},
      generated_at: new Date().toISOString(),
    } as unknown as Stats,
  }
}

describe('a11y-keyboard-001: Platform counts keeps focus on its run', () => {
  it('keeps focus on the run button while the count is in flight, and a second press bills nothing', async () => {
    const run = deferred<Result<Stats>>()
    api.loadMe.mockResolvedValue(failed())
    api.loadStats.mockReturnValueOnce(run.promise)
    render(<PlatformCountsScreen />)
    const button = screen.getByRole('button', { name: /^Run the count · / })
    press(button)
    await screen.findByText('counting…', undefined, WAIT)
    // MUTATION: `disabled={busy}` back on the `counts-run` Button
    // (PlatformCounts.tsx): the browser blurs it to <body> on the press.
    expect(button.hasAttribute('disabled')).toBe(false)
    expect(button.getAttribute('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(button)
    // MUTATION: `onClick={go}` with no in-flight guard: the second press runs
    // (and bills) a second count.
    fireEvent.click(button)
    expect(api.loadStats).toHaveBeenCalledTimes(1)
    await act(async () => run.resolve(stats()))
    const again = await screen.findByRole('button', { name: /^Run it again · / }, WAIT)
    // MUTATION: the re-run drawn as a different control: focus falls when the
    // answer lands.
    expect(again).toBe(button)
    expect(document.activeElement).toBe(button)
  })
})

// ---------------------------------------------------------------------------
// Accounts: the per-account refresh, which reloads (remounts) the screen
// ---------------------------------------------------------------------------

function account(): Account {
  return {
    account_id: 'eng:laptop',
    owner_tenant: 'eng',
    label: 'laptop',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: new Date(Date.now() - 60_000).toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
  } as Account
}

function board(): Result<AccountsBoard> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    serverAt: new Date().toISOString(),
    data: {
      page: { accounts: [account()], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: Date.now(),
    } as AccountsBoard,
  }
}

function refreshed(): Result<RefreshResponse> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      refresh: { tenant_id: 'eng', provider: 'anthropic-subscription', refreshed: false, reason: 'still_valid', expires_at: null },
    } as unknown as RefreshResponse,
  }
}

/** The refresh is in the account's side sheet (#2222), which opens on its row. */
async function openAccount(): Promise<void> {
  await waitFor(() => expect(document.querySelector('button.acct-open')).not.toBeNull(), WAIT)
  fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open')!)
}

describe('a11y-keyboard-001: Accounts keeps focus on the account refresh', () => {
  it('keeps focus on Refresh this account while it exchanges, and puts it back after the reload remounts it', async () => {
    const exchange = deferred<Result<RefreshResponse>>()
    const reread = deferred<Result<AccountsBoard>>()
    api.loadAccountsBoard.mockResolvedValueOnce(board()).mockReturnValueOnce(reread.promise)
    api.refreshAccount.mockReturnValueOnce(exchange.promise)
    render(<AccountsScreen />)
    await openAccount()
    const button = await screen.findByRole('button', { name: 'Refresh this account' }, WAIT)
    press(button)
    await screen.findByRole('button', { name: 'Exchanging…' }, WAIT)
    // MUTATION: `disabled={state.kind === 'sending'}` back on the button
    // (Accounts.tsx `RefreshControl`).
    expect(button.hasAttribute('disabled')).toBe(false)
    expect(button.getAttribute('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(button)
    // MUTATION: the click handler kept while sending: a second press asks the
    // broker for a second exchange.
    fireEvent.click(button)
    expect(api.refreshAccount).toHaveBeenCalledTimes(1)
    await act(async () => exchange.resolve(refreshed()))
    await act(async () => reread.resolve(board()))
    const back = await screen.findByRole('button', { name: 'Refresh this account' }, WAIT)
    // The reload remounts the screen, so this is a new button...
    expect(back).not.toBe(button)
    // MUTATION: drop the `focusRefresh` hand-over (Accounts.tsx): the remount
    // takes the focused button away and focus stays on <body>.
    await waitFor(() => expect(document.activeElement).toBe(back), WAIT)
  })

  it('moves focus from a failed exchange’s Try again to the refresh button, which stays', async () => {
    const second = deferred<Result<RefreshResponse>>()
    api.loadAccountsBoard.mockResolvedValue(board())
    api.refreshAccount.mockResolvedValueOnce(failed(failure('server_error'))).mockReturnValueOnce(second.promise)
    render(<AccountsScreen />)
    await openAccount()
    const refresh = await screen.findByRole('button', { name: 'Refresh this account' }, WAIT)
    press(refresh)
    // The refresh's own Try again: the sheet's holds read draws one as well.
    const action = refresh.closest<HTMLElement>('.acct-action')!
    const retry = await within(action).findByRole('button', { name: 'Try again' }, WAIT)
    press(retry)
    const sending = await screen.findByRole('button', { name: 'Exchanging…' }, WAIT)
    expect(within(action).queryByRole('button', { name: 'Try again' })).toBeNull()
    // MUTATION: `run` without moving focus to the account's refresh button:
    // Try again unmounts with its panel and focus is <body>.
    expect(document.activeElement).toBe(sending)
    await act(async () => second.resolve(failed(failure('server_error'))))
  })
})
