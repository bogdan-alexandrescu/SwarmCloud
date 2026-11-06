/**
 * THE ADMIN PAGES AND THE SHELL AROUND THEM, FROM VISUAL QA RUN 5b
 * (QAFIX5-SHELL-ADMIN, part of #1680; run wf_025ae735511546d99eff,
 * 2026-10-22, the fixture build of main at 58cd0c1e, 1440 and 390 px, light
 * and dark). Each `describe` is one finding, held by the property that was
 * false:
 *
 *   5b:QA-017    Every admin-persona page at 390 was 394-398px wide, and the
 *                header's `↑ Top` was cut at the right edge. Only an admin
 *                is drawn the switchable tenant chip; its `overflow: visible`
 *                (for the CH-19 hit area) gave it back an automatic minimum
 *                of its whole name, so it no longer gave way to the row.
 *   5b:QA-018    At 1440 Model prices was wider than its card: five unsized
 *                text fields, each the browser's 20 characters.
 *   5b:QA-019    At 390 Runner sizes' `What the pools admit` overflowed its
 *                card, its last head cut.
 *   r3:work-a-008 At 390 the Reads facts were parted by hairlines set flush
 *                against the text, unlike the spaced dots at 1440.
 *
 * QA-044 (the stacked keys in mono) was the key face of every stacked record
 * in the console (styles.css `td[data-label]::before`). BGD decided on
 * 2026-10-23 (task_7f0095fb2fd9481790cc q0) to set it sans console-wide;
 * qa044.sanskeys.test.tsx holds it.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ModelPricesCard } from '../AdminSettings'
import { PoolsTable } from '../RunnerSizes'
import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 4000 }

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllGlobals()
})

/** Stub `matchMedia`, which jsdom does not have, as a phone below 560px. */
function phone(): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('759px') || query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

/** Markup parsed into a host the cascade can read, for a fragment no component draws alone. */
function fragment(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

describe('5b:QA-017: an admin phone header gives way inside 390px', () => {
  it('lets the switchable tenant chip shrink, as the member label does', () => {
    const chip = build('header.sk-pbar > button.sk-tchip', hosts)
    const name = document.createElement('span')
    name.className = 'sk-tcn'
    chip.appendChild(name)
    const label = build('header.sk-pbar > span.sk-tn', hosts)
    // The chip's box stays unclipped so its 44px hit area shows (CH-19)...
    expect(painted(chip, 'overflow', PHONE)).toBe('visible')
    // MUTATION: drop the chip's `min-width: 0` and its automatic minimum is
    // its whole name again: with the admin tag beside it the row passes 390px
    // and `↑ Top` is cut at the glass.
    expect(painted(chip, 'min-width', PHONE)).toBe('0')
    // ...and its name is what gives way: wrapped at its space, never cut
    // (PHONE-BAR-CHIP, part of #2391; it was an ellipsis, `Your …`).
    expect(painted(name, 'min-width', PHONE)).toBe('0')
    expect(painted(name, 'white-space', PHONE)).toBe('normal')
    expect(painted(name, 'text-overflow', PHONE)).not.toBe('ellipsis')
    // The member's label already gave way: the two read alike now.
    expect(painted(label, 'min-width', PHONE)).toBe('0')
  })
})

describe('5b:QA-018: Model prices fits its card at 1440', () => {
  it('sizes every figure field to its figure, and lets a long head wrap', async () => {
    render(<ModelPricesCard admin />)
    const table = await screen.findByRole('table', { name: 'Model prices' }, WAIT)
    const fields = within(table).getAllByRole('textbox')
    expect(fields.length).toBeGreaterThan(0)
    for (const f of fields) {
      const day = (f.getAttribute('aria-label') ?? '').startsWith('As of')
      // MUTATION: drop the width and each field is the browser's 20
      // characters: five of them pushed Cache write /M, Currency, Source,
      // As of and Set by past the card's edge. The day is one character
      // over its ten (VQ-054): at exactly ten its placeholder was cut to
      // `YYYY-MM-DI`.
      expect(painted(f, 'width', WIDE), `${f.getAttribute('aria-label')} has no width`).toBe(
        day ? 'calc(11ch + 20px)' : 'calc(7ch + 20px)',
      )
    }
    const heads = within(table).getAllByRole('columnheader')
    // Nine columns, all drawn: the card is not narrowed by dropping one.
    expect(heads.map((h) => h.textContent)).toEqual([
      'Model', 'Input /M', 'Output /M', 'Cache read /M', 'Cache write /M', 'Currency', 'Source', 'As of', 'Set by',
    ])
    // MUTATION: keep the heads nowrap and `Cache write /M` holds its column
    // wider than the figure under it.
    for (const h of heads) expect(painted(h, 'white-space', WIDE)).toBe('normal')
  })

  it('draws a tiered row without the list indent that widened the Model column', async () => {
    render(<ModelPricesCard admin />)
    const tiers = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.adm-price-tiers')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    // MUTATION: drop the rule and the tier lines sit 40px in, under a bullet.
    expect(painted(tiers, 'padding-left', WIDE) ?? painted(tiers, 'padding', WIDE)).toBe('0')
    expect(painted(tiers, 'list-style', WIDE)).toBe('none')
  })
})

describe('5b:QA-019: What the pools admit fits a phone card', () => {
  const pools = [
    { pool: 'global', hard_limit: 40, admits_before: { standard: 20, large: 4 }, admits_after: { standard: 20, large: 4 } },
  ]

  it('is a stacked record on a phone, every field labelled', () => {
    phone()
    render(<PoolsTable pools={pools} />)
    const table = screen.getByRole('table', { name: 'What the pools admit' })
    // MUTATION: render the table bare (no `.is-stacked` wrapper) and at 390
    // its four columns overflow the card, the last head cut.
    expect(table.parentElement?.classList.contains('is-stacked')).toBe(true)
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)
    for (const row of rows) {
      // MUTATION: drop a `data-label` and that field is a value with no key.
      const cells = within(row).getAllByRole('cell')
      expect(cells.map((c) => c.getAttribute('data-label'))).toEqual(['Ceiling (units)', 'Class', 'Agents admitted, before → after'])
      // The pool names the record, at lead rank.
      expect(within(row).getByRole('rowheader').textContent).toBe('global')
      for (const c of cells) {
        // MUTATION: drop the shared stacked rule's reach (no wrapper class)
        // and the cell stays a table cell.
        expect(painted(c, 'display', PHONE)).toBe('grid')
        // MUTATION: drop the stacked override and every field carries the
        // table's row rule, a line under each key.
        expect(painted(c, ['border-bottom', 'border-bottom-width'], PHONE)).toMatch(/^0/)
      }
    }
    // One figure where nothing moves (QA2-FIX-20, admin2-12), never `4 → 4`.
    expect(within(rows[1]!).getByText('4')).toBeTruthy()
  })

  it('stays a table at 1440, scrolling inside the card if it ever must', () => {
    render(<PoolsTable pools={pools} />)
    const table = screen.getByRole('table', { name: 'What the pools admit' })
    const wrap = table.parentElement!
    expect(wrap.classList.contains('is-stacked')).toBe(false)
    // MUTATION: drop the wrapper's scroll and a narrow card clips the last head.
    expect(painted(wrap, ['overflow-x', 'overflow'], WIDE)).toBe('auto')
    expect(screen.getAllByRole('columnheader')).toHaveLength(4)
  })
})

