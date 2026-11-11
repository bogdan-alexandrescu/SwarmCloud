// LLM PROVIDERS › PLANS DRAWS ITS STRIP AND ITS (i) IN EVERY READ STATE (the
// owner's answer of 11 November 2026: "Add two optional props to Screen").
//
// `Screen` takes an `info`, forwarded to `PageHead` so the (i) sits by the
// title, and a `nav`, drawn right under the head whatever the read did. Plans
// passes the tab's (i) and its Plans | API Keys strip through them, so both
// show while the plans load and after their read failed, not only once it has
// data -- and still exactly once each, under one h1.
//
// BREAK IT: draw `nav` inside the ok branch of `Screen`, or put the strip
// back in the table's body, and the loading and failed cases turn red; draw
// it both places and the counting case does.
//
// Placeholders only; nothing is token-shaped.

import { render, screen, within } from '@testing-library/react'
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

const { LlmProvidersScreen } = await import('../LlmProviders')
const { Screen } = await import('../Shell')

const WAIT = { timeout: 5000 } as const

function acct(label: string): PoolAccount {
  const now = Date.now()
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
    last_assigned_at: new Date(now - 3_600_000).toISOString(),
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: null,
  }
}

function board(accounts: PoolAccount[]): Result<AccountsBoard> {
  const now = Date.now()
  return {
    status: 'ok',
    fetchedAt: now,
    serverAt: new Date(now).toISOString(),
    data: {
      page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: now,
    },
  }
}

/** The strip and the (i), each exactly once, the (i) in the page head beside its h1. */
function oneStripOneInfo() {
  const strips = screen.getAllByRole('navigation', { name: 'LLM Providers' })
  expect(strips).toHaveLength(1)
  expect(within(strips[0]!).getAllByRole('link').map((a) => a.textContent)).toEqual(['Plans', 'API Keys'])
  const infos = screen.getAllByRole('button', { name: 'About LLM Providers' })
  expect(infos).toHaveLength(1)
  expect(infos[0]!.closest('.c-phead .head')?.querySelector('h1')?.textContent).toBe('LLM Providers')
  expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['LLM Providers'])
}

describe('LLM Providers › Plans: the strip and the (i) in every read state', () => {
  it('draws both while the board is still loading', () => {
    api.loadAccountsBoard.mockReturnValue(new Promise(() => {}))
    render(<LlmProvidersScreen sub="plans" />)
    oneStripOneInfo()
  })

  it('draws both after the board’s read failed', async () => {
    api.loadAccountsBoard.mockResolvedValue({
      status: 'error',
      error: { kind: 'server_error', httpStatus: 500, code: null, message: 'no' },
    })
    const { container } = render(<LlmProvidersScreen sub="plans" />)
    await vi.waitFor(() => expect(container.querySelector('.state.failed')).not.toBeNull(), WAIT)
    oneStripOneInfo()
  })

  it('draws each exactly once, under one h1, once the board has data', async () => {
    api.loadTasksInState.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadAccountsBoard.mockResolvedValue(board([acct('a'), acct('b')]))
    render(<LlmProvidersScreen sub="plans" />)
    await screen.findByRole('table', { name: 'Plans' }, WAIT)
    oneStripOneInfo()
  })
})

describe('Screen’s `info` and `nav`', () => {
  it('draws `nav` right under the head while loading, and the (i) inside the head', () => {
    const { container } = render(
      <Screen title="T" load={() => new Promise(() => {})} info={<i data-testid="i" />} nav={<nav data-testid="strip" />}>
        {() => null}
      </Screen>,
    )
    const head = container.querySelector('.c-phead')!
    expect(head.nextElementSibling?.getAttribute('data-testid')).toBe('strip')
    expect(head.querySelector('.head [data-testid="i"]')).not.toBeNull()
  })

  it('draws nothing between the head and the skeleton with neither prop given', () => {
    const { container: bare } = render(
      <Screen title="T" load={() => new Promise(() => {})}>
        {() => null}
      </Screen>,
    )
    const head = bare.querySelector('.c-phead')!
    expect(head.querySelector('.head')!.children).toHaveLength(1)
    expect(head.nextElementSibling?.getAttribute('data-testid')).not.toBe('strip')
  })
})
