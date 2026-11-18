// A POOL THAT ADMITS NOTHING BY CONFIGURATION IS NOT A BUSY POOL.
//
// THE DEFECT, measured on the live console on 2026-09-24: a READY `browser`
// task whose resource pool had been set to `hard_limit 0` said "This resource
// class is busy platform-wide. (0/0)". Nothing was busy -- nothing was running
// at all. An operator had capped the pool at zero, and the sentence told the
// reader the one thing that sends them to the wrong remedy: wait.
//
// WHY THE TWO ARE TOLD APART BY THE NUMBERS AND NOT BY THE REASON. Admission
// (`evaluate_capacity`, admission.py) writes the SAME reason for both: a
// `resource:` pool that refuses is `RESOURCE_CLASS_LIMIT` whether it holds
// 4 of 4 or 0 of 0. What differs is the ceiling the blocker carries -- `limit`
// is the pool's `effective_limit` -- and a ceiling of zero is not a full pool,
// it is a pool that cannot admit anything until somebody raises it.
//
// WHY "BY OPERATOR" IS SAID FOR SOME POOLS AND NOT OTHERS. A non-provider
// pool's effective limit is its configured `hard_limit` and nothing else
// writes it: the quota broker's adaptive and quota-derived caps land only on
// `provider:` pools (quota_broker/service.py `_write_pool`). So a `resource:`,
// `runner:`, `backend:`, `tenant:` or `global` pool at zero was SET to zero by
// an admin. A `provider:` pool at zero may be the provider's quota state
// instead, and the screen does not know which -- so it says "limit 0" and
// names nobody.
//
// A drained pool is the other "paused": `enabled: false` makes admission
// write `MANUAL_PAUSE` with the pool's limit, which for a pool created only to
// carry the flag is `UNLIMITED_HARD_LIMIT` (store.py). "(0/1000000)" beside
// "paused" is a number that describes nothing, so it is not printed.

import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import { CeilingTag, ceilingFigure } from '../Blockers'
import {
  whyAgent,
  whyNotRunning,
  type BlockedEntry,
  type ProfileBlocker,
} from '../types'
import { task } from './runfixture'

function held(b: BlockedEntry) {
  return task({ state: 'READY', started_at: null, completed_at: null, blocked_by: [b] })
}

const ZERO: BlockedEntry = { pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 0, active: 0 }
const FULL: BlockedEntry = { pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 4 }

