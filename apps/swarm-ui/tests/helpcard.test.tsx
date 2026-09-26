/**
 * THE CARD OPENS ON FOCUS AND ON CLICK, NOT ONLY ON HOVER.
 *
 * This is the test the brief refused to make optional, and the reason is not
 * pedantry about WCAG. Hover does not exist on a touch device and does not
 * survive a screenshot. The screens this component is going on are the ones an
 * operator pastes into an incident channel at 3am -- so an explanation that
 * only hover can reach is an explanation that is absent exactly when it is
 * needed, which is the failure mode every honesty rule in this app exists to
 * prevent.
 *
 * HOW IT IS PROVED WITHOUT A BROWSER. `HelpCard.tsx` is split so that the
 * whole behaviour is `helpTransition` (pure), the whole markup is
 * `HelpCardView` (a state in, markup out), and `helpTriggerProps` is the only
 * wiring between an interaction and the state machine. Every state a browser
 * could put the card in is therefore a state this file can construct and
 * render. See `tests/run.mjs` for why there is no jsdom.
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  HELP_CLOSED,
  HOVER_DELAY_MS,
  HelpCard,
  HelpCardView,
  helpHoverProps,
  helpRole,
  helpTransition,
  helpTriggerProps,
  pressedInside,
  type HelpEvent,
  type HelpState,
} from '../src/HelpCard'
import { HELP } from '../src/help'

const TOPIC = 'absent-vs-zero' as const

function view(state: HelpState) {
  return renderToStaticMarkup(
    <HelpCardView
      topic={TOPIC}
      state={state}
      descriptionId="d"
      cardId="c"
      trigger={helpTriggerProps(() => {})}
      hover={helpHoverProps(() => {})}
    />,
  )
}

function collect(): { events: HelpEvent[]; dispatch: (e: HelpEvent) => void } {
  const events: HelpEvent[] = []
  return { events, dispatch: (e) => events.push(e) }
}

// ---------------------------------------------------------------------------
// The state machine
// ---------------------------------------------------------------------------

test('focus opens the card', () => {
  const after = helpTransition(HELP_CLOSED, { kind: 'focus' })
  assert.equal(after.open, true, 'a keyboard user cannot reach this explanation')
  assert.equal(helpRole(after), 'tooltip')
})

test('click opens the card AND pins it', () => {
  const after = helpTransition(HELP_CLOSED, { kind: 'click' })
  assert.equal(after.open, true, 'a touch user cannot reach this explanation')
  assert.equal(after.pinned, true)
  assert.equal(helpRole(after), 'dialog', 'a pinned card is a dialog, not a tooltip')
})

test('hover alone does not open it; the 120ms delay does', () => {
  const entered = helpTransition(HELP_CLOSED, { kind: 'pointer-enter' })
  assert.equal(entered.open, false, 'crossing the glyph flashes a card')
  assert.equal(entered.hovered, true)

  const settled = helpTransition(entered, { kind: 'hover-settled' })
  assert.equal(settled.open, true)
  assert.equal(HOVER_DELAY_MS, 120, 'the brief specifies a 120ms delay')
})

test('a delay that fires after the pointer left does not open a card', () => {
  const left = helpTransition(helpTransition(HELP_CLOSED, { kind: 'pointer-enter' }), {
    kind: 'pointer-leave',
  })
  assert.equal(helpTransition(left, { kind: 'hover-settled' }).open, false)
})

test('escape and an outside click dismiss a pinned card', () => {
  const pinned = helpTransition(HELP_CLOSED, { kind: 'click' })
  assert.equal(helpTransition(pinned, { kind: 'escape' }).open, false)
  assert.equal(helpTransition(pinned, { kind: 'outside-click' }).open, false)
  assert.equal(helpTransition(pinned, { kind: 'escape' }).pinned, false)
})

test('escape does not leave a state that springs back open', () => {
  // Pinned while hovered and focused, then dismissed. If `escape` kept either
  // flag, the next pointer-leave would re-derive `open` as true.
  let s = HELP_CLOSED
  for (const kind of ['pointer-enter', 'focus', 'click'] as const) {
    s = helpTransition(s, { kind })
  }
  const dismissed = helpTransition(s, { kind: 'escape' })
  assert.deepEqual(dismissed, HELP_CLOSED)
  assert.equal(helpTransition(dismissed, { kind: 'pointer-leave' }).open, false)
})

test('a pinned card survives the pointer leaving and the trigger blurring', () => {
  let s = helpTransition(HELP_CLOSED, { kind: 'pointer-enter' })
  s = helpTransition(s, { kind: 'hover-settled' })
  s = helpTransition(s, { kind: 'click' })
  assert.equal(helpTransition(s, { kind: 'pointer-leave' }).open, true)
  assert.equal(helpTransition(s, { kind: 'blur' }).open, true)
})

test('an unpinned card closes when the pointer leaves and focus goes', () => {
  let s = helpTransition(HELP_CLOSED, { kind: 'pointer-enter' })
  s = helpTransition(s, { kind: 'hover-settled' })
  assert.equal(helpTransition(s, { kind: 'pointer-leave' }).open, false)
})

// ---------------------------------------------------------------------------
// The wiring: an interaction reaches the state machine
// ---------------------------------------------------------------------------

test('the trigger sends focus, click and escape to the state machine', () => {
  const { events, dispatch } = collect()
  const p = helpTriggerProps(dispatch)

  p.onFocus()
  p.onClick()
  p.onKeyDown({ key: 'Escape' })
  p.onKeyDown({ key: 'a' })
  p.onBlur()

  assert.deepEqual(
    events.map((e) => e.kind),
    ['focus', 'click', 'escape', 'blur'],
    'a key that is not Escape must not dismiss, and focus/click must both arrive',
  )
})

/**
 * AH-6, the rule on its own. A press is outside only if it landed in neither
 * the card nor its trigger; `src/__tests__/helpcard.placement.test.tsx` drives
 * the same rule through real pointer events.
 *
 * MUTATION: return false unconditionally (every press is outside, the defect).
 */
