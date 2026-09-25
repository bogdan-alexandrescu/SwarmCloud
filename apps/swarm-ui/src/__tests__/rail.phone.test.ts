/**
 * THE TOP STRIP AT PHONE WIDTH SCROLLS; NOTHING IN IT IS SQUEEZED OR FADED OUT.
 *
 * THE DEFECT, measured on the live console at 390x844 on 2026-09-24
 * (`08-overview-390.png`): the strip read `Overview  Work  Capacity  Admin |
 * AP / reac`. "API reads" had been broken onto two lines and cut on the right.
 *
 * WHAT DID IT, from the sheet rather than from a guess. Below 900px the rail is
 * a flex ROW that scrolls sideways (design-system.md §6.15: "The strip keeps
 * every section visible and scrolls"). Three things in that block worked
 * against the scroll:
 *
 *   1. `.ctl-nav-util` -- the "API reads" + `?` corner -- was an ordinary flex
 *      item, `flex-shrink: 1`. When the row was wider than the phone, the
 *      browser SHRANK the corner rather than overflowing the scroller, down to
 *      the width of its longest word.
 *   2. `.ctl-nav-util button` had no `white-space: nowrap`, so at that width
 *      "API reads" wrapped to "API / reads" -- a two-line tab in a one-line
 *      strip whose `overflow-y` is hidden.
 *   3. The 24px fade that says "this strip continues" is a MASK on the
 *      scroller. A mask does not scroll with the content, so the last 24px of
 *      the row sat under the fade at EVERY scroll position, the end included:
 *      scrolling as far as it goes still could not uncover the last item.
 *
 * So the strip's items are held at their own width (they overflow, and the
 * strip scrolls), "API reads" is one line, and the corner carries the fade's
 * width as trailing padding so that at the end of the scroll the faded band
 * is empty space rather than a label. The fade's width is ONE custom property
 * read by both the mask and the padding: two copies of 24px is how the next
 * edit to one of them silently reintroduces (3).
 *
 * HOW THIS IS TESTED, and what it cannot see. jsdom has no layout engine
 * (`rail.labels.test.ts`'s header, `spaceprobe.ts`), so no test here can
 * measure that "API reads" fits. What it CAN hold is the set of declarations
 * whose absence produced the defect, read from the shipped sheet with the same
 * spec-following parser the stylesheet gate uses -- so a comment that has lost
 * its `/*` and swallowed the block fails here too, instead of being recovered
 * from the way jsdom's CSSOM recovers. Each assertion names the mutation it
 * catches.
 */
import STYLES from '../styles.css?raw'
import { createElement } from 'react'
import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest'

import { App } from '../App'
import { parseSheet, splitTop, type GateNode } from './cssgate'
import { stripComments } from './spaceprobe'

const PHONE = '@media (max-width: 899px)'

/** The phone-width rules, as (selector list, body). */
function phoneRules(): { selectors: string[]; body: string }[] {
  const out: { selectors: string[]; body: string }[] = []
  const walk = (nodes: readonly GateNode[], inPhone: boolean): void => {
    for (const n of nodes) {
      if (n.kind === 'group') walk(n.children, inPhone || n.prelude === PHONE)
      else if (n.kind === 'rule' && inPhone) out.push({ selectors: splitTop(n.prelude), body: n.body })
    }
  }
  walk(parseSheet(STYLES).nodes, false)
  return out
}

/**
 * Every declaration block that names `selector` inside the phone-width context,
 * in source order -- on its own or as one branch of a selector list, since the
 * two rows' scroller is ONE rule for both (CH-21). Keyed by CONDITION, as the
 * gate keys it: the sheet carries more than one `@media (max-width: 899px)`
 * block, and to the cascade they are one.
 */
function phoneBodies(selector: string): string[] {
  return phoneRules()
    .filter((r) => r.selectors.includes(selector))
    .map((r) => r.body)
}

/** The LAST value a phone-width block declares for `prop` on `selector`. */
function phoneValue(selector: string, prop: string): string | null {
  let value: string | null = null
  for (const body of phoneBodies(selector)) {
    for (const m of body.matchAll(new RegExp(`(?:^|[;{\\s])${prop}\\s*:\\s*([^;]+)`, 'g'))) {
      value = (m[1] ?? '').trim()
    }
  }
  return value
}

/**
 * Whether the rule stops the item shrinking: `flex-shrink: 0`, `flex: none`,
 * or a `flex` shorthand whose second number is 0 (`flex: 0 0 auto`).
 */
