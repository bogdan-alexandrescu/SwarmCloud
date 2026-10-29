// LANE QA2-FIX-12 (part of #2694, the console visual QA pass 2 epic):
// ovag2-11 and overview-agents-55, an open agent beside the list at 1440.
//
// WHAT IT HOLDS: with an agent open the list column is the agent column's
// height and scrolls itself, so the page column (`.ctl-scroll`) has nothing
// to scroll -- one scroller per column -- and a scrolled list's heading
// leaves at the list's own top edge, below the sticky breadcrumb, never
// under it. A phone and the overlay widths keep the page scroller.

import { afterEach, describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import '../styles/agents.css'

const WIDE: CascadeEnv = { width: 1440 }
const OVERLAY: CascadeEnv = { width: 900 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function frame(): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML =
    '<div class="ctl-frame has-ctl-bar"><div class="sk-main"><div class="ctl-scroll">' +
    '<header class="ctl-bar"></header><div class="app has-inspector"><main class="work"></main>' +
    '<div class="drawer ctl-drawer ag-split"></div></div></div></div></div>'
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

const flat = (v: string | null) => (v ?? '').replace(/\s+/g, ' ')

describe('ovag2-11, overview-agents-55: one scroller per column with an agent open', () => {
  it('makes the list the agent column’s height, scrolling itself', () => {
    const host = frame()
    const work = host.querySelector('.work')!
    const col = host.querySelector('.ag-split')!
    // MUTATION: drop the list's own scroller and the page scrolls the list
    // beside the agent's scrollbar again.
    expect(painted(work, 'overflow-y', WIDE)).toBe('auto')
    // MUTATION: size the list by the viewport alone and its foot runs under
    // the API-reads line, so the page scrolls by the bar and the padding.
    expect(flat(painted(work, 'max-height', WIDE))).toBe(flat(painted(col, 'max-height', WIDE)))
    expect(painted(work, 'min-height', WIDE)).toBe('0')
  })

  it('scrolls the list one way only, so no horizontal bar runs along its foot', () => {
    const work = frame().querySelector('.work')!
    // MUTATION: drop `overflow-x: hidden` and `overflow-y: auto` makes the
    // x axis auto too: a `.when-note` past the edge draws a second scrollbar.
    expect(painted(work, 'overflow-x', WIDE)).toBe('hidden')
  })

  it('leaves the overlay widths on the page scroller', () => {
    const work = frame().querySelector('.work')!
    expect(painted(work, 'overflow-y', OVERLAY)).not.toBe('auto')
  })
})
