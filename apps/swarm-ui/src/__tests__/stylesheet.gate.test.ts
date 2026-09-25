// THE STYLESHEET GATE: one selector, one place; one keyframes name, one
// definition; and a sheet a browser parses the way this suite does.
//
// WHY THIS IS A SOURCE SCAN, when most of this suite refuses to be one. The
// claim "no selector is declared twice non-adjacently" is a property of the
// TEXT, and no DOM question answers it: `getComputedStyle` sees only the
// winner of a collision, never that there was one. The loading skeleton is the
// proof. It declared `pulse` as .5 <-> .85 and the liveness dot declared a
// second `pulse` as 1 <-> .35 two and a half thousand lines later; every
// computed style in the suite was internally consistent, and every skeleton in
// the product flashed at the dot's depth.
//
// THE PARSE CHECK IS NOT OPTIONAL, and it is the finding that justifies the
// gate. `§B6 — THE DENSE AND OPERATOR SCREENS` lost its opening `/*`, so its
// seventeen lines of prose became the selector of the `@media (max-width:
// 899px)` block under it. A browser drops a rule with an invalid selector,
// block and all: the stacked-table layout written for every dense screen below
// 900px has never reached a phone. jsdom recovers from the same text and parses
// the block, which is why nothing here noticed. See `cssgate.ts`.
//
// `?raw`, as typescale.test.ts does: Vite's own loader resolves the path the
// bundle uses, so a moved sheet fails to import here instead of a stale copy
// being read quietly from disk.

import STYLES from '../styles.css?raw'
import { describe, expect, it } from 'vitest'

import { cascade, conditionHolds, gate, specificity } from './cssgate'

// ONE SHEET. Overview used to inject a second one -- `OVERVIEW_CSS`, a
// template literal rendered as a `<style>` after this sheet -- and this file
// ran the gate over both. U8 folded it into `styles.css` as the sheet's last
// block (design-system.md §9.5), so the gate below runs over the only sheet
// there is, and the test at the foot of this file holds that it stays the only
// one: a screen that injects CSS again has built its own primitive layer
// outside every gate that reads this file.
const SOURCES = import.meta.glob<string>('../*.tsx', { query: '?raw', import: 'default', eager: true })

// ---------------------------------------------------------------------------
// The gate, run against sheets whose answer is known. A gate that has only
// ever been run against a clean input has only ever been shown to say "clean".
// ---------------------------------------------------------------------------

describe('the gate, against fixtures', () => {
  it('flags an accidental duplicate and not an intentional adjacent split', () => {
    // `.nav button` is written as two consecutive rules in the real sheet, on
    // purpose, and the reader of one is looking at the other. `.b` is the
    // defect: the same selector reopened after another rule intervened.
    const r = gate(
      `.a { color: red }
       .a { margin: 0 }
       .b { color: red }
       .c { color: blue }
       .b { margin: 0 }`,
      'fixture',
    )
    expect(r.duplicates).toHaveLength(1)
    expect(r.duplicates[0]).toContain('`.b`')
  })

  it('compares rules by their conditions, across separate blocks', () => {
    // §12.6 of design-system.md: two `@media (max-width: 560px)` blocks both
    // styling `.row`. Two blocks, one context -- a duplicate.
    const same = gate(
      `@media (max-width: 560px) { .row { gap: 0 } }
       .x { color: red }
       @media (max-width: 560px) { .row { padding: 0 } }`,
    )
    expect(same.duplicates).toHaveLength(1)
    // A selector restated under a DIFFERENT condition is a breakpoint, which
    // is what a media query is for.
    const other = gate(
      `.row { gap: 4px }
       @media (max-width: 560px) { .row { gap: 0 } }`,
    )
    expect(other.duplicates).toEqual([])
  })

  it('flags a keyframes name declared twice, wherever the second one sits', () => {
    const r = gate(
      `@keyframes pulse { 0%, 100% { opacity: .5 } 50% { opacity: .85 } }
       .skeleton { animation: pulse 1.4s ease-in-out infinite }
       @media (min-width: 1px) { @keyframes pulse { 50% { opacity: .35 } } }`,
    )
    expect(r.keyframes).toHaveLength(1)
    expect(r.animations).toEqual([])
  })

  it('flags an animation that names no keyframes', () => {
    const r = gate(`.dot { animation: pluse 2s ease-in-out infinite !important }`)
    expect(r.animations).toHaveLength(1)
    expect(r.animations[0]).toContain('`pluse`')
  })

  it('flags a comment that lost its opener in front of an at-rule', () => {
    // The shape of the real defect: prose, an orphan `*/`, then a block.
    const r = gate(
      `.before { color: red }

         SECTION HEADING -- a paragraph that lost its opener; it goes on
         -------------------------------------------------------------- */

       /* a comment that is fine */
       @media (max-width: 899px) { .inside { display: block } }
       .after { color: blue }`,
    )
    expect(r.problems).toHaveLength(1)
    expect(r.problems[0]).toMatch(/not a selector/)
    // And a sheet with every comment intact reads clean.
    expect(gate(`/* ok */ @media (max-width: 899px) { .inside { display: block } }`).problems).toEqual([])
  })

  it('lets :root repeat but not a token inside it', () => {
    const r = gate(
      `:root { --measure: 78ch; --a: 1px }
       .x { color: red }
       :root { --measure: 74ch }
       @media (prefers-color-scheme: light) { :root:not([data-theme='dark']) { --a: 2px } }`,
    )
    // `:root` twice is the sheet's own pattern and is not a duplicate...
    expect(r.duplicates).toEqual([])
    // ...but `--measure` twice in one context is a silent override. `--a`
    // under the light-theme condition is a theme, not a collision.
    expect(r.rootTokens).toHaveLength(1)
    expect(r.rootTokens[0]).toContain('--measure')
  })
})