function holdsItsWidth(selector: string): boolean {
  if (phoneValue(selector, 'flex-shrink') === '0') return true
  const flex = phoneValue(selector, 'flex')
  if (flex === null) return false
  if (flex === 'none') return true
  const parts = flex.split(/\s+/)
  return parts.length >= 2 && parts[1] === '0'
}

describe('the top strip at phone width', () => {
  /**
   * The precondition: the block exists, and each of the strip's two rows is a
   * horizontal scroller.
   *
   * RE-POINTED BY CH-21. The strip was ONE scrolling row with the open
   * section's tabs inserted inline, which moved every section after it. It is
   * two rows now -- `.ctl-rail-main` (the sections and the utility corner) and
   * `.ctl-rail-sub` (the open section's tabs) -- and `.ctl-rail` is the column
   * that holds them rather than the scroller. The scroller declarations moved
   * to ONE rule naming both rows: two copies of them is how the next edit to
   * one row silently leaves the other behind.
   */
  it('is two horizontal scrollers, as design-system.md §6.15 says it is', () => {
    expect(phoneBodies('.ctl-rail').length, `no ${PHONE} rule for .ctl-rail`).toBeGreaterThan(0)
    expect(phoneValue('.ctl-rail', 'flex-direction'), '.ctl-rail holds the two rows in a column').toBe('column')
    expect(phoneValue('.ctl-rail', 'overflow-x') ?? 'visible', '.ctl-rail is still the scroller').not.toBe('auto')

    const scrollers = phoneRules().filter((r) => /(?:^|[;\s])overflow-x\s*:\s*auto/.test(r.body))
    const shared = scrollers.filter(
      (r) => r.selectors.includes('.ctl-rail-main') && r.selectors.includes('.ctl-rail-sub'),
    )
    expect(shared, 'the two rows do not share ONE scroller rule').toHaveLength(1)
    for (const row of ['.ctl-rail-main', '.ctl-rail-sub']) {
      expect(
        scrollers.filter((r) => r.selectors.includes(row)),
        `${row} is made a scroller by more than one rule`,
      ).toHaveLength(1)
    }
    expect(phoneValue('.ctl-rail-main', 'flex-direction')).toBe('row')
    expect(phoneValue('.ctl-rail-sub', 'flex-direction')).toBe('row')
  })

  /**
   * MUTATION: remove `flex: 0 0 auto` from the phone `.ctl-nav-util`. The
   * corner goes back to shrinking inside the strip instead of overflowing it,
   * which is the "AP / reac" in the screenshot.
   */
  it('never shrinks the utility corner to fit, so the strip scrolls instead', () => {
    expect(holdsItsWidth('.ctl-nav-util')).toBe(true)
  })

  /** MUTATION: the same, on the sections, which would ellipse "Capacity". */
  it('never shrinks the sections to fit either', () => {
    expect(holdsItsWidth('.ctl-rail-sections')).toBe(true)
  })

  /**
   * MUTATION: drop `white-space: nowrap` from the phone `.ctl-nav-util
   * button`. "API reads" can break onto a second line again, in a strip one
   * line tall with its vertical overflow hidden.
   */
  it('keeps "API reads" on one line', () => {
    expect(phoneValue('.ctl-nav-util button', 'white-space')).toBe('nowrap')
  })

  /**
   * MUTATION: delete the corner's trailing padding, or give the mask and the
   * padding two literal widths and change one. The last label is under the
   * fade again at the end of the scroll -- the one position from which the
   * reader has no further to go.
   */
  it('ends the scroll on empty space as wide as the fade, not on a label', () => {
    // ONE DECLARATION IN THE WHOLE SHEET, and it is a length. It lives on
    // `:root` (beside `--rail-w`), not on the phone `.ctl-rail`: every `var()`
    // in this sheet resolves from `:root`, and `spaceprobe.ts` -- which the
    // keyboard and spacing sweeps run the whole sheet through -- throws on a
    // name it cannot find there. (Declared on `.ctl-rail` first, which is what
    // this assertion used to demand; CI's first run of the fix found it.)
    const decls = [...stripComments(STYLES).matchAll(/--rail-fade\s*:\s*([^;}]+)/g)]
    expect(
      decls.map((m) => (m[1] ?? '').trim()),
      'the fade width is not ONE custom property',
    ).toHaveLength(1)
    expect((decls[0]?.[1] ?? '').trim()).toMatch(/^\d+px$/)
    const roots: string[] = []
    const walk = (nodes: readonly GateNode[]): void => {
      for (const n of nodes) {
        if (n.kind === 'group') walk(n.children)
        else if (n.kind === 'rule' && n.prelude === ':root') roots.push(stripComments(n.body))
      }
    }
    walk(parseSheet(STYLES).nodes)
    expect(roots.some((b) => /--rail-fade\s*:/.test(b)), 'the fade width is not a :root token').toBe(true)

    // RE-POINTED BY CH-21: the mask is on each row, from the one shared rule.
    for (const row of ['.ctl-rail-main', '.ctl-rail-sub']) {
      const mask = phoneValue(row, 'mask-image') ?? ''
      expect(mask, `${row}'s mask no longer reads the fade width it shares`).toContain('var(--rail-fade)')
      expect(phoneValue(row, '-webkit-mask-image') ?? '', row).toContain('var(--rail-fade)')
    }
    // And the old one-row mask is gone from the column, or it would fade row 2
    // under row 1's edge.
    expect(phoneValue('.ctl-rail', 'mask-image') ?? 'none').toBe('none')

    // Each row ends on the faded band's width of empty space: row 1 through
    // the utility corner, as before, and row 2 on its own.
    for (const end of ['.ctl-nav-util', '.ctl-rail-sub']) {
      const pad = phoneValue(end, 'padding-inline-end') ?? phoneValue(end, 'padding-right')
      expect(pad, `${end} does not reserve the faded band at the end of the scroll`).toBe('var(--rail-fade)')
    }
  })
})

