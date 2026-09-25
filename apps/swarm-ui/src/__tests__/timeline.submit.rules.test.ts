// EPIC #84's DECISIONS, AS RULES THE CASCADE HAS TO PICK.
//
// The Timeline and Submit boxes the owner decided on 2026-09-25 (TS-3, TS-9,
// TS-14, TS-15, TS-18, TS-20) each have a stylesheet half, and this file holds
// it. Asked of `cascade` (cssgate.ts) rather than `getComputedStyle`, for the
// reason shell.test.tsx's QA block gives: jsdom orders rules by source position
// alone and applies no `@media` block, and two of these are exactly a phone
// rule and a wide rule that must disagree.
//
// A FILE OF ITS OWN rather than more cases in shell.test.tsx's QA block,
// because several lanes are adding to that block at once and every one of them
// would be an adjacent insertion into the same `describe`.
//
// Each case was committed RED against the sheet it describes before the rule
// changed, and names the mutation that turns it red again.
//
// WHAT NONE OF THIS CAN SEE: a pixel. Whether 28px of fade is enough of a cue,
// and whether sixteen pixels of heading still leads a section, are only
// visible in a browser at 1440 and 390.

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it } from 'vitest'

import { cascade, flatRules, type CascadeEnv } from './cssgate'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

/** A fixture to ask the cascade about. Attached, so nothing about it is special. */
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

