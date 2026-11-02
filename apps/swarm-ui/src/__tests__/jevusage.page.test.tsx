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

import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { JevUsageBody, outcomeText } from '../JevUsagePage'
import { normJevUsage, type JevUsage } from '../jevUsage'
import { fixtureJevUsage } from '../jevUsage.fixture'

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
