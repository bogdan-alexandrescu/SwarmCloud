// THE PROSE BUDGET FOR THE OPERATOR SCREENS, MEASURED RATHER THAN ASSERTED.
//
// The owner directive is "remove prose from views, just features, data and
// functionality ... no text everywhere". Every previous pass at that was
// reverted, because the honesty suites require the FACT of an absence to be on
// screen and the easiest way to satisfy them is a sentence. So this file does
// the only thing that keeps the directive honest over time: it MOUNTS each
// operator screen and counts the words it actually renders.
//
// WHY A CEILING AND NOT A SNAPSHOT. A snapshot of the exact count fails on
// every legitimate edit and gets updated without being read. A ceiling fails
// only when prose comes BACK, which is the regression this file exists to
// catch. The numbers in `BUDGET` are the measured counts at the time of the
// operator-screen rebuild plus a small margin, and each one is the count the
// commit message quotes.
//
// WHAT A "WORD" IS HERE, STATED EXACTLY, BECAUSE THE NUMBER IS ONLY WORTH
// WHAT ITS DEFINITION IS. A whitespace-separated token of the subtree's
// `textContent`, with the always-present help descriptions stripped (see
// `words`). Two consequences, and both are deliberate:
//
//   1. `textContent` puts NO separator between sibling elements, so a table row
//      of `<td>`s reads back as `READY2` -- one token. Data therefore costs
//      almost nothing in this metric and a SENTENCE, which carries its own
//      spaces, costs one token per word. That makes this a prose meter rather
//      than a literal word count, which is the thing worth gating.
//   2. It is measured with every help card CLOSED, so words that moved behind
//      a `?` are correctly not counted. They moved; that was the instruction.
//
// The pass that introduced this file took the four screens from 72 / 74 / 47 /
// 8 to 32 / 24 / 44 / 7 by that measure.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { ProbeRecord } from '../fetch'
import type { Capacity, QuotaState, Stats } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  setPoolLimit: vi.fn(),
  loadStats: vi.fn(),
  loadAdminQuota: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen } = await import('../AdminSettings')
const { PlatformCountsScreen } = await import('../PlatformCounts')
const { QuotaDetailScreen } = await import('../QuotaDetail')
const { DataSourceCells } = await import('../DataSources')

/**
 * Whitespace-separated tokens of the VISIBLE rendered subtree.
 *
 * `[data-help-description]` is stripped for the reason `HelpCardView` states
 * next to it: that node is in `textContent` whether the card is open or shut,
 * so counting it would score a screen for words nobody can see AND would
 * PENALISE the migration for doing exactly what it was asked to do -- moving a
 * paragraph behind a `?`. `honesty.prose.test.tsx:125` strips it by the same
 * attribute, and this is that function's convention applied to a count.
 */
function words(el: HTMLElement): number {
  const clone = el.cloneNode(true) as HTMLElement
  for (const n of [...clone.querySelectorAll('[data-help-description], style, script')]) {
    n.remove()
  }
  return (clone.textContent ?? '').split(/\s+/).filter(Boolean).length
}

// ---------------------------------------------------------------------------
// Fixtures. Small, but every shape the screens branch on is present: a paused
// pool, an uneditable pool kind, a partial stats response, an absent quota
// figure, and a 403 probe -- so the counts below include the absence markers
// rather than measuring only the happy path.
// ---------------------------------------------------------------------------

function capacity(): Capacity {
  const pool = (name: string, over: Partial<Capacity['pools'][number]> = {}) => ({
    name,
    hard_limit: 20,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 20,
    active: 4,
    available: 16,
    enabled: true,
    updated_at: '2026-09-22T10:00:00Z',
    ...over,
  })
  return {
    pools: [
      pool('global'),
      pool('provider:anthropic:tenant:u-bogdan', { hard_limit: 5, effective_limit: 5, active: 5 }),
      pool('runner:claude-code', { enabled: false }),
    ],
    runner_profiles: {
      'claude-code': {
        resource_class: 'standard',
        backend: 'cloudrun',
        provider: 'anthropic',
        units: 1,
        pools: ['global', 'provider:anthropic:tenant:u-bogdan'],
      },
      codex: {
        resource_class: 'standard',
        backend: 'cloudrun',
        provider: 'openai',
        units: 2,
        pools: ['global'],
      },
    },
    generated_at: '2026-09-22T10:00:00Z',
  } as Capacity
}

