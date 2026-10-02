/**
 * HEADROOM STAYS INSIDE ITS CARD AT EVERY WIDTH (#503).
 *
 * The 2026-10-02 audit measured Headroom's runner-profile rows reaching
 * x=1564 on a card that ended at x=1408, under a "By subscription account"
 * column drawn on top of them. O1 draws Headroom as three stacks in ONE
 * column -- profile tiles, pool rows, the account line -- inside the narrow
 * track of `.ov-g21` (1.7 : 1 from 1100px, one track below it).
 *
 * jsdom has no layout engine, so what is held is the set of rules that make
 * an overflow impossible: every grid track inside the card can shrink to
 * zero (`minmax(0, …)` or `auto-fill` over a small minimum), and the text
 * that could be long -- a pool or profile name -- ellipsises instead of
 * pushing its row wide.
 *
 * MUTATION: give `.ov-pl` a fixed `118px` name track, take the ellipsis off
 * `.ov-idc`, or put the two side-by-side groups back.
 */
import { afterEach, describe, expect, it } from 'vitest'

import STYLES from '../styles.css?raw'
import OVERVIEW_CSS from '../styles/overview.css?raw'
import OVERVIEW_SOURCE from '../Overview.tsx?raw'
import { cascade } from './cssgate'

const SHEET = `${STYLES}\n${OVERVIEW_CSS}`
const won = (el: Element, prop: string, width: number) => cascade(SHEET, el, prop, { width }).winner?.value ?? null

function card(): HTMLElement {
  document.body.innerHTML =
    '<div class="ov-g21"><section class="ctl-card ov-card ov-waiting"></section>' +
    '<section class="ctl-card ov-card ov-headroom">' +
    '<div class="ov-hps"><div class="ov-hp"><span class="ov-idc">claude-code</span><b>+6</b><small>binds your tenant</small></div></div>' +
    '<div class="ov-pls"><div class="ov-pl"><span class="ov-idc">provider:anthropic:tenant:eng</span><span class="ctl-util-track"></span><b>22/30</b></div></div>' +
    '</section></div>'
  return document.querySelector<HTMLElement>('.ov-headroom')!
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('the overview Headroom card', () => {
  it('is drawn as one column of stacks, not two side-by-side groups', () => {
    expect(OVERVIEW_SOURCE).not.toMatch(/className="ov-groups"/)
    expect(OVERVIEW_SOURCE).toMatch(/className="ov-hps"/)
    expect(OVERVIEW_SOURCE).toMatch(/className="ov-pls"/)
  })

  it('sits in the narrow track from 1100px and alone below it, and never spans', () => {
    const headroom = card()
    const grid = headroom.parentElement!
    expect(won(grid, 'grid-template-columns', 1440)).toBe('minmax(0, 1.7fr) minmax(0, 1fr)')
    expect(won(grid, 'grid-template-columns', 1100)).toBe('minmax(0, 1.7fr) minmax(0, 1fr)')
    expect(won(grid, 'grid-template-columns', 900)).toBe('minmax(0, 1fr)')
    for (const width of [390, 900, 1100, 1440]) expect(won(headroom, 'grid-column', width)).toBeNull()
  })

  it('lets every track inside it shrink, and ellipsises a long name', () => {
    const headroom = card()
    for (const width of [390, 1100, 1440]) {
      expect(won(headroom.querySelector('.ov-hps')!, 'grid-template-columns', width)).toBe('repeat(auto-fill, minmax(92px, 1fr))')
      expect(won(headroom.querySelector('.ov-pl')!, 'grid-template-columns', width)).toBe('minmax(0, 118px) minmax(0, 1fr) auto')
    }
    const name = headroom.querySelector('.ov-pl .ov-idc')!
    expect(won(name, 'text-overflow', 1440)).toBe('ellipsis')
    expect(won(name, 'overflow', 1440)).toBe('hidden')
    expect(won(name, 'white-space', 1440)).toBe('nowrap')
    expect(won(headroom, 'min-width', 1440)).toBe('0')
  })
})
