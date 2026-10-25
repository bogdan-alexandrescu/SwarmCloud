// THE DEV PREVIEW'S OVER-TIME EXAMPLE ADDS UP (COUNTS-TIME-VIEW, #2149). The
// view draws a total row under its group rows and a total tile over its
// buckets; an example whose rows did not sum to its total would draw a
// contradiction the route never serves, and a review would read it as the
// view's bug. The review of 2026-10-24 found exactly that: agent-type rows
// summing to 10,741 under a total of 10,743.

import { describe, expect, it } from 'vitest'

import { BREAKDOWNS, TIME_BUCKETS, TOKEN_KINDS, isRead, platformTimeExample, platformTimeQuery, type UsageFigures } from '../platformTimeApi'

const NOW = Date.parse('2026-10-19T12:00:00Z')

function sum(list: readonly UsageFigures[], f: (u: UsageFigures) => number | null): number {
  return list.reduce((n, u) => n + (f(u) ?? 0), 0)
}

const FIELDS: Array<[string, (u: UsageFigures) => number | null]> = [
  ['agents', (u) => u.agents],
  ['agents_ended', (u) => u.agents_ended],
  ['retries', (u) => u.retries],
  ['attempts', (u) => u.attempts],
  ['run measured', (u) => u.run_s.measured],
  ['cost reporting', (u) => u.cost.reporting],
  ['cost cents', (u) => (u.cost.sum_usd === null ? null : Math.round(u.cost.sum_usd * 100))],
  ...TOKEN_KINDS.map((t) => [`${t.key} reporting`, (u: UsageFigures) => u.tokens[t.key].reporting] as [string, (u: UsageFigures) => number]),
]

describe('the over-time example', () => {
  for (const b of TIME_BUCKETS) {
    for (const g of BREAKDOWNS.filter((x) => x.key !== 'none')) {
      it(`adds up: ${b.key} by ${g.group}`, () => {
        const d = platformTimeExample(platformTimeQuery(b.key, g.key, 'UTC', NOW), NOW)
        const u = d.usage
        // MUTATION: one bucket fewer or more than the span promises.
        expect(u.buckets).toHaveLength(b.count)
        const read = u.buckets.filter(isRead) as unknown as UsageFigures[]
        for (const [name, f] of FIELDS) {
          // MUTATION: the splitter's loop bound shrinking as it adds (the 10,741 under 10,743).
          expect(sum(u.groups.rows, f), `${name}: rows against the total`).toBe(f(u.totals) ?? 0)
          expect(sum(read, f), `${name}: buckets against the total`).toBe(f(u.totals) ?? 0)
        }
        // Calendar buckets, newest holding now, each ending where the next starts.
        // MUTATION: 30-day chunks for a month, which labelled October "Sep".
        const last = u.buckets[u.buckets.length - 1]!
        expect(Date.parse(last.start)).toBeLessThanOrEqual(NOW)
        expect(Date.parse(last.end)).toBeGreaterThan(NOW)
        for (let i = 1; i < u.buckets.length; i++) expect(u.buckets[i]!.start).toBe(u.buckets[i - 1]!.end)
      })
    }
  }
})
