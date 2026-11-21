// ACCOUNTS: A REFRESH OUTCOME IS ONE SHORT LINE, ITS WHY BEHIND THE (i)
// (the owner's standing rule; issue 81). The audit read a 99-word paragraph
// under "Nothing could confirm what the secret holds" on this page. The
// outcome's heading and one line stay on the surface; every sentence that was
// drawn is still the (i)'s tip, word for word.
//
// BREAK IT: put the paragraph back in the panel, or drop a sentence from the
// tip -- a case below turns red.

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage, RefreshResponse } from '../types'

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

const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function board(): Result<AccountsBoard> {
  const account = {
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
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    serverAt: new Date().toISOString(),
    data: {
      page: { accounts: [account], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: Date.now(),
    } as AccountsBoard,
  }
}

function answered(reason: string): Result<RefreshResponse> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      refresh: { tenant_id: 'eng', provider: 'anthropic-subscription', refreshed: false, reason, expires_at: null },
    } as unknown as RefreshResponse,
  }
}

/** What a reader sees of `el`: the (i) tips' hidden text taken out. */
function surface(el: Element): string {
  const clone = el.cloneNode(true) as Element
  for (const n of [...clone.querySelectorAll('[data-help-description]')]) n.remove()
  return (clone.textContent ?? '').replace(/\s+/g, ' ').trim()
}

async function refreshWith(reason: string): Promise<HTMLElement> {
  api.loadAccountsBoard.mockResolvedValue(board())
  api.refreshAccount.mockResolvedValue(answered(reason))
  render(<AccountsScreen />)
  await waitFor(() => expect(document.querySelector('button.acct-open')).not.toBeNull(), WAIT)
  fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open')!)
  fireEvent.click(await screen.findByRole('button', { name: 'Refresh this account' }, WAIT))
  const status = await waitFor(() => {
    const el = document.querySelector<HTMLElement>('.acct-action .state[role="status"]')
    expect(el).not.toBeNull()
    return el!
  }, WAIT)
  return status
}

describe('a refresh outcome on Accounts draws one line and keeps its why behind the (i)', () => {
  it('unverified_skipped: the 99-word paragraph is a tip, not the panel', async () => {
    const panel = await refreshWith('unverified_skipped')
    const body = panel.querySelector('h3 + p')!
    // One short line on the surface: under nine words, one sentence.
    expect(surface(body)).toBe('The credential itself is fine.')
    // Every sentence that used to be drawn is still reachable, behind the (i).
    const tip = body.querySelector('[data-help-description]')!.textContent ?? ''
    expect(tip).toContain('With no evidence in either direction it declined to write')
    expect(tip).toContain('~1,700 identical versions')
    expect(tip).toContain('roles/secretmanager.secretAccessor')
    expect(tip).toContain('credential_publications')
    expect(body.querySelector('button[aria-label="About this outcome"]')).not.toBeNull()
  })

  it('an unknown reason is still printed as the broker wrote it, on the surface', async () => {
    const panel = await refreshWith('brand_new_reason')
    const body = panel.querySelector('h3 + p')!
    expect(surface(body)).toBe('Unrecognised reason: brand_new_reason')
    expect(body.querySelector('[data-help-description]')!.textContent).toContain('rather than guessed at')
  })
})
