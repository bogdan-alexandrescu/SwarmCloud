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
import { afterEach, describe, expect, it } from 'vitest'

import { cascade, declarations, flatRules, parseSheet, splitTop, type GateNode } from './cssgate'
import { build, painted, resolveColour, shapeOf, stateHueIn, tokensIn } from './marks'
import { colour, contrast, over, resolveVars, stripComments, tokenTables, type RGBA } from './spaceprobe'

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
  const hosts: HTMLElement[] = []
  afterEach(() => {
    for (const h of hosts.splice(0)) h.remove()
  })

  for (const theme of THEMES) {
    it(`separates --ctl-absent from CANCELLED in the ${theme} theme`, () => {
      const absent = resolved('var(--ctl-absent)', theme)
      // The two places CANCELLED is painted: the bar and its legend key.
      // RE-POINTED (TS-4): the segment is the neutral flat bar now, a stripe
      // pattern rather than a flat fill, so its colour is read out of the
      // pattern -- every colour it paints, the transparent gaps excepted --
      // through the cascade, which also reads the one rule the bar and its
      // key share. The claim is unchanged: no colour a cancelled segment
      // paints is the absence colour.
      for (const selector of ['.stackcol > i.cancelled', '.chart-legend > .k.cancelled']) {
        const value = painted(build(selector, hosts), ['background', 'background-image', 'background-color'], {
          width: 1440,
          theme,
        })
        const paints = tokensIn(value ?? '', theme).filter((t) => t.rgba.a > 0)
        expect(paints.length, `${selector} paints no colour at all`).toBeGreaterThan(0)
        for (const { token, rgba } of paints) {
          const d = deltaE(absent, rgba)
          expect(
            d,
            `${selector}'s ${token} and --ctl-absent are ΔE ${d.toFixed(1)} apart in ${theme}: ` +
              'a cancelled task and a figure nobody measured read as the same colour',
          ).toBeGreaterThanOrEqual(10)
        }
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

// ===========================================================================
// THE CHROME-SHARED LANE'S COLOUR CLAIMS (#87 CH-17, CH-19, CH-23; #84 TS-4)
// ===========================================================================

describe('a healthy or factual mark carries no state hue (CH-17)', () => {
  // THE RULING (design-system.md §6.6, §6.7): on a state mark, hue is spent
  // only on warn, bad, paused and live. `ok` and `info` keep their SHAPES --
  // a filled disc, a flat bar -- and lose their hue, and their word or figure
  // is plain or `--text-dim` ink. A healthy platform is a quiet grey screen,
  // and a dock of twenty-nine green cells was the opposite of that.
  //
  // Beside CP-13's block above, which made the same ruling for metadata and
  // hypotheticals. MUTATION: put `var(--ok)` or `var(--info)` back on any of
  // the marks below, or a hue back on a healthy cell's status.
  const hosts: HTMLElement[] = []
  afterEach(() => {
    for (const h of hosts.splice(0)) h.remove()
  })

  const CASES: ReadonlyArray<{ what: string; selector: string; props: readonly string[]; pseudo?: string }> = [
    { what: 'the ok dot', selector: '.ctl-dot.is-ok', props: ['background', 'background-color'] },
    { what: "the ok dot's edge", selector: '.ctl-dot.is-ok', props: ['border-color', 'border'] },
    { what: "the ok chip's mark", selector: '.ctl-chip.is-ok > i', props: ['background', 'background-color'] },
    { what: 'the info dot', selector: '.ctl-dot.is-info', props: ['background', 'background-color'] },
    { what: "the info dot's edge", selector: '.ctl-dot.is-info', props: ['border-color', 'border'] },
    { what: "the info chip's mark", selector: '.ctl-chip.is-info > i', props: ['background', 'background-color'] },
    {
      what: "the good metric's mark",
      selector: '.ctl-metric.is-good > .ctl-metric-label',
      props: ['background', 'background-color'],
      pseudo: 'after',
    },
    { what: "the good metric's value", selector: '.ctl-metric.is-good > .ctl-metric-value', props: ['color'] },
    { what: "a healthy read cell's status", selector: '.source.ok > .s-status', props: ['color'] },
    { what: "the admin-only read cell's status", selector: '.source.info > .s-status', props: ['color'] },
  ]

  for (const theme of THEMES) {
    it(`paints none of --ok, --info, --warn, --bad or --paused in the ${theme} theme`, () => {
      for (const c of CASES) {
        const value = painted(build(c.selector, hosts), c.props, { width: 1440, theme }, c.pseudo ?? null)
        expect(stateHueIn(value, theme), `${c.what} (${c.selector}) is painted ${value}`).toBeNull()
      }
    })
  }

  it("puts a failing read's status in full ink, never in its hue (§6.7)", () => {
    // Only warn and bad cells carry tone, and the tone is the left-edge rule
    // and the dot -- the word is read, so it is `--text`, which is how the API
    // reads table already draws the same records. MUTATION: `.source.bad
    // .s-status { color: var(--bad) }` back.
    for (const tone of ['warn', 'bad']) {
      expect(painted(build(`.source.${tone} > .s-status`, hosts), 'color', { width: 1440 }), tone).toBe('var(--text)')
    }
  })
})

/** The lowest opacity `@keyframes ctl-live-pulse` fades a live mark to. */
function pulseFloor(): number {
  const frames: string[] = []
  const walk = (nodes: readonly GateNode[]): void => {
    for (const n of nodes) {
      if (n.kind === 'group') walk(n.children)
      else if (n.kind === 'opaque' && /^@(?:-webkit-)?keyframes\s+ctl-live-pulse$/.test(n.prelude)) frames.push(n.body)
    }
  }
  walk(parseSheet(STYLES).nodes)
  expect(frames, '@keyframes ctl-live-pulse is declared once').toHaveLength(1)
  const stops = [...frames[0]!.matchAll(/opacity\s*:\s*([\d.]+)/g)].map((m) => Number(m[1]))
  expect(stops.length, 'the pulse animates no opacity; this check would be vacuous').toBeGreaterThan(0)
  return Math.min(...stops)
}

describe('the live pulse never fades a live mark under 3:1 (CH-19)', () => {
  // MEASURED BEFORE THE FIX: at the old floor of .35 a live mark blended to
  // 1.69:1 (light) and 1.95:1 (dark) for roughly half of every cycle -- the
  // running dot, the one thing on the screen that says "this costs money
  // now", was under the graphical-object floor half the time.
  //
  // EVERY RULE THAT NAMES THE PULSE, not a list of three, so a fourth live mark
  // is held to the same floor the moment it is written. MUTATION: the keyframe
  // back to `opacity: .35`, or a new live mark in a colour that cannot hold 3:1
  // at the floor.
  const hosts: HTMLElement[] = []
  afterEach(() => {
    for (const h of hosts.splice(0)) h.remove()
  })

  const pulses = flatRules(STYLES).filter(
    (r) =>
      r.conditions.length === 0 &&
      declarations(r.body).some(
        (d) => (d.property === 'animation' || d.property === 'animation-name') && /\bctl-live-pulse\b/.test(d.value),
      ),
  )

  it('finds every live mark the pulse is on', () => {
    expect(pulses.length, 'fewer live marks than the dot, the chip and the liveness badge').toBeGreaterThanOrEqual(3)
  })

  for (const theme of THEMES) {
    it(`holds every live mark at 3:1 on every surface at the pulse's floor, in the ${theme} theme`, () => {
      const floor = pulseFloor()
      for (const rule of pulses) {
        for (const branch of splitTop(rule.selector)) {
          const fill = painted(build(branch, hosts), ['background', 'background-color'], { width: 1440, theme })
          expect(fill, `${branch} paints no fill`).not.toBeNull()
          const mark = resolveColour(fill!, theme)
          const faded: RGBA = { ...mark, a: mark.a * floor }
          for (const ground of ['--bg', '--surface', '--surface-2']) {
            const g = resolved(`var(${ground})`, theme)
            const ratio = contrast(over(faded, g), g)
            expect(ratio, `${branch} at opacity ${floor} on ${ground} in ${theme}`).toBeGreaterThanOrEqual(3)
          }
        }
      }
    })
  }

  it('stops the pulse at rest under prefers-reduced-motion, rather than freezing it mid-fade', () => {
    // `.01ms` on its own leaves `infinite` running: every frame shows the mark
    // at an arbitrary opacity. One iteration plays once, invisibly, and rests
    // at the mark's base opacity. MUTATION: drop the iteration count from the
    // global reduced-motion rule.
    for (const rule of pulses) {
      for (const branch of splitTop(rule.selector)) {
        const el = build(branch, hosts)
        const w = cascade(STYLES, el, ['animation', 'animation-iteration-count'], {
          width: 1440,
          reducedMotion: true,
        }).winner
        expect(w?.property, `${branch} under reduced motion`).toBe('animation-iteration-count')
        expect(w?.value).toBe('1')
        expect(w?.important).toBe(true)
      }
    }
  })
})

describe('every resting underline clears the 3:1 boundary floor (CH-23)', () => {
  // A link is ink plus an underline, and the underline is the whole
  // affordance at rest -- so it is a component boundary and is held to §1.2's
  // floor for one. `--line-soft` measured 2.05:1 (light) and 1.72:1 (dark) on
  // `--surface`, and about 1.3:1 once antialiased, which is why Overview's row
  // links read as plain text.
  //
  // EVERY `text-decoration-color` IN THE SHEET, wherever it is written, so the
  // next underline drawn in `--line-soft` fails where it lands. `currentColor`
  // is the hover and focus state and takes the text's own contrast.
  // MUTATION: any resting underline back on `--line-soft`.
  const underlines = flatRules(STYLES).flatMap((r) =>
    declarations(r.body)
      .filter((d) => d.property === 'text-decoration-color')
      .map((d) => ({ selector: r.selector, value: d.value })),
  )

  it('finds the underlines it is about', () => {
    expect(underlines.length, 'fewer underlines than the link primitive and its fallback').toBeGreaterThanOrEqual(4)
  })

  for (const theme of THEMES) {
    it(`holds each one at 3:1 on --bg, --surface and --surface-2 in the ${theme} theme`, () => {
      for (const u of underlines) {
        if (/^(currentcolor|inherit|transparent)$/i.test(u.value)) continue
        const c = resolveColour(u.value, theme)
        for (const ground of ['--bg', '--surface', '--surface-2']) {
          const ratio = contrast(c, resolved(`var(${ground})`, theme))
          expect(ratio, `\`${u.selector}\` underlines in ${u.value} on ${ground} in ${theme}`).toBeGreaterThanOrEqual(3)
        }
      }
    })
  }
})

describe('the outcome stack is four shapes, not four hues (TS-4)', () => {
  // MEASURED: failed against cancelled was 1.01:1 in the dark theme and
  // succeeded against cancelled 1.08:1 in the light one -- three flat fills
  // told apart by hue alone, with only "still open" textured. The vocabulary:
  //
  //   succeeded  solid                     the one outcome that is just a fill
  //   failed     a 2px left rule           redesign-v2 §5.5 Tier 1's form for bad
  //   cancelled  the neutral flat bar      CH-22's "ended" mark, stacked
  //   open       the 45° hatch             unchanged: not an outcome yet
  //
  // MUTATION: give any two the same form, or draw a key its own way again.
  const hosts: HTMLElement[] = []
  afterEach(() => {
    for (const h of hosts.splice(0)) h.remove()
  })
  const OUTCOMES = ['succeeded', 'failed', 'cancelled', 'open'] as const
  const WIDE = { width: 1440 }
  const fill = (el: Element): string =>
    painted(el, ['background', 'background-image', 'background-color'], WIDE) ?? ''

  it('draws each outcome in a silhouette of its own', () => {
    const shapes = OUTCOMES.map((o) => shapeOf(build(`.stackcol > i.${o}`, hosts), WIDE))
    for (let i = 0; i < shapes.length; i++) {
      for (let j = i + 1; j < shapes.length; j++) {
        expect(shapes[i], `${OUTCOMES[i]} and ${OUTCOMES[j]} are one shape in greyscale`).not.toBe(shapes[j])
      }
    }
  })

  it('names the forms: succeeded solid, failed ruled, cancelled flat bars, open hatched', () => {
    const seg = (o: string): HTMLElement => build(`.stackcol > i.${o}`, hosts)
    const succeeded = seg('succeeded')
    expect(fill(succeeded), 'succeeded is the one flat fill').not.toMatch(/gradient/)
    expect(painted(succeeded, 'box-shadow', WIDE) ?? 'none').toBe('none')

    const failed = seg('failed')
    expect(painted(failed, 'box-shadow', WIDE) ?? '', 'no 2px rule down the failed segment').toMatch(
      /^inset\s+2px\s+0(px)?\s+0(px)?\s+var\(--bad\)$/,
    )

    const cancelled = fill(seg('cancelled'))
    expect(cancelled, 'cancelled is not drawn as flat bars').toMatch(/^repeating-linear-gradient\(\s*to bottom/)
    expect(stateHueIn(cancelled, 'light'), 'the ended bar is neutral').toBeNull()
    expect(stateHueIn(cancelled, 'dark'), 'the ended bar is neutral').toBeNull()

    expect(fill(seg('open')), 'still open lost its hatch').toMatch(/45deg/)
  })

  it('draws every legend key as the segment it names', () => {
    for (const o of OUTCOMES) {
      const bar = build(`.stackcol > i.${o}`, hosts)
      const key = build(`.chart-legend > .k.${o}`, hosts)
      expect(fill(key), `the ${o} key`).toBe(fill(bar))
      expect(painted(key, 'box-shadow', WIDE), `the ${o} key's rule`).toBe(painted(bar, 'box-shadow', WIDE))
    }
  })
})
