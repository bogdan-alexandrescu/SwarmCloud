// ADMIN › PLATFORM COUNTS, OVER TIME (COUNTS-TIME-VIEW, #2149). BGD's
// decisions, 2026-10-23: the unit is agents counted on the day they were
// SUBMITTED, with their retries a second figure in the same bucket; the
// breakdowns are agent type, model, workspace and pool account; cost is
// "Estimated cost (list price, not a bill)", never $0 when absent, and a
// partial total is "at least $X" with "k of n agents reported". The page is
// read on a priced Run and never on its own.
//
// The client is mocked: `loadPlatformTime` is the one function that reads, so
// a test that counts its calls counts every request this view makes. The
// query builders are the real ones, so "the request" below is the query the
// route would be sent.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { PlatformTime, UsageBucket, UsageFigures, UsageGroupRow } from '../platformTimeApi'

const loadPlatformTime = vi.hoisted(() => vi.fn<(q: URLSearchParams) => Promise<Result<PlatformTime>>>())
vi.mock('../platformTimeApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../platformTimeApi')>()),
  loadPlatformTime,
}))

const { PlatformOverTime, forgetOverTime } = await import('../PlatformOverTime')

const LABEL = 'Estimated cost (list price, not a bill)'

/** One set of usage figures; every sum measured in full unless a test says otherwise. */
function figures(agents: number, retries: number, over: Partial<UsageFigures> = {}): UsageFigures {
  const attempts = agents + retries
  const tok = (sum: number | null, reporting = attempts) => ({ sum, reporting, attempts })
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
    cost: { sum_usd: agents * 1.5, estimated_usd: null, agents, reporting: agents },
    ...over,
  }
}

function bucket(start: string, end: string, f: UsageFigures, inProgress = false): UsageBucket {
  return { start, end, state: inProgress ? 'open' : 'sealed', in_progress: inProgress, unread_reason: null, ...f }
}

function payload(opts: {
  bucket?: 'day' | 'week' | 'month'
  buckets: UsageBucket[]
  by?: string
  rows?: UsageGroupRow[]
  totals?: UsageFigures
}): Result<PlatformTime> {
  const buckets = opts.buckets
  const totals = opts.totals ?? figures(
    buckets.reduce((n, b) => n + (b.agents ?? 0), 0),
    buckets.reduce((n, b) => n + (b.retries ?? 0), 0),
  )
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      scope: { kind: 'platform', tenants: ['eng', 'smoke', 'u-operator'], excluded: [], tenants_complete: true },
      tz: 'UTC',
      since: buckets[0]!.start,
      until: buckets[buckets.length - 1]!.end,
      bucket: opts.bucket ?? 'week',
      reads: 560,
      cached: false,
      generated_at: new Date().toISOString(),
      usage: {
        basis: 'created_at',
        cost_label: LABEL,
        buckets,
        totals: { complete: buckets.every((b) => b.state !== 'unread'), ...totals },
        groups: { by: opts.by ?? 'runner_profile', rows_total: (opts.rows ?? []).length, rows: opts.rows ?? [] },
      },
    },
  }
}

const WEEKS = payload({
  buckets: [
    bucket('2026-10-05T00:00:00Z', '2026-10-12T00:00:00Z', figures(410, 38)),
    bucket('2026-10-12T00:00:00Z', '2026-10-19T00:00:00Z', figures(413, 60)),
    bucket('2026-10-19T00:00:00Z', '2026-10-26T00:00:00Z', figures(343, 35), true),
  ],
})

async function run(): Promise<void> {
  fireEvent.click(screen.getByRole('button', { name: /^Run/ }))
  await waitFor(() => expect(screen.queryByText(/reading now/i)).toBeNull())
}

beforeEach(() => {
  loadPlatformTime.mockReset()
  forgetOverTime()
})

describe('Platform counts over time: nothing is read before Run', () => {
  it('requests nothing on mount, nor on choosing a bucket or a breakdown, and the button carries a price', () => {
    render(<PlatformOverTime workspaces={3} />)
    fireEvent.click(screen.getByRole('radio', { name: /Month/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Breakdown/ }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Pool account/ }))
    // MUTATION: call `loadPlatformTime` from a useEffect on mount, or on a
    // bucket or breakdown change -- the page is read on a priced press only.
    expect(loadPlatformTime).not.toHaveBeenCalled()
    // MUTATION: drop the price from the button, or print it beside it.
    expect(screen.getByRole('button', { name: /^Run/ }).textContent).toMatch(/~[\d,]+ reads/)
    // MUTATION: draw a bar or a zero before anything was read.
    expect(document.querySelectorAll('[data-bar="agents"]')).toHaveLength(0)
    expect(screen.getAllByText(/not run yet/i).length).toBeGreaterThan(0)
  })
})

