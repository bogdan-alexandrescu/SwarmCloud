// diff.css GETS THE GATES THE SECTION SHEETS GET.
//
// The section-sheet gates (sections.stylesheet.test.ts and its neighbours)
// read `src/styles/*.css`. The viewer's own sheet lives in `src/diff/`, the
// DIFF1 lane's territory, so their glob does not reach it; this file applies
// the same checks to it, so a rule added there cannot dodge them by location.
//
// BREAK IT: drop a closing brace in diff.css, declare one selector twice
// apart, or write `font: 600 11px/1.4 var(--font)` -- each goes red below.

import { describe, expect, it } from 'vitest'

import SHEET from '../../diff/diff.css?raw'
import { gate } from '../cssgate'

function blankComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
}

function declarations(css: string, property: RegExp): { line: number; value: string }[] {
  const out: { line: number; value: string }[] = []
  blankComments(css)
    .split('\n')
    .forEach((text, i) => {
      for (const m of text.matchAll(new RegExp(`(?:^|[\\s;{])(${property.source})\\s*:\\s*([^;}]+)`, 'g'))) {
        out.push({ line: i + 1, value: (m[2] ?? '').replace(/!important/, '').trim() })
      }
    })
  return out
}

describe('src/diff/diff.css', () => {
  it('was read', () => {
    expect(SHEET.length).toBeGreaterThan(500)
  })

  it('parses the way a browser parses it', () => {
    expect(gate(SHEET, 'diff/diff.css').problems).toEqual([])
  })

  it('declares no selector twice non-adjacently, and no keyframes or :root tokens', () => {
    const r = gate(SHEET, 'diff/diff.css')
    expect(r.duplicates).toEqual([])
    expect(r.keyframes).toEqual([])
    expect(blankComments(SHEET)).not.toMatch(/:root\s*\{/)
  })

  it('scopes every rule to the viewer', () => {
    const selectors = blankComments(SHEET)
      .split('}')
      .map((r) => r.split('{')[0]!.trim())
      .filter((s) => s !== '')
    expect(selectors.length).toBeGreaterThan(10)
    for (const list of selectors) for (const sel of list.split(',')) expect(sel.trim()).toMatch(/^\.diff/)
  })

  it('tracks no text, sets no all-caps, and sizes type only from the scale tokens', () => {
    const bad = [
      ...declarations(SHEET, /letter-spacing/).filter((d) => !/^(?:normal|0(?:\.0+)?(?:px|em|rem)?)$/.test(d.value)),
      ...declarations(SHEET, /text-transform/).filter((d) => /uppercase/.test(d.value)),
      ...declarations(SHEET, /font-size/).filter((d) => /\d(?:px|rem|em|pt)\b/.test(d.value)),
      ...declarations(SHEET, /font/).filter((d) => /(?:^|\s)\d+(?:\.\d+)?(?:px|rem|pt)(?:\/|\s|$)/.test(d.value)),
    ]
    expect(bad.map((d) => `diff.css:${d.line} ${d.value}`)).toEqual([])
  })

  it('gives a syntax token colour only, so a row cannot move off its windowed offset', () => {
    const tokenRules = blankComments(SHEET).match(/\.diff-tk[^{]*\{[^}]*\}/g) ?? []
    expect(tokenRules.length).toBeGreaterThan(0)
    for (const r of tokenRules) expect(r.replace(/^[^{]*\{/, '').replace(/\}$/, '').trim()).toMatch(/^color: [^;]+;$/)
  })
})
