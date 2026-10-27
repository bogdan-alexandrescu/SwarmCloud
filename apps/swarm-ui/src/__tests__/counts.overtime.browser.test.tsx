// ADMIN › PLATFORM COUNTS: UNUSED BROWSER TIME (lane BROWSER-USE-UI, part of
// #2322). BROWSER-USE-FLAG serves `browser` on every usage row of
// `GET /v1/outcomes?usage=...` (span, bucket and group): the ended agents on
// a browser runtime, their run seconds, and the part whose agent the worker
// said did not use the browser. BGD's decision, 2026-10-25: unused browser
// hours are visible, beside the run-time figures, for the selected range and
// per bucket, linked to the Agents list of the browser agents that did not
// use it. A missing field is a dash with its reason, never 0 h.
//
// The client is mocked as counts.overtime.test.tsx mocks it: `loadPlatformTime`
// is the one function this view reads through.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { BrowserUsage, PlatformTime, UsageBucket, UsageFigures } from '../platformTimeApi'
import { painted } from './marks'

const loadPlatformTime = vi.hoisted(() => vi.fn<(q: URLSearchParams) => Promise<Result<PlatformTime>>>())
vi.mock('../platformTimeApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../platformTimeApi')>()),
  loadPlatformTime,
}))

const { PlatformOverTime, forgetOverTime } = await import('../PlatformOverTime')

function browser(over: Partial<BrowserUsage> = {}): BrowserUsage {
  return { agents: 40, run_s: 40 * 3600, measured: 40, unused_agents: 9, unused_s: 9 * 3600, unknown_agents: 0, ...over }
}

function figures(agents: number, b: BrowserUsage | null | undefined): UsageFigures {
  const tok = (sum: number) => ({ sum, reporting: agents, attempts: agents })
  const f: UsageFigures = {
    agents,
    agents_ended: agents,
    retries: 0,
    attempts: agents,
    run_s: { sum: agents * 1800, measured: agents, attempts: agents },
    tokens: { input_tokens: tok(agents), output_tokens: tok(agents), cache_read_input_tokens: tok(agents), cache_creation_input_tokens: tok(agents) },
    cost: { sum_usd: agents, estimated_usd: null, estimated: 0, agents, reporting: agents },
  }
  return b === undefined ? f : { ...f, browser: b }
}

function bucket(start: string, end: string, f: UsageFigures): UsageBucket {
  return { start, end, state: 'sealed', in_progress: false, unread_reason: null, ...f }
}

function payload(totals: UsageFigures, buckets: UsageBucket[]): Result<PlatformTime> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      scope: { kind: 'platform', tenants: ['eng'], excluded: [], tenants_complete: true },
      tz: 'UTC',
      since: buckets[0]!.start,
      until: buckets[buckets.length - 1]!.end,
      bucket: 'week',
      reads: 10,
      cached: false,
      generated_at: new Date().toISOString(),
      usage: {
        basis: 'created_at',
        cost_label: 'Estimated cost (list price, not a bill)',
        buckets,
        totals: { complete: true, ...totals },
        groups: { by: 'runner_profile', rows_total: 0, rows: [], series: [] },
        breakdowns: {
          runner_profile: { by: 'runner_profile', rows_total: 0, rows: [], series: [] },
          model: { by: 'model', rows_total: 0, rows: [], series: [] },
          tenant_id: { by: 'tenant_id', rows_total: 0, rows: [], series: [] },
          pool_account: { by: 'pool_account', rows_total: 0, rows: [], series: [] },
        },
      },
    },
  }
}

const W1 = ['2026-10-12T00:00:00Z', '2026-10-19T00:00:00Z'] as const
const W2 = ['2026-10-19T00:00:00Z', '2026-10-26T00:00:00Z'] as const

async function run(): Promise<void> {
  fireEvent.click(screen.getByRole('button', { name: /^Run/ }))
  await waitFor(() => expect(document.querySelector('.pot-tiles')).not.toBeNull())
}

function tile(): HTMLElement {
  const t = document.querySelector<HTMLElement>('[data-tile="Unused browser time"]')
  expect(t, 'no Unused browser time tile').not.toBeNull()
  return t!
}

beforeEach(() => {
  loadPlatformTime.mockReset()
  forgetOverTime()
})

