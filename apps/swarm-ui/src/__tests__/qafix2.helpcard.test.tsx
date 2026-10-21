/**
 * VISUAL QA RUN 2 (wf_b6e2441b419f423a84eb, 2026-10-20), the two page-head
 * defects that also lived in HelpCard.tsx, outside QAFIX2-PAGEHEAD's territory
 * (QAFIX2-HELPCARD; part of #1680).
 *
 * a11y-keyboard-003  HelpCard's pinned card was role=dialog with no name,
 *     the defect PAGEHEAD fixed on the section card in App.tsx. Held as: the
 *     pinned dialog is named by its own title, and two cards on one page point
 *     at two different title ids.
 * admin-008, work-b-008  HelpCard's `?` disc was an inline --surface-2 fill,
 *     1.02:1 on the page in light. Held as: the disc paints --ctl-q-fill at
 *     rest and --ctl-q-fill-hover under the pointer in both themes, steps off
 *     the page by at least the dark chip, and keeps its glyph at AA.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HelpCard } from '../HelpCard'
import { HELP } from '../help'
import type { CascadeEnv } from './cssgate'
import { expandLocal, resolveColour, sameColour, THEMES, type Theme } from './marks'
import { contrast } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  document.documentElement.removeAttribute('data-theme')
})

function glyphFor(title: string): HTMLButtonElement {
  return screen.getByRole('button', { name: `Help: ${title}` }) as HTMLButtonElement
}

/** The disc's fill as the browser paints it: the inline value, its local vars expanded. */
function discFill(glyph: HTMLElement, theme: Theme, states: readonly string[] = []) {
  const inline = glyph.style.getPropertyValue('background-color') || glyph.style.getPropertyValue('background')
  expect(inline, 'the `?` paints no inline fill').not.toBe('')
  return resolveColour(expandLocal(inline, glyph, { ...WIDE, theme, states }), theme)
}

describe('a11y-keyboard-003: a pinned HelpCard is a named dialog', () => {
  it('names the pinned card by its title', () => {
    const title = HELP['runner-profile-by-name'].title
    render(<HelpCard topic="runner-profile-by-name" />)
    const glyph = glyphFor(title)
    fireEvent.click(glyph)
    // MUTATION: drop `aria-labelledby` from HelpCardView's card and the dialog
    // has no accessible name, so this query finds nothing.
    const dialog = screen.getByRole('dialog', { name: title })
    expect(dialog.id).toBe(glyph.getAttribute('aria-controls'))
  })

  it('gives two cards on one page two distinct title ids', () => {
    const a = HELP['runner-profile-by-name'].title
    const b = HELP['absent-vs-zero'].title
    render(
      <div>
        <HelpCard topic="runner-profile-by-name" />
        <HelpCard topic="absent-vs-zero" />
      </div>,
    )
    fireEvent.click(glyphFor(a))
    fireEvent.click(glyphFor(b))
    const ids = screen.getAllByRole('dialog').map((d) => d.getAttribute('aria-labelledby'))
    expect(ids).toHaveLength(2)
    // MUTATION: give the title a fixed id (`id="help-title"`) and both
    // dialogs point at the same node -- the second card is named by the first's title.
    expect(ids[0]).not.toBeNull()
    expect(new Set(ids).size, `title ids ${ids.join(', ')}`).toBe(2)
    expect(screen.getByRole('dialog', { name: a })).not.toBe(screen.getByRole('dialog', { name: b }))
  })
})

describe("admin-008 / work-b-008: HelpCard's `?` disc has an edge in light as in dark", () => {
  it.each(THEMES)('paints --ctl-q-fill at rest and --ctl-q-fill-hover under the pointer, in %s', (theme) => {
    render(<HelpCard topic="runner-profile-by-name" />)
    const glyph = glyphFor(HELP['runner-profile-by-name'].title)
    // MUTATION: put GLYPH's fill back on `var(--surface-2)` and light reads
    // #eef4fa, not #d6e0ea -- red in light.
    expect(sameColour(discFill(glyph, theme), resolveColour('var(--ctl-q-fill)', theme)), `${theme} rest fill`).toBe(true)
    // MUTATION: drop the `.help-glyph:hover` rule in styles.css and hover
    // paints the rest fill -- red in both themes.
    expect(sameColour(discFill(glyph, theme, ['hover']), resolveColour('var(--ctl-q-fill-hover)', theme)), `${theme} hover fill`).toBe(true)
  })

  it.each(THEMES)('steps the disc off the page by the dark chip and keeps its glyph at AA, in %s', (theme) => {
    render(<HelpCard topic="runner-profile-by-name" />)
    const glyph = glyphFor(HELP['runner-profile-by-name'].title)
    const page = resolveColour('var(--bg)', theme)
    const chip = contrast(resolveColour('var(--surface-2)', 'dark'), resolveColour('var(--bg)', 'dark'))
    const ink = resolveColour(glyph.style.getPropertyValue('color'), theme)
    for (const states of [[], ['hover']] as const) {
      const disc = discFill(glyph, theme, states)
      // MUTATION: put the fill back on `var(--surface-2)` and light is 1.02:1, red.
      expect(contrast(disc, page), `${theme} ${states.join('') || 'rest'} disc on the page`).toBeGreaterThanOrEqual(chip - 0.01)
      // MUTATION: deepen the light fill past #c8d4e1 and the --text-dim glyph falls under AA.
      expect(contrast(ink, disc), `${theme} ${states.join('') || 'rest'} glyph on its disc`).toBeGreaterThanOrEqual(4.5)
    }
  })
})
