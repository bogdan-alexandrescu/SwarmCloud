// CONTRACT REQUEST 126, AS THE LEDGER CLASSES IT (BGD 2026-10-27).
//
// A fix loop whose rounds ended without a MERGE (`EndCause.FIX_LOOP_EXHAUSTED`)
// is its own failure class in the outcome ledger (`swarm_api/outcomes.py`,
// `fix_loop_exhausted`, DERIVE_VERSION 12): a failure of the work, not of the
// platform, so not a runner error -- every round ran. The page reads the
// classes from the route's vocabulary, but the fixture its tests draw and the
// `END_CAUSES` copy in types.ts are hand-kept, so both carry it.
//
// MUTATIONS these catch: drop `fix_loop_exhausted` from the fixture's
// vocabulary or its zero counts; place it after `other`; drop it from
// END_CAUSES.

import { describe, expect, it } from 'vitest'

import { ledgerFixture } from '../outcomes.fixture'
import { END_CAUSES } from '../types'

describe('the fix loop exhausted outcome class (contract request 126)', () => {
  it('is in the vocabulary, after budget exceeded and before other', () => {
    const classes = ledgerFixture().vocab.failure_classes
    const keys = classes.map((c) => c.key)
    expect(keys.indexOf('fix_loop_exhausted')).toBe(keys.indexOf('budget_exceeded') + 1)
    expect(keys.indexOf('fix_loop_exhausted')).toBe(keys.indexOf('other') - 1)
    expect(classes.find((c) => c.key === 'fix_loop_exhausted')?.label).toBe('fix loop exhausted')
  })

  it('is counted in every bucket, as zero when no loop was exhausted', () => {
    const d = ledgerFixture()
    for (const b of d.buckets) expect(b.failure_classes).toHaveProperty('fix_loop_exhausted')
    expect(d.totals.failure_classes.fix_loop_exhausted).toBe(0)
  })

  it('is the frozen enum\'s last end cause in the console\'s copy', () => {
    expect(END_CAUSES[END_CAUSES.length - 1]).toBe('fix_loop_exhausted')
  })
})