describe('Platform counts over time: the request', () => {
  it('asks for submitted-basis usage in platform scope, and week or month changes the bucket and the span', async () => {
    loadPlatformTime.mockResolvedValue(WEEKS)
    render(<PlatformOverTime workspaces={3} />)

    fireEvent.click(screen.getByRole('radio', { name: /Day/ }))
    await run()
    const day = loadPlatformTime.mock.calls[0]![0]
    // MUTATION: send usage=ended, or leave usage off (the response then carries no usage block).
    expect(day.get('usage')).toBe('submitted')
    // MUTATION: drop scope=platform: an admin's view would read their own tenant only.
    expect(day.get('scope')).toBe('platform')
    // MUTATION: send no tz -- buckets are the viewer's days, as the ledger's are.
    expect(day.get('tz')).toBeTruthy()
    // MUTATION: day over anything other than 30 days.
    expect(day.get('bucket')).toBe('day')
    expect(day.get('span')).toBe('30d')

    fireEvent.click(screen.getByRole('radio', { name: /Week/ }))
    await run()
    const week = loadPlatformTime.mock.calls[1]![0]
    // MUTATION: keep sending bucket=day after Week is chosen.
    expect(week.get('bucket')).toBe('week')
    expect(week.get('span')).toBeNull()
    const weekDays = (Date.now() - Date.parse(`${week.get('since')}T00:00:00Z`)) / 86_400_000
    // 26 weeks: the since is 25 weeks before this week (the route floors it to a Monday).
    // MUTATION: a since of 26 weeks back, or 30 days back.
    expect(weekDays).toBeGreaterThanOrEqual(174)
    expect(weekDays).toBeLessThan(176)

    fireEvent.click(screen.getByRole('radio', { name: /Month/ }))
    await run()
    const month = loadPlatformTime.mock.calls[2]![0]
    // MUTATION: keep the week bucket for Month.
    expect(month.get('bucket')).toBe('month')
    // 12 months: the first of the month eleven months back.
    // MUTATION: since a year and a day back, which is 13 month buckets.
    expect(month.get('since')).toMatch(/^\d{4}-\d{2}-01$/)
    const monthDays = (Date.now() - Date.parse(`${month.get('since')}T00:00:00Z`)) / 86_400_000
    expect(monthDays).toBeGreaterThan(330)
    expect(monthDays).toBeLessThan(367)
  })

  it('asks for the chosen breakdown as the route’s group', async () => {
    loadPlatformTime.mockResolvedValue(WEEKS)
    render(<PlatformOverTime workspaces={3} />)
    fireEvent.click(screen.getByRole('button', { name: /^Breakdown/ }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Pool account/ }))
    await run()
    // MUTATION: map Pool account to any group but pool_account.
    expect(loadPlatformTime.mock.calls[0]![0].get('group')).toBe('pool_account')
  })
})

describe('Platform counts over time: the figure', () => {
  it('draws agents per bucket as bars, with retries apart, in each drawing', async () => {
    loadPlatformTime.mockResolvedValue(WEEKS)
    render(<PlatformOverTime workspaces={3} />)
    await run()
    const wide = document.querySelector('.pot-agents svg.is-wide')!
    expect(wide, 'no wide drawing').not.toBeNull()
    // MUTATION: drop a bucket's bar, or draw one bar per group instead of per bucket.
    expect(wide.querySelectorAll('[data-bar="agents"]')).toHaveLength(3)
    // MUTATION: drop the retries marks, or stack them into the agents bar.
    expect(wide.querySelectorAll('[data-bar="retries"]')).toHaveLength(3)
    // The values each bar carries, in bucket order.
    // MUTATION: draw retries as agents, or attempts as agents.
    expect([...wide.querySelectorAll('[data-bar="agents"]')].map((b) => b.getAttribute('data-value'))).toEqual(['410', '413', '343'])
    expect([...wide.querySelectorAll('[data-bar="retries"]')].map((b) => b.getAttribute('data-value'))).toEqual(['38', '60', '35'])
    // The taller bar is the larger count, on one scale.
    const h = (i: number) => Number(wide.querySelectorAll('[data-bar="agents"]')[i]!.getAttribute('height'))
    expect(h(1)).toBeGreaterThan(h(2))
    // MUTATION: draw the 640-unit figure only (the 300-unit one is the phone's).
    expect(document.querySelector('.pot-agents svg.is-narrow')).not.toBeNull()
    // A legend names both series.
    const legend = document.querySelector('.pot-agents .pot-legend')!
    expect(legend.textContent).toMatch(/agents/i)
    expect(legend.textContent).toMatch(/retries/i)
  })

  it('draws an unread bucket as no bar and no digit, never a zero', async () => {
    const unread: UsageBucket = {
      start: '2026-10-12T00:00:00Z', end: '2026-10-19T00:00:00Z', state: 'unread', in_progress: false,
      unread_reason: 'read_failed', agents: null, agents_ended: null, retries: null, attempts: null,
      run_s: null, tokens: null, cost: null,
    }
    loadPlatformTime.mockResolvedValue(payload({
      buckets: [bucket('2026-10-05T00:00:00Z', '2026-10-12T00:00:00Z', figures(410, 38)), unread],
      totals: figures(410, 38),
    }))
    render(<PlatformOverTime workspaces={3} />)
    await run()
    const wide = document.querySelector('.pot-agents svg.is-wide')!
    // MUTATION: draw the unread bucket's bar at 0 height.
    expect(wide.querySelectorAll('[data-bar="agents"]')).toHaveLength(1)
    expect(wide.querySelectorAll('[data-unread]').length).toBeGreaterThan(0)
  })
})

