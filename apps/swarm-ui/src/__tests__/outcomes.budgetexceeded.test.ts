// CONTRACT REQUEST 118, AS THE LEDGER CLASSES IT (BGD 2026-10-27).
//
// An attempt the worker stopped on its budget (`EndCause.BUDGET_EXCEEDED`) is
// its own failure class in the outcome ledger (`swarm_api/outcomes.py`,
// `budget_exceeded`, DERIVE_VERSION 11), not a runner error. The page reads
// the classes from the route's vocabulary, but the fixture its tests draw is
// hand-kept, so it carries the class in the served order and a count.
//
// MUTATIONS these catch: drop `budget_exceeded` from the fixture's vocabulary
// or its zero counts; place it after `other`; drop the fixture's one stop.

import { describe, expect, it } from 'vitest'

import { ledgerFixture } from '../outcomes.fixture'

describe('the budget exceeded outcome class (contract request 118)', () => {
  it('is in the vocabulary, after publish refused and before the exhausted fix loop', () => {
    const classes = ledgerFixture().vocab.failure_classes
    const keys = classes.map((c) => c.key)
    expect(keys.indexOf('budget_exceeded')).toBe(keys.indexOf('publish_refused') + 1)
    // Contract request 126 sits between it and `other` (BGD 2026-10-27).
    expect(keys.indexOf('budget_exceeded')).toBe(keys.indexOf('fix_loop_exhausted') - 1)
    expect(classes.find((c) => c.key === 'budget_exceeded')?.label).toBe('budget exceeded')
  })

  it('is counted in every bucket, and the fixture carries one stop', () => {
    const d = ledgerFixture()
    for (const b of d.buckets) expect(b.failure_classes).toHaveProperty('budget_exceeded')
    expect(d.totals.failure_classes.budget_exceeded).toBe(1)
  })
})
