// THE SECTION SHEETS GET THE GATES styles.css GETS.
//
// Since 2026-10-02 each section's rules live in their own file under
// `src/styles/` (the seven UI lanes ran at once, so each section moved its
// rules out of styles.css). stylesheet.gate.test.ts, letterspacing.test.ts and
// typescale.test.ts read styles.css alone, so a rule moved out of it left
// every gate behind. This file runs the same checks over every section sheet
// it finds -- `import.meta.glob`, not a list, so a sheet added later is gated
// the day it lands -- plus the batch-3 type rules from PICKS.md: no
// letter-spacing, no all-caps, and no raw pixel font size (the 12px floor is
// the tokens' to keep).
//
// BREAK IT: write `.x { color: red` without its brace in styles/capacity.css
// -- "parses the way a browser parses it" fails. Or add `text-transform:
// uppercase` to any section sheet -- "no all-caps" fails.

import { describe, expect, it } from 'vitest'

import { gate } from './cssgate'

const SHEETS = import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true })

/** Comments blanked to spaces of the same length, so line numbers still match the file. */
function blankComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
}

/** Every `property: value` in a sheet, with its line. */
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

describe('the section stylesheets', () => {
  const entries = Object.entries(SHEETS)

  it('found the sheets this lane ships', () => {
    const names = entries.map(([path]) => path.replace('../styles/', ''))
    expect(names).toContain('capacity.css')
    expect(names).toContain('admin.css')
    for (const [, text] of entries) expect(text.length).toBeGreaterThan(200)
  })

  for (const [path, text] of entries) {
    const label = path.replace('../', '')
    describe(label, () => {
      it('parses the way a browser parses it', () => {
        expect(gate(text, label).problems).toEqual([])
      })

      it('declares no selector twice non-adjacently, and no keyframes twice', () => {
        const r = gate(text, label)
        expect(r.duplicates).toEqual([])
        expect(r.rootTokens).toEqual([])
        expect(r.keyframes).toEqual([])
        expect(r.animations).toEqual([])
      })

      it('declares no :root token: the tokens are the shell’s', () => {
        expect(blankComments(text)).not.toMatch(/:root\s*\{/)
      })

      it('tracks no text: every letter-spacing is 0 or normal', () => {
        const tracked = declarations(text, /letter-spacing/).filter(
          (d) => !/^(?:normal|0(?:\.0+)?(?:px|em|rem)?)$/.test(d.value),
        )
        expect(tracked.map((d) => `${label}:${d.line} ${d.value}`)).toEqual([])
      })

      it('sets no all-caps', () => {
        const caps = declarations(text, /text-transform/).filter((d) => /uppercase/.test(d.value))
        expect(caps.map((d) => `${label}:${d.line} ${d.value}`)).toEqual([])
      })

      it('sizes type from the scale tokens, never a raw pixel size', () => {
        const raw = [
          ...declarations(text, /font-size/).filter((d) => /\d(?:px|rem|em|pt)\b/.test(d.value)),
          // `font: 600 11px/1.4 …` is a raw size too.
          ...declarations(text, /font/).filter((d) => /(?:^|\s)\d+(?:\.\d+)?(?:px|rem|pt)(?:\/|\s|$)/.test(d.value)),
        ]
        expect(raw.map((d) => `${label}:${d.line} ${d.value}`)).toEqual([])
      })
    })
  }
})
