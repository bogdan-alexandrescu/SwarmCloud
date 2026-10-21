// VISUAL QA RUN 3 (wf_d598fa7bffdb41fc9d76, 2026-10-21), LANE
// QAFIX3-LIMITS-HEADER (part of #1680): the follow-up QAFIX3-CAPACITY-HEADER
// (#1863) found on Admin › Pool limits. Its capacity-007 fix pads the page
// scroller by the bar and the Pools head, and deliberately leaves the scroller
// that holds `.adm-ceilings` alone, because there the head sticks below the
// filter too, whose height only admin.css knows. So a row scrolled into view
// on Pool limits stopped at the bar alone: under the filter and the head.
//
//   limits-header  A scroll stops a Pool limits row below the sticky filter
//                  and head, not under them.

import { afterEach, describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const SAFE: CascadeEnv = { width: 800 }

const hosts: HTMLElement[] = []

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

const LIMITS = 'div.cap-families > div.ctl-table.is-scroll > table.adm-limits > thead > tr'

/** The head row of a Pool limits family, under a scroller framed as `frame`. */
function headRow(frame: string, filter: boolean): HTMLElement {
  return build(`${frame} > div.ctl-scroll > div.app > section.section.adm-ceilings${filter ? '.has-filter' : ''} > ${LIMITS}`, hosts)
}

describe('limits-header: a scroll stops a Pool limits row below the sticky filter and head', () => {
  it('keeps the head sticky, one row tall, below the bar and the filter', () => {
    const head = headRow('div.ctl-frame.has-ctl-bar', true)
    // MUTATION: drop `position: sticky` from the Pool limits thead
    // (styles/admin.css) -- the head scrolls away with the page again (V077).
    expect(painted(head.parentElement!, 'position', WIDE)).toBe('sticky')
    expect(painted(head.parentElement!, 'top', WIDE)).toBe('calc(48px + 60px)')
    // MUTATION: drop `height: var(--row-h)` from the sticky heads' row
    // (styles/capacity.css) -- the padding below no longer names the height
    // the head is drawn at.
    expect(painted(head, 'height', WIDE)).toBe('var(--row-h)')
  })

  it('pads the scroller by the bar, the filter and the head', () => {
    const head = headRow('div.ctl-frame.has-ctl-bar', true)
    const h = painted(head, 'height', WIDE) ?? 'no height on the head row'
    const scroller = head.closest('.ctl-scroll')!
    // MUTATION: drop the `.ctl-scroll:has(.adm-ceilings ...)` scroll-padding
    // rule (styles/admin.css) -- the scroller keeps styles.css's bar-only
    // `var(--ctl-bar-h, 0px)`, and a row scrolled into view stops under the
    // filter and the head, as the QAFIX3-CAPACITY-HEADER lane found.
    expect(painted(scroller, 'scroll-padding-top', WIDE)).toBe(`calc(48px + 60px + ${h})`)
  })

  it('pads by the bar and the head alone while no filter is drawn', () => {
    const head = headRow('div.ctl-frame.has-ctl-bar', false)
    const h = painted(head, 'height', WIDE) ?? 'no height on the head row'
    // MUTATION: declare `--adm-filter-h: 60px` on every scroller holding
    // `.adm-ceilings`, not only one holding `.has-filter` -- a row stops 60px
    // short, under a filter that is not there.
    expect(painted(head.closest('.ctl-scroll')!, 'scroll-padding-top', WIDE)).toBe(`calc(48px + 0px + ${h})`)
  })

  it('pads by no bar where the bar is drawn empty', () => {
    const head = headRow('div.ctl-frame.has-ctl-bar', true)
    const h = painted(head, 'height', WIDE) ?? 'no height on the head row'
    const scroller = head.closest<HTMLElement>('.ctl-scroll')!
    scroller.prepend(Object.assign(document.createElement('header'), { className: 'ctl-bar' }))
    // MUTATION: drop the scroller from the `.ctl-bar:empty` rule
    // (styles/admin.css) -- the head sticks at the filter's foot but a row
    // stops 48px lower, under a bar that is not drawn.
    expect(painted(scroller, 'scroll-padding-top', WIDE)).toBe(`calc(0px + 60px + ${h})`)
    expect(painted(head.parentElement!, 'top', WIDE)).toBe('calc(0px + 60px)')
  })

  it('leaves the bar-only padding below 900px, where nothing but the bar sticks', () => {
    const head = headRow('div.ctl-frame.has-ctl-bar', true)
    // MUTATION: lift the rule out of `@media (min-width: 900px)` -- below 900
    // neither the filter nor the head sticks, and a row would stop short.
    expect(painted(head.closest('.ctl-scroll')!, 'scroll-padding-top', SAFE)).toBe('48px')
  })
})
