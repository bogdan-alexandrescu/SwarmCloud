// JEV-USAGE-PAGE (part of #2688; the owner's pick of 2 November 2026):
// CAPACITY › JEV USAGE.
//
// The page draws `GET /v1/jev-usage` from its fixture: a daily table by kind,
// the 30-day total and the month's use of its cap. Dollars are a dash saying
// "rate not set", never $0, until Jev's rate is set; every skip shows its
// reason; output tokens not recorded are a dash, not a zero.
//
// BREAK IT: print $0 without a rate, drop the skip reasons from the outcome
// text, read the month's use from the 30-day total, or let `normJevUsage`
// turn a null into 0 -- a case below turns red.

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { JevUsageBody, outcomeText } from '../JevUsagePage'
import { normJevUsage, parseJevRate, type JevUsage } from '../jevUsage'
import { fixtureJevUsage } from '../jevUsage.fixture'
import { serve, visible } from './repofixture'

afterEach(cleanup)

function usage(patch: (raw: Record<string, unknown>) => void = () => {}): JevUsage {
  const raw = fixtureJevUsage()
  patch(raw)
  const u = normJevUsage(raw)
  if (u === null) throw new Error('fixture did not normalise')
  return u
}

describe('Capacity › Jev usage', () => {
  it('draws one row per day and kind, worker judgments by question', () => {
    render(<JevUsageBody usage={usage()} />)
    const table = screen.getByRole('table', { name: 'Jev usage by day and kind' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).getAllByRole('cell')[1]?.textContent)).toEqual([
      'Worker judgment J1',
      'Quality gate',
      'Not split by kind',
    ])
    const j1 = within(rows[0]!).getAllByRole('cell').map((c) => c.textContent)
    expect(j1.slice(0, 5)).toEqual(['2026-11-02', 'Worker judgment J1', '2', '5,000', '150'])
  })

  it('shows every skipped call’s reason', () => {
    render(<JevUsageBody usage={usage()} />)
    expect(screen.getByText('2 ok · 0 failed · 2 skipped (oversize 2)')).toBeTruthy()
    expect(screen.getByText('1 ok · 1 failed · 1 skipped (budget_calls 1)')).toBeTruthy()
    expect(outcomeText(usage().total.outcomes)).toBe('3 ok · 1 failed · 3 skipped (budget_calls 1, oversize 2)')
  })

  it('leaves dollars blank, never $0, while no rate is set', () => {
    const { container } = render(<JevUsageBody usage={usage()} />)
    expect(container.textContent).not.toMatch(/\$0/)
    expect(screen.getAllByRole('img', { name: 'rate not set' }).length).toBeGreaterThan(0)
    expect(container.textContent).toContain('rate not set')
  })

  it('prices the figures once the rate is set', () => {
    const u = usage((raw) => {
      raw.rate = { input_usd_per_million: 2, output_usd_per_million: 10, set: true }
      const total = raw.total as Record<string, unknown>
      total.usd = 0.0315
    })
    render(<JevUsageBody usage={u} />)
    expect(screen.getByText('$0.0315')).toBeTruthy()
    expect(screen.getByText(/Priced at \$2 per million tokens in, \$10 out/)).toBeTruthy()
  })

  it('keeps an unrecorded figure null rather than zero', () => {
    const u = usage()
    expect(u.total.usd).toBeNull()
    expect(u.days[1]?.outputTokens).toBeNull()
    render(<JevUsageBody usage={u} />)
    expect(screen.getAllByRole('img', { name: 'output tokens not recorded' }).length).toBeGreaterThan(0)
  })

  it('shows the month’s use against its cap, and says when there is none', () => {
    render(<JevUsageBody usage={usage()} />)
    expect(screen.getByText('Monthly cap, 2026-11')).toBeTruthy()
    expect(document.body.textContent).toContain('11000 / 50000 tokens')
    cleanup()
    render(<JevUsageBody usage={usage((raw) => {
      raw.month = { month: '2026-11', used_tokens: 11_000, cap_tokens: null, used_fraction: null }
    })} />)
    expect(screen.getByText(/No monthly cap is set for this workspace/)).toBeTruthy()
  })

  it('refuses a body in a shape it does not know', () => {
    expect(normJevUsage({ days: 'no' })).toBeNull()
    expect(normJevUsage(null)).toBeNull()
  })
})

// The rate behind the dollars: Admin › Pool limits › Jev's rate, saved as
// `jev_token_rates` through `PUT /v1/admin/settings` and nothing else.
describe('Admin › Jev’s rate', () => {
  type Json = Record<string, unknown>
  const settings = (rates: Json): Json => ({
    merge_by_default: false,
    max_additional_repositories: 5,
    additional_repositories_budget_gib: 6,
    model_prices: {},
    agent_budget_ceiling: {},
    jev_token_rates: rates,
    updated_at: null,
    updated_by: null,
    settings_document: 'present',
  })

  async function rateCard(rates: Json) {
    const calls = serve((m, url, body) => {
      if (m === 'GET' && url === '/v1/admin/settings') return { status: 200, body: settings(rates) }
      if (m === 'PUT' && url === '/v1/admin/settings') {
        return { status: 200, body: settings((body as Json).jev_token_rates as Json) }
      }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { JevRateCard } = await import('../AdminSettings')
    render(<JevRateCard />)
    const heading = await screen.findByRole('heading', { name: 'Jev’s rate' })
    const card = heading.closest('section') as HTMLElement
    await waitFor(() => expect(within(card).queryByText('Reading…')).toBeNull())
    return { calls, card }
  }

  afterEach(() => vi.unstubAllEnvs())

  it('has no rate until one is set, and says so', async () => {
    const { card } = await rateCard({})
    const input = within(card).getByRole('textbox', { name: 'Dollars per million tokens in' }) as HTMLInputElement
    expect(input.value).toBe('')
    expect(input.placeholder).toBe('rate not set')
    expect((within(card).getByRole('button', { name: 'Save rate' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('saves the rate as `jev_token_rates` and nothing else', async () => {
    const { calls, card } = await rateCard({})
    fireEvent.change(within(card).getByRole('textbox', { name: 'Dollars per million tokens in' }), { target: { value: '2.5' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save rate' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ jev_token_rates: { input: 2.5 } })
    expect(visible(await within(card).findByRole('status'))).toBe('Saved: $2.5 per million tokens in.')
  })

  it('refuses a rate that is not a number of dollars', async () => {
    const { card } = await rateCard({ input: 3 })
    fireEvent.change(within(card).getByRole('textbox', { name: 'Dollars per million tokens out' }), { target: { value: '-1' } })
    expect(visible(card)).toContain('A rate is a number of dollars, 0 or more.')
    expect((within(card).getByRole('button', { name: 'Save rate' }) as HTMLButtonElement).disabled).toBe(true)
    expect(parseJevRate('$1,000')).toBe(1000)
    expect(parseJevRate('abc')).toBeNull()
  })
})
