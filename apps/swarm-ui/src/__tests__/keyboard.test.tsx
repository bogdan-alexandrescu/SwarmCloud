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

//
// WHERE THE SWEEP IS. The walk over every route runs in slices,
// `keyboard.<group>.test.tsx`, built by `keyboardsweep.tsx` (part of #2688,
// 2026-11-07: the single file took 383 s and set the floor under its shard).
// This file holds the probe's own fixture, the sheet's rings, the drawer and
// the help card's bridge, and the bookkeeping that keeps the slices whole.

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import STYLES from '../styles.css?raw'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render } from '@testing-library/react'

import { App, SECTIONS } from '../App'
import { declaredTabIndex, isTabStop, tabStops } from '../focus'
import { outlineKillers } from './keyboard'
import {
  helpFloor,
  helpTriggersInSource,
  KEYBOARD_GROUPS,
  KEYBOARD_HELPS_TOTAL,
  POINTERS,
  RINGS,
  ROUTES,
  settle,
  STOP_FLOOR,
  tabSwallowed,
} from './keyboardsweep'
import { resolveSheet } from './spaceprobe'

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

    const rowTaskId = row!.getAttribute('data-task-id')
    expect(rowTaskId, 'the row names no agent').not.toBeNull()
    await act(async () => {
      row!.focus()
    })
    // The row is not a button around its copy-id button (visual QA QA-032):
    // focus given to it passes to its opener, the empty button over the row.
    expect(document.activeElement, 'the row is not focusable').toBe(row!.querySelector(':scope > .cr-open'))

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

    // THE PANEL THAT TAKES FOCUS DRAWS A RING THE SHEET CHOSE (AG-31). Moving
    // focus onto the panel is right; with no `:focus-visible` rule reaching it,
    // a keyboard open drew the user agent's default ring around the whole
    // viewport-tall drawer. The sweep (keyboardsweep.tsx) counts ringed stops; the panel is
    // not a tab stop (tabindex -1), so it is asked here, by the same stripped
    // selectors the sweep uses. MUTATION: delete `.ctl-drawer:focus-visible`.
    const ringedBy = RINGS.filter((sel) => {
      try {
        return panel!.matches(sel)
      } catch {
        return false
      }
    })
    expect(ringedBy, 'no :focus-visible rule in the sheet reaches the drawer that focus moves onto').not.toEqual([])

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
      if (el.matches('button[aria-label^="Help: "]')) {
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
    // The list beside an open agent draws the compact row and the full row
    // returns when it closes, so the node clicked is gone: the property is that
    // focus is on the row of the SAME agent, not on <body>.
    const back = document.activeElement as HTMLElement | null
    expect(back?.matches('.row.clickable > .cr-open'), 'closing the drawer did not put focus back on a row').toBe(true)
    expect(back?.closest('[data-task-id]')?.getAttribute('data-task-id'), 'focus came back to a different agent\'s row').toBe(rowTaskId)
    unmount()
  }, 60000)

  /**
   * THE BRIDGE ITSELF: Tab from the `?` into the portalled card, and back out.
   *
   * The sweep (keyboardsweep.tsx) asserts that a portalled card with a tab stop declares
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

    // The first `?` whose card actually carries a tab stop, because a bridge
    // over nothing proves nothing. Since AH-5 that is usually the head's
    // `SectionQuestion`, whose card now ends in `Help →` and is bridged by the
    // same `useCardBridge` the topic cards use -- so this exercises both.
    let trigger: HTMLElement | null = null
    let link: HTMLElement | null = null
    for (const candidate of container.querySelectorAll<HTMLElement>('button[aria-label^="Help: "]')) {
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

/** Each slice's file, found the way vitest finds it, so a group with no file fails. */
const SLICE_FILES = Object.keys(import.meta.glob('./keyboard.*.test.tsx'))

describe('keyboard traversal: the slices', () => {
  it('walks the route list the rail derives, not a copy of it', () => {
    expect(ROUTES).toEqual(SECTIONS.flatMap((s) => s.tabs.map((t) => `${s.id}/${t.id}`)))
    expect(ROUTES.length, 'the rail reached no routes').toBeGreaterThan(0)
  })

  it('puts every route in exactly one slice', () => {
    const owners = new Map<string, string[]>()
    for (const g of KEYBOARD_GROUPS)
      for (const r of g.routes) owners.set(r, [...(owners.get(r) ?? []), g.name])
    expect(ROUTES.filter((r) => owners.get(r) === undefined), 'routes no slice walks').toEqual([])
    expect(
      [...owners].filter(([, gs]) => gs.length > 1).map(([r, gs]) => `${r} in ${gs.join(', ')}`),
      'routes more than one slice walks',
    ).toEqual([])
    expect([...owners.keys()].filter((r) => !ROUTES.includes(r)), 'slice routes the rail cannot reach').toEqual([])
  })

  it('runs every slice from its own file, and nothing else as a slice', () => {
    expect(new Set(KEYBOARD_GROUPS.map((g) => g.name)).size, 'two slices share a name').toBe(KEYBOARD_GROUPS.length)
    expect([...SLICE_FILES].sort()).toEqual(KEYBOARD_GROUPS.map((g) => `./keyboard.${g.name}.test.tsx`).sort())
  })

  // The single sweep required more than STOP_FLOOR stops and at least half the
  // source's `?` call sites opened. Each slice asserts its part, so every slice
  // passing means the whole walk still cleared both.
  it('keeps the single sweep’s floors: the slices’ floors add up to them', () => {
    expect(STOP_FLOOR).toBe(480)
    for (const g of KEYBOARD_GROUPS) {
      expect(g.stopFloor, `${g.name} has no stop floor`).toBeGreaterThan(0)
      expect(g.helps, `${g.name} has no help share`).toBeGreaterThan(0)
    }
    expect(KEYBOARD_GROUPS.reduce((n, g) => n + g.stopFloor, 0)).toBeGreaterThanOrEqual(STOP_FLOOR)
    const inSource = helpTriggersInSource()
    expect(inSource, 'the source renders no `?` at all').toBeGreaterThan(0)
    expect(KEYBOARD_HELPS_TOTAL).toBe(KEYBOARD_GROUPS.reduce((n, g) => n + g.helps, 0))
    expect(KEYBOARD_GROUPS.reduce((n, g) => n + helpFloor(g, inSource), 0)).toBeGreaterThanOrEqual(Math.ceil(inSource / 2))
  })

  // THE SLICES' RECORDED WEIGHTS. The CI split (scripts/lib/ui_shards.py)
  // places a file by its weight in ui_test_durations.json and gives an
  // unrecorded one 2.9 s, so a slice with no figure lands on a shard as if it
  // were light. The owner's line for splitting a file is about 150 s.
  it('records every slice’s CI weight, each under the 150 s split line', () => {
    const weights = JSON.parse(
      readFileSync(resolve(__dirname, '../../../../tests/unit/scripts/ui_test_durations.json'), 'utf8'),
    ) as Record<string, number>
    const files = ['keyboard', ...KEYBOARD_GROUPS.map((g) => `keyboard.${g.name}`)].map((f) => `src/__tests__/${f}.test.tsx`)
    expect(files.filter((f) => weights[f] === undefined), 'slices with no recorded weight').toEqual([])
    expect(
      files.filter((f) => (weights[f] ?? 0) > 150).map((f) => `${f}: ${weights[f]} s`),
      'slices over the 150 s split line: split them',
    ).toEqual([])
  })
})
