// VISUAL QA LANE L09 (#1038): THE SHARED PRIMITIVES AND THE SECTION `?`.
//
// Four findings, each asserted as the property the reviewer saw broken, not
// as the markup that happens to fix it:
//
//   V062  the page-title `?` on the Submit pages asks Submit's question, not
//         Work's (owner decision Q2: everywhere else it stays section-level);
//   V108  the hatched band of an "absent" mark stands clear of the mark's
//         rounded border instead of running into it;
//   V119  a tab strip that scrolls draws a cue on the edge with tabs behind it;
//   V138  a disabled primary button's label is legible in both themes.
//
// The stylesheet properties go through `cascade` (cssgate.ts) rather than
// `getComputedStyle`: jsdom orders rules by source alone and knows no
// `::before`, and V108 is a pseudo-element.

import COMPONENTS_CSS from '../styles/components.css?raw'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import { addressToPath } from '../paths'
import type { CascadeEnv } from './cssgate'
import { THEMES, painted, resolveColour } from './marks'
import { contrast, over } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 6000 }

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
})

/** Open the head's `?` on `path` and return its card's text and the glyph's name. */
async function headQuestion(path: string): Promise<{ text: string; name: string }> {
  window.history.replaceState(null, '', path)
  render(<App />)
  const glyph = await waitFor(() => {
    const g = document.querySelector<HTMLButtonElement>('.ctl-q-glyph')
    expect(g, `${path} draws no head \`?\``).not.toBeNull()
    return g!
  }, WAIT)
  fireEvent.focus(glyph)
  const card = await waitFor(() => {
    const c = document.querySelector('.ctl-q-card')
    expect(c, `${path}: the \`?\` opened nothing`).not.toBeNull()
    return c!
  }, WAIT)
  const out = { text: (card.textContent ?? '').replace(/\s+/g, ' '), name: glyph.getAttribute('aria-label') ?? '' }
  cleanup()
  return out
}

describe('V062: the Submit pages ask their own question behind the title `?`', () => {
  // MUTATION: put `helpSection` back to `section ?? sectionOf(WORK)` and every
  // Submit route below opens "Work answers".
  it('the chooser and all three forms open Submit’s text, not Work’s', async () => {
    const paths = ['/submit', ...['work/new', 'work/new-workflow', 'work/new-issue'].map((a) => addressToPath(a))]
    const visited: string[] = []
    for (const path of paths) {
      const q = await headQuestion(path)
      expect(q.text, path).toContain('Submit answers')
      expect(q.text, path).toMatch(/run now or wait for room/)
      expect(q.text, `${path} still asks Work’s question`).not.toMatch(/What is running/)
      expect(q.name, path).toBe('Help: Submit answers')
      visited.push(path)
    }
    // Empty output is not success: four distinct routes were opened.
    expect(new Set(visited).size).toBe(4)
  })

  it('a section page keeps its section’s question (Q2: section-level help stays)', async () => {
    const q = await headQuestion('/capacity/accounts')
    expect(q.text).toContain('Capacity answers')
    expect(q.text).not.toContain('Submit answers')
  })
})

describe('V108: the absent mark’s hatch stands clear of its rounded border', () => {
  // MUTATION: drop the `margin` from `.ctl-mark.is-absent::before` and the
  // band is flush against the border again (all three insets read 0).
  it('insets the hatched band from the border on the top, bottom and leading edge', () => {
    const mark = document.createElement('i')
    mark.className = 'ctl-mark is-absent'
    document.body.appendChild(mark)
    expect(painted(mark, 'background', WIDE, 'before') ?? '').toMatch(/--ctl-hatch/)
    const margin = painted(mark, 'margin', WIDE, 'before')
    expect(margin, 'the band has no margin: it touches the border').not.toBeNull()
    const sides = margin!.trim().split(/\s+/).map((v) => Number.parseFloat(v))
    // CSS's 1-4 value shorthand, expanded to top, right, bottom, left.
    const [top, right = top, bottom = top, left = right] = sides as [number, number?, number?, number?]
    expect(top, 'top inset').toBeGreaterThan(0)
    expect(bottom, 'bottom inset').toBeGreaterThan(0)
    expect(left, 'leading inset').toBeGreaterThan(0)
  })
})

describe('V119: a tab strip that scrolls says so', () => {
  it('drives an edge cue off the strip’s own scroll position', () => {
    const strip = document.createElement('nav')
    strip.className = 'c-tabs'
    document.body.appendChild(strip)
    expect(painted(strip, 'animation-timeline', WIDE)).toBe('scroll(self inline)')
    expect(painted(strip, 'animation', WIDE) ?? '').toMatch(/^c-tabs-edge\b/)
  })

  // MUTATION: swap the 0% and 100% frames and this fails: the cue would sit on
  // the edge with nothing behind it.
  it('shades the trailing edge at rest and the leading edge at the end, and never neither mid-way', () => {
    const kf = /@keyframes c-tabs-edge \{([\s\S]*?)\n\}/.exec(COMPONENTS_CSS)
    expect(kf, 'the keyframes are gone').not.toBeNull()
    const frame = (sel: string): string[] => {
      const m = new RegExp(`(?:^|\\n)\\s*${sel} \\{ box-shadow: ([^;]+);`).exec(kf![1]!)
      expect(m, `no ${sel} frame`).not.toBeNull()
      return m![1]!.split(/,\s*(?=inset)/).map((x) => x.trim())
    }
    const shaded = (s: string): boolean => s.includes('var(--text)')
    const [restL, restR] = frame('0%')
    const [midL, midR] = frame('8%, 92%')
    const [endL, endR] = frame('100%')
    expect([shaded(restL!), shaded(restR!)]).toEqual([false, true])
    expect([shaded(midL!), shaded(midR!)]).toEqual([true, true])
    expect([shaded(endL!), shaded(endR!)]).toEqual([true, false])
    // The trailing shade is cast inward from the right, the leading from the left.
    expect(restR).toMatch(/^inset -\d+px/)
    expect(endL).toMatch(/^inset \d+px/)
  })
})

describe('V138: a disabled primary button’s label is legible', () => {
  // MUTATION: delete the filled-button `:disabled` rule and the cascade falls
  // back to `.c-btn:disabled { opacity: .45 }`, which fails the opacity check
  // and, composited, the ratio.
  it.each(['is-primary', 'is-danger-filled'])('.c-btn.%s:disabled is not faded, and its label clears AA in both themes', (kind) => {
    const btn = document.createElement('button')
    btn.className = `c-btn ${kind}`
    btn.disabled = true
    btn.textContent = 'Submit'
    document.body.appendChild(btn)
    const opacity = Number(painted(btn, 'opacity', WIDE) ?? '1')
    expect(painted(btn, 'background', WIDE)).not.toBeNull()
    expect(painted(btn, 'color', WIDE)).not.toBeNull()
    for (const theme of THEMES) {
      const env = { ...WIDE, theme }
      // The button sits on a panel: `--surface`, the card ground.
      const ground = resolveColour('var(--surface)', theme)
      const back = over(resolveColour(painted(btn, 'background', env)!, theme), ground)
      const ink = over(resolveColour(painted(btn, 'color', env)!, theme), back)
      // Opacity composites the whole button, label and fill, toward the ground.
      const fade = (c: typeof ink) => over({ ...c, a: c.a * opacity }, ground)
      const ratio = contrast(fade(ink), fade(back))
      expect(ratio, `${kind} disabled, ${theme}: ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5)
    }
    expect(opacity).toBe(1)
  })
})
