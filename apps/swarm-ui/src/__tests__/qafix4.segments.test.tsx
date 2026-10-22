/**
 * QAFIX4-SEGMENTS, EVERY SEGMENTED CONTROL HAS SQUARE CELLS (part of #1680,
 * visual QA run 4, box comment 6298216578, 2026-10-22).
 *
 * QAFIX4-RUNS (#1905) squared the Runs state filter's cells at 390: each cell
 * took `:where(.app button)`'s radius (styles.css), so its hairlines curved
 * off at their ends and every inner join was notched. The owner decided
 * (2026-10-22, "Everywhere") that the same holds for every segmented control,
 * at every width: the cells are square in the canonical `.c-seg` rules
 * (components.css), and the control's own radius, with its `overflow: hidden`,
 * rounds the four outer corners.
 *
 * Every assertion names the mutation that turns it red.
 */
import { afterEach, describe, expect, it } from 'vitest'

import INTAKE from '../styles/intake.css?raw'
import type { CascadeEnv } from './cssgate'
import { build, painted, TABLES } from './marks'
import { resolveVars } from './spaceprobe'
import { lengthPx } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WIDTHS = [['1440', WIDE], ['390', PHONE]] as const
const CORNERS = ['top-left', 'top-right', 'bottom-right', 'bottom-left'] as const

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

const px = (v: string | null) => lengthPx(v, 0)

/** A cell's four corner radii, as the cascade paints them. */
function corners(cell: Element, env: CascadeEnv): number[] {
  return CORNERS.map((c) => px(painted(cell, ['border-radius', `border-${c}-radius`], env)))
}

/**
 * Every control, inside `.app` as main.tsx mounts it, so the app's button
 * floor (`:where(.app button)`, which carries a radius) applies to its cells.
 */
const CONTROLS = [
  ['a plain control (Segmented, Tabs.tsx)', 'div.app > div.c-seg > button'],
  ['a small control (`is-sm`)', 'div.app > div.c-seg.is-sm > button'],
  ['a stacked control (`is-stack`, RepositoriesParts.tsx Radio)', 'div.app > div.c-seg.ur-radio.is-stack > button'],
  ['the Runs state filter', 'div.app > div.rn-filters > div.c-seg > button'],
] as const

describe('box comment 6298216578: every segmented control has square cells', () => {
  for (const [name, selector] of CONTROLS) {
    for (const [w, env] of WIDTHS) {
      it(`${name} at ${w}: every cell corner is square`, () => {
        const cell = build(selector, hosts)
        // MUTATION: drop `border-radius: 0` from `.c-seg > button`
        // (components.css) and the cell takes `:where(.app button)`'s radius:
        // its hairlines curve off at their ends and each join is notched.
        expect(corners(cell, env), `${selector} at ${w}`).toEqual([0, 0, 0, 0])
      })
    }
  }

  for (const [name, selector] of CONTROLS) {
    for (const [w, env] of WIDTHS) {
      it(`${name} at ${w}: the control keeps its own radius and clips its cells`, () => {
        const seg = build(selector, hosts).parentElement!
        const outer = painted(seg, ['border-radius'], env)
        // MUTATION: square the control too (`border-radius: 0` on `.c-seg`)
        // and it loses the rounded outline every segmented control has.
        expect(outer, `${selector} at ${w}`).toBe('var(--r-ctl)')
        expect(Number.parseFloat(resolveVars(outer ?? '0', TABLES.dark)), `the control's radius is ${outer}`).toBeGreaterThan(0)
        // MUTATION: drop `overflow: hidden` from `.c-seg` and the square
        // corner cells' fill pokes past the control's rounded corners.
        expect(painted(seg, 'overflow', env), `${selector} at ${w}`).toBe('hidden')
      })
    }
  }

  it('the Runs filter takes its square cells from the shared rule; intake.css no longer declares a radius', () => {
    // The Runs-specific rule keeps its other declarations and leaves the
    // radius to components.css, so the two cannot drift apart.
    const rule = /\.rn-filters > \.c-seg > button\s*\{([^}]*)\}/.exec(INTAKE)
    expect(rule, 'the Runs filter cell rule in intake.css').not.toBeNull()
    const body = rule![1]!
    // MUTATION: leave `border-radius: 0` in `.rn-filters > .c-seg > button`
    // (intake.css) and the Runs filter restates the shared rule.
    expect(body).not.toMatch(/border-radius/)
    // MUTATION: delete the whole Runs rule instead of the one declaration and
    // the 3×2 grid's joins are drawn twice again.
    expect(body).toMatch(/margin-top: 0/)
    expect(body).toMatch(/border-top: 0/)
    expect(body).toMatch(/border-right: 1px solid var\(--line\)/)
    expect(body).toMatch(/justify-content: center/)
  })
})
