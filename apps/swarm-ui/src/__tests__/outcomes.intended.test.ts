// #631, MIRRORED: a failure the caller asked for (the mock's `fail: true`) is
// its own failure class in the outcome ledger (`swarm_api/outcomes.py`,
// `intended`), served LAST in the vocabulary. The page reads the classes from
// the route, but the fixture its tests draw is hand-kept.
//
// MUTATIONS these catch: drop `intended` from the fixture's vocabulary or its
// zero counts; place it anywhere but last; give it another label.

import { describe, expect, it } from 'vitest'

import { ledgerFixture } from '../outcomes.fixture'

describe('the failed on purpose outcome class (#631)', () => {
  it('is the last class in the vocabulary, after no reason recorded', () => {
    const classes = ledgerFixture().vocab.failure_classes
    const keys = classes.map((c) => c.key)
    expect(keys[keys.length - 1]).toBe('intended')
    expect(keys[keys.length - 2]).toBe('no_reason')
    expect(classes.find((c) => c.key === 'intended')?.label).toBe('failed on purpose')
  })

  it('is counted in every total, as zero when nothing failed on purpose', () => {
    expect(ledgerFixture().totals.failure_classes).toHaveProperty('intended', 0)
  })
})
