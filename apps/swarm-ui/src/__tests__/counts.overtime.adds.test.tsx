// ADMIN › PLATFORM COUNTS, OVER TIME, REDRAWN TO THE ROUTE'S ADDITIONS
// (COUNTS-VIEW-ADDS, #2149). BGD's answers to the view's questions,
// 2026-10-24 17:26-17:30Z, each held here:
//
//   1. Per-bucket group rows: with a breakdown picked, the agents chart is
//      mock-up A's stacked area (F3, F4) -- five series plus "all others",
//      retries apart on their own scale -- with the split table beside it. An
//      unknown ("not recorded", "not ended") is never a stacked layer: it is
//      hatched, and a row of the table.
//   2. `cost.estimated`: every cost figure that names estimates reads "n
//      estimated from a stopped run ($X)", as F2's cost tile does.
//   3. Every grouping in one read: picking a breakdown redraws at once from
//      the read already made and never shows the not-run card.
//   4. "holds no account": the pool-account breakdown shows it and "account
//      not known yet" as separate rows, as F4 does.
//   5. A `run` prop lets COUNTS-PAGE's one shared Run trigger this view.
//
// The client is mocked: `loadPlatformTime` is the one function that reads.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { PlatformTime, UsageBucket, UsageFigures, UsageGrouping, UsageSeries } from '../platformTimeApi'

const loadPlatformTime = vi.hoisted(() => vi.fn<(q: URLSearchParams) => Promise<Result<PlatformTime>>>())
vi.mock('../platformTimeApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../platformTimeApi')>()),
  loadPlatformTime,
}))

const { PlatformOverTime, forgetOverTime } = await import('../PlatformOverTime')

function figures(agents: number, retries: number, over: Partial<UsageFigures> = {}): UsageFigures {
  const attempts = agents + retries
  const tok = (sum: number | null) => ({ sum, reporting: attempts, attempts })
  return {
    agents,
    agents_ended: agents,
    retries,
    attempts,
    run_s: { sum: attempts * 1800, measured: attempts, attempts },
    tokens: {
      input_tokens: tok(attempts * 1000),
      output_tokens: tok(attempts * 200),
      cache_read_input_tokens: tok(attempts * 5000),
      cache_creation_input_tokens: tok(attempts * 300),
    },
    cost: { sum_usd: agents * 1.5, estimated_usd: null, estimated: 0, agents, reporting: agents },
    ...over,
  }
}

function bucket(start: string, end: string, f: UsageFigures, inProgress = false): UsageBucket {
  return { start, end, state: inProgress ? 'open' : 'sealed', in_progress: inProgress, unread_reason: null, ...f }
}

function series(key: string, agents: number[], retries: number[], measured = true, other = false): UsageSeries {
  return { key, measured, other, keys: other ? 2 : 1, agents, retries }
}

/** A grouping whose rows are the span sums of its series, so the two always agree. */
function grouping(by: string, list: UsageSeries[]): UsageGrouping {
  const rows = list.map((s) => ({ key: s.key, ...figures(s.agents.reduce<number>((n, v) => n + (v ?? 0), 0), s.retries.reduce<number>((n, v) => n + (v ?? 0), 0)) }))
  return { by, rows_total: rows.length, rows, series: list }
}

// Three weeks: 100, 120 and 60 agents (the newest in progress); 10, 12 and 6 retries.
const BUCKETS = [
  bucket('2026-10-05T00:00:00Z', '2026-10-12T00:00:00Z', figures(100, 10)),
  bucket('2026-10-12T00:00:00Z', '2026-10-19T00:00:00Z', figures(120, 12)),
  bucket('2026-10-19T00:00:00Z', '2026-10-26T00:00:00Z', figures(60, 6), true),
]

const BY_PROFILE = grouping('runner_profile', [
  series('claude-code', [50, 60, 30], [5, 6, 3]),
  series('claude-code-large', [20, 25, 12], [2, 3, 1]),
  series('indexer', [12, 14, 7], [1, 1, 1]),
  series('browser', [8, 9, 5], [1, 1, 0]),
  series('mock', [6, 7, 3], [1, 1, 1]),
  series('all others', [4, 5, 3], [0, 0, 0], true, true),
])

const BY_MODEL = grouping('model', [
  series('claude-sonnet-5', [70, 80, 30], [7, 8, 3]),
  series('claude-opus-5-5', [25, 32, 10], [3, 4, 1]),
  series('not ended', [5, 8, 20], [0, 0, 2], false),
])

const BY_TENANT = grouping('tenant_id', [
  series('eng', [80, 90, 45], [8, 9, 5]),
  series('smoke', [20, 30, 15], [2, 3, 1]),
])

const BY_ACCOUNT = grouping('pool_account', [
  series('acct-claude-01', [40, 50, 20], [4, 5, 2]),
  series('acct-claude-02', [30, 35, 15], [3, 4, 2]),
  series('holds no account', [20, 22, 10], [2, 2, 1]),
  series('not recorded', [6, 5, 3], [1, 1, 0], false),
  series('not ended', [4, 8, 12], [0, 0, 1], false),
])

