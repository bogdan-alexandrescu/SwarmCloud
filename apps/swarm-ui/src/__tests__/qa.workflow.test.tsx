/**
 * VISUAL QA Q3 (owner, 2026-10-02): the workflow page's Steps table and its
 * graph's node cards, against wide-workflows.html A and components.html A.
 *
 *   * the `why` cell ran into `runner` (its 40ch line was wider than its 15%
 *     column) and values broke mid-word (`not reporte / d`, `attempt / s`):
 *     the table now has real column widths, `why` takes the slack, heads and
 *     figures do not wrap, nothing breaks inside a word, and the table
 *     scrolls inside its card only below its minimum width;
 *   * the node cards were cramped mono: the card's words are the sans face.
 *
 * Asked of the shipped cascade (`marks.painted`), at desktop width.
 * MUTATIONS: put `overflow-wrap: anywhere` back on the cells, a width on the
 * why column, or `var(--mono)` on `.node-id` -- each goes red here.
 */
import { afterEach, describe, expect, it } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { familyOf } from './faces'

const WIDE: CascadeEnv = { width: 1440 }
const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

const COLS = ['step', 'state', 'why', 'runner', 'waited', 'ran', 'attempts', 'cost', 'tokens', 'inputs'] as const
const NUM = new Set(['waited', 'ran', 'attempts', 'cost', 'tokens'])

function table(): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML =
    '<div class="wf-card"><div class="ctl-table wf-table is-scroll"><table><thead><tr>' +
    COLS.map((c) => `<th data-col="${c}"${NUM.has(c) ? ' class="is-num"' : ''}>${c}</th>`).join('') +
    '</tr></thead><tbody><tr>' +
    COLS.map((c) => `<td data-col="${c}"${NUM.has(c) ? ' class="is-num"' : ''}>${c === 'why' ? '<span class="wf-why">Waiting on an earlier step in this workflow</span>' : 'x'}</td>`).join('') +
    '</tr></tbody></table></div></div>'
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

describe('Q3: the Steps table has real column widths', () => {
  it('scrolls inside its card only below a minimum width', () => {
    const h = table()
    expect(painted(h.querySelector('.wf-table')!, ['overflow-x', 'overflow'], WIDE)).toBe('auto')
    const min = painted(h.querySelector('table')!, 'min-width', WIDE) ?? ''
    expect(min, 'the table has no minimum width').toMatch(/^\d+(?:\.\d+)?(?:px|rem)$/)
    expect(painted(h.querySelector('table')!, 'table-layout', WIDE)).toBe('fixed')
  })

  it('gives every column but why a width, so why takes the slack', () => {
    // WALKTHROUGH A (2026-10-03): the shares overprinted at the table's real
    // width, so the widths are px sized to the measured figures, and their
    // sum leaves why the slack inside the table's minimum width
    // (walk.steps.test.tsx models where every cell's text then lands).
    const h = table()
    const widths = COLS.map((c) => [c, painted(h.querySelector(`th[data-col="${c}"]`)!, 'width', WIDE)] as const)
    expect(widths.filter(([c, w]) => c !== 'why' && w === null).map(([c]) => c)).toEqual([])
    expect(widths.find(([c]) => c === 'why')![1]).toBeNull()
    const px = widths.filter(([c]) => c !== 'why').map(([c, w]) => {
      expect(w, `${c} width is not a px width`).toMatch(/^\d+px$/)
      return parseFloat(w!)
    })
    const min = parseFloat(painted(h.querySelector('table')!, 'min-width', WIDE)!)
    // Browser QA D12 (2026-10-04): Ran and Cost grew to what they hold, and Why
    // is cut with its title (or not drawn when empty), so it keeps 60px here.
    expect(min - px.reduce((a, b) => a + b, 0), 'why has no slack at the minimum width').toBeGreaterThanOrEqual(60)
  })

  it('never breaks a word, never wraps a head or a figure, and keeps why inside its cell', () => {
    const h = table()
    for (const c of COLS) {
      const th = h.querySelector(`th[data-col="${c}"]`)!
      const td = h.querySelector(`td[data-col="${c}"]`)!
      expect(painted(th, 'white-space', WIDE), `${c} head wraps`).toBe('nowrap')
      for (const cell of [th, td]) {
        expect(painted(cell, 'overflow-wrap', WIDE) ?? 'normal', `${c} breaks inside a word`).toBe('normal')
        expect(painted(cell, 'word-break', WIDE) ?? 'normal', `${c} breaks inside a word`).toBe('normal')
      }
      if (NUM.has(c)) {
        expect(painted(td, 'white-space', WIDE), `${c} figure wraps`).toBe('nowrap')
        expect(painted(td, 'text-align', WIDE)).toBe('right')
      }
    }
    const why = h.querySelector('.wf-why')!
    expect(painted(why, 'max-width', WIDE), 'the why line is wider than its column').toBe('100%')
    expect(painted(why, 'text-overflow', WIDE)).toBe('ellipsis')
  })
})

describe('Q3: the graph node cards are the sans card of wide-workflows.html A', () => {
  it('draws the step name, state, duration and note in the sans face', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="app"><button class="node t-park"><span class="node-id"><span class="node-name">fix</span></span>' +
      '<span class="node-line"><span class="node-state">parked</span><i class="node-dur">4m</i></span>' +
      '<span class="node-note">Waiting on an earlier step</span><dl class="node-nums"><div class="node-num"><dt>ckpts</dt><dd>2</dd></div></dl></button></div>'
    document.body.appendChild(host)
    hosts.push(host)
    const words = ['.node-id', '.node-name', '.node-state', '.node-dur', '.node-note', '.node-num dt'].map((s) => host.querySelector(s)!)
    expect(words.filter((el) => familyOf(el, WIDE) === 'mono').map((el) => el.className || el.tagName)).toEqual([])
  })
})
