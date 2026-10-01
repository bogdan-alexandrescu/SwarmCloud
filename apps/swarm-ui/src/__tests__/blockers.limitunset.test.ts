// A POOL WITH NO LIMIT SET IS NOT A POOL SET TO ZERO, AND NOT A FULL ONE (#374).
//
// A pool document with no `hard_limit` key used to reach every screen as a
// limit of 0: admission reads the missing key as 0 and refused with the
// pool's ordinary reason, so the console said an operator had set the pool to
// zero. Nobody had. The scheduler now records that refusal as
// `POOL_LIMIT_UNSET` with `limit: null` (scheduler/codec.py), and the verdict
// these screens share (`blockerCeiling`) names it: no limit set, and a person
// has to set one. A null limit read as "not 0" would otherwise fall through to
// `full` -- "busy, wait" -- which is the one remedy that cannot work.

import { createElement } from 'react'
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { blockerCeiling, ceilingCopy, needsAPerson, POOL_LIMIT_UNSET, type ProfileBlocker } from '../types'
import { blockerVerdict, CeilingTag, ceilingFigure } from '../Blockers'

const unset: ProfileBlocker = {
  pool: 'tenant:eng',
  reason: POOL_LIMIT_UNSET,
  limit: null,
  active: 0,
  group: null,
}

describe('blocker ceiling for a pool with no limit set', () => {
  it('is its own verdict, not set-to-zero and not full', () => {
    expect(blockerCeiling(unset)).toBe('limit-unset')
    expect(blockerCeiling(unset, 2)).toBe('limit-unset')
    expect(blockerVerdict(unset, null)).toBe('limit-unset')
  })

  it('needs a person', () => {
    expect(needsAPerson('limit-unset')).toBe(true)
  })

  it('says no limit is set and that somebody has to set one, never "limit 0" or busy', () => {
    const copy = ceilingCopy(unset)
    expect(copy).not.toBeNull()
    expect(copy).toContain('tenant:eng has no limit set')
    expect(copy).toContain('somebody has to set its limit')
    expect(copy).not.toMatch(/limit 0|busy|paused by operator/)
    expect(ceilingFigure(unset)).toBe('no limit set · 0 units held')
    // The tag the submit box and the blocker list draw (the status marks
    // `ceilingMark` / `ceilingTitle` fed went with the Profiles cards).
    const { container } = render(createElement(CeilingTag, { blocker: unset }))
    const tag = container.querySelector('.tag')!
    expect(tag.textContent).toBe('no limit set')
    expect(tag.getAttribute('title')).toContain('Nobody set it to zero')
  })

  it('leaves a pool set to zero on purpose as set-to-zero', () => {
    const zero = { ...unset, reason: 'TENANT_LIMIT', limit: 0 }
    expect(blockerCeiling(zero)).toBe('set-to-zero')
  })

  it('leaves a paused pool paused, whatever its limit', () => {
    expect(blockerCeiling({ ...unset, reason: 'MANUAL_PAUSE' })).toBe('paused')
  })
})

// #66, at the verdict itself: a limit above 0 and below one task's units.
describe('blocker ceiling for a pool below one task', () => {
  const small: ProfileBlocker = { pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 1, active: 0, group: 'no_room' }

  it('is below-units, needs a person and says the task can never be admitted', () => {
    expect(blockerCeiling(small, 2)).toBe('below-units')
    expect(needsAPerson(blockerCeiling(small, 2))).toBe(true)
    const copy = ceilingCopy(small, small.pool, 2)
    expect(copy).toContain('can never be admitted at this limit')
    expect(copy).toContain('somebody has to raise the limit to at least 2')
  })

  it('is full at a limit one task fits under, and when the units are unknown', () => {
    expect(blockerCeiling({ ...small, limit: 2 }, 2)).toBe('full')
    expect(blockerCeiling(small, null)).toBe('full')
  })
})
