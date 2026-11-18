/**
 * QA ROUND 3, LANE U12 (owner, 2026-10-04): Accounts › History.
 *
 *   D24  The Holder's task link overran "attempt 1" by 2-3px in a 196px cell:
 *        inline pieces in one clipped line. The Holder is a row of boxes; the
 *        link and the tenant shrink with their own ellipsis, "attempt N" and a
 *        mark never do, and no piece starts before the one before it ends.
 *
 * MUTATIONS: drop `.acct-hist-whoin`'s flex, let the link keep its width, or
 * let "attempt N" shrink -- each turns the case red.
 */
import { describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { textPx } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const TASK = 'task_7f3e2c9a1b8d4e6f0a5c'

describe('D24: the Holder never prints its link over "attempt N"', () => {
  it('lays the pieces out as boxes in a row, the link the one that gives way', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<table class="acct-hist"><tbody><tr><td class="acct-hist-who"><span class="acct-hist-whoin">' +
      `<a class="ctl-link mono" href="#">${TASK}</a><span class="muted"> attempt 1</span><span class="mono"> eng</span>` +
      '</span></td></tr></tbody></table>'
    document.body.appendChild(host)
    const row = host.querySelector('.acct-hist-whoin')!
    expect(painted(row, 'display', WIDE)).toBe('flex')
    expect(painted(row, 'min-width', WIDE)).toBe('0')
    const [link, attempt, tenant] = [...row.children]
    for (const shrinks of [link!, tenant!]) {
      expect(painted(shrinks, 'flex', WIDE)).toBe('0 1 auto')
      expect(painted(shrinks, 'min-width', WIDE)).toBe('0')
      expect(painted(shrinks, 'text-overflow', WIDE)).toBe('ellipsis')
      expect(painted(shrinks, ['overflow-x', 'overflow'], WIDE)).toBe('hidden')
    }
    expect(painted(attempt!, 'flex', WIDE)).toBe('none')
    // The model: a 196px cell less its 10px right padding. "attempt 1" keeps
    // its width; the link is what is left after the gaps -- never negative,
    // so it never starts under "attempt 1".
    const room = 196 - 10
    const fixed = textPx('attempt 1', attempt!, WIDE)
    const left = room - fixed - 2 * 4
    expect(left, 'nothing is left for the link').toBeGreaterThan(40)
    host.remove()
  })
})