/**
 * CH-14. THE STRIP SCROLLS, AND NOTHING SCROLLED IT TO WHERE YOU ARE.
 *
 * Measured at 390px: on `#work/new`, `#admin/tenants` and `#help` the current
 * tab (or the Help button) sat past the right edge of the strip, so the one
 * item that says where the reader is was the one item they could not see.
 * On every route change the rail now brings its current item into view along
 * the strip -- `nearest` on both axes, so an item already visible does not
 * move and the page itself is never scrolled vertically for it.
 *
 * jsdom implements no `scrollIntoView` and no layout, so what is asserted is
 * the call: on which element, with which options.
 */
describe('the strip brings the current item into view (CH-14)', () => {
  const ORIGINAL = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
  let spy: Mock

  function stub(): void {
    spy = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', { value: spy, configurable: true, writable: true })
  }

  afterEach(() => {
    if (ORIGINAL) Object.defineProperty(Element.prototype, 'scrollIntoView', ORIGINAL)
    else delete (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView
    window.location.hash = ''
  })

  /** Which rail elements the spy was called on. */
  function scrolledInRail(container: HTMLElement): Element[] {
    const rail = container.querySelector('.ctl-rail')
    return spy.mock.contexts.filter((el): el is Element => el instanceof Element && rail !== null && rail.contains(el))
  }

  /**
   * MUTATION: remove the effect. Nothing in the rail is scrolled to.
   *
   * RE-POINTED BY CH-21: the open section's tabs are drawn twice -- inline for
   * the desktop column, and as the phone's second row -- and only one copy is
   * ever displayed. Below 900px the visible copy is the second row's, so that
   * is the one scrolled to; jsdom has no layout to tell the two apart, and the
   * effect falls back to the second row's copy there too.
   */
  it('scrolls the selected tab into view when a route opens', () => {
    stub()
    window.location.hash = '#admin/counts'
    const { container } = render(createElement(App))
    const selected = container.querySelector('.ctl-rail-sub [role="tab"][aria-selected="true"]')
    expect(selected?.textContent).toContain('Platform counts')
    const hits = scrolledInRail(container)
    expect(hits, 'the rail scrolled nothing into view').toContain(selected)
    const call = spy.mock.calls[spy.mock.contexts.indexOf(selected)]
    expect(call?.[0]).toEqual({ inline: 'nearest', block: 'nearest' })
  })

  it('follows the route when it changes', async () => {
    stub()
    window.location.hash = '#admin/counts'
    const { container } = render(createElement(App))
    spy.mockClear()
    await act(async () => {
      window.location.hash = '#capacity/quota'
      window.dispatchEvent(new Event('hashchange'))
    })
    const selected = container.querySelector('.ctl-rail-sub [role="tab"][aria-selected="true"]')
    expect(selected?.textContent).toContain('Provider quota')
    expect(scrolledInRail(container)).toContain(selected)
  })

  /** Help and API reads are not tabs; their on-state button is what is scrolled to. */
  it('scrolls the utility button when Help is open', () => {
    stub()
    window.location.hash = '#help'
    const { container } = render(createElement(App))
    const on = container.querySelector('.ctl-nav-util .is-on')
    expect(on, 'no utility button is on under #help').not.toBeNull()
    expect(scrolledInRail(container)).toContain(on)
  })
})
