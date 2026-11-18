// THE SHEET A PROBE READS, CUT TO WHAT CAN DECIDE A STYLE (5 November 2026).
//
// Moved out of spacingsweep.tsx unchanged so that a single-screen probe
// (setup.access.styles.test.tsx) reads the same cut the sweep does: the
// injected section-sheet twins come out while the probe reads, and the
// resolved sheet in the document is cut to the rules that reach what is
// attached, so nwsapi's selector cache holds them all. Each block keeps the
// reason it was written with.

import { expect } from 'vitest'

/**
 * THE INJECTED TWINS COME OUT WHILE THE PROBE READS (5 November 2026). jsdom
 * runs every rule of every sheet in the document against each element it
 * cascades, and `css: true` has already injected each section sheet once,
 * unresolved; the sweep's resolved copy of SHEET repeats every one of those
 * rules after them, so the document held them twice and the admin-setup slice
 * took about 13 minutes alone (once ending its CI shard on "Timeout calling
 * onTaskUpdate"). jsdom cascades by document order alone (its style-rules.js
 * implements no specificity), so a rule followed by its twin decides nothing
 * the twin does not, and the probe computes the same style without it. The
 * twins are detached for the probe phase and put back after, and only after
 * `expectTwins` has held, in each theme, that every rule a twin applies --
 * selector, properties, priority -- the resolved copy applies too: a sheet
 * that stops being a twin fails the sweep by name instead of being dropped.
 */
export function applied(sheet: CSSStyleSheet): string[] {
  // What jsdom's cascade reads (style-rules.js): a top-level style rule, and a
  // style rule directly inside an `@media` whose list names `screen`. Nothing
  // else applies, in either copy.
  const out: string[] = []
  const one = (r: CSSRule) => {
    if (r.constructor.name !== 'CSSStyleRule') return
    const style = (r as CSSStyleRule).style
    const props: string[] = []
    for (let i = 0; i < style.length; i++) props.push(`${style[i]}${style.getPropertyPriority(style[i]!) ? '!' : ''}`)
    out.push(`${(r as CSSStyleRule).selectorText}{${props.sort().join(',')}}`)
  }
  for (const r of sheet.cssRules) {
    if (r.constructor.name === 'CSSMediaRule') {
      const m = r as CSSMediaRule
      if (Array.prototype.indexOf.call(m.media, 'screen') !== -1) for (const inner of m.cssRules) one(inner)
    } else one(r)
  }
  return out
}

export interface Twin {
  el: HTMLStyleElement
  rules: string[]
}

/**
 * The injected twins of `sheetFiles` -- the keys of an `import.meta.glob`
 * made from `src/__tests__/`, as `../styles/x.css` -- in the document now.
 */
export function sectionTwins(sheetFiles: readonly string[]): Twin[] {
  const files = sheetFiles.map((path) => path.replace(/^\.\./, '/src'))
  return [...document.querySelectorAll<HTMLStyleElement>('style[data-vite-dev-id]')]
    .filter((el) => el.sheet !== null && files.some((f) => el.getAttribute('data-vite-dev-id')!.endsWith(f)))
    .map((el) => ({ el, rules: applied(el.sheet!) }))
}

/** Hold that every rule a twin applies is applied again by the resolved copy, later. */
export function expectTwins(twins: readonly Twin[], resolved: HTMLStyleElement): void {
  const have = new Set(applied(resolved.sheet!))
  for (const t of twins)
    expect(
      t.rules.filter((r) => !have.has(r)),
      `${t.el.getAttribute('data-vite-dev-id')} applies rules its resolved copy does not`,
    ).toEqual([])
}

/**
 * THE RULES THAT CAN REACH WHAT IS ATTACHED, in their order (5 November 2026).
 * jsdom matches a selector through nwsapi, which keeps its compiled selectors
 * in a least-recently-used cache of 1,000 (`CACHE_LIMIT` in nwsapi 2.2.28,
 * not configurable). The document held about 11,200 rules, and every
 * `getComputedStyle` runs all of them in the same order, so every lookup
 * missed and every selector was compiled again: about 690 ms an element, and
 * about 13 minutes for the admin-setup slice. Cut to the rules that reach the
 * one attached snapshot, the cache holds them all.
 *
 * NOTHING THAT DECIDES A STYLE IS CUT. A style rule stays when its selector
 * matches any element in the document -- jsdom's cascade asks exactly that of
 * each element, and a rule that matches none of them changes no style -- and
 * also when any comma part, stripped of its pseudo-classes as
 * `geometryCandidates` strips them, matches one, so the probe's candidates are
 * the ones the whole sheet gave. A selector nwsapi refuses stays (it matches
 * nothing in jsdom either way). Every other rule stays as written, and order
 * is kept, which is all jsdom's cascade reads.
 */
export function reaching(rules: readonly CSSRule[]): { text: string; count: number } {
  const out: string[] = []
  for (const r of rules) {
    if (r.constructor.name === 'CSSStyleRule') {
      if (reaches((r as CSSStyleRule).selectorText)) out.push(r.cssText)
    } else if (r.constructor.name === 'CSSMediaRule') {
      const m = r as CSSMediaRule
      const inner = reaching([...m.cssRules])
      if (inner.count > 0) out.push(`@media ${m.media.mediaText} {\n${inner.text}\n}`)
    } else out.push(r.cssText)
  }
  return { text: out.join('\n'), count: out.length }
}

function reaches(selector: string): boolean {
  const bases = selector.split(',').map((one) => one.replace(/::?[a-zA-Z-]+(\([^)]*\))?/g, '').trim())
  for (const sel of [selector, ...bases]) {
    if (sel === '') continue
    try {
      if (document.querySelector(sel) !== null) return true
    } catch {
      return true
    }
  }
  return false
}
