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
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadStats: vi.fn(),
  // Platform counts reads the session to price its first run (AH-9).
  loadMe: vi.fn(),
  // And the workspace roster, once on mount, to price its Over time read
  // (COUNTS-PAGE, #2149). Refused here: the price stays per workspace.
  loadTenants: vi.fn(() =>
    Promise.resolve({
      status: 'error',
      error: { kind: 'admin_required', httpStatus: 403, code: null, message: 'not an admin' },
    }),
  ),
  loadAdminQuota: vi.fn(),
}))
vi.mock('../api', () => api)
// The over-time read, which Platform counts' one Run starts once its count
// shows an admin (COUNTS-ONE-RUN). It never answers here, so Over time's
// words are measured in the state the budget below was set in.
vi.mock('../platformTimeApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../platformTimeApi')>()),
  loadPlatformTime: vi.fn(() => new Promise(() => {})),
}))

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
function words(el: HTMLElement, without = ''): number {
  const clone = el.cloneNode(true) as HTMLElement
  const drop = ['[data-help-description]', 'style', 'script', ...(without === '' ? [] : [without])].join(', ')
  for (const n of [...clone.querySelectorAll(drop)]) {
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
    { path: '/v1/capacity', lastUrl: '/v1/capacity', lastStatus: 200, lastKind: null, lastLatencyMs: 31, lastAttemptAt: Date.now(), lastSuccessAt: Date.now() - 4000 },
    { path: '/v1/admin/quota', lastUrl: '/v1/admin/quota', lastStatus: 403, lastKind: 'admin_required', lastLatencyMs: 12, lastAttemptAt: Date.now(), lastSuccessAt: null },
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
  // RAISED 40 -> 42, 2026-10-19, BY THE OWNER'S #1481 DECISION that model
  // prices are a platform setting edited in the console's admin settings
  // (PRICES-UI). Measured 40 before it, at the ceiling. The Model prices card
  // costs two tokens here: its heading and the two-word "Not read" mark its
  // failed read is drawn as in this file's offline fixtures, both run into
  // their neighbours by `textContent`. The figures are a table and cost
  // nothing; no sentence was added.
  // RAISED 42 -> 60, 2026-10-25, BY BGD'S S10 RULE FOR EVERY ADMIN PAGE
  // (proposal E, config-e.html, picked for #2157; lane ADMIN-LEADS): "Every
  // Admin page opens with what needs an admin now, in one sentence with one
  // button". Measured 42 before it and 57 with it: this fixture's
  // `provider:anthropic:tenant:u-bogdan` is at 5 of 5, so the lead is its
  // longest branch -- the kicker, "Pool <name> is at 100% of its ceiling",
  // "5 of 5 units in use." and the Open button, 15 tokens. The green lead is 6.
  // The margin is three tokens: a renamed word fits, a second sentence does not.
  // RAISED 60 -> 67, 2026-10-28, BY BGD'S VISUAL QA PASS 1 (admin-shell-03,
  // part of #2596; lane QA1-FIX-04): the lead names the pool as its row does,
  // "anthropic · u-bogdan" (three tokens where the key was one), in the
  // headline and the button, with the key on a mono line of its own. Measured
  // 60 before (main, at the ceiling) and 64 with it: a name and an id, no
  // sentence. The margin stays three tokens.
  'Pool limits': 67,
  // RAISED 32 -> 46, 2026-10-25, BY BGD'S DECISION OF 2026-10-25 (COUNTS-ONE-RUN,
  // #2149): the page in mock-up F1's order, Over time first and the state
  // counts under F1's own heading, "Right now: agents in each state", with
  // its one line, "A snapshot, read by the same Run." -- twelve words that
  // say the counts are not part of the span above them and that the head's
  // one Run reads them. Measured 31 before and 43 with them; the margin is
  // three words, so a renamed label fits and a second sentence does not.
  'Platform counts': 46,
  // THE PLATFORM TICKS, MOVED ONTO PLATFORM COUNTS BY THE OWNER'S DECISION OF
  // 2026-10-13 (SCHED-ONE-PAGE: Admin › Schedules folded into Automate ›
  // Schedules, and its "Platform ticks" table came here with the same intro
  // sentence). Measured on their own, so the moved words neither hide inside
  // Platform counts' 32 nor raise it: 137 at the move -- the intro sentence
  // and each Cloud Scheduler job's own description, one per job in jobs.tf, so
  // it grows by about ten when a job is added. The margin is the quarter the
  // others have, less: a new job fits, a paragraph does not.
  // RAISED 165 -> 195, 2026-10-18: jobs.tf now holds 13 jobs, with
  // secrets_rotation_sweep and mcp_credentials_sweep each adding its row, and
  // measured 170 at that point. Each added word is a job's own description,
  // not a sentence of prose; the margin is the same as before, so one more
  // job fits and a paragraph still does not.
  'Platform ticks': 195,
  // OVER TIME, MOUNTED ON PLATFORM COUNTS BY COUNTS-PAGE (#2149, BGD's pick
  // of 2026-10-24, counts-a.html). Measured on its own, as the ticks are, so
  // its words neither hide inside Platform counts' 32 nor raise it: 196 at the
  // mount, before its Run, with the roster refused so the price reads per
  // workspace. Most of it is the not-run card that names what Run will draw
  // and what it costs, and the five tiles' labels and feet, each a fact
  // about a figure rather than a sentence of prose. The margin is under the
  // others' quarter on purpose: a renamed tile fits, a paragraph does not.
  'Over time': 205,
  // RAISED 52 -> 76, 2026-09-25, BY THE OWNER'S CP-8 / CP-9 / CP-10 DECISIONS
  // (#85), measured at 61 on CI after them. What the words are: a `Feeds pool`
  // column naming the pool each cap feeds, column names that carry their basis
  // and window (`Quota cap`, `429s (this run)`), the stale mark with its age on
  // every row older than twice the broker's interval -- all three fixture rows
  // are -- and `quota-row-fields` in the footer index. Each is a fact on the
  // row or a name on a column, not a sentence; the ceiling keeps the quarter
  // margin the others have.
  'Provider quota': 76,
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
    api.loadMe.mockResolvedValue({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: null, message: 'no session' },
    })
    const { container } = render(<PlatformCountsScreen />)
    screen.getByRole('button', { name: /count/i }).click()
    await waitFor(() => expect(container.querySelector('.ctl-util, .split-row')).not.toBeNull())
    measured['Platform counts'] = words(container, '.counts-ticks, .pot')
    expect(measured['Platform counts']).toBeLessThanOrEqual(BUDGET['Platform counts'])
    const ticks = container.querySelector<HTMLElement>('.counts-ticks')
    expect(ticks, 'the platform ticks section was not drawn, so its budget measured nothing').not.toBeNull()
    measured['Platform ticks'] = words(ticks!)
    expect(measured['Platform ticks']).toBeLessThanOrEqual(BUDGET['Platform ticks'])
    // MUTATION: drop <PlatformOverTime /> from the page, and this goes red
    // rather than measuring nothing.
    const over = container.querySelector<HTMLElement>('.pot')
    expect(over, 'the Over time section was not drawn, so its budget measured nothing').not.toBeNull()
    measured['Over time'] = words(over!)
    expect(measured['Over time']).toBeLessThanOrEqual(BUDGET['Over time'])
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
