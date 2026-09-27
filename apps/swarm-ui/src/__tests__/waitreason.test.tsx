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

import { BlockerList } from '../Blockers'
import {
  headroomFor,
  whyAgent,
  whyNotRunning,
  type BlockedEntry,
  type ProfileAdmission,
  type ProfileBlocker,
  type RunnerProfile,
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
// The same pool on the profile-headroom list (Blockers.tsx)
// ---------------------------------------------------------------------------

function blocker(over: Partial<ProfileBlocker>): ProfileBlocker {
  return { pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 4, group: 'no_room', ...over }
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 2, headroom: 0, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(a: ProfileAdmission): RunnerProfile {
  return { resource_class: 'browser', backend: 'cloudrun', provider: null, units: 2, pools: ['resource:browser'], admission: a }
}

function renderBlockers(b: ProfileBlocker): HTMLElement {
  const h = headroomFor(profile(admission({ blockers: [b], binding: [b.pool] })))
  return render(<BlockerList h={h} groups={undefined} />).container as HTMLElement
}

/**
 * The words the list's marks say, under `scope`.
 *
 * #159 RE-POINT. These tests read the list's `.tag.full` / `.tag.paused`. The
 * list is back on the Profile headroom card, and CP-12 (#85) left no `.tag` in
 * that card: each refusal is drawn in the card's own marks, Pools' vocabulary
 * -- `.ctl-chip.is-paused` paused, `limit 0` in the paused tone when a person
 * set it and the bad tone when quota zeroed it, `.ctl-chip.is-warn` full. The
 * claims below did not move; only the element they are read off did.
 */
function marksIn(el: HTMLElement, scope = ''): string[] {
  return [...el.querySelectorAll(`${scope} .ctl-chip`)].map((c) => {
    const tone = [...c.classList].find((k) => k.startsWith('is-')) ?? '(none)'
    return `${(c.textContent ?? '').trim()}|${tone}`
  })
}

describe('the profile-headroom list draws the same pool the same way', () => {
  /**
   * The server files every `*_LIMIT` reason under `no_room` ("waiting is a
   * valid answer"), because the grouping is by reason and the reason cannot
   * see the ceiling. For a pool capped at zero waiting clears nothing, so a
   * row drawn there would sit under a heading that contradicts it.
   */
  it('files a pool an operator capped at zero under "somebody has to act", not as full', () => {
    const el = renderBlockers(blocker({ limit: 0, active: 0 }))
    const acting = el.querySelector('.blocker-group.needs-action')
    expect(acting, 'a zero-limit pool was filed where waiting is the answer').not.toBeNull()
    expect(acting!.textContent).toMatch(/limit 0/)
    expect(el.querySelector('.blocker-group.no-room')).toBeNull()
    expect(marksIn(el), 'a pool capped at zero is not full').toEqual(['limit 0|is-paused'])
    expect(el.textContent).not.toMatch(/busy platform-wide/)
  })

  it('keeps a genuinely full pool under "eligible, no room", tagged full', () => {
    const el = renderBlockers(blocker({}))
    expect(marksIn(el, '.blocker-group.no-room')).toEqual(['full|is-warn'])
    expect(el.querySelector('.blocker-group.needs-action')).toBeNull()
  })

  /**
   * A drained pool carries the unlimited sentinel as its `limit`, and "0 of
   * 1000000 units in use" beside a `paused` tag is a ceiling nobody set.
   * MUTATION: print `active of limit` for a paused row again.
   */
  it('prints no sentinel ceiling beside a drained pool', () => {
    const el = renderBlockers(
      blocker({ reason: 'MANUAL_PAUSE', limit: 1_000_000, active: 0, group: 'needs_action' }),
    )
    expect(marksIn(el, '.blocker-group.needs-action')).toEqual(['paused|is-paused'])
    expect(el.textContent).not.toMatch(/1000000|1,000,000/)
  })

  /**
   * A provider pool at zero may be its quota state rather than a person, and
   * a cooldown ends by itself -- so the server's grouping stands, and the row
   * says `limit 0` rather than `full` or anybody's name. MUTATION: file every
   * zero under "somebody has to act", or tag it `full`.
   */
  it('leaves a provider pool at zero where the server filed it, tagged limit 0 and not full', () => {
    const el = renderBlockers(
      blocker({ pool: 'provider:anthropic', reason: 'PROVIDER_CONCURRENCY_LIMIT', limit: 0, active: 0 }),
    )
    const room = el.querySelector('.blocker-group.no-room')
    expect(room, 'a provider pool at zero was refiled').not.toBeNull()
    expect(room!.textContent).toMatch(/limit 0/)
    expect(marksIn(el), 'a provider pool at zero is drawn full, or in a person’s tone').toEqual(['limit 0|is-bad'])
    expect(el.textContent).not.toMatch(/operator/i)
  })
})
