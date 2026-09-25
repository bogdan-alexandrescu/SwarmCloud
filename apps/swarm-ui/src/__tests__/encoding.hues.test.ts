// TWO COLOUR CLAIMS, read off the shipped tokens in both themes.
//
// 1. AN ABSENCE IS NOT AN OUTCOME. `--ctl-absent` was `var(--text-faint)`, and
//    `--text-faint` is what the Activity chart paints CANCELLED with -- so a
//    cancelled bar and a never-measured band were the same colour, exactly,
//    which is the one collapse design-system.md §5.5 and the audit (§A3.3,
//    table row 6) name as the thing this product exists to prevent. The shape
//    channels (solid bar, hatched band, dashed tile) survive greyscale; this
//    is the colour channel, for everyone reading in colour.
//
// 2. THE TOKEN MIX IS READABLE IN GREYSCALE, AND STILL VISIBLE ON THE CARD.
//    Overview's `.ov-mix` drew four token counts in `--series-1..4`, and the
//    series block says of itself that those five sit in a band 1.36:1 from end
//    to end and are NOT separable in greyscale. The keyed legend (§12.1) told a
//    colour reader which swatch was which; nothing told a greyscale one.
//    Separating the four may not be bought by fading them into the card: the
//    series block's promise is that every series fill clears 3:1 against
//    `--bg`, `--surface` and `--surface-2` in both themes (WCAG 1.4.11, a
//    graphical object), and a tone derived from a series inherits that floor.
//    The first one-hue ramp stepped toward `--surface` and left c-wr's 8px
//    swatch at 1.35:1 on white -- this file's old floor of 1.2 held it there.
//
// Everything is resolved from the source the app ships, through the same
// `tokenTables` / `resolveVars` / `colour` the spacing probe uses, so a token
// renamed or a `var()` broken fails here by name rather than being compared as
// an empty string.

import STYLES from '../styles.css?raw'
import { describe, expect, it } from 'vitest'

import { colour, contrast, resolveVars, stripComments, tokenTables, type RGBA } from './spaceprobe'

type Theme = 'dark' | 'light'
const THEMES: readonly Theme[] = ['dark', 'light']
const TABLES = tokenTables(STYLES)

function resolved(value: string, theme: Theme): RGBA {
  const v = resolveVars(value.trim(), TABLES[theme])
  const c = colour(v)
  expect(c, `could not read ${JSON.stringify(value)} -> ${JSON.stringify(v)} as a colour`).not.toBeNull()
  return c!
}

/** The declared value of `prop` in the first rule for exactly `selector`. */
function declared(css: string, selector: string, prop: string): string {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s+')
  const rule = new RegExp(`(?:^|[}\\s])${esc}\\s*\\{([^}]*)\\}`, 'm').exec(css)
  expect(rule, `no rule for ${selector}`).not.toBeNull()
  const decl = new RegExp(`(?:^|;|\\s)${prop}\\s*:\\s*([^;]+)`).exec(rule![1]!)
  expect(decl, `${selector} declares no ${prop}`).not.toBeNull()
  return decl![1]!.trim()
}

/** CIE L*a*b* (D65) of an opaque sRGB colour. */
function lab(c: RGBA): [number, number, number] {
  const lin = (v: number): number => {
    const s = v / 255
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  const r = lin(c.r)
  const g = lin(c.g)
  const b = lin(c.b)
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
  const f = (t: number): number => (t > 216 / 24389 ? Math.cbrt(t) : (24389 / 27 * t + 16) / 116)
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))]
}

/**
 * CIE76 colour difference. About 2.3 is the smallest difference a practised
 * eye can see side by side; 10 is a difference nobody has to look for.
 */
function deltaE(a: RGBA, b: RGBA): number {
  const [l1, a1, b1] = lab(a)
  const [l2, a2, b2] = lab(b)
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2)
}

