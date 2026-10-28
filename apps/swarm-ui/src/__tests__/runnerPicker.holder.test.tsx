// THE POOL HOLDING A RUNNER AT ZERO, IN WORDS (QA1-FIX-23, runs-submit-21;
// part of #2596).
//
// The runner card read "0 can start now · held by
// provider:anthropic:tenant:u-bogdan", the pool's key in mono: jargon to the
// person submitting. The card now names the pool in words -- what it caps and
// whose it is -- and keeps the key in the title, so the name is never lost
// and the id is never shown without it.
//
// A profile's pools are the CALLING tenant's (types.ts `headroomFor`, trap
// D), so a tenant pool is "this tenant". Nothing here is token-shaped.

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import { CanStartFact, poolInWords } from '../RunnerPicker'
import type { RunnerProfile } from '../types'

function heldBy(pool: string): RunnerProfile {
  return {
    name: 'claude-code',
    resource_class: 'standard',
    units: 1,
    backend: 'CLOUD_RUN_JOB',
    pools: ['global', pool],
    admission: {
      units: 1, headroom: 0, basis: 'measured', blockers: [{ pool, reason: 'full' }], binding: [pool],
      counterfactual: [], complete: true, unread: [], uncapped: [],
    },
  } as unknown as RunnerProfile
}

describe('poolInWords', () => {
  it('names every kind of pool in words', () => {
    // MUTATION: return the key, or `poolLabel`'s `anthropic · u-bogdan`.
    expect(poolInWords('provider:anthropic:tenant:u-bogdan')).toBe('anthropic key, this tenant')
    expect(poolInWords('provider:anthropic')).toBe('anthropic key, platform-wide')
    expect(poolInWords('tenant:u-bogdan')).toBe('this tenant')
    expect(poolInWords('global')).toBe('the platform')
    expect(poolInWords('resource:large')).toBe('large class, platform-wide')
    expect(poolInWords('runner:browser')).toBe('browser runner, platform-wide')
    expect(poolInWords('backend:gke')).toBe('gke backend, platform-wide')
  })
})

describe('the runner card names the pool holding it at zero', () => {
  it('in words, with the pool’s key in its title, and not in mono', () => {
    const { container } = render(<CanStartFact profile={heldBy('provider:anthropic:tenant:u-bogdan')} />)
    const text = (container.textContent ?? '').replace(/\s+/g, ' ').trim()
    // MUTATION: print `room.binding` as the holder's text again.
    expect(text).toBe('0 can start now · held by anthropic key, this tenant')
    const holder = container.querySelector<HTMLElement>('[data-pool]')!
    expect(holder, 'the holder is not its own element').not.toBeNull()
    expect(holder.dataset.pool).toBe('provider:anthropic:tenant:u-bogdan')
    // MUTATION: drop the key from the title; the name is never shown without it reachable.
    expect(holder.getAttribute('title')).toBe('anthropic key, this tenant (pool provider:anthropic:tenant:u-bogdan)')
    // MUTATION: keep `.sbf-runner-holder`, styles/submit.css's mono id.
    expect(holder.classList.contains('sbf-runner-holder')).toBe(false)
  })
})