test('a press on the card or its trigger is not an outside press', () => {
  const node = {} as Node
  const holding = { contains: (n: Node | null) => n === node }
  const empty = { contains: () => false }
  assert.equal(pressedInside(node, [holding, null]), true, 'a press inside the card counted as outside')
  assert.equal(pressedInside(node, [null, holding]), true, 'a press on the trigger counted as outside')
  assert.equal(pressedInside(node, [empty, empty]), false, 'a press elsewhere did not count as outside')
  assert.equal(pressedInside(null, [holding]), false, 'a press with no target counted as inside')
  assert.equal(pressedInside(node, [null, null]), false, 'an unmounted card swallowed an outside press')
})

test('hover is on the wrapper, so the card itself counts as hovered', () => {
  const { events, dispatch } = collect()
  const h = helpHoverProps(dispatch)
  h.onPointerEnter()
  h.onPointerLeave()
  assert.deepEqual(
    events.map((e) => e.kind),
    ['pointer-enter', 'pointer-leave'],
  )
})

// ---------------------------------------------------------------------------
// The view: what each state actually renders
// ---------------------------------------------------------------------------

test('the trigger is a real button, so Tab can reach it', () => {
  const markup = view(HELP_CLOSED)
  assert.match(markup, /<button[^>]*type="button"/, 'a span with a click handler is not focusable')
  assert.match(markup, /aria-label="[^"]+"/)
  assert.match(markup, /aria-expanded="false"/)
})

test('the card is absent until something opens it', () => {
  const markup = view(HELP_CLOSED)
  assert.ok(!markup.includes('role="tooltip"'))
  assert.ok(!markup.includes('role="dialog"'))
})

test('the state focus produces renders a tooltip', () => {
  const markup = view(helpTransition(HELP_CLOSED, { kind: 'focus' }))
  assert.ok(markup.includes('role="tooltip"'), 'focus renders nothing')
  assert.match(markup, /aria-expanded="true"/)
  assert.ok(markup.includes('#help/absent-vs-zero'), 'no link to the long form')
})

test('the state click produces renders a dialog', () => {
  const markup = view(helpTransition(HELP_CLOSED, { kind: 'click' }))
  assert.ok(markup.includes('role="dialog"'), 'click renders nothing')
})

test('the short text is published to assistive tech without any interaction', () => {
  // `aria-describedby` on the label points at this node. If it only existed
  // while the card was open, a screen reader would have to find and open a
  // popup to hear what a sighted reader gets by hovering.
  const markup = view(HELP_CLOSED)
  assert.ok(markup.includes('id="d"'), 'the description node is missing when closed')
})

test('a card whose topic carries values renders them, read from types.ts', () => {
  const markup = renderToStaticMarkup(
    <HelpCardView
      topic="capacity"
      state={helpTransition(HELP_CLOSED, { kind: 'click' })}
      descriptionId="d"
      cardId="c"
      trigger={helpTriggerProps(() => {})}
      hover={helpHoverProps(() => {})}
    />,
  )
  assert.ok(markup.includes('LEASED'), 'the capacity card lists no states')
})

// ---------------------------------------------------------------------------
// What the glyph and the card look like (AH-4, CH-3, CH-7, CH-9)
// ---------------------------------------------------------------------------

