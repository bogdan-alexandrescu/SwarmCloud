/**
 * HEADROOM SPANS THE OVERVIEW PAIR ON A TABLET-WIDTH SCREEN.
 *
 * Headroom shares `.ov-pair` with "Waiting, and why". From 1100px the pair is
 * two tracks; below the system breakpoint (1280px) half of that is too narrow
 * for five four-column utilisation rows, which is how `mock · 15 can start`
 * rendered as `mock · 1…` (F1 of the overflow inventory). So between 1100 and
 * 1279 Headroom takes the whole row, `grid-column: 1 / -1`. The rule was
 * deleted in #432 while Overview.tsx still draws `ov-headroom`; this holds both
 * ends -- the rule exists, and it applies to the class the screen renders.
 *
 * From 1280 the two cards sit side by side (O1), so the span stops there.
 *
 * MUTATION: delete the `.ov-headroom` span and the 1200px case goes red.
 */
import { afterEach, describe, expect, it } from 'vitest'

import STYLES from '../styles.css?raw'
import OVERVIEW_SOURCE from '../Overview.tsx?raw'
import { cascade } from './cssgate'

/** The class list Overview.tsx gives the Headroom card, read from its source. */
function headroomClass(): string {
  const m = /<section className="([^"]*\bov-headroom\b[^"]*)"/.exec(OVERVIEW_SOURCE)
  expect(m, 'Overview.tsx no longer draws a section with ov-headroom').not.toBeNull()
  return m![1]!
}

function pair(): { pairEl: HTMLElement; headroom: HTMLElement } {
  // Headroom must still be a child of the pair for the span to mean anything.
  const pairAt = OVERVIEW_SOURCE.indexOf('<div className="ov-pair">')
  const headAt = OVERVIEW_SOURCE.indexOf(`<section className="${headroomClass()}"`)
  expect(pairAt, 'Overview.tsx no longer draws .ov-pair').toBeGreaterThan(-1)
  expect(headAt, 'Headroom is no longer drawn after the pair opens').toBeGreaterThan(pairAt)
  document.body.innerHTML =
    '<div class="ov-pair"><section class="ctl-card ov-waiting"></section>' +
    `<section class="${headroomClass()}"></section></div>`
  return {
    pairEl: document.querySelector<HTMLElement>('.ov-pair')!,
    headroom: document.querySelector<HTMLElement>('.ov-headroom')!,
  }
}

const span = (el: Element, width: number) => cascade(STYLES, el, 'grid-column', { width }).winner?.value ?? null
const tracks = (el: Element, width: number) => cascade(STYLES, el, 'grid-template-columns', { width }).winner?.value ?? null

afterEach(() => {
  document.body.innerHTML = ''
})

describe('the overview Headroom card on a tablet-width screen', () => {
  it('spans both tracks of the two-column pair between 1100 and 1279px', () => {
    const { pairEl, headroom } = pair()
    for (const width of [1100, 1200, 1279]) {
      expect(tracks(pairEl, width), `the pair is not two tracks at ${width}px`).toBe('minmax(0, 1fr) minmax(0, 1fr)')
      expect(span(headroom, width), `Headroom does not span the pair at ${width}px`).toBe('1 / -1')
    }
  })

  it('sits beside Waiting from 1280px, and needs no span on one track', () => {
    const { headroom } = pair()
    expect(span(headroom, 1280)).toBeNull()
    expect(span(headroom, 1440)).toBeNull()
    expect(span(headroom, 900)).toBeNull()
  })
})