describe('Platform counts: unused browser time for the range', () => {
  it('draws the figure from the read, beside the run hours, with the agents it covers', async () => {
    loadPlatformTime.mockResolvedValue(
      payload(figures(100, browser()), [bucket(...W1, figures(50, browser({ unused_s: 3 * 3600 }))), bucket(...W2, figures(50, browser({ unused_s: 6 * 3600 })))]),
    )
    render(<PlatformOverTime workspaces={1} />)
    await run()
    const t = tile()
    // MUTATION: read `run_s` (all browser time) instead of `unused_s`.
    expect(t.querySelector('.pot-tile-value')!.textContent).toBe('9.0 h')
    expect(t.textContent).toMatch(/9 of 40 browser agents did not use it/)
    expect(t.textContent).toMatch(/of 40 h browser run time/)
    // BESIDE THE RUN-TIME FIGURES: the tile after Run hours.
    const names = [...document.querySelectorAll('.pot-tile')].map((e) => e.getAttribute('data-tile'))
    expect(names.indexOf('Unused browser time')).toBe(names.indexOf('Run hours') + 1)
  })

  it('six tiles are two even rows of three on a wide screen, not five and a stray', async () => {
    loadPlatformTime.mockResolvedValue(payload(figures(100, browser()), [bucket(...W1, figures(100, browser()))]))
    render(<PlatformOverTime workspaces={1} />)
    await run()
    const grid = document.querySelector<HTMLElement>('.pot-tiles')!
    expect(grid.querySelectorAll('.pot-tile')).toHaveLength(6)
    // REVIEW (light-desktop-04-counts-tile): the five-wide auto-fit grid put
    // Estimated cost alone on a second row with a four-tile hole at 1440.
    // MUTATION: drop the six-tile layout and leave the auto-fit grid.
    expect(painted(grid, 'grid-template-columns', { width: 1440 })).toBe('repeat(3, minmax(0, 1fr))')
    // The phone keeps its two columns, tokens and cost on rows of their own.
    expect(painted(grid, 'grid-template-columns', { width: 390, height: 844 })).toBe('1fr 1fr')
  })

  it('is a floor when some browser agents have no verdict', async () => {
    loadPlatformTime.mockResolvedValue(payload(figures(100, browser({ unknown_agents: 3 })), [bucket(...W1, figures(100, browser({ unknown_agents: 3 })))]))
    render(<PlatformOverTime workspaces={1} />)
    await run()
    // MUTATION: print it as a whole figure while three agents were never judged.
    expect(tile().querySelector('.pot-tile-value')!.textContent).toBe('at least 9.0 h')
    expect(tile().textContent).toMatch(/3 with no verdict, counted in neither/)
  })

  it('a missing field is a dash with its reason, never 0 h', async () => {
    loadPlatformTime.mockResolvedValue(payload(figures(100, undefined), [bucket(...W1, figures(100, undefined))]))
    render(<PlatformOverTime workspaces={1} />)
    await run()
    const value = tile().querySelector('.pot-tile-value')!
    // MUTATION: default a missing `browser` to 0 h.
    expect(value.textContent).toBe('—')
    expect(value.className).toMatch(/is-absent/)
    expect(tile().textContent).toMatch(/not in this read/)
    // Nothing to filter by when nothing was read; what the figure means still links.
    expect([...tile().querySelectorAll('a')].map((l) => l.getAttribute('href'))).toEqual(['#help/browser-not-used'])
  })

  it('no browser agent with a measured run is a dash that says so', async () => {
    const none = browser({ agents: 0, run_s: null, measured: 0, unused_agents: 0, unused_s: null })
    loadPlatformTime.mockResolvedValue(payload(figures(10, none), [bucket(...W1, figures(10, none))]))
    render(<PlatformOverTime workspaces={1} />)
    await run()
    expect(tile().querySelector('.pot-tile-value')!.textContent).toBe('—')
    expect(tile().textContent).toMatch(/no agent ended on a browser runtime/)
  })

  it('links to the Agents list filtered to browser agents that did not use it', async () => {
    loadPlatformTime.mockResolvedValue(payload(figures(100, browser()), [bucket(...W1, figures(100, browser()))]))
    render(<PlatformOverTime workspaces={1} />)
    await run()
    const links = [...tile().querySelectorAll('a')].map((l) => l.getAttribute('href'))
    // MUTATION: link to the plain Agents list, or to the Browsers switch.
    expect(links).toEqual(['/agents/recent?browser=unused', '#help/browser-not-used'])
  })
})

describe('Platform counts over time: unused browser time per bucket', () => {
  it('the readout on a bucket carries its own unused browser time', async () => {
    loadPlatformTime.mockResolvedValue(
      payload(figures(100, browser()), [bucket(...W1, figures(50, browser({ unused_s: 3 * 3600 }))), bucket(...W2, figures(50, undefined))]),
    )
    render(<PlatformOverTime workspaces={1} />)
    await run()
    const bars = document.querySelectorAll<SVGElement>('.pot-agents svg.is-wide .pot-hit')
    fireEvent.click(bars[0]!)
    const readout = () => document.querySelector<HTMLElement>('.pot-readout')!
    const row = () => [...readout().querySelectorAll('.pot-readout-row')].find((r) => r.querySelector('dt')?.textContent === 'Unused browser')
    // MUTATION: drop the per-bucket row, or draw the span's figure in it.
    expect(row()?.querySelector('dd')?.textContent).toMatch(/^3\.0 h/)
    fireEvent.click(bars[1]!)
    expect(row()?.querySelector('dd')?.textContent).toMatch(/^—/)
  })
})
