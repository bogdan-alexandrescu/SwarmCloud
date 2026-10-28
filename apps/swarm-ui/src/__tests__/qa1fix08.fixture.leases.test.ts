// LANE QA1-FIX-08-PATCHES (part of #2596), capacity-02's fixture half: the
// fixture's leases named `global` and `tenant:u-bogdan` only, so the five
// other pools claude-code reserves held units with no lease on them, and the
// fixture build's Pools page read `holders` (no count) beside a full
// `anthropic · u-bogdan`. A lease names every pool its runner profile
// reserves (invariant 2: admission is all-or-nothing across those pools).
//
// Read through the real loaders in the fixture build, as
// qafix4.fixture.reads.test.ts does: the defect was in what the fixture serves.

import { describe, expect, it } from 'vitest'

import { loadCapacity, loadLeases } from '../api'

describe('the fixture build: every lease names its profile\'s pools', () => {
  it('serves leases naming claude-code\'s seven pools', async () => {
    expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
    expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
    const [leases, capacity] = await Promise.all([loadLeases(), loadCapacity()])
    expect(leases.status).toBe('ok')
    expect(capacity.status).toBe('ok')
    if (leases.status !== 'ok' || capacity.status !== 'ok') return
    const want = [...capacity.data.runner_profiles['claude-code'].pools].sort()
    expect(want.length).toBe(7)
    // MUTATION: `pools: ['global', 'tenant:u-bogdan']` -- two of seven.
    for (const l of leases.data.leases) expect([...l.pools].sort(), l.lease_id).toEqual(want)
  })
})