/** The inline style of the first element whose opening tag matches `tag`. */
function styleOf(markup: string, tag: RegExp): string {
  const m = tag.exec(markup)
  assert.ok(m, `no element matched ${tag}`)
  const s = /style="([^"]*)"/.exec(m[0])
  assert.ok(s, `the element matched by ${tag} carries no inline style`)
  return s[1]!
}

/**
 * AH-4. The accessible name was built as "What <title> means", which read
 * "What what reserves capacity means" for a title that is itself a question
 * and "What absent is not zero means" for one that is a sentence. A name that
 * works for every title is the title, said to be help.
 *
 * MUTATION: restore the "What ... means" template.
 */
test('the glyph is named "Help: <title>" whatever shape the title has', () => {
  for (const topic of ['absent-vs-zero', 'capacity'] as const) {
    const markup = renderToStaticMarkup(
      <HelpCardView
        topic={topic}
        state={HELP_CLOSED}
        descriptionId="d"
        cardId="c"
        trigger={helpTriggerProps(() => {})}
        hover={helpHoverProps(() => {})}
      />,
    )
    const title = HELP[topic].title
    assert.ok(markup.includes(`aria-label="Help: ${title}"`), `the ${topic} glyph is not named "Help: ${title}"`)
    assert.ok(!/aria-label="What /.test(markup), 'the old "What ... means" name is back')
  }
})

/**
 * CH-3. The card title was forced to capitals and tracked through an inline
 * style -- the one place the stylesheet's "nothing shouts" rule could not see.
 * The values chips showed raw uppercase enums. Both now sit in the console's
 * register: the title as written, the chips lowercased by CSS so the strings
 * themselves stay the owner's spelling.
 *
 * MUTATION: put `textTransform: 'uppercase'` or the `.04em` tracking back on
 * CARD_TITLE; or drop the lowercase transform from CARD_VALUE.
 */
test('the card title does not shout, and its enum chips are lowercased by style', () => {
  const markup = renderToStaticMarkup(
    <HelpCardView
      topic="capacity"
      state={helpTransition(HELP_CLOSED, { kind: 'click' })}
      descriptionId="d"
      cardId="c"
      trigger={helpTriggerProps(() => {})}
      hover={helpHoverProps(() => {})}
    />,
  )
  const title = styleOf(markup, /<strong[^>]*>/)
  assert.ok(!/text-transform:uppercase/.test(title), 'the card title is still uppercased')
  assert.ok(!/letter-spacing:\.?[0-9]/.test(title), 'the card title is still tracked')
  const chip = styleOf(markup, /<li[^>]*>/)
  assert.match(chip, /text-transform:lowercase/, 'the value chips are not lowercased')
  // The TEXT is untouched -- the state names still arrive in their own case.
  assert.ok(markup.includes('>LEASED<'), 'the chip text was rewritten rather than styled')
})

/**
 * CH-7. `:where(.app button) { min-height: 28px }` outranked the glyph's 14px
 * height, so the disc rendered as a 14x28 pill. An inline min-height equal to
 * the size wins over any sheet rule that does not use !important.
 *
 * MUTATION: drop the min-height. The sheet's 28px floor stretches it again.
 */
test('the glyph holds its own height against the button floor', () => {
  const style = styleOf(renderToStaticMarkup(<HelpCard topic={TOPIC} />), /<button[^>]*>/)
  assert.match(style, /(^|;)height:14px/)
  assert.match(style, /min-height:14px/, 'the glyph has no min-height, so the 28px button floor stretches it')
})

/**
 * CH-9. The card drew `rgba(0,0,0,.28)` in both themes -- the dark theme's
 * drop -- which reads as grime on the light background, while the token made
 * for exactly this surface (`--ctl-shadow-pop`, corrected per theme) was used
 * nowhere.
 *
 * MUTATION: put the literal shadow back.
 */
test('the card lifts with the popover token, which follows the theme', () => {
  const markup = renderToStaticMarkup(
    <HelpCardView
      topic={TOPIC}
      state={helpTransition(HELP_CLOSED, { kind: 'click' })}
      descriptionId="d"
      cardId="c"
      trigger={helpTriggerProps(() => {})}
      hover={helpHoverProps(() => {})}
    />,
  )
  const card = styleOf(markup, /<span[^>]*role="dialog"[^>]*>/)
  assert.match(card, /box-shadow:var\(--ctl-shadow-pop\)/)
  assert.ok(!/rgba?\(/.test(card), 'the card still hard-codes a shadow colour')
})

test('<HelpCard> renders closed by default', () => {
  const markup = renderToStaticMarkup(<HelpCard topic={TOPIC} />)
  assert.match(markup, /<button[^>]*type="button"/)
  assert.ok(!markup.includes('role="tooltip"'))
  assert.ok(!markup.includes('role="dialog"'))
})