describe('r3:work-a-008: the Reads facts are parted by spaced dots on a phone, as at 1440', () => {
  it('draws a ` · ` before each fact, clipped where a fact starts a line', () => {
    const f = fragment(
      '<div class="ctl-dock-facts"><span class="ctl-dock-fact">3 routes<span class="ctl-dock-sep"> · </span></span>' +
        '<span class="ctl-dock-fact">p95 40ms</span></div>',
    )
    const facts = f.querySelector<HTMLElement>('.ctl-dock-facts')!
    const fact = f.querySelectorAll<HTMLElement>('.ctl-dock-fact')[1]!
    const sep = f.querySelector<HTMLElement>('.ctl-dock-sep')!
    for (const theme of THEMES) {
      const env = { ...PHONE, theme }
      // MUTATION: put the hairline back (`border-left` on the fact) and the
      // rule sits flush against the text: `8 routes|p95 400 ms|0 failed`.
      expect(painted(fact, ['border-left-style', 'border-left', 'border'], env)).toBeNull()
      // MUTATION: drop the dot and the facts run together again.
      expect(painted(fact, 'content', env, 'before')).toBe("'·'")
      // One character of air either side of the dot, as ` · ` is at 1440.
      expect(painted(fact, 'width', env, 'before')).toBe('1ch')
      expect(painted(fact, 'padding', env)).toBe('0 3ch 0 2ch')
      // MUTATION: drop the -2ch margin or the clip and a fact that starts the
      // second line opens with a dangling dot, the QA-039 defect.
      expect(painted(fact, 'margin-left', env)).toBe('-2ch')
      expect(painted(facts, 'overflow', env)).toBe('hidden')
      // The text separator stays out of the way on a phone: the dot is the
      // fact's own, so none trails at a line's end.
      expect(painted(sep, 'display', env)).toBe('none')
    }
    // Wider, the text ` · ` is the separator and no dot is drawn.
    expect(painted(fact, 'content', WIDE, 'before')).toBeNull()
    expect(painted(sep, 'display', WIDE)).not.toBe('none')
  })
})