describe('the one-line wait reason on a READY task', () => {
  /**
   * THE MEASURED DEFECT. MUTATION: drop the zero-limit branch and this reads
   * "This resource class is busy platform-wide. (0/0)" again.
   */
  it('says a pool capped at zero is paused by an operator, not busy', () => {
    const why = whyAgent(held(ZERO))
    expect(why).toMatch(/paused by operator \(limit 0\)/i)
    expect(why, 'the reader has to know WHICH pool to go and raise').toContain('resource:browser')
    expect(why).not.toMatch(/busy/i)
    expect(why).not.toContain('(0/0)')
  })

  /** The other half: the fix must not turn every refusal into a pause. */
  it('keeps "busy" for a pool genuinely at a positive ceiling', () => {
    expect(whyAgent(held(FULL))).toBe('This resource class is busy platform-wide. (4/4)')
  })

  it('says a switched-off pool is paused by an operator, and prints no fraction', () => {
    const why = whyAgent(
      held({ pool: 'resource:browser', reason: 'MANUAL_PAUSE', limit: 1_000_000, active: 0 }),
    )
    expect(why).toMatch(/paused by operator/i)
    expect(why).toContain('resource:browser')
    expect(why, 'a drained pool carries the unlimited sentinel, which is not a ceiling').not.toMatch(
      /1000000|1,000,000/,
    )
  })

  it('names no operator for a provider pool at zero, because its quota can do that too', () => {
    const why = whyAgent(
      held({ pool: 'provider:anthropic', reason: 'PROVIDER_CONCURRENCY_LIMIT', limit: 0, active: 0 }),
    )
    expect(why).toMatch(/limit 0/)
    expect(why).not.toMatch(/operator/i)
    expect(why).not.toMatch(/busy|at its concurrency limit/i)
  })

  it('still counts what is held when a limit was lowered to zero under running work', () => {
    const why = whyAgent(held({ ...ZERO, active: 2 }))
    expect(why).toMatch(/paused by operator \(limit 0\)/i)
    expect(why).toMatch(/2 units? still held/)
  })

  it('whyNotRunning tells the same two apart', () => {
    const zero = whyNotRunning(held(ZERO)) ?? ''
    expect(zero).toMatch(/paused by operator \(limit 0\)/i)
    expect(zero).not.toMatch(/busy/i)
    expect(whyNotRunning(held(FULL))).toBe(
      'This resource class is busy platform-wide. (resource:browser) -- 4 of 4 in use',
    )
  })

  /**
   * THE LINE HAS ROOM FOR ONE POOL, SO IT NAMES THE ONE WAITING CANNOT CLEAR.
   * Admission lists every refusing pool in the order the profile names them,
   * so `global` at its ceiling comes before `resource:browser` at zero. The
   * first entry would say "waiting is the answer" about a task that no amount
   * of waiting will start. MUTATION: read `blocked_by[0]` again.
   */
  it('names the pool somebody has to act on, even when it is not the first to refuse', () => {
    const task_ = task({
      state: 'READY',
      started_at: null,
      completed_at: null,
      blocked_by: [
        { pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', limit: 50, active: 50 },
        ZERO,
      ],
    })
    expect(whyAgent(task_)).toMatch(/resource:browser is paused by operator \(limit 0\)/)
    expect(whyNotRunning(task_) ?? '').toMatch(/resource:browser is paused by operator \(limit 0\)/)
  })
})

// ---------------------------------------------------------------------------
// The same pool on a blocker row (Blockers.tsx `CeilingTag`, `ceilingFigure`)
// ---------------------------------------------------------------------------
//
// RE-POINTED 2026-10-01. These read the Profile headroom card's grouped list,
// which was removed with the per-profile cards. The row Submit's right-now box
// draws is the surviving surface for a refusing pool, built from the same two
// exports, so the claims are read off them: a pool at zero is never `full`, a
// drained pool prints no sentinel, a provider pool at zero names nobody.

function blocker(over: Partial<ProfileBlocker>): ProfileBlocker {
  return { pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 4, group: 'no_room', ...over }
}

/** The row as Submit draws it: the tag, then the figure. */
function renderRow(b: ProfileBlocker): HTMLElement {
  return render(
    <span>
      <CeilingTag blocker={b} />
      {ceilingFigure(b)}
    </span>,
  ).container as HTMLElement
}

function tagOf(el: HTMLElement): string {
  const t = el.querySelector('.tag')!
  const tone = [...t.classList].filter((k) => k !== 'tag').join(' ')
  return `${(t.textContent ?? '').trim()}|${tone}`
}

describe('a blocker row draws the same pool the same way', () => {
  it('tags a pool an operator capped at zero `limit 0`, not full', () => {
    const el = renderRow(blocker({ limit: 0, active: 0 }))
    expect(tagOf(el), 'a pool capped at zero is not full').toBe('limit 0|paused')
    expect(el.textContent).toMatch(/limit 0/)
    expect(el.textContent).not.toMatch(/busy platform-wide/)
  })

  it('keeps a genuinely full pool tagged full, with its fraction', () => {
    const el = renderRow(blocker({}))
    expect(tagOf(el)).toBe('full|full')
    expect(el.textContent).toMatch(/4 of 4 units in use/)
  })

  /**
   * A drained pool carries the unlimited sentinel as its `limit`, and "0 of
   * 1000000 units in use" beside a `paused` tag is a ceiling nobody set.
   * MUTATION: print `active of limit` for a paused row again.
   */
  it('prints no sentinel ceiling beside a drained pool', () => {
    const el = renderRow(blocker({ reason: 'MANUAL_PAUSE', limit: 1_000_000, active: 0, group: 'needs_action' }))
    expect(tagOf(el)).toBe('paused|paused')
    expect(el.textContent).not.toMatch(/1000000|1,000,000/)
  })

  /**
   * A provider pool at zero may be its quota state rather than a person, so
   * the row says `limit 0` in the capped tone rather than `full` or anybody's
   * name. MUTATION: tag it `full`, or in the operator's paused tone.
   */
  it('tags a provider pool at zero limit 0 and not full, and names no operator', () => {
    const el = renderRow(blocker({ pool: 'provider:anthropic', reason: 'PROVIDER_CONCURRENCY_LIMIT', limit: 0, active: 0 }))
    expect(tagOf(el), 'a provider pool at zero is drawn full, or in a person’s tone').toBe('limit 0|capped')
    expect(el.textContent).not.toMatch(/operator/i)
  })
})
