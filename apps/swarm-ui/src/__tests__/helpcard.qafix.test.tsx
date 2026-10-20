/**
 * THE `?` ON A TASK PAGE, AND THE HEADING IT SITS IN (QAFIX-HELP, part of #1680).
 *
 * Two findings from the first console visual-QA run (wf_9e9bf74834634db7bcc3,
 * 2026-10-20), each held here by the property that was false:
 *
 *   QA-022. On a task page the first Tab stop, `Help: Work answers`, was under
 *   the sticky trail bar (`elementFromPoint` returned the breadcrumb's
 *   `A.ctl-crumb-at`), so its card opened below a glyph nobody could see; and
 *   every `?` cost three Tab stops -- the `?`, the card's link, then the `?`
 *   AGAIN, because leaving the card forwards handed focus back to its trigger.
 *   Held as: both kinds of glyph keep `--ctl-bar-h` of scroll margin above
 *   them, so the browser's focus scroll brings them out from under the bar;
 *   and Tab off the card's last stop lands on the control AFTER the `?`.
 *
 *   QA-070. The `<h2>` "Choose a runner" was named with its whole help text,
 *   because the hidden description node sat inside the heading and a
 *   clipped-but-rendered node is still name-from-content. Held as: the
 *   heading's accessible name carries none of the topic's short text, and the
 *   label that points `aria-describedby` at it is still described by it.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HelpCard } from '../HelpCard'
import { HELP } from '../help'
import { build, painted } from './marks'
import type { CascadeEnv } from './cssgate'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

describe('QA-022: a `?` is never left under the trail bar', () => {
  it('both kinds of glyph clear the sticky bar when focus scrolls to them', () => {
    // Built inside a frame that draws the bar, so `--ctl-bar-h` resolves to
    // the bar's own height (notifications.css: 48px, 44px at phone width)
    // rather than to its fallback -- the margin follows the bar, not a copy.
    for (const sel of ['.ctl-q-glyph', '.help-glyph']) {
      const glyph = build(`div.ctl-frame.has-ctl-bar ${sel}`, hosts)
      for (const [env, bar] of [[WIDE, 48], [PHONE, 44]] as const) {
        // MUTATION: drop the `scroll-margin-top` rule on `.ctl-q-glyph,
        // .help-glyph` in styles.css (or the `help-glyph` class on HelpCard's
        // button) and Tab parks the glyph under `.ctl-bar` again -- this reads
        // 'no rule'.
        expect(painted(glyph, 'scroll-margin-top', env) ?? 'no rule', `${sel} at ${env.width}px`).toMatch(new RegExp(`^calc\\(${bar}px \\+ `))
      }
    }
  })

  it("HelpCard's `?` carries the class that rule is written against", () => {
    render(<HelpCard topic="runner-profile-by-name" />)
    // MUTATION: remove `className="help-glyph"` from the trigger and the rule
    // above reaches only the section `?`, not the topic ones.
    expect(screen.getByRole('button', { name: /^Help: / }).classList.contains('help-glyph')).toBe(true)
  })

  it('a `?` costs two Tab stops: Tab off the card lands after the `?`, not on it again', async () => {
    render(
      <div>
        <button type="button">before</button>
        <HelpCard topic="runner-profile-by-name" />
        <button type="button">after</button>
      </div>,
    )
    const trigger = screen.getByRole('button', { name: /^Help: / })
    await act(async () => {
      trigger.focus()
    })
    await act(async () => {
      fireEvent.keyDown(trigger, { key: 'Tab' })
    })
    const link = screen.getByRole('link', { name: /Full explanation/ })
    expect(document.activeElement, 'Tab from the `?` did not reach the card').toBe(link)

    await act(async () => {
      fireEvent.keyDown(link, { key: 'Tab' })
    })
    // MUTATION: send forward Tab back through `dismissToTrigger` (the old
    // shape) and focus is on the `?` again -- the third stop QA-022 counted.
    expect(document.activeElement, 'Tab off the card returned to the `?`').toBe(
      screen.getByRole('button', { name: 'after' }),
    )
    // MUTATION: skip the dismissal on the forward exit and the card stays open
    // over the control focus just moved to.
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })

  it('Shift+Tab out of the card still returns to its `?`', async () => {
    render(
      <div>
        <HelpCard topic="runner-profile-by-name" />
        <button type="button">after</button>
      </div>,
    )
    const trigger = screen.getByRole('button', { name: /^Help: / })
    await act(async () => {
      trigger.focus()
    })
    await act(async () => {
      fireEvent.keyDown(trigger, { key: 'Tab' })
    })
    const link = screen.getByRole('link', { name: /Full explanation/ })
    await act(async () => {
      fireEvent.keyDown(link, { key: 'Tab', shiftKey: true })
    })
    // MUTATION: route Shift+Tab through the forward exit too and focus skips
    // past the `?` to whatever follows it.
    expect(document.activeElement).toBe(trigger)
  })
})

describe('QA-070: a heading holding a `?` is named by its words, not the help text', () => {
  const short = HELP['runner-profile-by-name'].short

  it("the h2's accessible name carries none of the topic's short text", () => {
    render(
      <h2>
        Choose a runner
        <HelpCard topic="runner-profile-by-name" />
      </h2>,
    )
    expect(screen.getByRole('heading', { level: 2, name: /^Choose a runner/ })).toBeTruthy()
    // The first sentence-sized slice of the short text: enough to be unique,
    // short enough to survive a wrap in help.ts.
    const probe = short.slice(0, 40)
    // MUTATION: render HelpNote without `hidden` (the clipped-but-rendered
    // span it was) and the heading's name runs on into ~330 chars of help --
    // this query then finds it.
    expect(screen.queryByRole('heading', { level: 2, name: new RegExp(probe.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')) })).toBeNull()
  })

  it('the label that points at the description is still described by it', () => {
    render(
      <p>
        <button type="button" aria-describedby="qa070-desc">
          runner
        </button>
        <HelpCard topic="runner-profile-by-name" describedById="qa070-desc" />
      </p>,
    )
    // MUTATION: drop the description node (rather than hiding it) and the
    // label loses the sentence it publishes to a screen reader.
    expect(screen.getByRole('button', { name: 'runner', description: short })).toBeTruthy()
  })
})
