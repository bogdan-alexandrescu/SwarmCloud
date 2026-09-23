// THE KEYBOARD GATE. Fifteen routes, every tab stop on each of them, pressed.
//
// WHY THIS SUITE EXISTS. The visual QA pass exercised 588 interactive elements
// by click and none of them by Tab, and it named its own prime suspect: the
// help cards had just been changed to PORTAL to `document.body` so they could
// escape a stacking context. That fixed nine cards which opened where nobody
// could read them. It also moved every card OUT OF DOM ORDER -- and DOM order
// IS tab order -- so the "Full explanation" link inside a card opened from the
// middle of a screen landed after every other control on the page. Nothing had
// checked, because nothing could: there was no test in this repository that
// pressed a key.
//
// WHAT IT FOUND, which is recorded here because a test's value is the defects
// it caught and not the ones it might:
//
//   1. BOTH PANE RESIZERS WERE MOUSE-ONLY. `.ctl-inspector-grip` and
//      `.ctl-dock-grip` were `<div role="separator">` with four pointer
//      handlers and no `tabindex`, so the inspector's width and the dock's
//      height could be changed by dragging and by nothing else.
//   2. THE AGENT DRAWER RESTORED FOCUS NOWHERE. It carried `role="dialog"`
//      and no focus handling at all: closing it unmounted the element holding
//      focus, which puts `document.activeElement` back on `<body>`. A reader
//      forty rows down the list was returned to the top of the document.
//   3. THE PORTALLED HELP CARD'S LINK WAS OUT OF ORDER WHEN PINNED AND
//      UNREACHABLE OTHERWISE -- tabbing off the trigger blurs it, and an
//      unpinned card closes on blur, so the card being tabbed towards shut
//      before it was reached.
//
// WHAT IT MEASURES AND WHAT IT CANNOT. `keyboard.ts` carries the predicates
// and says, at length, what a DOM with no React handlers and no layout engine
// can and cannot answer. The short version: this file cannot see overlap and
// therefore cannot assert "tab order follows VISUAL order" directly. It
// asserts the two things that make tab order and visual order come apart in
// this app -- a positive `tabindex` and a portal -- and it says so rather than
// claiming the coverage it does not have.
//
// HOW TO USE IT WHEN IT FAILS. Every assertion prints the per-route table
// first, so the counts before and after a change are always in the log.

import STYLES from '../styles.css?raw'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, createEvent, fireEvent, render } from '@testing-library/react'

import { App, SECTIONS } from '../App'
import { declaredTabIndex, isTabStop, tabStops } from '../focus'
import {
  outlineKillers,
  pointerSelectors,
  probeKeyboard,
  ringSelectors,
  signature,
  type Finding,
} from './keyboard'
import { resolveSheet } from './spaceprobe'

/**
 * Every route the rail can reach, DERIVED rather than restated.
 *
 * The same expression `spacing.test.tsx` uses, and for the same reason it now
 * uses it: that file held a hand-written copy of these fifteen routes, two
 * sections were renamed, nine of its routes resolved through SECTION_ALIASES
 * instead of failing, and the sweep quietly examined 500 fewer shapes while
 * reporting zero findings. A list kept in step by hand is a list that will
 * not be.
 */
const ROUTES = SECTIONS.flatMap((s) => s.tabs.map((t) => `${s.id}/${t.id}`))

/**
 * 700ms, on a clock this file controls -- see `spacing.test.tsx` for the long
 * version. Past every fixture delay in `api.ts` (30-350ms) and short of the 1s
 * and 5s refresh intervals in `Agents.tsx`, `Overview.tsx`, `App.tsx` and
 * `Dock.tsx`, so each screen sees a fixed number of ticks rather than a
 * function of how busy the machine is.
 */
const CLOCK_MS = 700

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(CLOCK_MS)
  })
}

