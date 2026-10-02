// CONTRACT REQUEST 29, MIRRORED (applied 2026-10-02).
//
// `EndCause.PUBLISH_REFUSED` is a new frozen value and a new failure class in
// the outcome ledger (`swarm_api/outcomes.py`, `publish_refused`). The page
// reads the classes from the route's vocabulary, but the fixture the page's
// tests draw and the `END_CAUSES` copy in types.ts are hand-kept: without the
// class, a ledger carrying it would draw a strip no fixture ever had.
//
// MUTATIONS these catch: drop `publish_refused` from the fixture's vocabulary
// or its zero counts; drop it from END_CAUSES; place it after `other`.

import { describe, expect, it } from 'vitest'

import { ledgerFixture } from '../outcomes.fixture'
import { END_CAUSES } from '../types'

describe('the publish refused outcome class (contract request 29)', () => {
  it('is in the vocabulary, after the worker actions and before other', () => {
    const keys = ledgerFixture().vocab.failure_classes.map((c) => c.key)
    expect(keys).toContain('publish_refused')
    expect(keys.indexOf('publish_refused')).toBe(keys.indexOf('merge_failed') + 1)
    expect(keys.indexOf('publish_refused')).toBe(keys.indexOf('other') - 1)
    const label = ledgerFixture().vocab.failure_classes.find((c) => c.key === 'publish_refused')
    expect(label?.label).toBe('publish refused')
  })

  it('is counted in every total, as zero when nothing was refused', () => {
    expect(ledgerFixture().totals.failure_classes).toHaveProperty('publish_refused')
  })
})

describe('the end cause mirror', () => {
  it('carries publish_refused, last, as the frozen enum does', () => {
    expect(END_CAUSES[END_CAUSES.length - 1]).toBe('publish_refused')
    expect(new Set(END_CAUSES).size).toBe(END_CAUSES.length)
  })
})
