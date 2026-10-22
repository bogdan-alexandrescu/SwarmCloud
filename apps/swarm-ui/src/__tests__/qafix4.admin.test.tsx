// QAFIX4-ADMIN (part of #1680): Admin › Pool limits, from the fourth console
// visual-QA run (wf_ecd935541ced4a539540, the fixture build of main at
// fabe4d6c, 1440 and 390 px, light and dark). One case per finding:
//
//   admin-008 (N6)  the GitHub fallback token inputs and the phone's 'filter
//                   pools' box drew a `--line` edge, 1.34-1.5:1 against what
//                   they sit on, with a fill 1.08:1 off it -- in dark the
//                   fields read as holes with no edge (WCAG 2.1 SC 1.4.11)
//   admin-009 (N7)  the fallback token card's `?` was a ghost Button toggling
//                   an inline paragraph, a bare grey glyph beside the page's
//                   two filled `HelpCard` badges
//
// The edges are read off the shipped sheets through `painted` (the cascade,
// with its `@media` blocks) and resolved per theme, so a rule that wins only
// at 390 or only in light is measured where it wins.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

import { FallbackTokenCard } from '../AdminSettings'
import { HELP } from '../help'
import type { CascadeEnv } from './cssgate'
import { build, painted, resolveColour, THEMES, type Theme } from './marks'
import { contrast, type RGBA } from './spaceprobe'

const WIDTHS = [1440, 390] as const
const TOPIC = 'github-fallback-token'
const COMMAND = 'uv run sc setup token --owner <org>'

const hosts: HTMLElement[] = []

afterEach(() => {
  // Unmount first: the opened card is portalled to <body>, and React must
  // remove it before anything else touches the document.
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
})

/** The colour a `border`/`background` value paints: its token, or its literal. */
function colourIn(value: string, theme: Theme): RGBA {
  const token = /var\(\s*--[\w-]+\s*\)/.exec(value)
  if (token !== null) return resolveColour(token[0], theme)
  const literal = /#[0-9a-f]{3,8}\b|rgba?\([^)]*\)/i.exec(value)
  if (literal === null) throw new Error(`no colour in ${JSON.stringify(value)}`)
  return resolveColour(literal[0], theme)
}

/** The first ancestor's painted background, else the page's `--bg`. */
function groundOf(el: Element, env: CascadeEnv, theme: Theme): { from: string; rgba: RGBA } {
  for (let node = el.parentElement; node !== null; node = node.parentElement) {
    const v = painted(node, ['background-color', 'background'], env)
    if (v === null || /^(none|transparent|inherit|initial|unset)$/.test(v.trim())) continue
    return { from: `${node.className || node.tagName}: ${v}`, rgba: colourIn(v, theme) }
  }
  return { from: 'the page: var(--bg)', rgba: resolveColour('var(--bg)', theme) }
}

/** The input's edge and fill against what it sits on, at one width and theme. */
function measure(input: HTMLElement, width: number, theme: Theme) {
  const env: CascadeEnv = { width, theme }
  const edge = painted(input, ['border-color', 'border'], env)
  const fill = painted(input, ['background-color', 'background'], env)
  expect(edge, 'the input paints no border').not.toBeNull()
  expect(fill, 'the input paints no fill').not.toBeNull()
  const ground = groundOf(input, env, theme)
  return {
    edge: contrast(colourIn(edge!, theme), ground.rgba),
    fill: contrast(colourIn(fill!, theme), ground.rgba),
    says: `${theme} at ${width}: border \`${edge}\`, fill \`${fill}\`, on ${ground.from}`,
  }
}

