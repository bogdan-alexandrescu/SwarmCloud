// THE LEDGER'S STYLESHEET HALF (#185), AS RULES THE CASCADE HAS TO PICK.
//
// Asked of `cascade` (cssgate.ts) rather than `getComputedStyle`, because jsdom
// applies no `@media` and no `@container`, and every rule here is exactly one
// of those: which of the three drawings shows at a container width, what a
// phone hides behind `Filters · N`, and where the 44px targets are.
//
// MUTATIONS each case names: lower a container threshold below the width its
// drawing was authored at (the drawing is then scaled down and its ticks
// render under --t-micro); drop the container declaration (a query naming a
// container nothing establishes matches nothing, silently); draw a legend key
// its own way instead of by TS-4's rule; give the pick a hue; leave the phone's
// step buttons under 44px; put the scale back inside the scroller; show a
// card's strips in a card too narrow for its row.

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it } from 'vitest'

import { cascade, type CascadeEnv } from './cssgate'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function fragment(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

function pick(host: Element, sel: string): Element {
  const el = host.querySelector(sel)
  expect(el, `the fixture has no ${sel}`).not.toBeNull()
  return el!
}

function won(el: Element, prop: string | readonly string[], env: CascadeEnv, states: readonly string[] = []): string | null {
  const r = cascade(STYLES, el, prop, { ...env, states })
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

const CHART =
  '<figure class="ol-chart">' +
  '<div class="ol-drawing is-wide" id="w"><svg class="ol-gutter"></svg><div class="ol-plot"></div></div>' +
  '<div class="ol-drawing is-mid" id="m"><svg class="ol-gutter"></svg><div class="ol-plot"></div></div>' +
  '<div class="ol-drawing is-narrow" id="n"><svg class="ol-gutter"></svg><div class="ol-plot"></div></div>' +
  '</figure>'

describe('the ledger is drawn three times, and its own width picks one (§7.2)', () => {
  it('is asked of the chart’s own box: the figure is the size container the queries name', () => {
    const f = fragment(CHART)
    expect(won(pick(f, '.ol-chart'), 'container', WIDE)).toMatch(/^ol-chart\s*\/\s*inline-size$/)
    expect(STYLES).toMatch(/@container\s+ol-chart\s*\(min-width:\s*640px\)/)
    expect(STYLES).toMatch(/@container\s+ol-chart\s*\(min-width:\s*1080px\)/)
  })

  it('shows exactly one drawing at each container width, never one wider than its box', () => {
    const f = fragment(CHART)
    const shown = (container: number) =>
      ['#w', '#m', '#n'].filter((id) => won(pick(f, id), 'display', { width: 1440, container }) !== 'none')
    expect(shown(358), 'a phone column').toEqual(['#n'])
    expect(shown(639)).toEqual(['#n'])
    expect(shown(640), 'the 640 drawing shows in a 640 box, and not narrower').toEqual(['#m'])
    expect(shown(1079)).toEqual(['#m'])
    expect(shown(1144), 'the page-width content column').toEqual(['#w'])
  })

  it('fades the plot’s older edge only while buckets are off-screen (TS-3)', () => {
    const f = fragment('<div class="ol-plot" id="a"></div><div class="ol-plot has-older" id="b"></div>')
    expect(won(pick(f, '#a'), ['mask-image', 'mask'], WIDE)).toBeNull()
    expect(won(pick(f, '#b'), ['mask-image', 'mask'], WIDE) ?? '').toMatch(/^linear-gradient\(to right, transparent/)
  })

  it('pins the scale column and the lane labels over the plot that scrolls, on an opaque ground (wireframe_390)', () => {
    const f = fragment(
      '<figure class="ol-chart"><div class="ol-drawing is-narrow">' +
        '<svg class="ol-gutter"><text class="ol-tick">100%</text></svg>' +
        '<span class="ol-lane-label">Success rate</span>' +
        '<div class="ol-plot"></div></div></figure>',
    )
    expect(won(pick(f, '.ol-drawing'), 'position', PHONE)).toBe('relative')
    expect(won(pick(f, '.ol-plot'), 'overflow-x', PHONE)).toBe('auto')
    // The scale is laid over the drawing's left edge, outside the scroller.
    expect(won(pick(f, '.ol-gutter'), 'position', PHONE)).toBe('absolute')
    expect(won(pick(f, '.ol-gutter'), 'left', PHONE)).toBe('0')
    // A lane label sits over columns that move under it: its own ground keeps it legible,
    // and it takes no pointer so a tap on the column beneath still picks it.
    const label = pick(f, '.ol-lane-label')
    expect(won(label, 'position', PHONE)).toBe('absolute')
    expect(won(label, ['background', 'background-color'], PHONE)).toBe('var(--bg)')
    expect(won(label, 'pointer-events', PHONE)).toBe('none')
    expect(won(label, 'white-space', PHONE)).toBe('nowrap')
  })

  it('draws its tick text at --t-micro, in mono', () => {
    const f = fragment('<svg class="ol-svg"><text class="ol-tick">0</text></svg>')
    expect(won(pick(f, 'text'), ['font-size', 'font'], PHONE)).toBe('var(--t-micro)')
  })
})

describe('the marks keep TS-4’s forms and the pick keeps no hue', () => {
  it('fills succeeded with --ok, failed with --bad and a --surface cut, the cancels a failure caused as an outline', () => {
    const f = fragment(
      '<svg class="ol-svg"><rect class="ol-m-ok"/><rect class="ol-m-bad"/><rect class="ol-m-cut"/><rect class="ol-m-after"/>' +
        '<rect class="ol-sel"/><rect class="ol-sel-rule"/><polyline class="ol-m-rate"/><path class="ol-m-sub"/></svg>',
    )
    expect(won(pick(f, '.ol-m-ok'), 'fill', WIDE)).toBe('var(--ok)')
    expect(won(pick(f, '.ol-m-bad'), 'fill', WIDE)).toBe('var(--bad)')
    expect(won(pick(f, '.ol-m-cut'), 'fill', WIDE)).toBe('var(--surface)')
    expect(won(pick(f, '.ol-m-after'), 'fill', WIDE)).toBe('none')
    expect(won(pick(f, '.ol-m-after'), 'stroke', WIDE)).toBe('var(--text-dim)')
    // The rate line is a series identity, never a verdict colour (§1.6).
    expect(won(pick(f, '.ol-m-rate'), 'stroke', WIDE)).toBe('var(--series-1)')
    expect(won(pick(f, '.ol-m-sub'), 'stroke', WIDE)).toBe('var(--series-2)')
    // The pick: a surface step and an ink rule.
    expect(won(pick(f, '.ol-sel'), 'fill', WIDE)).toBe('var(--surface-2)')
    expect(won(pick(f, '.ol-sel-rule'), 'fill', WIDE)).toBe('var(--text)')
  })

  it('draws each TS-4 legend key by the very rule its segment uses (TS-22)', () => {
    const f = fragment(
      '<p class="chart-legend"><i class="k succeeded"></i><i class="k failed"></i><i class="k cancelled"></i></p>' +
        '<p class="ol-legend"><i class="ol-k is-ok"></i><i class="ol-k is-bad"></i><i class="ol-k is-ended"></i></p>',
    )
    const pairs: Array<[string, string]> = [
      ['.k.succeeded', '.ol-k.is-ok'],
      ['.k.failed', '.ol-k.is-bad'],
      ['.k.cancelled', '.ol-k.is-ended'],
    ]
    for (const [ts4, ol] of pairs) {
      expect(won(pick(f, ol), ['background', 'background-image'], WIDE), `${ol} is drawn its own way`).toBe(
        won(pick(f, ts4), ['background', 'background-image'], WIDE),
      )
      expect(won(pick(f, ol), 'box-shadow', WIDE)).toBe(won(pick(f, ts4), 'box-shadow', WIDE))
    }
  })

  it('draws a card’s outcome track in the same forms, succeeded in --ok, a failure never under 4px', () => {
    const f = fragment(
      '<span class="ctl-track ol-meter"><i class="ol-seg succeeded"></i><i class="ol-seg failed"></i><i class="ol-seg cancelled"></i></span>',
    )
    expect(won(pick(f, '.ol-seg.succeeded'), ['background', 'background-color'], WIDE)).toBe('var(--ok)')
    expect(won(pick(f, '.ol-seg.failed'), ['background', 'background-color'], WIDE)).toBe('var(--bad)')
    expect(won(pick(f, '.ol-seg.failed'), 'min-width', WIDE)).toBe('4px')
    expect(won(pick(f, '.ol-seg.cancelled'), ['background', 'background-image'], WIDE) ?? '').toMatch(
      /^repeating-linear-gradient\(\s*to bottom/,
    )
  })

  it('rings a focused column and draws the pick on it without a hue', () => {
    const f = fragment('<div class="ol-cols"><div class="ol-col" tabindex="0"></div></div>')
    const ring = won(pick(f, '.ol-col'), ['outline', 'outline-style', 'outline-width'], WIDE, ['focus-visible'])
    expect(ring ?? '', 'a column Tab reaches draws no focus ring').toContain('var(--info)')
    expect(won(pick(f, '.ol-col'), ['background', 'background-color'], WIDE)).toBeNull()
  })
})

describe('at 390 (§7.2)', () => {
  const TOOLBAR =
    '<div class="ctl-toolbar ol-toolbar"><div class="ctl-seg ol-span"><button class="is-wide-only">24h</button><button>14d</button></div>' +
    '<button class="ol-sheet-toggle">Filters · 0</button>' +
    '<div class="ol-sheet" id="closed"><div class="ctl-seg ol-span-more"><button>24h</button></div></div>' +
    '<div class="ol-sheet is-open" id="open"></div>' +
    '<button class="ol-table-toggle">Table</button></div>' +
    '<p class="ol-readout-head"><button class="ol-step">‹</button></p>'

  it('keeps the span, moves 24h and the range into the sheet, and hides the sheet behind Filters · N', () => {
    const f = fragment(TOOLBAR)
    expect(won(pick(f, '.ol-span > .is-wide-only'), 'display', PHONE)).toBe('none')
    expect(won(pick(f, '.ol-span > .is-wide-only'), 'display', WIDE)).toBeNull()
    expect(won(pick(f, '.ol-sheet-toggle'), 'display', WIDE), 'the sheet toggle shows on a wide screen').toBe('none')
    expect(won(pick(f, '.ol-sheet-toggle'), 'display', PHONE)).toBe('inline-flex')
    expect(won(pick(f, '#closed'), 'display', PHONE)).toBe('none')
    expect(won(pick(f, '#open'), 'display', PHONE)).toBe('flex')
    // At a wide width the sheet's controls sit in the toolbar's own row.
    expect(won(pick(f, '#closed'), 'display', WIDE)).toBe('contents')
    expect(won(pick(f, '.ol-span-more'), 'display', WIDE)).toBe('none')
    expect(won(pick(f, '.ol-span-more'), 'display', PHONE)).toBe('inline-flex')
  })

  it('gives the phone’s bucket steps, the sheet toggle and the Table toggle 44px targets, and no steps on a wide screen', () => {
    const f = fragment(TOOLBAR)
    const px = (v: string | null) => Number.parseFloat(v ?? '')
    expect(px(won(pick(f, '.ol-step'), 'min-height', PHONE))).toBeGreaterThanOrEqual(44)
    expect(px(won(pick(f, '.ol-step'), 'min-width', PHONE))).toBeGreaterThanOrEqual(44)
    expect(px(won(pick(f, '.ol-sheet-toggle'), 'min-height', PHONE))).toBeGreaterThanOrEqual(44)
    expect(px(won(pick(f, '.ol-table-toggle'), 'min-height', PHONE))).toBeGreaterThanOrEqual(44)
    expect(won(pick(f, '.ol-step'), 'display', WIDE)).toBe('none')
  })

  it('draws the cards’ mini strips only at 900px and up', () => {
    const f = fragment(
      '<section class="ctl-card ol-card"><div class="ctl-card-body"><div class="ol-row"><svg class="ol-strip is-n14"></svg></div></div></section>',
    )
    expect(won(pick(f, '.ol-strip'), 'display', { width: 899, container: 700 })).toBe('none')
    expect(won(pick(f, '.ol-strip'), 'display', { width: 900, container: 700 })).toBe('block')
  })

})

describe('a card’s mini strips are drawn only where its row fits (1440, 3-up)', () => {
  // A row is name (≥104) · track (≥48) · count (44) · strip, 8px apart: 220px
  // before the strip, which is 6px a bucket. At 1440 the cards are 3-up
  // (.ctl-cards' minmax(min(100%, 340px), 1fr) in a ~1144px column), so a
  // card body is ~329px wide -- room for a 14-bucket strip (304px) and not for
  // 24 hours (364px) or 30 days (400px), which spilled 20-70px over the card's
  // border into the next card.
  const CARD =
    '<section class="ctl-card ol-card"><div class="ctl-card-body">' +
    ['14', '24', '31', '45', '60'].map((n) => `<div class="ol-row"><svg class="ol-strip is-n${n}" id="s${n}"></svg></div>`).join('') +
    '</div></section>'
  const shown = (container: number, width = 1440) => {
    const f = fragment(CARD)
    return ['#s14', '#s24', '#s31', '#s45', '#s60'].filter((id) => won(pick(f, id), 'display', { width, container }) !== 'none')
  }

  it('asks the card body, which is the size container the strips are measured against', () => {
    const f = fragment(CARD)
    expect(won(pick(f, '.ctl-card-body'), 'container', WIDE)).toMatch(/^ol-card\s*\/\s*inline-size$/)
  })

  it('keeps a 14-day strip and drops 24-hour and 30-day ones in a 329px card body', () => {
    expect(shown(329)).toEqual(['#s14'])
  })

  it('draws each band only once its row fits: 304, 364, 406, 490 and 580px', () => {
    expect(shown(303)).toEqual([])
    expect(shown(364)).toEqual(['#s14', '#s24'])
    expect(shown(406)).toEqual(['#s14', '#s24', '#s31'])
    expect(shown(489)).toEqual(['#s14', '#s24', '#s31'])
    expect(shown(580)).toEqual(['#s14', '#s24', '#s31', '#s45', '#s60'])
  })

  it('draws none below 900px, however wide the card', () => {
    expect(shown(800, 899)).toEqual([])
  })
})
