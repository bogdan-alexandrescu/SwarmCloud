// THE SUBMIT BOX'S "RIGHT NOW" PANEL DRAWS A PAUSED POOL AS PAUSED.
//
// THE DEFECT. PR #54 fixed the live "This resource class is busy
// platform-wide. (0/0)" in three places -- the one-line wait reason, the
// profile-headroom list (Blockers.tsx) and the Capacity chips -- and missed a
// fourth copy of the same row in `Submit.tsx`'s `ProfileFacts`. That copy
// decided its tag on `reason === 'MANUAL_PAUSE'` alone and printed
// `active of limit` for every row, so with `pools/resource:browser` at
// `hard_limit 0` (the live case) a submitter choosing `browser` read:
//
//   Room for 0 more tasks ... held down by resource:browser, 0 of 0 weighted
//   units in use.
//   [full] resource:browser — 0 of 0 units in use
//
// -- "full", which says waiting clears it, about a pool no wait will reopen.
// And a drained pool (MANUAL_PAUSE carrying the UNLIMITED_HARD_LIMIT sentinel)
// printed "0 of 1000000 units in use", which Blockers.tsx had stopped doing.
//
// WHY THE SENTENCE NAMES THE POOL A PERSON MUST ACT ON. `room.binding` is the
// FIRST binding pool in the profile's order, so `global` at its ceiling is
// named ahead of a pool capped at zero, and the sentence says "held down by
// global, 50 of 50" -- wait -- about a task that waiting will not start. The
// wait reason (`leadBlocker` in types.ts) already picks the pool a person must
// act on; this holds the submit box to the same choice.
//
// Rendered with hand-built arguments, as submit.input.test.tsx does, because
// the claim is about what ONE admission block draws, not about the fixture.

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import { HELP } from '../help'
import { ProfileFacts } from '../Submit'
import type { Pool, ProfileAdmission, ProfileBlocker, RunnerProfile } from '../types'

function pool(over: Partial<Pool> & { name: string }): Pool {
  return {
    hard_limit: 50,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 50,
    active: 0,
    available: 50,
    enabled: true,
    updated_at: '2026-09-24T10:00:00Z',
    ...over,
  }
}

function blocker(over: Partial<ProfileBlocker>): ProfileBlocker {
  return { pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 4, group: 'no_room', ...over }
}

const REQUIRED = ['tenant:acme', 'global', 'resource:browser']

function profile(blockers: ProfileBlocker[], over: Partial<ProfileAdmission> = {}): RunnerProfile {
  const admission: ProfileAdmission = {
    units: 2,
    headroom: 0,
    basis: 'measured',
    blockers,
    // At headroom 0 the server's `binding` IS its blocker list (headroom.py
    // `analyse_profile`: both are `_stopping` at n=1), in the profile's order.
    binding: blockers.map((b) => b.pool),
    counterfactual: [],
    complete: true,
    unread: [],
    uncapped: [],
    ...over,
  }
  return { resource_class: 'browser', backend: 'cloudrun', provider: null, units: 2, pools: REQUIRED, admission }
}

function room(blockers: ProfileBlocker[], pools: Pool[]): HTMLElement {
  return render(<ProfileFacts name="browser" profile={profile(blockers)} pools={pools} />)
    .container as HTMLElement
}

/** The sentence under the heading: "Room for N more tasks ...". */
function sentence(el: HTMLElement): string {
  const p = Array.from(el.querySelectorAll('p')).find((n) => /Room for/.test(n.textContent ?? ''))
  expect(p, 'the room sentence was not rendered').toBeDefined()
  return p!.textContent ?? ''
}

/** The refusing pool's row. Every case below asserts on exactly one. */
function firstRow(el: HTMLElement): HTMLElement {
  const row = el.querySelector<HTMLElement>('.blocker-list .blocker-row')
  expect(row, 'the refusing pool has no row').not.toBeNull()
  return row!
}

const ZERO = blocker({ limit: 0, active: 0 })
const ZERO_POOL = pool({ name: 'resource:browser', hard_limit: 0, effective_limit: 0, available: 0 })