// ---------------------------------------------------------------------------
// The cascade resolver, against sheets whose answer is known.
//
// `shell.test.tsx` asks `cascade` which rule wins for a dozen QA defects, and
// every one of those answers is only as good as this file's proof that the
// resolver weighs specificity, order, importance and conditions the way a
// browser does -- which is exactly what jsdom's own cascade does not do.
// ---------------------------------------------------------------------------

describe('the cascade resolver, against fixtures', () => {
  /** A detached fragment; `Element.matches` needs no document around it. */
  function el(html: string, pick: string): Element {
    const host = document.createElement('div')
    host.innerHTML = html
    const found = host.querySelector(pick)
    expect(found, `fixture has no ${pick}`).not.toBeNull()
    return found!
  }

  it('computes Selectors Level 4 specificity for what this sheet writes', () => {
    expect(specificity('.a')).toEqual([0, 1, 0])
    expect(specificity('.state p')).toEqual([0, 1, 1])
    expect(specificity('.state p.checked-at')).toEqual([0, 2, 1])
    expect(specificity('#root .x')).toEqual([1, 1, 0])
    // `:where()` contributes nothing, which is the whole reason the sheet's
    // control floor is written with it.
    expect(specificity(':where(.app button)')).toEqual([0, 0, 0])
    expect(specificity(':where(.app input:not([type="checkbox"]))')).toEqual([0, 0, 0])
    // `:not()` and `:is()` take their most specific argument.
    expect(specificity('.row:not(.clickable)')).toEqual([0, 2, 0])
    expect(specificity(':is(.a, #b) span')).toEqual([1, 0, 1])
    // An attribute and a pseudo-class are classes; a pseudo-element is a type.
    expect(specificity('td[data-label]::before')).toEqual([0, 1, 2])
    expect(specificity('.x:hover')).toEqual([0, 2, 0])
  })

  it('lets a later equal-specificity base rule beat an earlier phone rule', () => {
    // The `.ctl-seg` defect in miniature: a media query adds no specificity,
    // so the phone rule written ABOVE the base rule loses at every width.
    const sheet = `@media (max-width: 560px) { .seg > button { min-height: 44px } }
                   .seg > button { min-height: 26px }`
    const b = el('<div class="seg"><button>x</button></div>', 'button')
    expect(cascade(sheet, b, 'min-height', { width: 390 }).winner?.value).toBe('26px')
    // The same rules in the other order: the phone rule wins at 390 only.
    const fixed = `.seg > button { min-height: 26px }
                   @media (max-width: 560px) { .seg > button { min-height: 44px } }`
    expect(cascade(fixed, b, 'min-height', { width: 390 }).winner?.value).toBe('44px')
    expect(cascade(fixed, b, 'min-height', { width: 1440 }).winner?.value).toBe('26px')
  })

  it('weighs specificity over order, and importance over both', () => {
    const sheet = `.state p.checked-at { font-size: 12px }
                   .state p { font-size: 16px }
                   .checked-at { font-size: 10px }`
    const p = el('<div class="state"><p class="checked-at">x</p></div>', 'p')
    expect(cascade(sheet, p, 'font-size', { width: 1440 }).winner?.value).toBe('12px')
    const loud = `.state p { color: red } .checked-at { color: blue !important }`
    expect(cascade(loud, p, 'color', { width: 1440 }).winner?.value).toBe('blue')
    // A shorthand and its longhand compete for one value when both are asked.
    const short = `.checked-at { font: 12px/1.4 mono } .state p { font-size: 16px }`
    const w = cascade(short, p, ['font-size', 'font'], { width: 1440 }).winner
    expect(w?.property).toBe('font-size')
    expect(w?.value).toBe('16px')
  })

  it('answers for a pseudo-element separately from its element', () => {
    const sheet = `.t .n { text-align: right } td[data-label]::before { text-align: left }`
    const td = el('<table class="t"><tr><td class="n" data-label="x">1</td></tr></table>', 'td')
    expect(cascade(sheet, td, 'text-align', { width: 390 }).winner?.value).toBe('right')
    expect(cascade(sheet, td, 'text-align', { width: 390 }, 'before').winner?.value).toBe('left')
  })

  it('evaluates media and container conditions, and only the states it is given', () => {
    expect(conditionHolds('@media (min-width: 561px) and (max-width: 900px)', { width: 700 })).toBe(true)
    expect(conditionHolds('@media (min-width: 561px) and (max-width: 900px)', { width: 390 })).toBe(false)
    expect(conditionHolds('@media (prefers-color-scheme: light)', { width: 1440 })).toBe(false)
    expect(conditionHolds('@media (prefers-color-scheme: light)', { width: 1440, theme: 'light' })).toBe(true)
    // No container is no match, as in a browser; a container is asked its own size.
    expect(conditionHolds('@container side (max-width: 899px)', { width: 390 })).toBe(false)
    expect(conditionHolds('@container side (max-width: 899px)', { width: 1440, container: 480 })).toBe(true)
    // An unknown feature is loud, never read as "does not apply".
    expect(() => conditionHolds('@media (orientation: portrait)', { width: 390 })).toThrow(/cannot evaluate/)

    const sheet = `.a { color: red } .a:hover { color: blue }`
    const a = el('<a class="a">x</a>', 'a')
    expect(cascade(sheet, a, 'color', { width: 1440 }).winner?.value).toBe('red')
    expect(cascade(sheet, a, 'color', { width: 1440, states: ['hover'] }).winner?.value).toBe('blue')
  })
})

