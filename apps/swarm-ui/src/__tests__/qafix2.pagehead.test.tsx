/**
 * VISUAL QA RUN 2 (wf_b6e2441b419f423a84eb, 2026-10-20, the fixture build of
 * main at 75148351), the page head, the help cards, focus rings and API reads
 * (QAFIX2-PAGEHEAD; part of #1680, one comment in the epic per finding).
 *
 * admin-008, work-b-008  In light the head's `?` disc was --surface-2
 *     (238,244,250) on --bg (243,246,249), 1.03:1: no visible edge. In dark
 *     the same disc is a 1.21:1 chip. Held as: the disc steps off the page by
 *     at least the dark chip's step in BOTH themes, and its glyph keeps AA.
 * capacity-004  At 390 the API reads table scrolled sideways and cut Took
 *     mid-number, where every other Capacity table stacks at the phone
 *     breakpoint (`tableMode`, QA G5-08). Held as: it takes `is-stacked` there.
 * capacity-005  The head said "reads nothing" over a table of reads.
 * a11y-keyboard-003  A pinned section card was role=dialog with no name.
 * a11y-keyboard-004  HelpCard's `?` (`.help-glyph`, the one beside '8/8
 *     reads') drew the browser's 1px ring, not the app's 2px solid --info.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { noteFixtureProbe, route } from '../fetch'
import type { CascadeEnv } from './cssgate'
import { build, painted, resolveColour, THEMES, type Theme } from './marks'
import { contrast } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 6000 }

const hosts: HTMLElement[] = []

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

/** Stub `matchMedia`, which jsdom does not have, as a phone or a desktop. */
function media(phone: boolean): void {
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

function fill(el: Element, theme: Theme, states: readonly string[] = []): string {
  const v = painted(el, ['background-color', 'background'], { ...WIDE, theme, states })
  expect(v, `.ctl-q-glyph paints no fill in ${theme}`).not.toBeNull()
  return v!
}

describe('admin-008 / work-b-008: the page head `?` has an edge in light as in dark', () => {
  it.each(THEMES)('steps the disc off the page by at least the dark chip, in %s', (theme) => {
    const glyph = build('button.ctl-q-glyph', hosts)
    const disc = resolveColour(fill(glyph, theme), theme)
    const page = resolveColour('var(--bg)', theme)
    // The dark chip the run called "clear": --surface-2 on --bg, 1.21:1.
    const chip = contrast(resolveColour('var(--surface-2)', 'dark'), resolveColour('var(--bg)', 'dark'))
    // MUTATION: put `.ctl-q-glyph` back on `var(--surface-2)` and light is 1.03:1, red.
    expect(contrast(disc, page), `${theme} disc on the page`).toBeGreaterThanOrEqual(chip - 0.01)
    const ink = painted(glyph, 'color', { ...WIDE, theme })!
    // MUTATION: deepen the light fill past #c8d4e1 and the glyph falls under AA.
    expect(contrast(resolveColour(ink, theme), disc), `${theme} glyph on its disc`).toBeGreaterThanOrEqual(4.5)
  })

  it.each(THEMES)('hover still moves the fill away from rest, in %s', (theme) => {
    const glyph = build('button.ctl-q-glyph', hosts)
    const rest = resolveColour(fill(glyph, theme), theme)
    const hover = resolveColour(fill(glyph, theme, ['hover']), theme)
    // MUTATION: give hover the rest fill (or the old --line-soft, which is
    // LIGHTER than the new light rest) and hover stops stepping darker in light.
    expect(contrast(hover, resolveColour('var(--bg)', theme))).toBeGreaterThan(contrast(rest, resolveColour('var(--bg)', theme)))
  })
})

describe('a11y-keyboard-004: every `?` draws the app focus ring', () => {
  it("rings HelpCard's `?` (`.help-glyph`) and the head's `?` 2px solid --info", async () => {
    window.history.replaceState(null, '', '/')
    render(<App />)
    const glyphs = await waitFor(() => {
      const found = [...document.querySelectorAll<HTMLElement>('button.help-glyph')]
      expect(found.length, 'Overview drew no HelpCard `?`').toBeGreaterThan(0)
      return found
    }, WAIT)
    const head = document.querySelector<HTMLElement>('button.ctl-q-glyph')
    expect(head, 'Overview drew no head `?`').not.toBeNull()
    for (const g of [...glyphs, head!]) {
      // MUTATION: drop `.help-glyph:focus-visible` and the HelpCard `?` paints
      // no outline here (the browser's own 1px `auto` ring in a real one).
      expect(painted(g, 'outline', { ...WIDE, states: ['focus-visible'] }), g.getAttribute('aria-label') ?? '').toBe('2px solid var(--info)')
    }
  })
})

describe('a11y-keyboard-003: a pinned section card is a named dialog', () => {
  it('names the pinned card by its title', async () => {
    // Runtimes, not Pools: Pools' title `?` opens its own topics since VQ-033
    // (BGD 2026-10-26), so its section card is not drawn there.
    window.history.replaceState(null, '', '/capacity/runtimes')
    render(<App />)
    const glyph = await waitFor(() => screen.getByRole('button', { name: /^Help: .* answers$/ }), WAIT)
    const title = glyph.getAttribute('aria-label')!.replace(/^Help: /, '')
    fireEvent.click(glyph)
    // MUTATION: drop `aria-labelledby` from `SectionQuestion`'s card and the
    // dialog has no name, so this query finds nothing.
    const dialog = await waitFor(() => screen.getByRole('dialog', { name: title }))
    expect(dialog.id).toBe(glyph.getAttribute('aria-controls'))
  })
})

describe('capacity/api-reads', () => {
  it("capacity-005: the head does not say 'reads nothing' over a table of reads", async () => {
    noteFixtureProbe(route('/v1/tenants/me', {}), 12, true)
    window.history.replaceState(null, '', '/capacity/api-reads')
    render(<App />)
    const age = await waitFor(() => {
      const el = document.querySelector('.c-phead .ctl-head-age')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    await waitFor(() => expect(document.querySelectorAll('main tbody tr').length).toBeGreaterThan(0), WAIT)
    // MUTATION: put "reads nothing" back in `ScreenAge` and this is red.
    expect(age.textContent).not.toMatch(/reads nothing/)
    // QA2 capacity-19: "no reads of its own" over a table of reads still read
    // as "no reads". The rows are the other screens' reads; the head says so.
    // MUTATION: put "no reads of its own" back in `ScreenAge` and this is red.
    expect(age.textContent).not.toMatch(/no reads/)
    expect(age.textContent).toBe('reads made by other screens')
  })

  it('capacity-004: stacks into labelled records at the phone breakpoint, like every Capacity table', async () => {
    media(true)
    noteFixtureProbe(route('/v1/tenants/me', {}), 40, true)
    // A call with an id, so the row draws its URL line: a call that is its
    // route draws none (visual QA run 3, capacity-015).
    noteFixtureProbe(route('/v1/tasks/{id}/checkpoints', { id: 'task_b40e5a1c' }), 40, true)
    window.history.replaceState(null, '', '/capacity/api-reads')
    render(<App />)
    const table = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('main .ctl-table')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    // MUTATION: keep the wrapper `is-scroll` at the phone and Took is cut at the card's edge.
    expect(table.classList.contains('is-stacked')).toBe(true)
    expect(table.classList.contains('is-scroll')).toBe(false)
    const took = table.querySelector<HTMLElement>('tbody td.ctl-ref-ms')!
    expect(took.getAttribute('data-label')).toBe('Took')
    // The stacked record's value cell is not a table cell any more.
    expect(painted(took, 'display', PHONE)).not.toBe('table-cell')
    // MUTATION: drop the stacked rule for the URL line and a 110-character
    // checkpoint URL runs off a 356px record.
    const sub = table.querySelector<HTMLElement>('.ctl-ref-path > .ctl-sub')!
    expect(painted(sub, 'text-overflow', PHONE)).toBe('ellipsis')
    expect(painted(sub, 'white-space', PHONE)).toBe('nowrap')
  })

  it('capacity-004: scrolls with its route held above the phone breakpoint, as before', async () => {
    media(false)
    noteFixtureProbe(route('/v1/tenants/me', {}), 40, true)
    window.history.replaceState(null, '', '/capacity/api-reads')
    render(<App />)
    const table = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('main .ctl-table')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    // MUTATION: stack it at every width and the data table loses its columns on a laptop.
    expect(table.classList.contains('is-scroll')).toBe(true)
  })
})
