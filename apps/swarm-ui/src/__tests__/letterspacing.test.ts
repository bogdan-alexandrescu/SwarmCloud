// THE TYPE RULE'S THIRD CLAUSE: NO TRACKING.
//
// The 2026-09-24 type rule is a 12px floor, no all-caps, and no tracking.
// `typescale.test.ts` holds the first two and holds POSITIVE letter-spacing at
// zero, because positive tracking is what capitals bring with them. Negative
// tracking slipped through that gap: the headings, the figures and the
// wordmark each carried a `-0.01em` to `-0.02em`, a local decision every time,
// and the rule says none.
//
// A SOURCE SCAN, for the reason typescale.test.ts gives: "no rule in this
// sheet tracks its text" is a property of the text, and a rule on a screen no
// test mounts would go unchecked by any DOM question.
//
// `0` and `normal` stay legal: they say "no tracking", which is the rule.
//
// MUTATION: write `letter-spacing: -0.01em` on any rule and this goes red,
// naming the line.

import STYLES from '../styles.css?raw'
import { describe, expect, it } from 'vitest'

/** Comments blanked to spaces of the same length, so line numbers still match the file. */
function blankComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
}

const ZERO = /^(?:normal|0(?:\.0+)?(?:px|em|rem)?|initial|inherit|unset)$/

describe('letter-spacing', () => {
  it('the scan reads the real sheet', () => {
    expect(STYLES.length, 'styles.css read as almost nothing').toBeGreaterThan(10000)
  })

  it('tracks no text anywhere in styles.css: every letter-spacing is 0 or normal', () => {
    const lines = blankComments(STYLES).split('\n')
    const tracked: string[] = []
    lines.forEach((line, i) => {
      for (const m of line.matchAll(/letter-spacing\s*:\s*([^;}]+)/g)) {
        const value = (m[1] ?? '').replace(/!important/, '').trim()
        if (!ZERO.test(value)) tracked.push(`styles.css:${i + 1} letter-spacing: ${value}`)
      }
    })
    expect(tracked).toEqual([])
  })

  it('the predicate catches what it is for', () => {
    // A guard that matched nothing would pass on any sheet; prove it bites.
    const sample = '.a { letter-spacing: -0.01em; }\n.b { letter-spacing: 0; }\n/* letter-spacing: 2px */\n.c{letter-spacing:.04em}'
    const found = [...blankComments(sample).matchAll(/letter-spacing\s*:\s*([^;}]+)/g)]
      .map((m) => (m[1] ?? '').trim())
      .filter((v) => !ZERO.test(v))
    expect(found).toEqual(['-0.01em', '.04em'])
  })
})