describe('the submit box tells a pool capped at zero from a full one', () => {
  /**
   * THE LIVE CASE. MUTATION: decide the tag on `reason === 'MANUAL_PAUSE'`
   * again, or print `active of limit` for every row, and this goes red.
   */
  it('tags a pool an operator set to limit 0 as limit 0, not full, and prints no 0 of 0', () => {
    const el = room([ZERO], [pool({ name: 'global' }), ZERO_POOL])
    const row = firstRow(el)
    expect(row.querySelector('.tag.full'), 'a pool capped at zero is not full').toBeNull()
    expect(row.querySelector('.tag')?.textContent).toMatch(/limit 0/)
    expect(row.textContent).toContain('resource:browser')
    expect(el.textContent, '"0 of 0" is the fraction that read as busy').not.toMatch(/\b0 of 0\b/)
  })

  it('says the pool is paused by an operator in the sentence, not "held down by ... 0 of 0"', () => {
    const line = sentence(room([ZERO], [pool({ name: 'global' }), ZERO_POOL]))
    expect(line).toMatch(/resource:browser is paused by operator \(limit 0\)/)
    expect(line).not.toMatch(/held down by/)
    expect(line).not.toMatch(/\b0 of 0\b/)
  })

  /**
   * A drained pool carries the unlimited sentinel as its `limit`, and
   * "0 of 1000000 units in use" is a ceiling nobody set. MUTATION: print
   * `active of limit` beside a paused row again.
   */
  it('prints no sentinel ceiling beside a drained pool, and tags it paused', () => {
    const drained = blocker({ reason: 'MANUAL_PAUSE', limit: 1_000_000, active: 0, group: 'needs_action' })
    const el = room([drained], [
      pool({ name: 'global' }),
      pool({ name: 'resource:browser', hard_limit: 1_000_000, effective_limit: 1_000_000, enabled: false }),
    ])
    const row = firstRow(el)
    expect(row.querySelector('.tag.paused')).not.toBeNull()
    expect(row.querySelector('.tag.full')).toBeNull()
    expect(el.textContent).not.toMatch(/1000000|1,000,000/)
    expect(sentence(el)).toMatch(/paused/)
  })

  /**
   * THE SENTENCE HAS ROOM FOR ONE POOL, so it names the one waiting cannot
   * clear. MUTATION: name `room.binding` (the first in profile order) again.
   */
  it('names the pool somebody has to act on, even when global refuses first', () => {
    const line = sentence(
      room(
        [blocker({ pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', limit: 50, active: 50 }), ZERO],
        [pool({ name: 'global', active: 50, available: 0 }), ZERO_POOL],
      ),
    )
    expect(line).toMatch(/resource:browser is paused by operator \(limit 0\)/)
    expect(line, 'global at its ceiling was named over a pool no wait reopens').not.toMatch(/held down by global/)
  })

  /** A provider pool at zero may be its quota state: limit 0, nobody named. */
  it('names no operator for a provider pool at zero', () => {
    const provider = blocker({ pool: 'provider:anthropic', reason: 'PROVIDER_CONCURRENCY_LIMIT', limit: 0, active: 0 })
    const el = room([provider], [pool({ name: 'global' }), pool({ name: 'provider:anthropic', hard_limit: 8, effective_limit: 0 })])
    const row = firstRow(el)
    expect(row.querySelector('.tag.full')).toBeNull()
    expect(row.querySelector('.tag')?.textContent).toMatch(/limit 0/)
    expect(el.textContent).not.toMatch(/operator/i)
    expect(el.textContent).not.toMatch(/\b0 of 0\b/)
  })

  /** The other half: the fix must not turn every refusal into a pause. */
  it('keeps full, and the fraction, for a pool genuinely at a positive ceiling', () => {
    const el = room([blocker({})], [pool({ name: 'global' }), pool({ name: 'resource:browser', hard_limit: 4, effective_limit: 4, active: 4, available: 0 })])
    const row = firstRow(el)
    expect(row.querySelector('.tag.full')).not.toBeNull()
    expect(row.textContent).toMatch(/4 of 4 units in use/)
    expect(sentence(el)).toMatch(/held down by resource:browser, 4 of 4 weighted units in use/)
  })
})

// TS-23 (epic #84, owner decision 2026-09-25): the box carries ONE sentence.
// The cost is a fact on its head, and an unread room is the kit's unread
// encoding rather than a paragraph saying it is not zero. Committed RED first.
describe('the room box is one sentence, and an unread room is the unread mark', () => {
  it('puts the cost on the head as a fact -- "in each of", never a multiplication -- and drops the cost sentence', () => {
    const el = room([blocker({})], [pool({ name: 'global' }), pool({ name: 'resource:browser', hard_limit: 4, effective_limit: 4, active: 4, available: 0 })])
    expect(el.querySelector('.sbf-room-h')!.textContent).toBe('browser right now · 2 units in each of 3 pools')
    expect(el.textContent, 'the cost sentence is still a paragraph').not.toMatch(/Costs|all at once/)
    // What is left under the head is the room sentence and the refusing pools.
    expect(el.querySelectorAll('.sbf-room > p.muted')).toHaveLength(1)
  })

  it('says "in each of" for a one-pool profile too -- the decided phrase, not a second one', () => {
    // The decided head is `{name} right now · {units} unit(s) in each of {n}
    // pools`. A one-pool profile read "in its 1 pool", a wording nobody
    // decided. The count agrees with its noun, as `unit(s)` already does.
    const one: RunnerProfile = { ...profile([]), pools: ['global'] }
    const el = render(<ProfileFacts name="browser" profile={one} pools={[pool({ name: 'global' })]} />)
      .container as HTMLElement
    expect(el.querySelector('.sbf-room-h')!.textContent).toBe('browser right now · 2 units in each of 1 pool')
  })

  it('draws an unread room as the unread mark and a pool count, with no room figure and no "not zero" prose', () => {
    const unread = profile([], { headroom: null, basis: 'unknown', binding: [], unread: ['global'], complete: false })
    const el = render(<ProfileFacts name="browser" profile={unread} pools={[pool({ name: 'global' })]} />)
      .container as HTMLElement
    const mark = el.querySelector('.sbf-room .ctl-mark.is-unread')
    expect(mark, 'an unread room is drawn without the unread mark').not.toBeNull()
    expect(mark!.getAttribute('aria-label')).toBe(HELP['room-unknown-not-zero'].short)
    const line = mark!.closest('p')!
    expect((line.textContent ?? '').replace(/\s+/g, ' ')).toContain('room unknown · 1 of 3 pools could not be read')
    // The pool count is the only numeral in the room slot.
    expect((line.textContent ?? '').replace('1 of 3 pools', ''), 'a figure in the slot of a room nobody measured').not.toMatch(/\d/)
    // "not measured" is the absent mark's word, and the rule is the mark's now.
    expect(el.textContent).not.toMatch(/not measured|That is not zero|could not be measured/)
  })
})