describe('admin-008 (N6): Pool limits’ text inputs have an edge of 3:1 against what they sit on', () => {
  it('the fallback token inputs, against the card, in both themes at 1440 and 390', () => {
    const input = build('section.c-card.adm-fallback-token > form.adm-token-form > input', hosts)
    let seen = 0
    for (const theme of THEMES) {
      for (const width of WIDTHS) {
        const m = measure(input, width, theme)
        seen++
        // MUTATION: put `border: 1px solid var(--line)` back on
        // `.adm-token-form > input` (styles/admin.css) -- 1.37:1 on the dark
        // card and 1.34:1 on the white one, with a `--bg` fill 1.08:1 off it.
        expect(Math.max(m.edge, m.fill), m.says).toBeGreaterThanOrEqual(3)
      }
    }
    // Four measurements, or the loops above measured nothing.
    expect(seen).toBe(4)
  })

  it('the filter over the pool names, against the page, in both themes at 1440 and 390', () => {
    const input = build('div.app > section.section.adm-ceilings.has-filter > div.ctl-toolbar.adm-filter > input', hosts)
    let seen = 0
    for (const theme of THEMES) {
      for (const width of WIDTHS) {
        const m = measure(input, width, theme)
        seen++
        // MUTATION: put `border: 1px solid var(--line)` back on
        // `.adm-filter > input` (styles/admin.css) -- the phone's 'filter
        // pools' box measured 1.2-1.5:1, a fill on its own ground.
        expect(Math.max(m.edge, m.fill), m.says).toBeGreaterThanOrEqual(3)
      }
    }
    expect(seen).toBe(4)
  })
})

describe('admin-009 (N7): the fallback token card’s help is the page’s HelpCard, not a bare toggle', () => {
  it('draws a HelpCard trigger for its own topic in the card head, and the inline toggle is gone', () => {
    render(<FallbackTokenCard />)
    const card = document.querySelector<HTMLElement>('section.adm-fallback-token')!
    expect(card).not.toBeNull()
    const head = card.querySelector<HTMLElement>('.c-card-h')!
    // MUTATION: put the ghost `<Button aria-label="About the fallback token">`
    // back in the card's `action` (AdminSettings.tsx) -- the head carries no
    // `Help: ...` trigger, the grey glyph run 4 saw.
    const trigger = within(head).getByRole('button', { name: `Help: ${HELP[TOPIC].title}` })
    expect(trigger.className).toContain('help-glyph')
    // MUTATION: keep the ghost Button beside the HelpCard -- two `?` on one card.
    expect(within(card).queryByRole('button', { name: 'About the fallback token' })).toBeNull()
    expect(card.querySelectorAll('button[aria-label^="Help: "]')).toHaveLength(1)
    // MUTATION: keep the `about` paragraph rendered in the card body -- the
    // words would be in two places, one of them a toggled paragraph.
    expect(card.querySelector('p.ur-small')).toBeNull()
  })

  it('opens a card that carries the paragraph’s words, with the terminal command as code', () => {
    render(<FallbackTokenCard />)
    const card = document.querySelector<HTMLElement>('section.adm-fallback-token')!
    fireEvent.click(within(card).getByRole('button', { name: `Help: ${HELP[TOPIC].title}` }))
    const dialog = screen.getByRole('dialog')
    // MUTATION: drop "resource owner is the org" from the topic's short
    // (help.ts) -- the one instruction the old paragraph gave goes missing.
    expect(dialog.textContent).toContain(HELP[TOPIC].short)
    expect(HELP[TOPIC].short).toMatch(/personal access token of yours, for one owner/)
    expect(HELP[TOPIC].short).toMatch(/fine-grained token whose resource owner is the org/)
    expect(HELP[TOPIC].short).toMatch(/removing the owner there revokes it/)
    // MUTATION: drop the command from the topic's `values` (help.ts) -- the
    // card no longer says how to do the same from a terminal.
    const command = within(dialog).getByText(COMMAND)
    // MUTATION: render the command as prose inside `short` -- it is no longer
    // a monospaced value of its own.
    expect(command.tagName).toBe('LI')
    expect(command.style.fontFamily).toBe('var(--mono)')
  })
})
