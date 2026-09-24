// THE PROSE BUDGET, AS A NUMBER.
//
// The owner's directive is "remove prose from views, just features, data and
// functionality ... no text everywhere". Every previous pass at that was
// argued in adjectives and reverted, so this file makes the claim countable:
// it mounts the real screen in jsdom, reads what a reader would actually SEE,
// and holds the word count under a ceiling.
//
// WHAT COUNTS AS A WORD A READER SEES. The same exclusions
// `honesty.prose.test.tsx` makes, and for the same reasons:
//
//   * `[data-help-description]` is the visually-hidden copy every `<HelpCard>`
//     renders so assistive technology gets the explanation at the label. It is
//     in `textContent` whether the card is open or shut, so counting it would
//     report that nothing moved.
//   * `<style>` and `<script>` are not read by anyone.
//
// An `aria-label` is an ATTRIBUTE and is therefore not counted here. That is
// deliberate and it is the whole mechanism of the migration: the invariant
// stays perceivable as a visual encoding on the surface, and the sentence that
// used to carry it moves to the accessible name and the `?` topic. This file
// counts the surface; `honesty.prose.test.tsx` is what proves the invariant
// still reads without hovering anything.
//
// THE CEILING IS A RATCHET, NOT A TARGET. It exists so the next well-meaning
// paragraph has to argue with a failing test instead of with a reviewer.

import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { SpendRollup } from '../api'
import type { Account, AccountsPage, Capacity, Stats, TaskPage } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
}))
vi.mock('../api', () => api)

const { OverviewScreen } = await import('../Overview')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-23T10:00:00Z' }
}

function account(over: Partial<Account>): Account {
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
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

const NEVER_POLLED = account({ account_id: 'eng:never', label: 'never' })
const MEASURED_ZERO = account({
  account_id: 'eng:fresh',
  label: 'fresh',
  observed_at: new Date().toISOString(),
  windows: {
    five_hour: { utilization: 0, resets_at: '2099-01-01T00:00:00Z', reset: false },
    seven_day: { utilization: 0, resets_at: '2099-01-01T00:00:00Z', reset: false },
  },
})

const EMPTY_TASKS: TaskPage = { tasks: [], next_page_token: null }
const EMPTY_STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: {},
  dispatch_paused: false,
  limits: {},
  generated_at: '2026-09-23T10:00:00Z',
}
const CAPACITY: Capacity = {
  pools: [],
  runner_profiles: {},
  tenant_id: 'eng',
  generated_at: '2026-09-23T10:00:00Z',
}

function spend(over: Partial<SpendRollup> = {}): SpendRollup {
  return {
    tenantId: 'eng',
    tasksOnPage: 4,
    tasksWithAttempts: 4,
    tasksSampled: 4,
    failedReads: 0,
    failedDetail: null,
    attempts: 4,
    attemptsWithCost: 0,
    attemptsWithTokens: 0,
    costUsd: null,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheCreationTokens: null,
    from: '2026-09-23T09:00:00Z',
    to: '2026-09-23T10:00:00Z',
    ...over,
  }
}

function accountsPage(accounts: Account[]): AccountsPage {
  return { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 }
}

/** What a reader can see, with the help copy and the stylesheet taken out. */
function visibleWords(): number {
  const clone = document.body.cloneNode(true) as HTMLElement
  for (const el of [...clone.querySelectorAll('[data-help-description], style, script')]) {
    el.remove()
  }
  const text = (clone.textContent ?? '').replace(/\s+/g, ' ').trim()
  return text === '' ? 0 : text.split(' ').length
}

async function settle(): Promise<void> {
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
}

describe('the prose budget', () => {
  it('holds Overview under its word ceiling on the healthy path', async () => {
    api.loadCapacity.mockResolvedValue(ok(CAPACITY))
    api.loadTasks.mockResolvedValue(ok(EMPTY_TASKS))
    api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadStats.mockResolvedValue(ok(EMPTY_STATS))
    api.loadAccountPool.mockResolvedValue(ok(accountsPage([NEVER_POLLED, MEASURED_ZERO])))
    api.loadSpend.mockResolvedValue(ok(spend()))

    render(<OverviewScreen />)
    await settle()

    const words = visibleWords()
    console.log(`OVERVIEW RENDERED WORDS: ${words}`)
    expect(words).toBeLessThanOrEqual(OVERVIEW_CEILING)
  })
})

/**
 * The ceiling, and where it came from.
 *
 * MEASURED, both numbers, on this exact fixture: the screen this pass replaced
 * rendered 309 words and the rebuilt one renders 64. 110 is that figure with
 * room for a fixture that lists a few more rows, a longer tenant id and a
 * couple of problem headlines -- and deliberately NOT room for another
 * paragraph, which is the smallest thing this is meant to stop.
 *
 * WHAT THE 245 WORDS WERE. The eight check notes joined with ' · ' (about 90
 * of them, on the healthy path, saying nothing was wrong eight different
 * ways), five tile `sub` lines defining their own terms, four empty-state
 * paragraphs and four provenance paragraphs under the spend figures. None of
 * them was a measurement; every one of them shipped unconditionally on the
 * landing screen.
 */
const OVERVIEW_CEILING = 110