/** The value the cascade chooses; an unevaluable selector fails by name. */
function won(el: Element, prop: string | readonly string[], env: CascadeEnv, states: readonly string[] = []): string | null {
  const r = cascade(STYLES, el, prop, { ...env, states })
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

/** Every rule whose selector names this class, anywhere in it. */
const naming = (needle: string) => flatRules(STYLES).filter((r) => r.selector.includes(needle))

describe('epic #84: the Timeline and Submit rules', () => {
  it('TS-3: a phone shows every third hour and a wide screen its own thinning', () => {
    // MUTATION: drop either visibility rule, or the @media around the phone pair.
    const f = fragment(
      '<div class="chart"><div class="col"><span class="col-label is-wide-only">01 AM</span></div>' +
        '<div class="col"><span class="col-label is-phone-only">03 AM</span></div>' +
        '<div class="col"><span class="col-label">06 AM</span></div></div>',
    )
    const wide = pick(f, '.is-wide-only')
    const phone = pick(f, '.is-phone-only')
    const both = pick(f, '.col-label:not(.is-wide-only):not(.is-phone-only)')
    expect(won(wide, 'visibility', PHONE), 'a wide-only label prints at phone width').toBe('hidden')
    expect(won(phone, 'visibility', PHONE), 'a phone label is hidden at phone width').toBe('visible')
    expect(won(wide, 'visibility', WIDE)).not.toBe('hidden')
    expect(won(phone, 'visibility', WIDE), 'a phone-only label crowds the wide axis').toBe('hidden')
    // A label on both axes is never hidden.
    expect(won(both, 'visibility', PHONE)).toBeNull()
    expect(won(both, 'visibility', WIDE)).toBeNull()
  })

  it('TS-3: fades the chart\'s left edge only while older buckets are off-screen', () => {
    // MUTATION: put the mask on `.chart` itself, or delete it.
    const f = fragment('<div class="chart" id="a"></div><div class="chart has-older" id="b"></div>')
    expect(won(pick(f, '#a'), ['mask-image', 'mask'], WIDE), 'a chart that fits is faded').toBeNull()
    const mask = won(pick(f, '#b'), ['mask-image', 'mask'], WIDE) ?? ''
    expect(mask, 'nothing cues the buckets off the left edge').toMatch(/^linear-gradient\(to right, transparent/)
    expect(won(pick(f, '#b'), '-webkit-mask-image', WIDE)).toBe(mask)
  })

  it('TS-9: a picked column is backed by the hueless selection step, and a focused one is ringed', () => {
    // MUTATION: a hue on the pick, or no ring on the column.
    const f = fragment('<div class="chart"><div class="col is-picked"></div><div class="col"></div></div>')
    expect(won(pick(f, '.col.is-picked'), ['background', 'background-color'], WIDE)).toBe('var(--surface-2)')
    const ring = won(pick(f, '.col:not(.is-picked)'), ['outline', 'outline-style', 'outline-width'], WIDE, ['focus-visible'])
    expect(ring ?? '', 'a column Tab reaches draws no focus ring').toContain('var(--info)')
    // The readout's counts are ink, so they read as the figures they are.
    const legend = fragment('<p class="chart-legend"><b class="cl-n">2</b></p>')
    expect(won(pick(legend, '.cl-n'), 'color', WIDE)).toBe('var(--text)')
  })

  it('TS-14: nothing on the dispatch control draws the dashed "unavailable" card any more', () => {
    // A dashed edge is reserved for a failed or partial read (§8.6).
    // MUTATION: restore `.dsp-option.is-off` or `.dsp-off-why`.
    expect(naming('.dsp-option.is-off').map((r) => `${r.selector} :${r.line}`)).toEqual([])
    expect(naming('.dsp-off-why').map((r) => `${r.selector} :${r.line}`)).toEqual([])
  })

  it('TS-20: the consequence box has no headline of its own', () => {
    // MUTATION: restore `.dsp-consequence-head`.
    expect(naming('.dsp-consequence-head').map((r) => `${r.selector} :${r.line}`)).toEqual([])
  })

  it('TS-15: a required field left empty takes the warn edge, and the line under it is warn ink', () => {
    // MUTATION: drop `.sbf-field [aria-invalid='true']` from the warn-edge rule.
    const f = fragment(
      '<div class="app"><div class="sbf-field"><textarea aria-invalid="true"></textarea>' +
        '<p class="sbf-miss">x</p></div><div class="sbf-field"><textarea></textarea></div></div>',
    )
    const [bad, fine] = [...f.querySelectorAll('textarea')]
    expect(won(bad!, ['border-color', 'border'], WIDE)).toBe('var(--warn)')
    expect(won(fine!, ['border-color', 'border'], WIDE) ?? '').not.toContain('--warn')
    expect(won(pick(f, '.sbf-miss'), 'color', WIDE)).toBe('var(--warn-ink)')
    // The send panel's "prompt missing" is a `.sbf-mini` button in `.sbf-bad`
    // ink; `.sbf-mini`'s own grey is later in the sheet and must not win.
    const panel = fragment('<div class="sbf-send"><button class="sbf-mini sbf-bad">prompt missing</button></div>')
    expect(won(pick(panel, 'button'), 'color', WIDE)).toBe('var(--warn-ink)')
  })

  it('TS-18: every section, panel and step heading is --t-lead; --t-title is the h1 and two named exceptions', () => {
    // MUTATION: any of these back at --t-title, or an exception moved down.
    const lead: Array<[string, string]> = [
      ['<section class="section"><h2>x</h2></section>', 'h2'],
      ['<section class="section"><div class="ctl-toolbar"><h2>x</h2></div></section>', 'h2'],
      ['<h2 class="sbf-move-h">x</h2>', 'h2'],
      ['<div class="state"><h3>x</h3></div>', 'h3'],
      ['<div class="ctl-empty"><h3>x</h3></div>', 'h3'],
      ['<div class="art-head"><h3>x</h3></div>', 'h3'],
      ['<div class="ckb-head"><h3>x</h3></div>', 'h3'],
    ]
    for (const [html, sel] of lead) {
      const el = pick(fragment(html), sel)
      expect(won(el, ['font-size', 'font'], WIDE), html).toBe('var(--t-lead)')
    }
    // The four that declare their leading declare the lead's.
    for (const html of [lead[0]![0], lead[1]![0], lead[2]![0], lead[3]![0]]) {
      const el = pick(fragment(html), 'h2, h3')
      expect(won(el, ['line-height', 'font'], WIDE), html).toBe('var(--lh-lead)')
    }
    const title: Array<[string, string]> = [
      ['<div class="head"><h1>x</h1></div>', 'h1'],
      ['<div class="ctl-page-head"><h1>x</h1></div>', 'h1'],
      // Overview's attention lead, at page rank on purpose (layout.overview.test.tsx).
      ['<p class="ov-lead-title">x</p>', 'p'],
      // A rendered document's own h1 follows the document's ladder.
      ['<div class="art-md"><h1 class="art-h" data-level="1">x</h1></div>', 'h1'],
    ]
    for (const [html, sel] of title) {
      const el = pick(fragment(html), sel)
      expect(won(el, ['font-size', 'font'], WIDE), html).toBe('var(--t-title)')
    }
  })
})
