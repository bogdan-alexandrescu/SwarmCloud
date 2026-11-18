/**
 * VISUAL QA Q8 (owner, 2026-10-02): a Pools row's name was an underlined bold
 * link and its mono sublabel wrapped ("global / global / · platform" on three
 * lines). Per capacity.html the name is a normal-weight row title and the
 * sublabel is one line of sans, dim text. Asked of the shipped cascade on the
 * row `<App />` renders at /capacity/pools.
 * MUTATION: drop `.cap-pools tbody th[scope='row']`'s weight, or put the
 * mono face or `white-space: normal` back on `.cap-sub`.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { familyOf } from './faces'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('Q8: a pool row reads as a row title over one sans line', () => {
  it('draws the name at normal weight without an underline, and the sublabel on one sans line', async () => {
    window.history.replaceState(null, '', '/capacity/pools')
    render(<App />)
    const rows = await waitFor(() => {
      const r = [...document.querySelectorAll<HTMLElement>('.cap-pools tbody th[scope="row"]')]
      expect(r.length).toBeGreaterThan(0)
      return r
    })
    for (const th of rows) {
      const name = th.querySelector('a')!
      // A <th> is bold by the browser's own sheet, so the weight must be said.
      expect(painted(th, ['font-weight', 'font'], WIDE) ?? 'bold (the browser default)', 'the row title is bold').toMatch(/^(?:400|normal)$|^400\s/)
      expect(painted(name, ['text-decoration', 'text-decoration-line'], WIDE), 'the row title is underlined').toMatch(/^none/)
      const sub = th.querySelector('.cap-sub')!
      expect(painted(sub, 'white-space', WIDE)).toBe('nowrap')
      expect(painted(sub, 'text-overflow', WIDE)).toBe('ellipsis')
      expect(familyOf(sub, WIDE)).toBe('sans')
      expect(familyOf(sub.querySelector('.ctl-sub')!, WIDE), 'the pool id under the name is mono').toBe('sans')
    }
  })
})
