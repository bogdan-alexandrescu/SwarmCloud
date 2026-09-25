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
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest'
import type { CSSProperties, RefObject } from 'react'

import { HelpCard, useEdgeSafePlacement } from '../HelpCard'
import { HELP } from '../help'
import { HelpScreen } from '../HelpSection'

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

/**
 * AH-6. A PRESS INSIDE A PINNED CARD IS NOT AN OUTSIDE PRESS.
 *
 * The pinned card dismissed itself on ANY `pointerdown` in the document,
 * captured before it reached anything, and never looked at where it landed.
 * So pressing "Full explanation" unmounted the card between pointerdown and
 * click and the link never navigated; and pressing the `?` of a pinned card
 * closed it on pointerdown and re-pinned it on click, so it could not be shut
 * by the control that opened it.
 */
describe('the pinned card and the press that dismisses it (AH-6)', () => {
  function pin(): HTMLButtonElement {
    const { container } = render(<HelpCard topic="absent-vs-zero" />)
    const button = container.querySelector('button')!
    fireEvent.click(button)
    expect(screen.queryByRole('dialog'), 'a click did not pin the card').not.toBeNull()
    return button
  }

  /** MUTATION: dismiss on every pointerdown again. The card is gone before the link's click. */
  it('stays open when the press lands inside it, so its link can be followed', () => {
    pin()
    const link = screen.getByRole('dialog').querySelector('a')!
    fireEvent.pointerDown(link)
    expect(screen.queryByRole('dialog'), 'a press on the card link dismissed the card').not.toBeNull()
  })

  it('still closes on a press anywhere else', () => {
    pin()
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  /** MUTATION: count the trigger as outside. pointerdown closes, click re-pins: it never shuts. */
  it('closes when its own `?` is pressed again, rather than closing and re-pinning', () => {
    const button = pin()
    fireEvent.pointerDown(button)
    fireEvent.click(button)
    expect(screen.queryByRole('dialog'), 'the second press on the ? re-pinned the card').toBeNull()
  })
})

/**
 * CH-8 (the HelpCard half). §7.2 asks for a 44px target at phone width. The
 * `?` is a 14px disc, and growing the disc would make it the loudest thing in
 * the line it annotates -- so the TARGET grows and the disc does not.
 */
describe('the `?` target at phone width (CH-8)', () => {
  function media(phone: boolean) {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: phone && query.includes('560'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }))
  }

  /** A descendant of the glyph at least 44px square: the hit area. */
  function hitArea(button: HTMLElement): HTMLElement | undefined {
    return [...button.querySelectorAll<HTMLElement>('*')].find(
      (el) => Number.parseFloat(el.style.width) >= 44 && Number.parseFloat(el.style.height) >= 44,
    )
  }

  /** MUTATION: drop the hit area. Nothing inside the 14px glyph reaches 44px. */
  it('reaches 44px at 560px and under, without growing the disc', () => {
    media(true)
    const { container } = render(<HelpCard topic="absent-vs-zero" />)
    const button = container.querySelector('button')!
    const hit = hitArea(button)
    expect(hit, 'the phone-width ? has no 44px target').toBeTruthy()
    expect(hit!.getAttribute('aria-hidden')).toBe('true')
    expect(hit!.textContent).toBe('')
    // The disc itself is unchanged.
    expect(button.style.width).toBe('14px')
    expect(button.style.height).toBe('14px')
    expect(button.textContent).toBe('?')
  })

  it('adds nothing on a wide screen, where a 44px target would cover its neighbours', () => {
    media(false)
    const { container } = render(<HelpCard topic="absent-vs-zero" />)
    expect(hitArea(container.querySelector('button')!)).toBeUndefined()
  })
})

/**
 * AH-16. A deep link that lands on a topic already on screen jumped the page
 * anyway, putting the topic hard against the top edge and the group heading
 * above it out of sight. It scrolls only when the topic is out of view.
 */
describe('the Help page deep link (AH-16)', () => {
  const ORIGINAL = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')

  afterEach(() => {
    if (ORIGINAL) Object.defineProperty(Element.prototype, 'scrollIntoView', ORIGINAL)
    else delete (Element.prototype as unknown as { scrollIntoView?: unknown }).scrollIntoView
    vi.restoreAllMocks()
  })

  function scrolls(top: number): Mock {
    const spy = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', { value: spy, configurable: true, writable: true })
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(
      rect({ top, left: 0, width: 600, height: 200 }),
    )
    render(<HelpScreen topic="absent-vs-zero" />)
    return spy
  }

  it('scrolls to a target that is out of view', () => {
    const spy = scrolls(2000)
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.contexts[0]).toBe(document.getElementById(HELP['absent-vs-zero'].anchor))
  })

  /** MUTATION: scroll unconditionally. The in-view target is jumped to the top. */
  it('leaves a target that is already in view where it is', () => {
    const spy = scrolls(100)
    expect(spy).not.toHaveBeenCalled()
  })

  /**
   * Inside the app frame: `.ctl-scroll` ends at `bottom`, and the target's box
   * is `target`. Everything else answers zeroes, as jsdom does.
   */
  function scrollsInFrame(bottom: number, target: number): Mock {
    const spy = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', { value: spy, configurable: true, writable: true })
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      if (this.classList.contains('ctl-scroll')) return rect({ top: 0, left: 0, width: 600, height: bottom })
      if (this.id === HELP['absent-vs-zero'].anchor) return rect({ top: target, left: 0, width: 600, height: 100 })
      return rect({ top: 0, left: 0, width: 0, height: 0 })
    })
    render(
      <div className="ctl-scroll">
        <HelpScreen topic="absent-vs-zero" />
      </div>,
    )
    return spy
  }

  /**
   * THE SCROLLPORT IS `.ctl-scroll`, NOT THE WINDOW. The frame is two grid
   * rows, the scroller and the dock under it (styles.css `.ctl-frame`), and
   * the dock's height can be dragged. A target whose top sits in the dock's
   * band is below the scroller's bottom edge, out of sight, and still above
   * `window.innerHeight` -- so measured against the window it counted as in
   * view, and the deep link left it under the dock.
   *
   * MUTATION: compare against `window.innerHeight`. The hidden target is not scrolled to.
   */
  it('scrolls to a target hidden under the dock, which the window alone counts as in view', () => {
    // A 768px window (jsdom's), a scroller ending at 600, the dock below it.
    // The target spans 620-720: inside the window, outside the scroller.
    expect(window.innerHeight, 'the case needs the target to fit the window').toBeGreaterThanOrEqual(720)
    const spy = scrollsInFrame(600, 620)
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.contexts[0]).toBe(document.getElementById(HELP['absent-vs-zero'].anchor))
  })

  it('leaves a target inside the scroller where it is', () => {
    const spy = scrollsInFrame(600, 100)
    expect(spy).not.toHaveBeenCalled()
  })
})
