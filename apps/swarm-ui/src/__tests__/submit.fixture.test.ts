// THE REQUIRED-KEYS PATH HAS TO BE REACHABLE WITHOUT A DEPLOYMENT.
//
// docs/web-ui/ux-plan.md §1.2: the rebuilt submit flow could not be verified
// against its required-keys path, because the fixture build never served one.
// `fixtureCapacity` carried no `input_contract` on any runner profile, so
// `requiredInputKeys` returned null for every profile, and a fixture session
// could only ever show the UNREAD branch -- the mark that says "this API did
// not say what the runner requires". The branch that matters most, a form
// refusing to send `claude-code` a blank prompt, was unreachable by anyone
// working on the screen, which is how a panel ships having never been seen.
//
// Two claims, both about what the fixture SERVES rather than about the
// components (submit.input.test.tsx already covers those with hand-built
// arguments):
//
//   1. every profile carries a READ rule -- an empty list is a measured
//      "nothing required", and must be reachable too;
//   2. at least one profile requires a key, and a form seeded from the fixture
//      refuses it blank, naming it.
//
// That the fixture's lists are the API's lists -- rather than a plausible copy
// -- is held from the other side, by
// tests/unit/control_plane/test_ui_fixture_input_contract.py, which compares
// them with `swarm_api.runnerinputs.input_contract` over the frozen catalogue.

import { describe, expect, it } from 'vitest'

import { loadCapacity } from '../api'
import { requiredInputKeys, type RunnerProfile } from '../types'
import { missingRequired, seedFields } from '../Submit'

async function fixtureProfiles(): Promise<[string, RunnerProfile][]> {
  // The fixture branch is taken only in a DEV build without VITE_LIVE. Said
  // here so a change to that switch fails this file by name rather than by
  // reaching the offline guard in setup.ts.
  expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
  expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
  const r = await loadCapacity()
  expect(r.status).toBe('ok')
  if (r.status !== 'ok') throw new Error('unreachable')
  const profiles = Object.entries(r.data.runner_profiles)
  expect(profiles.length, 'the fixture serves no runner profiles at all').toBeGreaterThan(0)
  return profiles
}

describe('the fixture serves the runner input contract', () => {
  it('every profile carries a read rule, so the unread mark is not the only thing a fixture can show', async () => {
    for (const [name, profile] of await fixtureProfiles()) {
      expect(
        requiredInputKeys(profile),
        `${name}: no input_contract in the fixture, so the form reads its rule as UNREAD`,
      ).not.toBeNull()
    }
  })

  it('a profile that requires a key is refused blank, and the refusal names the key', async () => {
    const demanding = (await fixtureProfiles())
      .map(([name, profile]) => [name, requiredInputKeys(profile) ?? []] as const)
      .filter(([, keys]) => keys.length > 0)
    expect(demanding.length, 'no fixture profile requires a key; the refusal path is unreachable').toBeGreaterThan(0)
    for (const [name, keys] of demanding) {
      const fields = seedFields([], keys)
      expect(missingRequired(fields, keys), `${name} was not refused with every required key blank`).toEqual(keys)
    }
  })
})
