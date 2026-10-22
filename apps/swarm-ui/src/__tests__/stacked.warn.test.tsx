// LANE STACKED-WARN (part of #1680). Owner decision 2026-10-22, answering
// QAFIX4-CAPACITY-2's question "Should every stacked (phone) table's warn rule
// run the record's full height?": "Every table, follow-up lane".
//
// A stacked record is ONE `tr` holding the name and every field, so the warn
// tone's centred half rule (`3px 46%`) marked only its middle rows: it began
// under the name and stopped above the last fields. PR 1918 fixed Provider
// quota alone in styles/capacity.css; this lane moves its full-height DASHED
// rule into the shared stacked rule, in every block that stacks a table (the
// 899px media block and the inspector's container block), and drops the
// quota-only copy.
//
// Everything is asked of the cascade over every sheet in the bundle's order
// (`painted()`, marks.ts): jsdom lays nothing out and applies no `@media` or
// `@container`, so "the full height of the record" is held as "the rule is on
// the `tr` and its size is the whole of it".

import { afterEach, describe, expect, it } from 'vitest'

import CAPACITY_CSS from '../styles/capacity.css?raw'
import type { CascadeEnv } from './cssgate'
import { build, painted, stripColours } from './marks'
import { stripComments } from './spaceprobe'

const DASHED = 'repeating-linear-gradient(to bottom, var(--warn) 0 6px, transparent 6px 9px)'
const SOLID_BAD = 'linear-gradient(var(--bad), var(--bad))'

// The two blocks that stack a table. The phone width alone reaches the 899px
// media block; the inspector block is reached by a wide viewport with a narrow
// `ctl-inspector` container, so the media block cannot be what answers.
const BLOCKS: { name: string; env: CascadeEnv }[] = [
  { name: '899px media block', env: { width: 390 } },
  { name: '899px media block, light', env: { width: 390, theme: 'light' } },
  { name: 'inspector container block', env: { width: 1440, container: 600 } },
]
const WIDE: CascadeEnv = { width: 1440 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

const record = (tone: string, table = 'ctl-table'): HTMLElement =>
  build(`div.${table}.is-stacked > table > tbody > tr.${tone}`, hosts)

const image = (el: Element, env: CascadeEnv): string => painted(el, 'background-image', env) ?? 'none'
const size = (el: Element, env: CascadeEnv): string | null => painted(el, 'background-size', env)

describe('a stacked warn record carries a dashed rule down its whole height', () => {
  for (const b of BLOCKS) {
    it(`in the ${b.name}`, () => {
      const tr = record('is-warn')
      // MUTATION: put `background-size: 3px 46%` back on the shared stacked
      // `tr.is-warn` rule in this block -- the rule marks the record's middle.
      expect(size(tr, b.env), 'the warn rule covers part of the record').toBe('3px 100%')
      // MUTATION: make the shared stacked warn rule the solid
      // `linear-gradient(var(--warn), var(--warn))` -- warn and bad become one
      // shape in greyscale.
      expect(image(tr, b.env)).toBe(DASHED)
      // MUTATION: drop `background-position: left center` (or the no-repeat)
      // from the shared stacked tone rule -- the rule tiles across the record.
      expect(painted(tr, 'background-repeat', b.env)).toBe('no-repeat')
      // MUTATION: drop the stacked `> :first-child { background-image: none }`
      // reset -- the table's half rule reappears under the name's first letters.
      expect(image(tr.appendChild(document.createElement('th')), b.env)).toBe('none')
    })
  }
})

describe('the other two tones are unchanged when stacked', () => {
  for (const b of BLOCKS) {
    it(`in the ${b.name}`, () => {
      const bad = record('is-bad')
      const paused = record('is-paused')
      // MUTATION: give the stacked `tr.is-bad` rule the dashed warn image --
      // exhausted reads as throttled.
      expect(image(bad, b.env)).toBe(SOLID_BAD)
      expect(size(bad, b.env)).toBe('3px 100%')
      // MUTATION: replace the stacked `tr.is-paused` hatch with a plain
      // gradient -- paused loses its "not a measurement" mark.
      expect(image(paused, b.env)).toMatch(/^repeating-linear-gradient\(\s*45deg/)
      expect(size(paused, b.env)).toBe('3px 100%')
      // MUTATION: make the stacked warn rule any one of the other two forms --
      // two tones read alike once the colour is taken away.
      const form = (el: Element): string => `${stripColours(image(el, b.env))} ${size(el, b.env)}`
      const forms = new Set([form(record('is-warn')), form(bad), form(paused)])
      expect(forms.size, 'warn, bad and paused draw three shapes').toBe(3)
    })
  }
})

describe('a desktop (not stacked) warn row keeps the half rule', () => {
  it('on the first cell, at 46%, and none on the row', () => {
    const cell = build('div.ctl-table > table > tbody > tr.is-warn > td', hosts)
    const tr = cell.parentElement!
    // MUTATION: let the full-height dashed rule escape `.is-stacked` (or its
    // media / container block) -- the desktop row grows a second rule.
    expect(image(tr, WIDE)).toBe('none')
    expect(size(tr, WIDE)).toBeNull()
    // MUTATION: change styles.css's table `tr.is-warn > :first-child` half
    // rule -- the desktop look this lane must not move.
    expect(image(cell, WIDE)).toBe('linear-gradient(var(--warn), var(--warn))')
    expect(size(cell, WIDE)).toBe('3px 46%')
  })
})

describe('Provider quota takes the shared rule, not a copy of its own', () => {
  it('styles/capacity.css declares no quota-only warn rule', () => {
    // MUTATION: put PR 1918's `.quota-table.is-stacked > table > tbody >
    // tr.is-warn` block back in capacity.css -- two copies of one rule, which
    // is how they start to disagree.
    expect(stripComments(CAPACITY_CSS)).not.toMatch(/\.quota-table[^{}]*tr\.is-warn[^{}]*\{/)
  })

  it('a stacked throttled quota record still gets the full dashed rule', () => {
    for (const b of BLOCKS) {
      const tr = record('is-warn', 'quota-table')
      // MUTATION: scope the shared stacked warn rule to `.ctl-table` -- the
      // quota table (which is not one) loses its rule now its copy is gone.
      expect(image(tr, b.env), b.name).toBe(DASHED)
      expect(size(tr, b.env), b.name).toBe('3px 100%')
    }
  })
})