function payload(over: Partial<UsageFigures> = {}): Result<PlatformTime> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      scope: { kind: 'platform', tenants: ['eng', 'smoke'], excluded: [], tenants_complete: true },
      tz: 'UTC',
      since: BUCKETS[0]!.start,
      until: BUCKETS[2]!.end,
      bucket: 'week',
      reads: 560,
      cached: false,
      generated_at: new Date().toISOString(),
      usage: {
        basis: 'created_at',
        cost_label: 'Estimated cost (list price, not a bill)',
        buckets: BUCKETS,
        totals: { complete: true, ...figures(280, 28, over) },
        groups: BY_PROFILE,
        breakdowns: { runner_profile: BY_PROFILE, model: BY_MODEL, tenant_id: BY_TENANT, pool_account: BY_ACCOUNT },
      },
    },
  }
}

async function run(): Promise<void> {
  fireEvent.click(screen.getByRole('button', { name: /^Run/ }))
  await waitFor(() => expect(screen.queryByText(/reading now/i)).toBeNull())
}

function pick(name: RegExp): void {
  fireEvent.click(screen.getByRole('button', { name: /^Breakdown/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name }))
}

/** The split table beside the stacked area: its rows' keys, top of the stack first. */
function splitKeys(): string[] {
  const table = screen.getByRole('table', { name: /layers/i })
  return within(table).getAllByRole('row').filter((r) => r.closest('tbody')).map((r) => r.getAttribute('data-key') ?? '')
}

beforeEach(() => {
  loadPlatformTime.mockReset()
  forgetOverTime()
})

describe('answer 3: every breakdown comes with the one read', () => {
  it('redraws at once on a breakdown picked after Run, with no second read and no not-run card', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    await run()
    for (const name of [/Agent type/, /Model/, /Workspace/, /Pool account/, /None/]) {
      pick(name)
      // MUTATION: key the session's reads by bucket AND breakdown, so a new
      // breakdown finds no read and shows "Not run yet".
      expect(screen.queryByText(/not run yet/i)).toBeNull()
      expect(document.querySelector('.pot-agents')).not.toBeNull()
    }
    // MUTATION: read again on a breakdown change.
    expect(loadPlatformTime).toHaveBeenCalledTimes(1)
  })

  it('sends the same query whatever breakdown is picked, so the read is one', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    pick(/Pool account/)
    await run()
    pick(/Model/)
    await run()
    const [a, b] = loadPlatformTime.mock.calls.map((c) => c[0].toString())
    // MUTATION: send the picked breakdown as `group`: two cache keys client-side for one fold.
    expect(a).toBe(b)
  })

  it('says a breakdown costs no read', () => {
    render(<PlatformOverTime workspaces={2} />)
    // MUTATION: keep "another is its own read", which was true before the route served every grouping.
    expect(screen.getByText(/A breakdown costs no read: it groups the same days/)).toBeTruthy()
    expect(screen.queryByText(/another is its own read/)).toBeNull()
  })
})

describe('answer 1: a breakdown stacks the agents chart, unknowns hatched', () => {
  it('stacks one layer per measured series, in the served order, and hatches the unknown ones', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    await run()
    pick(/Model/)
    const wide = document.querySelector('.pot-agents svg.is-wide')!
    const layers = [...wide.querySelectorAll('[data-layer]')]
    // MUTATION: draw the totals as bars with a breakdown picked (the chart before answer 1).
    expect(wide.querySelectorAll('[data-bar="agents"]')).toHaveLength(0)
    // MUTATION: stack "not ended" as a coloured layer of its own.
    expect(layers.map((l) => l.getAttribute('data-layer'))).toEqual(['claude-sonnet-5', 'claude-opus-5-5'])
    const unknown = [...wide.querySelectorAll('[data-unknown]')]
    expect(unknown.length).toBeGreaterThan(0)
    // MUTATION: fill the unknown layer with a series colour rather than the hatch.
    for (const u of unknown) expect(u.getAttribute('fill') ?? '').toMatch(/^url\(#/)
    // Retries stay apart, as totals on their own lane.
    // MUTATION: drop the retries lane under a breakdown.
    expect([...wide.querySelectorAll('[data-bar="retries"]')].map((r) => r.getAttribute('data-value'))).toEqual(['10', '12', '6'])
  })

  it('stacks five series plus "all others", the layers adding up to each bucket', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    await run()
    pick(/Agent type/)
    const wide = document.querySelector('.pot-agents svg.is-wide')!
    const keys = [...wide.querySelectorAll('[data-layer]')].map((l) => l.getAttribute('data-layer'))
    // MUTATION: drop "all others", or cap the named series at four.
    expect(keys).toEqual(['claude-code', 'claude-code-large', 'indexer', 'browser', 'mock', 'all others'])
    // The top of the stack in a bucket is that bucket's agents.
    // MUTATION: stack each series from zero instead of on the one below.
    const tops = [...wide.querySelectorAll('[data-stack-top]')].map((t) => t.getAttribute('data-stack-top'))
    expect(tops).toEqual(['100', '120', '60'])
  })

  it('puts the split table beside the chart: top of the stack first, a share each, and all agents', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    await run()
    pick(/Agent type/)
    // MUTATION: list the layers bottom first, which reads against the stack beside it.
    expect(splitKeys()).toEqual(['all others', 'mock', 'browser', 'indexer', 'claude-code-large', 'claude-code', '__total'])
    const table = screen.getByRole('table', { name: /layers/i })
    const row = (key: string) => table.querySelector(`tr[data-key="${key}"]`)!.textContent ?? ''
    // 140 of 280 agents.
    // MUTATION: share of the newest bucket, or of the named series only.
    expect(row('claude-code')).toContain('140')
    expect(row('claude-code')).toContain('50%')
    // MUTATION: a total that is not the span's agents.
    expect(row('__total')).toContain('280')
  })

  it('draws no split table and the bars with no breakdown', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    await run()
    expect(screen.queryByRole('table', { name: /layers/i })).toBeNull()
    expect(document.querySelector('.pot-agents svg.is-wide')!.querySelectorAll('[data-bar="agents"]')).toHaveLength(3)
  })
})

