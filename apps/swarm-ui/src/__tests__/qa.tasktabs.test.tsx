// QAFIX-TASKTABS (part of #1680): the agent pane's tab strip, as the first
// console visual-QA run (wf_9e9bf74834634db7bcc3, 2026-10-20) drew it at
// 390 px.
//
// QA-019: on a phone the column is the one scroller and the strip sticks in
// it, but at `top: 0` it stuck 18px below the column's top -- a sticky inset
// is measured from the scroller's CONTENT edge, inside the drawer's 18px of
// top padding (AG-31, the same correction `.drawer::before` carries) -- and
// the scrolled content (`Attempt 2 · gen 2`, `model not measured`) showed in
// the gap above the tabs.
//
// QA-020 (also QA-024, QA-089): each tab reaches 1px under the strip
// (`margin-bottom: -1px`, so its underline lies on the strip's hairline), and
// `overflow-x: auto` alone makes the other axis `auto` too, so that pixel was
// a vertical scroll range and a classic-scrollbar browser drew a scrollbar,
// arrows and all, in the middle of the row.

import { afterEach, describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const PHONE: CascadeEnv = { width: 390, height: 844 }
const WIDE: CascadeEnv = { width: 1440, height: 900, container: 700 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function tree(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

function column(extra = ''): { split: HTMLElement; edge: HTMLElement; strip: HTMLElement; tab: HTMLElement } {
  const host = tree(
    `<div class="drawer ctl-drawer ag-split${extra}">` +
      '<div class="ag-tabs-edge"><div class="c-tabs ag-split-tabs" role="tablist"><button role="tab">Attempts <em>3</em></button></div></div>' +
      '<div class="ag-split-pane"></div>' +
      '</div>',
  )
  return {
    split: host.querySelector<HTMLElement>('.ag-split')!,
    edge: host.querySelector<HTMLElement>('.ag-tabs-edge')!,
    strip: host.querySelector<HTMLElement>('.ag-split-tabs')!,
    tab: host.querySelector<HTMLElement>('[role="tab"]')!,
  }
}

/** The column's top padding, off whichever of `padding-top` and `padding` wins. */
function padTop(el: Element, env: CascadeEnv): string {
  const v = painted(el, ['padding-top', 'padding'], env)
  expect(v, 'the column declares a top padding').not.toBeNull()
  return v!.trim().split(/\s+/)[0]!
}

describe('QA-019: on a phone the pane tabs stick at the column’s top edge, with nothing showing above them', () => {
  it('cancels the column’s top padding in the sticky inset', () => {
    const { split, edge } = column()
    const pad = padTop(split, PHONE)
    // The premise: the column has a top padding, so `top: 0` is a gap.
    // MUTATION: drop the drawer's 18px top padding and this premise fails, telling the reader the inset below must change with it.
    expect(pad).toMatch(/^[1-9]\d*px$/)
    expect(painted(edge, 'position', PHONE)).toBe('sticky')
    // MUTATION: put `top: 0` back on `.ag-split:not(.on-logs) > .ag-tabs-edge` and the strip sticks 18px down, under the scrolled content (QA-019).
    expect(painted(edge, 'top', PHONE)).toBe(`-${pad}`)
  })

  it('paints the strip opaque, so what scrolls under it is hidden', () => {
    const { edge } = column()
    // MUTATION: drop `background: var(--bg)` from the sticky edge and the content shows through the tabs.
    expect(painted(edge, 'background', PHONE)).toBe('var(--bg)')
  })

  it('leaves the Logs tab’s column alone: it does not scroll, so nothing sticks', () => {
    const { edge } = column(' on-logs')
    // MUTATION: drop `:not(.on-logs)` from the sticky rule and the Logs column's tabs stick too.
    expect(painted(edge, 'position', PHONE)).not.toBe('sticky')
  })
})

describe('QA-020, QA-024, QA-089: the pane tab strip scrolls sideways only', () => {
  it('has no vertical scroll range for the tab’s 1px underline overlap', () => {
    const { strip, tab } = column()
    // The premise: a tab reaches 1px under the strip, onto its hairline.
    // MUTATION: change the tabs' `margin-bottom: -1px` and this premise fails; the overflow-y rule below is what keeps that pixel from scrolling.
    expect(painted(tab, 'margin-bottom', PHONE)).toBe('-1px')
    // MUTATION: drop `overflow-y: hidden` from `.ag-split-tabs` and the phone strip draws a vertical scrollbar mid-row (QA-020).
    expect(painted(strip, ['overflow-y', 'overflow'], PHONE)).toBe('hidden')
    // MUTATION: scope `overflow-y: hidden` to the phone block only and the wide strip keeps the same 1px vertical range.
    expect(painted(strip, ['overflow-y', 'overflow'], WIDE)).toBe('hidden')
    // MUTATION: replace `overflow-x: auto` with the `overflow: hidden` shorthand and the phone strip stops scrolling sideways (V148).
    expect(painted(strip, ['overflow-x', 'overflow'], PHONE)).toBe('auto')
  })
})