describe('Platform counts over time: the table', () => {
  it('renders one row per breakdown group, and a total', async () => {
    loadPlatformTime.mockResolvedValue(payload({
      buckets: WEEKS.status === 'ok' ? WEEKS.data.usage.buckets : [],
      by: 'runner_profile',
      rows: [
        { key: 'claude-code', ...figures(600, 80) },
        { key: 'indexer', ...figures(400, 40) },
        { key: 'mock', ...figures(166, 13) },
      ],
    }))
    render(<PlatformOverTime workspaces={3} />)
    fireEvent.click(screen.getByRole('button', { name: /^Breakdown/ }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Agent type/ }))
    await run()
    const table = screen.getByRole('table', { name: /by agent type/i })
    const rows = within(table).getAllByRole('row').filter((r) => r.closest('tbody'))
    // MUTATION: drop a group's row, or draw the groups as buckets.
    expect(rows.map((r) => r.getAttribute('data-key'))).toEqual(['claude-code', 'indexer', 'mock', '__total'])
    // MUTATION: a total that is not the served totals.
    expect(within(rows[3]!).getAllByRole('cell')[0]!.textContent).toBe('1,166')
  })

  it('reads a null cost as "not reported" and a partial one as "at least", with its coverage', async () => {
    loadPlatformTime.mockResolvedValue(payload({
      buckets: WEEKS.status === 'ok' ? WEEKS.data.usage.buckets : [],
      by: 'runner_profile',
      rows: [
        { key: 'claude-code', ...figures(600, 80, { cost: { sum_usd: 812.4, estimated_usd: 3.1, agents: 600, reporting: 597 } }) },
        { key: 'generic', ...figures(10, 0, { cost: { sum_usd: null, estimated_usd: null, agents: 10, reporting: 0 } }) },
      ],
    }))
    render(<PlatformOverTime workspaces={3} />)
    fireEvent.click(screen.getByRole('button', { name: /^Breakdown/ }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Agent type/ }))
    await run()
    const table = screen.getByRole('table', { name: /by agent type/i })
    const cost = (key: string) => table.querySelector(`tr[data-key="${key}"] [data-col="cost"]`)!.textContent ?? ''
    // MUTATION: print `$0.00` (or a bare dash) for a cost nobody reported.
    expect(cost('generic')).toContain('not reported')
    expect(cost('generic')).not.toContain('$0')
    // MUTATION: print a partial sum as if it were whole.
    expect(cost('claude-code')).toContain('at least $812')
    // MUTATION: drop the coverage beside the figure.
    expect(cost('claude-code')).toContain('597 of 600 agents reported')
    // MUTATION: hide the stopped-run share, or add it to the sum twice.
    expect(cost('claude-code')).toMatch(/\$3\.10 estimated from a stopped run/)
  })

  it('labels cost with exactly BGD’s words', async () => {
    loadPlatformTime.mockResolvedValue(WEEKS)
    render(<PlatformOverTime workspaces={3} />)
    await run()
    // MUTATION: "Reported cost", "Cost", or the label with any word changed.
    const exact = screen.getAllByText((_, el) => el !== null && el.children.length === 0 && el.textContent === LABEL)
    expect(exact.length).toBeGreaterThan(0)
  })
})
