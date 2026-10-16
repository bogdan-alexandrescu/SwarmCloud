// VISUAL QA RE-RUN LANE R14 (#1427): THE SHARED PRIMITIVES (components.css).
//
// Five findings, each asserted as the property the reviewer saw broken, not
// as the markup that happens to fix it:
//
//   V108  the absent mark's hatched band clears the mark's ROUNDED corner, not
//         merely its straight border;
//   V119  on a phone a tab strip never cuts its last visible label mid-word:
//         it wraps, and each row sits on its own hairline;
//   V172  a small segmented control on a phone grows to its 44px segments
//         instead of clipping them to 28px;
//   V207  a phone segmented control of five to eight segments never leaves one
//         segment alone on its last row;
//   V256  a disabled secondary button is drawn from tokens and its label clears
//         AA, as the disabled primary beside it already does.
//
// Everything goes through `cascade` (cssgate.ts, via `painted`) rather than
// `getComputedStyle`: jsdom applies no `@media` block and knows no `::before`,
// and V172 is a specificity contest inside a phone rule.

import { afterEach, describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { TABLES, THEMES, painted, resolveColour } from './marks'
import { contrast, over, resolveVars } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400, height: 800 }

afterEach(() => {
  document.body.innerHTML = ''
})

function tree(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  return host
}

const px = (v: string): number => {
  const n = Number.parseFloat(resolveVars(v.trim(), TABLES.dark))
  if (!Number.isFinite(n)) throw new Error(`not a length: ${v}`)
  return n
}

/** CSS's 1-4 value shorthand, expanded to [top, right, bottom, left]. */
function sides(v: string): [number, number, number, number] {
  const s = v.trim().split(/\s+/).map(px)
  const [top, right = top, bottom = top, left = right] = s as [number, number?, number?, number?]
  return [top!, right, bottom, left]
}

describe('V108: the absent mark’s hatch clears the mark’s rounded corner', () => {
  // MUTATION: put the band's inset back to `margin: 2px 0 2px 2px` and its
  // top-left corner sits 0.76px inside a 5px inner radius: the stripe runs
  // onto the curve, which is what the reviewer saw in dark.
  it('keeps the band’s leading corner at least 2px inside the curve of the mark’s border', () => {
    const mark = tree('<span class="ctl-mark is-absent">not reported</span>').firstElementChild!
    expect(painted(mark, 'background', WIDE, 'before') ?? '').toMatch(/--ctl-hatch/)
    const radius = px(painted(mark, 'border-radius', WIDE)!)
    const border = px(/(\d+(?:\.\d+)?px)/.exec(painted(mark, ['border', 'border-width'], WIDE)!)![1]!)
    const inner = radius - border
    const [top, , bottom, left] = sides(painted(mark, 'margin', WIDE, 'before')!)
    expect(top, 'top inset').toBe(bottom)
    // The band's corner (left, top) against the inner corner's circle, centred
    // at (inner, inner) in the padding box. Outside the corner square it is
    // clear of the curve by construction.
    const clearance = left >= inner || top >= inner ? Infinity : inner - Math.hypot(inner - left, inner - top)
    expect(clearance, `inner radius ${inner}px, band at (${left}, ${top})`).toBeGreaterThanOrEqual(2)
  })

  it('is still a band of hatch, wide enough to show more than one stroke', () => {
    const mark = tree('<span class="ctl-mark is-absent">not measured</span>').firstElementChild!
    expect(px(painted(mark, 'width', WIDE, 'before')!)).toBeGreaterThanOrEqual(6)
  })
})

describe('V119: a phone tab strip never cuts its last label mid-word', () => {
  // MUTATION: drop the phone `.c-tabs` rule and the strip is one row that
  // scrolls at 400px, its last visible tab cut under the shade ("Hot-sp").
  it('wraps at 400px, and stays one scrolling row with its edge cue at 1440', () => {
    const strip = tree('<nav class="c-tabs"><a>Overview</a><a aria-current="page">Hot-spots</a></nav>').firstElementChild!
    expect(painted(strip, 'flex-wrap', PHONE)).toBe('wrap')
    expect(painted(strip, 'flex-wrap', WIDE)).toBe('nowrap')
    expect(painted(strip, 'animation-timeline', WIDE)).toBe('scroll(self inline)')
  })

  // MUTATION: drop the per-tab hairline and a selected tab on the first row
  // draws its underline floating over the second (V148's objection to a wrap).
  it('draws a hairline under every tab, so each wrapped row reads as a strip', () => {
    const strip = tree('<nav class="c-tabs"><a>Overview</a><a aria-current="page">Graph</a></nav>').firstElementChild!
    for (const tab of [...strip.children]) {
      expect(painted(tab, 'box-shadow', PHONE) ?? '', tab.textContent ?? '').toMatch(/^inset 0 -1px 0 var\(--line\)$/)
    }
    // No gap between tabs on a phone, so a row's hairline is unbroken.
    expect(painted(strip, 'gap', PHONE)).toBe('0')
  })

  it('leaves the agent split’s strip one row (V148, owner decision Q2 of 2026-10-16)', () => {
    const host = tree('<div class="ag-split"><div class="ag-tabs-edge"><div class="c-tabs ag-split-tabs" role="tablist"></div></div></div>')
    expect(painted(host.querySelector('.ag-split-tabs')!, 'flex-wrap', PHONE)).toBe('nowrap')
  })
})

