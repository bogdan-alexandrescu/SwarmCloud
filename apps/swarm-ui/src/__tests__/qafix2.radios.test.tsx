/**
 * THE SUBMIT FORMS' RADIO RINGS, AS THE SECOND CONSOLE VISUAL-QA RUN READ THEM
 * (QAFIX2-RADIOS, part of #1680; run wf_b6e2441b419f423a84eb, 2026-10-20).
 *
 *   work-b-006  an unselected radio's ring measured 1.31:1 against its card
 *               in both themes -- the hairline `--line` -- and it is the only
 *               mark an enabled choice carries: the runner cards on
 *               /submit/task, "A PR per step" / "One PR for all steps" on
 *               /submit/workflow. WCAG 1.4.11 asks 3:1 of it.
 *
 * Every enabled, unselected ring is held to 3:1 against the card it sits on,
 * in light and dark, inside a step not yet reached as well; the chosen and the
 * disabled ring are each held apart from it (the disabled one by its line and
 * fill, not its colour: QA-046). Colours are read through the
 * cascade (`painted`), never `getComputedStyle`, which jsdom cannot order.
 */
import { afterEach, describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { build, painted, resolveColour, THEMES, tokensIn, type Theme } from './marks'
import { contrast, over } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

/** Both placements of the forms' radio: a runner card and a dispatch option. */
const PLACES = [
  { name: 'a runner card', card: 'div.sbf-runners.is-cards > label.sbf-runner' },
  { name: 'a dispatch option', card: 'div.dsp-options > label.dsp-option' },
] as const

/** A native radio inside the card `selector` names, and that card. */
function radioIn(selector: string, state: { checked?: boolean; disabled?: boolean } = {}) {
  const card = build(selector, hosts)
  const input = document.createElement('input')
  input.type = 'radio'
  input.checked = state.checked ?? false
  input.disabled = state.disabled ?? false
  card.appendChild(input)
  return { card, input }
}

/** The one colour a `border` declaration paints, resolved for `theme`. */
function ringOf(input: HTMLElement, env: CascadeEnv, theme: Theme) {
  const border = painted(input, ['border', 'border-color'], env)
  expect(border, 'the radio has no border of its own').not.toBeNull()
  const tokens = tokensIn(border!, theme)
  expect(tokens, `${border} names no single colour token`).toHaveLength(1)
  return tokens[0]!.rgba
}

/** The ring's border-style and the fill inside it, resolved for `theme`. */
function lookOf(input: HTMLElement, env: CascadeEnv, theme: Theme) {
  const border = painted(input, ['border', 'border-style'], env)
  expect(border, 'the radio has no border of its own').not.toBeNull()
  const style = /\b(solid|dashed|dotted|double|groove|ridge|inset|outset|none|hidden)\b/.exec(border!)?.[1] ?? 'none'
  const fill = painted(input, ['background', 'background-color'], env)
  return { style, fill: fill === null ? null : resolveColour(fill, theme) }
}

/** What the card under the ring is painted. */
function cardOf(card: HTMLElement, env: CascadeEnv, theme: Theme) {
  const bg = painted(card, ['background', 'background-color'], env)
  expect(bg, 'the card has no background of its own').not.toBeNull()
  return resolveColour(bg!, theme)
}

/** The ring as it reaches the eye: its own opacity over the card. */
function seen(input: HTMLElement, env: CascadeEnv, theme: Theme, card: HTMLElement) {
  const opacity = Number(painted(input, 'opacity', env) ?? '1')
  const ring = ringOf(input, env, theme)
  const under = cardOf(card, env, theme)
  return contrast(over({ ...ring, a: ring.a * opacity }, under), under)
}

describe('work-b-006: an enabled, unselected radio ring is at least 3:1 against its card', () => {
  for (const theme of THEMES) {
    const env: CascadeEnv = { ...WIDE, theme }
    for (const place of PLACES) {
      it(`${theme}, ${place.name}`, () => {
        const { card, input } = radioIn(place.card)
        // MUTATION: paint the unselected ring `var(--line)` again (the
        // hairline, 1.37:1 dark / 1.34:1 light) and this falls under 3.
        expect(seen(input, env, theme, card)).toBeGreaterThanOrEqual(3)
      })

      it(`${theme}, ${place.name} in a step not reached yet`, () => {
        const { card, input } = radioIn(`section.sbf-move.sb-step.is-dim > ${place.card}`)
        // MUTATION: let `.sb-step.is-dim input { opacity: .55 }` reach the
        // radio again and the 3:1 ring is seen at about 1.9:1.
        expect(seen(input, env, theme, card)).toBeGreaterThanOrEqual(3)
      })
    }
  }
})

describe('work-b-006: the chosen and the disabled ring stay distinct from the unselected one', () => {
  for (const theme of THEMES) {
    const env: CascadeEnv = { ...WIDE, theme }
    for (const place of PLACES) {
      it(`${theme}, ${place.name}`, () => {
        const plain = radioIn(place.card).input
        const chosen = radioIn(place.card, { checked: true }).input
        const off = radioIn(place.card, { disabled: true }).input
        const plainBorder = painted(plain, ['border', 'border-color'], env)
        // MUTATION: drop the `:checked` rule and the chosen ring is the
        // unselected one -- no mark says which choice is made.
        expect(painted(chosen, ['border', 'border-color'], env)).not.toBe(plainBorder)
        // MUTATION: drop the `:disabled` rule and a refused runner's radio
        // reads exactly as a live one.
        expect(painted(off, ['border', 'border-color'], env)).not.toBe(plainBorder)
        // The disabled ring shares the live ring's 3:1 `--ctl-bd` colour on
        // purpose (QA-046: on the hairline a refused runner read as a card
        // with no control), so what sets it apart is its line and its fill.
        // BGD's decision of 2026-10-23 kept QA-046 and amended this assertion
        // from "a different colour" to "a different border-style or fill".
        // MUTATION: make the disabled ring `solid` on `--surface` and the two
        // are the same ring under two declarations.
        expect(lookOf(off, env, theme)).not.toEqual(lookOf(plain, env, theme))
      })
    }
  }
})