describe('answer 4: "holds no account" apart from "account not known yet"', () => {
  it('lists both in the pool-account split, the first stacked and the second hatched', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    await run()
    pick(/Pool account/)
    const table = screen.getByRole('table', { name: /layers/i })
    // MUTATION: fold "holds no account" into "not recorded", as the route did before answer 4.
    expect(table.querySelector('tr[data-key="holds no account"]')!.textContent).toContain('holds no account')
    expect(table.querySelector('tr[data-key="not ended"]')!.textContent).toContain('account not known yet')
    const wide = document.querySelector('.pot-agents svg.is-wide')!
    const stacked = [...wide.querySelectorAll('[data-layer]')].map((l) => l.getAttribute('data-layer'))
    // MUTATION: hatch "holds no account": it is a fact, not an unknown.
    expect(stacked).toContain('holds no account')
    expect(stacked).not.toContain('not ended')
    expect(stacked).not.toContain('not recorded')
  })

  it('lists them apart in the breakdown table too', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    await run()
    pick(/Pool account/)
    const table = screen.getByRole('table', { name: /Breakdown by pool account/ })
    const keys = within(table).getAllByRole('row').filter((r) => r.closest('tbody')).map((r) => r.getAttribute('data-key'))
    // MUTATION: draw the requested `groups` (agent type) under a pool-account pick.
    expect(keys).toEqual(['acct-claude-01', 'acct-claude-02', 'holds no account', 'not recorded', 'not ended', '__total'])
  })
})

describe('answer 2: "n estimated from a stopped run ($X)"', () => {
  it('names the count beside the dollars on the cost tile and the cost card', async () => {
    loadPlatformTime.mockResolvedValue(payload({ cost: { sum_usd: 400, estimated_usd: 56, estimated: 55, agents: 280, reporting: 270 } }))
    render(<PlatformOverTime workspaces={2} />)
    await run()
    const tile = document.querySelector('[data-tile="Estimated cost"]')!.textContent ?? ''
    // MUTATION: print the dollars alone ("$56.00 estimated from a stopped run").
    expect(tile).toContain('55 estimated from a stopped run ($56.00)')
    expect(document.querySelector('.pot-cost .pot-card-sub')!.textContent).toContain('55 estimated from a stopped run ($56.00)')
  })

  it('says nothing of estimates when none was estimated', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    render(<PlatformOverTime workspaces={2} />)
    await run()
    // MUTATION: print "0 estimated from a stopped run".
    expect(document.querySelector('[data-tile="Estimated cost"]')!.textContent).not.toMatch(/estimated from a stopped run/)
  })
})

describe('the run prop: the page’s one shared Run', () => {
  it('reads nothing on mount, and reads once each time the page’s Run moves it', async () => {
    loadPlatformTime.mockResolvedValue(payload())
    const view = render(<PlatformOverTime workspaces={2} run={0} />)
    // MUTATION: read on mount because `run` is set.
    expect(loadPlatformTime).not.toHaveBeenCalled()
    view.rerender(<PlatformOverTime workspaces={2} run={1} />)
    await waitFor(() => expect(loadPlatformTime).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByText(/reading now/i)).toBeNull())
    // A re-render with the same value is not a press.
    // MUTATION: read on every render.
    view.rerender(<PlatformOverTime workspaces={3} run={1} />)
    expect(loadPlatformTime).toHaveBeenCalledTimes(1)
    view.rerender(<PlatformOverTime workspaces={3} run={2} />)
    await waitFor(() => expect(loadPlatformTime).toHaveBeenCalledTimes(2))
  })
})
