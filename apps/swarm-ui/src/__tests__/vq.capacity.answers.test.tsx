// VISUAL QA PASS 1 (#2391), BGD'S ANSWERS OF 2026-10-26 08:20-08:30Z, LANE
// VQ-CAPACITY-BUILD (PR 2414).
//
//   VQ-031  "Needs a look, as the mock-up draws it". Pools' top group is
//           capacity.html A's `Needs a look`: every full, lowered, paused and
//           80%+ pool, each shown once, the group taking it out of its
//           family. It was `Needs action`, the Agents list's Waiting rule,
//           which left a full or lowered pool down in its family.
//   VQ-033  "Move the topics behind the page's one ?". Pools, Accounts and
//           Provider quota ended in a `Reading this screen:` row of
//           underlined links. The row is gone; the `?` beside each page's
//           title opens every topic it listed, and it is the page's one `?`.
//
// BREAK IT: file the top group by `needsAction` again (the full, lowered and
// near pools fall back into their families), put a `<HelpLinks>` back at the
// foot of any of the three pages, or drop a topic from a page's `help` list
// -- a case below turns red.

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import { HELP, type TopicId } from '../help'
import type { AccountsPage, Capacity, Pool, QuotaState } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadLeases: vi.fn(),
  loadAdminQuota: vi.fn(),
  loadAccountsBoard: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { QuotaDetailScreen } = await import('../QuotaDetail')
const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active,
    available: Math.max(0, limit - active),
    enabled: true,
    updated_at: '2026-10-26T08:30:00Z',
    ...over,
  }
}

/** One pool of each kind the group takes, and two it leaves in their families. */
function board(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-26T08:30:00Z',
    pools: [
      pool('global', 10, 100), // ok: stays in Global
      pool('tenant:eng', 20, 20), // full
      pool('provider:anthropic', 5, 50, { adaptive_target: 30, effective_limit: 30, available: 25 }), // lowered
      pool('runner:codex', 0, 10, { enabled: false }), // paused
      pool('backend:CLOUD_RUN_JOB', 85, 100), // 85%: near its ceiling
      pool('runner:claude-code', 3, 20), // ok: stays in Runner profiles
    ],
    runner_profiles: {},
  }
}

const LOOK = ['backend:CLOUD_RUN_JOB', 'provider:anthropic', 'runner:codex', 'tenant:eng']

async function renderPools(): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(board()))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

const rowsOf = (card: Element) => [...card.querySelectorAll('tbody tr')].map((r) => r.id.replace(/^pool-/, ''))

describe('VQ-031: Pools opens on Needs a look, as capacity.html A draws it', () => {
  it('heads the board with every full, lowered, paused and 80%+ pool', async () => {
    await renderPools()
    const first = document.querySelector('.cap-families > section')!
    expect(first.querySelector('.ctl-card-title')?.textContent).toBe('Needs a look · 4')
    expect(rowsOf(first).sort()).toEqual(LOOK)
    expect(screen.queryByRole('heading', { name: /Needs action/ })).toBeNull()
  })

  it('shows each pool once: the group takes it out of its family', async () => {
    await renderPools()
    for (const name of [...LOOK, 'global', 'runner:claude-code']) {
      expect(document.querySelectorAll(`[id="pool-${name}"]`).length, name).toBe(1)
    }
    const families = [...document.querySelectorAll('.cap-families > section')].slice(1)
    const inFamilies = families.flatMap(rowsOf)
    expect(inFamilies.sort()).toEqual(['global', 'runner:claude-code'])
  })
})

/** The `?` beside the page's `<h1>`, opened, and the topic anchors its card links. */
async function titleTopics(): Promise<string[]> {
  const head = document.querySelector('.c-phead')!
  const glyphs = within(head as HTMLElement).getAllByRole('button', { name: /^Help:/ })
  // THE PAGE'S ONE `?`: no second glyph grew beside the title.
  expect(glyphs.length).toBe(1)
  fireEvent.click(glyphs[0]!)
  const card = await screen.findByRole('dialog', {}, WAIT)
  return [...card.querySelectorAll('a[href^="#help/"]')].map((a) => a.getAttribute('href')!)
}

const anchors = (ids: readonly TopicId[]) => ids.map((id) => `#${HELP[id].anchor}`)

describe('VQ-033: the footer topics sit behind the page title’s one ?', () => {
  it('Pools: no footer, and the title’s ? opens every topic it had', async () => {
    await renderPools()
    expect(screen.queryByText('Reading this screen:')).toBeNull()
    expect(await titleTopics()).toEqual(
      anchors(['units-not-agents', 'pools-all-at-once', 'pool-freshness', 'tenant-scope', 'absent-vs-zero']),
    )
  })

  it('Provider quota: no footer, and the title’s ? opens every topic it had', async () => {
    const q = {
      provider: 'anthropic', tenant_id: 'eng', state: 'AVAILABLE', updated_at: new Date().toISOString(),
      configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
      tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
      retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50,
    } as QuotaState
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [q] }))
    api.loadCapacity.mockResolvedValue(ok(board()))
    render(<QuotaDetailScreen />)
    await screen.findAllByRole('rowheader', { name: 'eng' }, WAIT)
    expect(screen.queryByText('Reading this screen:')).toBeNull()
    expect(await titleTopics()).toEqual(
      anchors(['provider-quota-states', 'quota-row-fields', 'quota-document-absent', 'absent-vs-zero']),
    )
  })

  it('Accounts: no footer, and the title’s ? opens every topic it had', async () => {
    const now = Date.now()
    const accountsBoard = {
      page: { accounts: [], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: now,
    } as AccountsBoard
    api.loadAccountsBoard.mockResolvedValue(ok(accountsBoard))
    render(<AccountsScreen />)
    await screen.findByRole('heading', { level: 1, name: 'Accounts' }, WAIT)
    await waitFor(() => expect(document.querySelector('.acct-page')).not.toBeNull(), WAIT)
    expect(screen.queryByText('Reading this screen:')).toBeNull()
    expect(await titleTopics()).toEqual(
      anchors([
        'accounts-table-shape',
        'binding-window',
        'no-amber-band',
        'unreadable-documents',
        'skipped-for-this-tenant',
        'refresh-token-required',
        'advisory-vs-lease',
        'never-assigned-pool',
      ]),
    )
  })
})