/**
 * Press Tab on an element and answer whether a handler swallowed it.
 *
 * THE EVENT IS BUILT AND READ BACK RATHER THAN TRUSTED TO `fireEvent`'s RETURN
 * VALUE. `fireEvent` answers with `dispatchEvent`'s boolean, which is the same
 * fact -- but through two wrappers (`@testing-library/react` re-wraps every
 * event in `act` and hands the result back), and "the return value survives
 * both wrappers" is an assumption about a library rather than an observation
 * about this app. `defaultPrevented` on the event object is the observation.
 *
 * jsdom implements no sequential focus navigation, so this moves nothing by
 * itself. That is exactly why the question is phrased as "was it swallowed":
 * in this app the only way to change where Tab goes is `preventDefault`, so an
 * unprevented Tab is one the browser will handle normally.
 */
function tabSwallowed(el: Element, shiftKey = false): boolean {
  const ev = createEvent.keyDown(el, { key: 'Tab', shiftKey })
  fireEvent(el, ev)
  return ev.defaultPrevented
}

/** Selectors read off the sheet once. Neither depends on what is rendered. */
const RINGS = ringSelectors(STYLES)
const POINTERS = pointerSelectors(STYLES)

/**
 * THE FLOOR, AND WHY IT IS THIS NUMBER.
 *
 * THE SHELL ALONE IS 375, AND THAT IS THE NUMBER THIS FLOOR IS ABOUT. The rail
 * draws six section buttons, fourteen tab buttons (Overview's single pane
 * draws no second level) and two utility buttons on EVERY route: 22 tab stops
 * that are there whatever the screen behind them does. The header's home link,
 * the dock's line and the head's `?` add three more. 25 x 15 routes = 375
 * before a single screen has rendered anything at all.
 *
 * So 420 asserts "the shell, plus at least 45 controls that came from actual
 * screens". It is deliberately far under what a working sweep reports, because
 * a floor's job is to fail when the sweep reaches NOTHING -- the shape this
 * repository keeps producing: a loop that did not word-split, a probe that
 * returned `[]`, a route list that went stale and examined 500 fewer shapes
 * with nothing red. What it does NOT do is notice one screen going blank;
 * that is what the per-route table printed on every run is for.
 */
const STOP_FLOOR = 420

/**
 * 25 `?` triggers opened, dismissed and checked for focus return.
 *
 * Fifteen of those are certain: `SectionQuestion` puts one in the head of
 * every route. The rest are `<HelpCard>`s on screens whose fixtures resolve --
 * and some screens in this app read `/v1/admin/*`, answer 403 and render an
 * admin panel instead of their content, so the count is a property of the
 * fixtures rather than of the source. 25 is chosen to be true even when
 * several screens render their failure state, for the same reason the stop
 * floor is: it exists to catch a sweep that opened nothing.
 */
const HELP_FLOOR = 25

let sheet: HTMLStyleElement