// ---------------------------------------------------------------------------
// The shipped sheets.
// ---------------------------------------------------------------------------

describe('the shipped stylesheets', () => {
  const sheets: ReadonlyArray<readonly [string, () => string]> = [['styles.css', () => STYLES]]

  for (const [label, read] of sheets) {
    describe(label, () => {
      it('parses the way a browser parses it', () => {
        expect(gate(read(), label).problems).toEqual([])
      })

      it('declares no selector twice non-adjacently', () => {
        expect(gate(read(), label).duplicates).toEqual([])
      })

      it('declares no custom property in two :root blocks of one context', () => {
        expect(gate(read(), label).rootTokens).toEqual([])
      })

      it('declares each @keyframes name once, and every animation names one', () => {
        const r = gate(read(), label)
        expect(r.keyframes).toEqual([])
        expect(r.animations).toEqual([])
      })
    })
  }

  it('actually read the sheet it passed', () => {
    // The precondition, so none of the above can pass against a sheet that
    // failed to load: an empty string has no duplicates either. `.ov-mix` is
    // the Overview block's own rule, so this also says the fold landed.
    expect(STYLES.length).toBeGreaterThan(100_000)
    expect(STYLES).toContain('.ov-mix {')
  })

  it('is the only sheet: no screen injects a <style> of its own', () => {
    // Read the way test_state_colour_discriminability.py reads it: a `<style`
    // tag in a screen's CODE. Comments are blanked first, because the house
    // style is a comment beside every decision and the comment recording this
    // one names the element it removed.
    const code = (text: string) =>
      text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ')
    const files = Object.entries(SOURCES)
    expect(files.length, 'the glob read no screen sources; this check is vacuous').toBeGreaterThan(20)
    const injecting = files.filter(([, text]) => /<style[\s>{]/.test(code(text))).map(([path]) => path)
    expect(injecting, `these screens inject CSS outside styles.css: ${injecting.join(', ')}`).toEqual([])
    const literal = files.filter(([, text]) => /const OVERVIEW_CSS\b/.test(text)).map(([path]) => path)
    expect(literal, 'OVERVIEW_CSS came back as a template literal').toEqual([])
  })
})