describe('an absence is not drawn in the colour of an outcome', () => {
  const CSS = stripComments(STYLES)

  for (const theme of THEMES) {
    it(`separates --ctl-absent from CANCELLED in the ${theme} theme`, () => {
      const absent = resolved('var(--ctl-absent)', theme)
      // The two places CANCELLED is painted: the bar and its legend key.
      for (const selector of ['.stackcol > i.cancelled', '.chart-legend .k.cancelled']) {
        const cancelled = resolved(declared(CSS, selector, 'background'), theme)
        const d = deltaE(absent, cancelled)
        expect(
          d,
          `${selector} and --ctl-absent are ΔE ${d.toFixed(1)} apart in ${theme}: ` +
            'a cancelled task and a figure nobody measured read as the same colour',
        ).toBeGreaterThanOrEqual(10)
      }
      // And the small print, which the audit counted as the same collapse:
      // "not measured" in the colour of every caption on the screen.
      expect(deltaE(absent, resolved('var(--text-faint)', theme))).toBeGreaterThanOrEqual(10)
    })

    it(`keeps --ctl-absent legible as text in the ${theme} theme`, () => {
      // It is drawn as TEXT -- `.ctl-fact.is-absent`, `.node-num.is-absent dd`,
      // `.ctl-metric.is-absent .ctl-metric-value` -- at 12-14px, so moving it
      // may not be bought by making the absence hard to read. AA, every
      // surface it can sit on.
      const absent = resolved('var(--ctl-absent)', theme)
      for (const surface of ['--bg', '--surface', '--surface-2']) {
        const ratio = contrast(absent, resolved(`var(${surface})`, theme))
        expect(ratio, `--ctl-absent on ${surface} in ${theme}`).toBeGreaterThanOrEqual(4.5)
      }
    })
  }
})

describe('metadata and hypotheticals carry no state hue (CP-13)', () => {
  // A STATE HUE IS A VERDICT (design-system.md §1.2-§1.3): `--paused` means an
  // operator is holding something, `--ok` means it went well. Profile headroom
  // spent both on things that are neither -- the "this tenant" scope pill in
  // `--paused`, beside pools that really were paused, and every counterfactual
  // effect in `--ok`, including the one that says it "could not be measured".
  const CSS = stripComments(STYLES)
  const STATE = /var\(--(?:ok|warn|bad|paused|info)(?:-ink)?\)/

  it('draws the tenant scope as a neutral step, not in the paused hue', () => {
    // MUTATION: `.scope.tenant` back on a `--paused` tint.
    for (const prop of ['background', 'color']) {
      expect(declared(CSS, '.scope.tenant', prop), `.scope.tenant ${prop}`).not.toMatch(STATE)
    }
  })

  it('draws a counterfactual effect in ink, not in the healthy hue', () => {
    // MUTATION: `.cf-effect { color: var(--ok) }` back.
    expect(declared(CSS, '.cf-effect', 'color')).toBe('var(--text)')
  })
})

describe("the token mix's four segments are separable in greyscale", () => {
  // `styles.css`, where Overview's rules have lived since U8 folded the
  // `OVERVIEW_CSS` template literal into the sheet. The `.ov-sN` tones are the
  // first rules of those exact selectors, so `declared` finds them there.
  const CSS = stripComments(STYLES)

  it('reads the Overview block of the sheet', () => {
    expect(CSS).toContain('.ov-mix')
    expect(CSS).toContain('.ov-s1')
  })

  for (const theme of THEMES) {
    it(`draws in, out, c-rd and c-wr at four distinct tones in the ${theme} theme`, () => {
      // The legend keys each swatch to a word; this is the claim that a reader
      // without the hue can still match a swatch to its segment. Every PAIR,
      // not just neighbours: an absent count drops its segment, so any two can
      // end up side by side, and the legend puts all four in one row.
      const fills = [1, 2, 3, 4].map((n) => resolved(declared(CSS, `.ov-s${n}`, 'background'), theme))
      for (let i = 0; i < fills.length; i++) {
        // 3:1 on every ground a series may be drawn on, which is the series
        // block's own claim (styles.css, THE SERIES PALETTE) and WCAG 1.4.11's
        // floor for a graphical object. The swatch is an 8px square and the
        // segment an 8px rule: a key nobody can find keys nothing.
        for (const ground of ['--bg', '--surface', '--surface-2']) {
          expect(
            contrast(fills[i]!, resolved(`var(${ground})`, theme)),
            `.ov-s${i + 1} on ${ground} in ${theme} is under the 3:1 a series fill promises`,
          ).toBeGreaterThanOrEqual(3)
        }
        // 1.2:1 between tones is the floor test_state_colour_discriminability.py
        // gives PARKED against the severity triad, for the reason it gives: at
        // or under about 1.2 a step is not visible.
        for (let j = i + 1; j < fills.length; j++) {
          expect(
            contrast(fills[i]!, fills[j]!),
            `.ov-s${i + 1} and .ov-s${j + 1} are one grey in ${theme}`,
          ).toBeGreaterThanOrEqual(1.2)
        }
      }
    })
  }
})
