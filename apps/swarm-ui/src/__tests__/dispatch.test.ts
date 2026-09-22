// The dispatch vocabulary, as behaviour rather than as source text.
//
// `dispatchOf` is the one place in this client where "the API did not say" and
// "the caller chose the default" have to stay apart, and they look identical
// once either is rendered. The Python source-grep that used to cover this
// asserted the string `dispatchOf` appeared in a screen -- which is true of a
// screen that calls it and throws the answer away.

import { describe, expect, it } from 'vitest'

import {
  CARRIER_NOTE,
  DEFAULT_CARRIER,
  DEFAULT_STRATEGY,
  DISPATCH_CARRIERS,
  DISPATCH_STRATEGIES,
  STRATEGY_LABEL,
  consequenceOf,
  dispatchOf,
  needsRepository,
  type DispatchStrategy,
  type Task,
} from '../types'

function task(dispatch: unknown): Task {
  return { id: 't1', dispatch } as unknown as Task
}

describe('dispatchOf', () => {
  it('reads back what the API sent', () => {
    const d = dispatchOf(task({ strategy: 'integrate', carrier: 'branches', role: 'integrator', integrates: ['t0'] }))
    expect(d).toEqual({ strategy: 'integrate', carrier: 'branches', role: 'integrator', integrates: ['t0'] })
  })

  it('an API that sent NO dispatch key at all reports null, never "collect"', () => {
    // THE DISTINCTION. `dispatch_of` on the server already substitutes
    // collect/checkpoints for a task that predates the feature, and that
    // substitution is correct. Substituting it a SECOND time here would invent
    // a caller's choice out of a version skew: "nothing was pushed because you
    // asked for collect" is a different sentence from "nothing was pushed and
    // we cannot say why".
    expect(dispatchOf(task(undefined))).toBeNull()
    expect(dispatchOf(task(null))).toBeNull()
  })

  it('a vocabulary neither side knows reports null rather than being coerced', () => {
    // `patches` is the WORKER's spelling and swarm-api refuses it outright. A
    // client that quietly rendered it as a carrier would be describing a
    // submission the API would have 422'd.
    expect(dispatchOf(task({ strategy: 'collect', carrier: 'patches', role: null, integrates: [] }))).toBeNull()
    expect(dispatchOf(task({ strategy: 'squash', carrier: 'checkpoints', role: null, integrates: [] }))).toBeNull()
  })

  it('an array or a scalar in the dispatch slot is not an object to read', () => {
    expect(dispatchOf(task([]))).toBeNull()
    expect(dispatchOf(task('collect'))).toBeNull()
  })

  it('an unknown role degrades to null without losing the rest', () => {
    const d = dispatchOf(task({ strategy: 'collect', carrier: 'checkpoints', role: 'reviewer', integrates: [] }))
    expect(d?.role).toBeNull()
    expect(d?.strategy).toBe('collect')
  })

  it('drops non-string entries from integrates rather than rendering them', () => {
    const d = dispatchOf(task({ strategy: 'integrate', carrier: 'checkpoints', role: 'integrator', integrates: ['t0', 7, null] }))
    expect(d?.integrates).toEqual(['t0'])
  })

  it('a missing integrates list is empty, not undefined', () => {
    const d = dispatchOf(task({ strategy: 'collect', carrier: 'checkpoints', role: null }))
    expect(d?.integrates).toEqual([])
  })
})

describe('the defaults are the API’s defaults', () => {
  it('and they are members of the offered vocabulary', () => {
    expect(DISPATCH_STRATEGIES).toContain(DEFAULT_STRATEGY)
    expect(DISPATCH_CARRIERS).toContain(DEFAULT_CARRIER)
    expect(DEFAULT_STRATEGY).toBe('collect')
    expect(DEFAULT_CARRIER).toBe('checkpoints')
  })

  it('every offered strategy has a label', () => {
    for (const s of DISPATCH_STRATEGIES) expect(STRATEGY_LABEL[s].trim()).not.toBe('')
  })

  it('the default option never promises a pull request', () => {
    // `collect` publishes nothing. A picker whose default reads like it opens
    // a PR sets an expectation the platform then fails.
    expect(STRATEGY_LABEL[DEFAULT_STRATEGY].toLowerCase()).not.toContain('pr')
    expect(STRATEGY_LABEL[DEFAULT_STRATEGY].toLowerCase()).not.toContain('pull request')
  })
})

describe('needsRepository', () => {
  it.each([
    ['collect', 'checkpoints', false],
    ['collect', 'branches', true],
    ['direct-pr', 'checkpoints', true],
    ['integrate', 'checkpoints', true],
  ] as ReadonlyArray<[DispatchStrategy, 'checkpoints' | 'branches', boolean]>)(
    '%s + %s -> %s',
    (strategy, carrier, expected) => {
      expect(needsRepository(strategy, carrier)).toBe(expected)
    },
  )
})

describe('consequenceOf', () => {
  it('integrate over six steps is ONE pull request and direct-pr is six', () => {
    // The whole reason the control exists: without this a caller is picking
    // between three words.
    expect(consequenceOf('integrate', 6).pullRequests).toBe(1)
    expect(consequenceOf('direct-pr', 6).pullRequests).toBe(6)
    expect(consequenceOf('collect', 6).pullRequests).toBe(0)
  })

  it('a count that can be zero is labelled a ceiling, and one that cannot is not', () => {
    // A step whose agent changed nothing opens nothing, so the PR figures are
    // an upper bound. `collect` opens none, always -- which is not a ceiling.
    expect(consequenceOf('direct-pr', 6).atMost).toBe(true)
    expect(consequenceOf('integrate', 6).atMost).toBe(true)
    expect(consequenceOf('collect', 6).atMost).toBe(false)
  })

  it('says plainly that collect pushes nothing', () => {
    const c = consequenceOf('collect', 3)
    expect(c.pushes).toBe(false)
    expect(c.headline).toContain('Nothing is pushed')
    expect(c.detail).toContain('read-only token')
  })

  it('integrate on a single step says there is nothing to integrate', () => {
    expect(consequenceOf('integrate', 1).detail).toContain('nothing to integrate')
    expect(consequenceOf('integrate', 2).detail).not.toContain('nothing to integrate')
  })

  it('never renders a zero step count or a broken plural', () => {
    for (const s of DISPATCH_STRATEGIES) {
      for (const n of [0, 1, 2, 7]) {
        const c = consequenceOf(s, n)
        expect(c.headline).not.toContain('0 step')
        expect(c.headline).not.toContain('1 steps')
        expect(c.detail).not.toContain('1 steps')
        expect(c.detail).not.toContain('undefined')
        expect(c.detail).not.toContain('NaN')
      }
    }
  })
})

describe('the carrier note', () => {
  it('says the carrier is recorded and not yet acted on', () => {
    // `_dispatch_carrier` in agent-worker has no caller in production code.
    // Describing a mechanism that is not wired up is precisely the claim this
    // client exists to not make.
    expect(CARRIER_NOTE).toContain('no worker code reads it yet')
    expect(CARRIER_NOTE).toContain('repository URL required')
  })
})