describe('V172: a small segmented control on a phone holds its 44px segments', () => {
  // MUTATION: drop `.c-seg.is-sm` from the phone rule and `.c-seg.is-sm {
  // height: var(--h-sm) }` (0,2,0) beats the phone `.c-seg { height: auto }`
  // (0,1,0): two 44px segments in a 28px box, their labels cut at the foot.
  it('is as tall as its segments at 400px, and 28px at 1440', () => {
    const seg = tree('<div class="c-seg is-sm"><button>Mine</button><button>All tenants</button></div>').firstElementChild!
    expect(painted(seg, 'height', PHONE)).toBe('auto')
    expect(painted(seg.firstElementChild!, 'min-height', PHONE)).toBe('44px')
    expect(painted(seg, 'height', WIDE)).toBe('var(--h-sm)')
  })
})

describe('V207: a phone segmented control never strands one segment on its last row', () => {
  /** The most segments of `seg`'s children a 100%-wide row holds, from their basis. */
  function perRow(seg: Element): number {
    const bases = [...seg.children].map((c) => {
      const flex = painted(c, ['flex', 'flex-basis'], PHONE) ?? ''
      const m = /(\d+(?:\.\d+)?)%/.exec(flex)
      return m === null ? null : Number.parseFloat(m[1]!)
    })
    expect(new Set(bases).size, 'every segment of one control has one basis').toBe(1)
    const basis = bases[0]
    expect(basis, 'the segments have no percentage basis: rows pack by content').not.toBeNull()
    return Math.floor(100 / basis!)
  }

  // MUTATION: drop the quantity rules and Runs' six states pack by content at
  // 400px: five on the first row and "rejected 0" alone on the second.
  it.each([5, 6, 7, 8])('lays %i segments out in rows of at least two', (n) => {
    const seg = tree(`<div class="c-seg">${'<button>state 0</button>'.repeat(n)}</div>`).firstElementChild!
    const k = perRow(seg)
    expect(k, `${n} segments`).toBeGreaterThanOrEqual(2)
    const last = n % k === 0 ? k : n % k
    expect(last, `${n} segments at ${k} a row leave ${last} on the last`).toBeGreaterThanOrEqual(2)
  })

  it('leaves a two- or three-segment control sized by its labels', () => {
    for (const n of [2, 3]) {
      const seg = tree(`<div class="c-seg">${'<button>x</button>'.repeat(n)}</div>`).firstElementChild!
      for (const c of [...seg.children]) expect(painted(c, ['flex', 'flex-basis'], PHONE)).toBe('1 0 auto')
    }
  })

  it('does not touch the desktop control', () => {
    const seg = tree(`<div class="c-seg">${'<button>state 0</button>'.repeat(6)}</div>`).firstElementChild!
    expect(painted(seg.firstElementChild!, ['flex', 'flex-basis'], WIDE)).toBeNull()
  })
})

describe('V256: a disabled secondary button is drawn from tokens, not faded', () => {
  // MUTATION: put `opacity: .45` back on `.c-btn:disabled` and the plain
  // "Read" button composites to 2.95:1 in light; drop the ghost exception and
  // a disabled ghost grows a filled ground.
  it.each(['secondary', 'is-ghost', 'is-danger'])('a disabled %s button is not faded, and its label clears AA in both themes', (name) => {
    const kind = name === 'secondary' ? '' : name
    const btn = document.createElement('button')
    btn.className = `c-btn ${kind}`.trim()
    btn.disabled = true
    btn.textContent = 'Read'
    document.body.appendChild(btn)
    const opacity = Number(painted(btn, 'opacity', WIDE) ?? '1')
    expect(opacity, `${kind || 'secondary'} is faded`).toBe(1)
    // A hover cannot relight it.
    expect(painted(btn, 'color', { ...WIDE, states: ['hover'] })).toBe('var(--text-dim)')
    for (const theme of THEMES) {
      const env = { ...WIDE, theme }
      const ground = resolveColour('var(--surface)', theme)
      // `none` (a ghost) and no declaration both leave the panel showing.
      const fill = painted(btn, ['background', 'background-color'], env)
      const back = over(resolveColour(fill === null || fill === 'none' ? 'transparent' : fill, theme), ground)
      const ink = over(resolveColour(painted(btn, 'color', env)!, theme), back)
      const ratio = contrast(ink, back)
      expect(ratio, `${kind || 'secondary'} disabled, ${theme}: ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('keeps a disabled ghost groundless and borderless', () => {
    const btn = document.createElement('button')
    btn.className = 'c-btn is-ghost'
    btn.disabled = true
    document.body.appendChild(btn)
    expect(painted(btn, ['background', 'background-color'], WIDE)).toBe('none')
    expect(painted(btn, 'border-color', WIDE)).toBe('transparent')
  })
})
