// THE PROBE REGISTRY DOES NOT LEAK BETWEEN TESTS.
//
// `fetch.ts` keeps the routes this tab has called in a module-level Map. That
// is right for a browser tab and wrong for a test file, where the module is
// loaded once and every test after the first inherits whatever the ones before
// it registered.
//
// The cost was real and it was invisible. `shell.test.tsx` asserts that a head
// with no successful read says "nothing has loaded in this tab" and prints no
// digit -- which is the defining bug of this product, rendered in the frame --
// and that test passed on its own while failing inside its own file, because
// earlier tests had rendered screens whose fixture reads registered successes.
// A test that is green alone and red in company is worse than a red one: it
// reports the order tests ran in.
//
// `src/__tests__/setup.ts` now calls `forgetProbes()` after every test. The two
// tests below are what keep that true: the first fills the registry, the second
// asserts it starts empty. Deleting the call in setup.ts turns the second one
// red immediately, which is not something the shell suite can promise -- its
// version of this failure is a race, and it happens to be winning it today.

import { describe, expect, it } from 'vitest'

import { noteFixtureProbe, probeSnapshot } from '../fetch'

describe('the probe registry', () => {
  it('records what a read did, so the next test has something to inherit', () => {
    expect(probeSnapshot()).toEqual([])
    noteFixtureProbe('/v1/capacity', 30, true)
    noteFixtureProbe('/v1/admin/dispatch', 120, false)
    const seen = probeSnapshot()
    expect(seen.length).toBe(2)
    // A SUCCESS, which is the half that matters: the head's age is the age of
    // the newest successful payload, so a leaked success is a tab that claims
    // to have loaded something it never did.
    expect(seen.find((p) => p.path === '/v1/capacity')?.lastSuccessAt).not.toBeNull()
  })

  it('is empty again before the next test, because the registry is module state', () => {
    expect(
      probeSnapshot(),
      'the previous test’s reads are still registered: setup.ts is not calling forgetProbes()',
    ).toEqual([])
  })
})