function stats(): Stats {
  return {
    tenant_id: 'eng',
    // RUNNING deliberately absent: the partial branch is the expensive one to
    // draw and the one this budget must not let regrow into a paragraph.
    tasks_by_state: {
      READY: 2, PARKED: 1, LEASED: 0, DISPATCHED: 0, STARTING: 1,
      SUCCEEDED: 40, FAILED: 5, CANCELLED: 2,
    },
    dispatch_paused: false,
    limits: {},
    generated_at: '2026-09-22T10:00:00Z',
  } as Stats
}

function quota(): QuotaState[] {
  const row = (over: Partial<QuotaState>): QuotaState => ({
    provider: 'anthropic',
    tenant_id: 'u-bogdan',
    state: 'HEALTHY',
    updated_at: '2026-09-22T10:00:00Z',
    configured_hard_max: 20,
    adaptive_target: null,
    quota_derived_limit: null,
    requests_remaining: 120,
    tokens_remaining: null,
    reset_at: null,
    cooldown_until: null,
    last_429_at: null,
    retry_after_seconds: null,
    success_count: 10,
    rate_limit_count: 0,
    effective_limit: 20,
    ...over,
  })
  return [
    row({}),
    // The two rows that carry an absence and a real zero respectively.
    row({ tenant_id: 'u-other', requests_remaining: null, state: 'UNKNOWN' }),
    row({ provider: 'openai', state: 'EXHAUSTED', effective_limit: 0, rate_limit_count: 7 }),
  ]
}

function probes(): ProbeRecord[] {
  return [
    { path: '/v1/capacity', lastStatus: 200, lastKind: null, lastLatencyMs: 31, lastAttemptAt: Date.now(), lastSuccessAt: Date.now() - 4000 },
    { path: '/v1/admin/quota', lastStatus: 403, lastKind: 'admin_required', lastLatencyMs: 12, lastAttemptAt: Date.now(), lastSuccessAt: null },
  ]
}

// ---------------------------------------------------------------------------
// The budget
// ---------------------------------------------------------------------------
//
// Measured on the rebuilt screens. Raising a number here is a decision to put
// words back on a view and must be argued in the commit that does it.

const BUDGET = {
  // Measured on the rebuilt screens against the fixtures below: 32 / 24 / 44 /
  // 7, against 72 / 74 / 47 / 8 before the pass. The margin is about a quarter
  // -- enough that renaming a column does not fail the gate, far too little to
  // fit a sentence back in.
  'Pool limits': 40,
  'Platform counts': 32,
  'Provider quota': 52,
  'Data sources': 10,
} as const

const measured: Record<string, number> = {}

describe('the operator screens carry data, not prose', () => {
  it('Pool limits stays inside its word budget', async () => {
    api.loadCapacity.mockResolvedValue({ status: 'ok', data: capacity(), fetchedAt: Date.now() })
    const { container } = render(<AdminSettingsScreen />)
    await waitFor(() => expect(container.querySelector('table')).not.toBeNull())
    measured['Pool limits'] = words(container)
    expect(measured['Pool limits']).toBeLessThanOrEqual(BUDGET['Pool limits'])
  })

  it('Platform counts stays inside its word budget', async () => {
    api.loadStats.mockResolvedValue({ status: 'ok', data: stats(), fetchedAt: Date.now() })
    const { container } = render(<PlatformCountsScreen />)
    screen.getByRole('button', { name: /count/i }).click()
    await waitFor(() => expect(container.querySelector('.ctl-util, .split-row')).not.toBeNull())
    measured['Platform counts'] = words(container)
    expect(measured['Platform counts']).toBeLessThanOrEqual(BUDGET['Platform counts'])
  })

  it('Provider quota stays inside its word budget', async () => {
    api.loadAdminQuota.mockResolvedValue({
      status: 'ok',
      data: { quota: quota() },
      fetchedAt: Date.now(),
    })
    const { container } = render(<QuotaDetailScreen />)
    await waitFor(() => expect(container.querySelector('table')).not.toBeNull())
    measured['Provider quota'] = words(container)
    expect(measured['Provider quota']).toBeLessThanOrEqual(BUDGET['Provider quota'])
  })

  it('the data-source cells stay inside their word budget', () => {
    const { container } = render(<DataSourceCells probes={probes()} />)
    measured['Data sources'] = words(container)
    expect(measured['Data sources']).toBeLessThanOrEqual(BUDGET['Data sources'])
  })

  it('reports what it measured, so the number in the report is this number', () => {
    // Printed rather than asserted: the assertions above are the gate, and this
    // is the evidence a reviewer reads without re-deriving it.
    // eslint-disable-next-line no-console
    console.log('[prose] rendered words:', JSON.stringify(measured))
    expect(Object.keys(measured).length).toBeGreaterThan(0)
  })
})
