/**
 * `useEdgeSafePlacement` MEASURES THE CARD IT IS PLACING.
 *
 * THE DEFECT THIS FILE EXISTS FOR. The hook decides whether a help card opens
 * below its `?` or flips above it, and that decision needs the card's height.
 * It used to get it with
 *
 *     ref.current?.querySelector('[role="tooltip"], [role="note"]')
 *
 * where `ref` is the hook's ANCHOR ref. Both callers portal their card to
 * `document.body` -- that portal is the fix that rescued nine cards from
 * stacking contexts they could not escape -- so the card is not inside the
 * anchor's subtree and that query returned `null` on every call that has ever
 * been made. The height therefore fell through to its `|| 220` fallback
 * permanently, which is the exact estimate the comment beside it records as
 * having been measured WRONG by 16px on Submit. The measurement had been
 * written, wired to the wrong element, and never once taken.
 *
 * The role list was independently wrong too: `helpRole` returns `dialog` or
 * `tooltip`, and nothing in this app renders `role="note"`, so a PINNED card
 * would have been missed even in the right subtree. Two faults on one line,
 * both producing the same silent `null`, which is why neither ever surfaced.
 *
 * WHY THIS IS A REAL TEST AND NOT A SOURCE GREP. jsdom has no layout engine, so
 * every `getBoundingClientRect()` on a real element here returns zeroes and a
 * test that rendered a card and read its height would measure nothing -- which
 * is precisely how this defect survived a suite that already covers this file
 * heavily. But the height the hook uses comes from a ref the CALLER supplies,
 * and a test can supply one whose rect it controls completely. So the card's
 * box is stubbed rather than rendered, the anchor's box is stubbed too, and
 * what is asserted is the arithmetic the hook does with them. That is the part
 * that was broken, and it is fully determined.
 */
import { render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { CSSProperties, RefObject } from 'react'

import { useEdgeSafePlacement } from '../HelpCard'

/** A DOMRect literal, since jsdom will not produce a non-zero one. */
function rect(box: { top: number; left: number; width: number; height: number }): DOMRect {
  const { top, left, width, height } = box
  return {
    top,
    left,
    width,
    height,
    right: left + width,
    bottom: top + height,
    x: left,
    y: top,
    toJSON: () => ({}),
  } as DOMRect
}

/**
 * The `?` glyph's box: 14px square, low on an 800px-tall viewport.
 *
 * `bottom: 520` leaves `800 - 520 - 6 = 274px` of room below it, which is the
 * number both cases below straddle -- a 220px card fits in it and a 300px card
 * does not. That is what makes the two answers differ, and it is a shape this
 * product actually has: these cards are as tall as their content and the ones
 * carrying a `values()` list are the tall ones.
 */
const ANCHOR = rect({ top: 500, left: 100, width: 14, height: 20 })

/** A card too tall to fit below the anchor. Nothing renders it; only its box. */
const TALL_CARD = 300

function fakeCardRef(height: number): RefObject<HTMLElement> {
  return {
    current: {
      getBoundingClientRect: () => rect({ top: 0, left: 0, width: 360, height }),
    } as unknown as HTMLElement,
  } as RefObject<HTMLElement>
}

/** Renders the hook and hands back the style it produced. */
function placementWith(cardRef?: RefObject<HTMLElement>): CSSProperties {
  let out: CSSProperties = {}
  function Probe() {
    const [anchorRef, placement] = useEdgeSafePlacement(true, cardRef)
    out = placement
    // The hook returns early unless the anchor ref holds an element, so one is
    // rendered. Its rect comes from the prototype stub, not from jsdom.
    return <span ref={anchorRef}>?</span>
  }
  render(<Probe />)
  return out
}

describe('useEdgeSafePlacement', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  function stubViewport() {
    // Every real element reports the anchor's box. The only other element in the
    // probe is the text node's parent, which the hook never measures.
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(ANCHOR)
    Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true })
    Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true })
  }

  /**
   * MUTATION THIS CATCHES: reverting to `ref.current.querySelector(...)`, or
   * dropping the `cardRef` argument at either call site. Both make the height
   * 220 again, which fits below the anchor, so the card opens DOWNWARD and the
   * expected 194 becomes 526 -- a card whose bottom edge lands at 826 on an
   * 800px viewport, 26px below the fold. That is the defect, restored.
   */
  it('flips a card above the anchor when it is too tall to fit below', () => {
    stubViewport()
    const place = placementWith(fakeCardRef(TALL_CARD))

    // 274px of room below, a 300px card: it does not fit, and above it does --
    // anchor.top - 6 - 300 = 194, comfortably clear of the 8px margin.
    expect(place.top).toBe(ANCHOR.top - 6 - TALL_CARD)
    expect(place.position).toBe('fixed')
    // Horizontal is unaffected and asserted so a regression there cannot hide
    // behind this test passing: 100 + 360 + 8 < 1440, so no leftward flip.
    expect(place.left).toBe(ANCHOR.left)
  })

  /**
   * THE FALLBACK, ASSERTED SO THE TEST ABOVE CANNOT PASS VACUOUSLY.
   *
   * Without a card ref the hook has nothing to measure and uses its documented
   * 220px estimate. 220 fits in the 274px below the anchor, so it opens
   * downward at `bottom + 6 = 526`. This is exactly what the broken version did
   * for EVERY card, tall or short, and the contrast with the case above is the
   * whole content of the fix.
   */
  it('falls back to the 220px estimate when given no card ref', () => {
    stubViewport()
    const place = placementWith(undefined)

    expect(place.top).toBe(ANCHOR.bottom + 6)
    // And that is below the fold for a 300px card: 526 + 300 = 826 > 800. The
    // number is asserted rather than described so the arithmetic is checked.
    expect(ANCHOR.bottom + 6 + TALL_CARD).toBeGreaterThan(window.innerHeight)
  })

  /**
   * MUTATION THIS CATCHES: a card so tall it fits NEITHER way being left to
   * hang out of the viewport. The clamp is the third branch and the comment in
   * the hook says it is what makes "below the fold" impossible; an unclamped
   * flip is what put a card 299px past the edge before.
   */
  it('pins a card that fits neither way inside the viewport', () => {
    stubViewport()
    const place = placementWith(fakeCardRef(2000))

    // Neither below (274px of room) nor above (500px, and 2000 does not fit),
    // so it is clamped to the 8px margin rather than allowed off-screen.
    expect(place.top).toBe(8)
  })
})