describe('keyboard traversal', () => {
  beforeAll(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    // THE SHIPPED SHEET, RESOLVED. Two things here need it and both are
    // silent without it: `display: none` is how `styles.css` removes the
    // inner drawer's close button from the focus order, and `position: fixed`
    // vs `sticky` is how `isOverlay` tells the modal drawer from the side
    // column. With `css: false` in vitest.config.ts this import would be the
    // empty string and every one of those questions would answer "no rule",
    // which reads as a pass.
    sheet = document.createElement('style')
    sheet.textContent = resolveSheet(STYLES, 'dark')
    document.head.appendChild(sheet)
  })

  afterAll(() => {
    sheet.remove()
    window.location.hash = ''
    vi.useRealTimers()
  })

  /**
   * THE PROBE'S OWN DEFINITION, FIRST, AGAINST A KNOWN ANSWER.
   *
   * `tabStops` is imported by the components AND by the sweep, so that the
   * drawer's trap and the measurement of it cannot disagree. The cost of
   * sharing it is that the sweep cannot notice it breaking -- a `tabStops`
   * that returned `[]` would empty every count below and report a flawless
   * app. This fixture is the guard: nine elements with one right answer, run
   * before anything is rendered.
   */
  it('agrees with a fixture about what Tab stops on', () => {
    const box = document.createElement('div')
    box.innerHTML = [
      '<button id="a">a</button>',
      '<button id="b" disabled>b</button>',
      '<a id="c" href="#x">c</a>',
      '<a id="d">d</a>',
      '<input id="e">',
      '<div id="f" tabindex="0"></div>',
      '<div id="g" tabindex="-1"></div>',
      '<span id="h">h</span>',
      '<button id="i" hidden>i</button>',
    ].join('')
    document.body.appendChild(box)
    try {
      // `b` is disabled, `d` is an anchor with no href, `g` is explicitly out
      // of the sequence, `h` is not focusable at all and `i` is hidden.
      expect(tabStops(box).map((el) => el.id)).toEqual(['a', 'c', 'e', 'f'])
      expect(isTabStop(box.querySelector('#g')!)).toBe(false)
      expect(declaredTabIndex(box.querySelector('#g')!)).toBe(-1)
      expect(declaredTabIndex(box.querySelector('#a')!)).toBeNull()
    } finally {
      box.remove()
    }
  })

  /**
   * THE SHEET MUST NOT TAKE A RING AWAY WITHOUT PUTTING ONE BACK.
   *
   * This is the only assertion in the file that can catch a defect nobody has
   * written yet, and it is the honest form of "every interactive element has a
   * visible focus indicator". Every engine draws its own ring on a focused
   * control, so an element with no rule in `styles.css` is still visibly
   * focused -- 7,600 lines of this sheet contain no `outline: none` at all,
   * which the sweep below confirms by counting how many stops a rule DOES
   * reach rather than by claiming they all need one.
   *
   * The way a focus indicator actually disappears from an app is one
   * `outline: none` written to tidy up a button, on a selector with no
   * `:focus-visible` beside it. That is what this fails on.
   */
  it('never removes a focus ring it does not replace', () => {
    expect(outlineKillers(STYLES).map((f) => `${f.where} — ${f.detail}`)).toEqual([])
    // A sheet this file could not parse would produce zero killers AND zero
    // ring selectors, which is the silent-pass shape. 20 is well under the 31
    // `:focus-visible` rules the sheet carries today.
    expect(RINGS.length, 'the sheet parsed into almost no focus rules').toBeGreaterThan(20)
    expect(POINTERS.length, 'the sheet parsed into almost no cursor rules').toBeGreaterThan(20)
  })

  it('reaches every control on every route, and nothing swallows Tab', async () => {
    const per: { route: string; stops: number; helps: number; elements: number }[] = []
    const findings: Finding[] = []
    const unsupported = new Set<string>()
    const swallowers: string[] = []
    let stops = 0
    let ringed = 0
    let helps = 0

    for (const route of ROUTES) {
      window.location.hash = `#${route}`
      const { container, unmount } = render(<App />)
      await settle()

      // Overview ships its own grid and its own cursors in a `<style>`
      // element (`OVERVIEW_CSS`). Left out, every control it draws would read
      // as one the sheet never gave a cursor to -- a gap on the one screen
      // this product opens on.
      const local = [...container.querySelectorAll('style')].flatMap((el) =>
        pointerSelectors(el.textContent ?? ''),
      )

      const r = probeKeyboard(document, {
        appRoot: container,
        ringSelectors: RINGS,
        pointerSelectors: [...POINTERS, ...local],
      })
      findings.push(...r.findings.map((f) => ({ ...f, where: `${route} ${f.where}` })))
      for (const u of r.unsupported) unsupported.add(u)
      stops += r.stops
      ringed += r.ringed

      // EVERY `?` ON THIS SCREEN, OPENED FROM THE KEYBOARD AND DISMISSED WITH
      // ESCAPE. `button[aria-label^="What "]` is both kinds -- `HelpCard`'s
      // "What absent vs zero means" and `SectionQuestion`'s "What the Work
      // section answers" -- and no other control in the app is named that way.
      const triggers = [...container.querySelectorAll<HTMLElement>('button[aria-label^="What "]')]
      for (const trigger of triggers) {
        const name = trigger.getAttribute('aria-label') ?? ''
        await act(async () => {
          trigger.focus()
        })
        expect(trigger.getAttribute('aria-expanded'), `${route}: "${name}" did not open on focus`).toBe(
          'true',
        )
        const cardId = trigger.getAttribute('aria-controls')
        expect(cardId, `${route}: "${name}" is open and controls nothing`).not.toBeNull()
        const card = document.getElementById(cardId ?? '')
        expect(card, `${route}: "${name}" points aria-controls at no element`).not.toBeNull()

        // A PORTALLED CARD WITH A TAB STOP IN IT HAS TO SAY WHERE FOCUS GOES
        // BACK TO. This is the assertion the portal defect fails: strip
        // `data-focus-return` or the bridge that justifies it and this names
        // the card. A card with nothing focusable in it needs no bridge and
        // is not asked for one.
        if (card !== null && tabStops(card).length > 0) {
          expect(
            card.closest('[data-focus-return]')?.getAttribute('data-focus-return'),
            `${route}: "${name}" is portalled with a tab stop in it and no way back`,
          ).toBe(trigger.id)
        }

        await act(async () => {
          fireEvent.keyDown(trigger, { key: 'Escape' })
        })
        expect(trigger.getAttribute('aria-expanded'), `${route}: "${name}" survived Escape`).toBe(
          'false',
        )
        expect(document.getElementById(cardId ?? ''), `${route}: "${name}" left its card behind`).toBeNull()
        expect(document.activeElement, `${route}: "${name}" lost focus on Escape`).toBe(trigger)
        helps += 1
      }

      // NOTHING ON A PLAIN ROUTE MAY SWALLOW TAB. The only way to change where
      // Tab goes in this app is `preventDefault` on the keydown, and
      // `fireEvent` answers false when that happened. None of these fifteen
      // routes opens a drawer, and every help card above was dismissed, so the
      // expected set here is empty -- the drawer's trap is asserted on its own
      // route, below, where it is meant to exist.
      // `r.stopElements`, not a second `tabStops(document.body)`: the probe has
      // already paid for the computed-style pass that produces this list, and
      // paying for it twice per route is most of this suite's running time.
      for (const el of r.stopElements) {
        for (const shiftKey of [false, true]) {
          if (tabSwallowed(el, shiftKey)) {
            swallowers.push(`${route} ${signature(el)}${shiftKey ? ' [shift]' : ''}`)
          }
        }
      }

      per.push({ route, stops: r.stops, helps: triggers.length, elements: r.elements })
      unmount()
    }

    // PRINTED BEFORE ANYTHING IS ASSERTED, for the reason `spacing.test.tsx`
    // gives: six assertions follow and the first to fail hides the rest, but
    // the useful artefact is the whole picture and "the numbers before and
    // after" cannot be read off a run that stopped at the first difference.
    console.log(
      [
        `${per.length} routes · ${stops} tab stops · ${helps} help cards opened` +
          ` · ${ringed} stops covered by a :focus-visible rule` +
          ` · ${findings.length} findings`,
        ...per.map(
          (p) => `  ${p.route.padEnd(22)} ${String(p.stops).padStart(4)} stops` +
            ` ${String(p.helps).padStart(3)} cards  of ${p.elements} elements`,
        ),
        ...findings.map((f) => `    ${f.kind} ${f.where} — ${f.detail}`),
        ...swallowers.map((s) => `    swallowed Tab: ${s}`),
        ...[...unsupported].map((s) => `    unsupported selector: ${s}`),
      ].join('\n'),
    )

    // THE SWEEP HAS TO HAVE LOOKED AT SOMETHING. Four guards, in the order
    // they would catch a break: a route list that stopped matching the rail, a
    // screen that rendered nothing, a body that produced no controls, and a
    // pass that opened no cards.
    expect(per.length, 'the sweep rendered a different number of routes than the rail has').toBe(
      ROUTES.length,
    )
    for (const p of per) {
      // The rail alone is 22 stops on every route, so this proves the SHELL
      // rendered rather than the screen. What notices a screen going empty is
      // the total below and the per-route numbers in the log.
      expect(p.stops, `${p.route} rendered almost no controls`).toBeGreaterThan(20)
    }
    expect(stops, 'the sweep reached almost nothing').toBeGreaterThan(STOP_FLOOR)
    expect(helps, 'the sweep opened almost no help cards').toBeGreaterThan(HELP_FLOOR)

    // Selectors jsdom would not evaluate. Each one is a rule that silently
    // stopped being checked, which is worse than a finding.
    expect([...unsupported]).toEqual([])
    expect(swallowers).toEqual([])
    expect(findings.map((f) => `${f.kind} ${f.where} — ${f.detail}`)).toEqual([])
  }, 240000)

  /**
   * THE DRAWER, WHICH IS THE ONE THAT MATTERS MOST.
   *
   * It opens over a list of forty rows and closing it should put you back on
   * the row you opened. Four things are asserted and each was broken before
   * this pass: focus moves in, Tab wraps at both ends, Escape closes it, and
   * the row gets focus back.
   *
   * WHICH LAYOUT THIS MEASURES, SAID OUT LOUD. The drawer is a modal overlay
   * below 1100px and a grid column beside the list above it, and trapping
   * focus in the column would be a worse bug than the one being fixed. jsdom
   * evaluates no media query, so `.drawer`'s own `position: fixed` wins and
   * this exercises the OVERLAY branch. That is asserted rather than assumed --
   * a jsdom that starts evaluating widths would otherwise silently measure the
   * other branch and report a trap that never ran. THE COLUMN BRANCH IS NOT
   * COVERED BY ANY TEST HERE; only a real browser at 1600px can show that Tab
   * still reaches the list beside it.
   */
  it('traps Tab, closes on Escape, and hands the row back', async () => {
    window.location.hash = '#work/running'
    const { container, unmount } = render(<App />)
    await settle()

    const row = container.querySelector<HTMLElement>('.row.clickable')
    expect(row, 'the agent list rendered no rows, so this test measured nothing').not.toBeNull()

    await act(async () => {
      row!.focus()
    })
    expect(document.activeElement, 'the row is not focusable').toBe(row)

    await act(async () => {
      fireEvent.click(row!)
      // The drawer is a ROUTE. jsdom's own `hashchange` is queued, and this
      // file runs on a fake clock, so the event is dispatched here rather than
      // waited for -- `App`'s listener reads `window.location.hash`, which the
      // click has already set synchronously.
      window.dispatchEvent(new Event('hashchange'))
    })
    await settle()

    const panel = document.querySelector<HTMLElement>('.ctl-drawer')
    expect(panel, 'clicking a row did not open the inspector').not.toBeNull()
    expect(
      getComputedStyle(panel!).position,
      'this test is written for the overlay branch; jsdom applied a media query',
    ).toBe('fixed')
    expect(document.activeElement, 'the overlay opened without taking focus').toBe(panel)

    const inside = tabStops(panel!)
    // Grip, close, and two pane tabs at the very least. A drawer with one stop
    // cannot demonstrate a wrap and would pass this test vacuously.
    expect(inside.length, 'the drawer has too few tab stops to show a wrap').toBeGreaterThan(3)
    const first = inside[0]!
    const last = inside[inside.length - 1]!

    /*
     * FOCUS, THEN SHUT ANY CARD THAT OPENING IT OPENED.
     *
     * `AgentDetail.tsx` puts nine `?` glyphs inside this drawer, so the last
     * tab stop in it is quite possibly one -- and a `?` with its card open
     * bridges Tab INTO the card, correctly, which is a different mechanism
     * from the one being measured here. Closing the card first is what makes
     * the next Tab a question about the drawer's edge rather than about the
     * help card's bridge. Both are asserted; they are just not asserted at
     * once.
     */
    const focusQuietly = async (el: HTMLElement): Promise<void> => {
      await act(async () => {
        el.focus()
      })
      if (el.matches('button[aria-label^="What "]')) {
        await act(async () => {
          fireEvent.keyDown(el, { key: 'Escape' })
        })
      }
    }

    // `tabSwallowed` inside `act`: the wrap calls `.focus()` on another
    // control, and the element it leaves may be a `?` whose `onBlur` updates
    // state. React is entitled to complain about that outside `act`.
    let swallowed = false

    await focusQuietly(last)
    await act(async () => {
      swallowed = tabSwallowed(last)
    })
    expect(swallowed, 'Tab off the last control left the drawer').toBe(true)
    expect(document.activeElement, 'Tab at the end did not wrap to the first control').toBe(first)

    await act(async () => {
      swallowed = tabSwallowed(first, true)
    })
    expect(swallowed, 'Shift+Tab off the first control left the drawer').toBe(true)
    expect(document.activeElement, 'Shift+Tab at the start did not wrap to the last control').toBe(
      last,
    )

    // A control in the MIDDLE must not be intercepted, or this is not a trap
    // but a cage: Tab has to move normally everywhere except the two edges.
    // `inside[1]` is the drawer's own close button -- the grip is `inside[0]`,
    // being the panel's first child.
    const middle = inside[1]!
    await focusQuietly(middle)
    await act(async () => {
      swallowed = tabSwallowed(middle)
    })
    expect(
      swallowed,
      'Tab was intercepted in the middle of the drawer, not only at its edge',
    ).toBe(false)

    await act(async () => {
      fireEvent.keyDown(panel!, { key: 'Escape' })
      window.dispatchEvent(new Event('hashchange'))
    })
    await settle()

    expect(document.querySelector('.ctl-drawer'), 'Escape did not close the drawer').toBeNull()
    expect(document.activeElement, 'closing the drawer did not put focus back on the row').toBe(row)
    unmount()
  }, 60000)

  /**
   * THE BRIDGE ITSELF: Tab from the `?` into the portalled card, and back out.
   *
   * The sweep above asserts that a portalled card with a tab stop declares
   * where focus returns. This asserts that the declaration is true -- that the
   * link really is one Tab away from the trigger rather than at the end of the
   * document, and that leaving it does not leave the reader inside a loop.
   *
   * THE THIRD TAB IS THE ANTI-TRAP ASSERTION. jsdom moves no focus on Tab, so
   * "and then it continues into the page" is not something this file can
   * watch. What it CAN establish is that the third Tab is not intercepted at
   * all -- the handler does not call `preventDefault` -- which is exactly the
   * condition under which the browser's own sequential navigation takes over.
   */
  it('tabs from a help trigger into its portalled card and back out', async () => {
    window.location.hash = '#overview/now'
    const { container, unmount } = render(<App />)
    await settle()

    // The first `?` whose card actually carries a tab stop: `SectionQuestion`
    // renders a card with none, and a bridge over nothing proves nothing.
    let trigger: HTMLElement | null = null
    let link: HTMLElement | null = null
    for (const candidate of container.querySelectorAll<HTMLElement>('button[aria-label^="What "]')) {
      await act(async () => {
        candidate.focus()
      })
      const card = document.getElementById(candidate.getAttribute('aria-controls') ?? '')
      const stops = card === null ? [] : tabStops(card)
      if (stops.length > 0) {
        trigger = candidate
        link = stops[0]!
        break
      }
      await act(async () => {
        fireEvent.keyDown(candidate, { key: 'Escape' })
      })
    }
    expect(trigger, 'no help card on this screen has anything focusable in it').not.toBeNull()

    // The card is portalled, so its link is NOT inside the app root -- which
    // is the whole defect, stated as an assertion rather than as prose.
    expect(container.contains(link), 'the card was not portalled; this test is measuring the old shape').toBe(
      false,
    )

    let swallowed = false
    await act(async () => {
      swallowed = tabSwallowed(trigger!)
    })
    expect(swallowed, 'Tab on the `?` was not intercepted, so the card was never bridged').toBe(true)
    expect(document.activeElement, 'Tab from the `?` did not reach the card').toBe(link)

    await act(async () => {
      swallowed = tabSwallowed(link!, true)
    })
    expect(swallowed, 'Shift+Tab inside the card was not intercepted').toBe(true)
    expect(document.activeElement, 'Shift+Tab out of the card did not return to the `?`').toBe(
      trigger,
    )
    expect(trigger!.getAttribute('aria-expanded'), 'leaving the card left it open').toBe('false')

    // AND NOW NOTHING IS INTERCEPTED. With the card shut the bridge is
    // inactive, so the next Tab is the browser's -- which is what makes this a
    // bridge rather than a loop between two elements. It is also the only form
    // "you can tab back out" can take here: jsdom moves no focus on Tab, so an
    // unprevented keydown IS the assertion.
    await act(async () => {
      swallowed = tabSwallowed(trigger!)
    })
    expect(swallowed, 'the `?` went on swallowing Tab after its card closed').toBe(false)

    unmount()
  }, 60000)
})
