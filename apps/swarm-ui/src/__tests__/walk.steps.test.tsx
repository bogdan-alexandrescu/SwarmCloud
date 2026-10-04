/**
 * WALKTHROUGH A (owner, 2026-10-03, live console at 1440x900): the Steps table
 * on /workflows/<id>/table.
 *
 * Measured: Tokens overprinted Inputs ("656 in · 1084.2k"); Ran and Attempts
 * touched ("45m 40s2 of 3"); Runner wrapped "claude-/code"; Inputs was a
 * one-word-per-line tower; a stray "12/275 sampled" chip floated above the
 * table, outside its card.
 *
 * The table is laid out fixed with explicit column widths and Why takes the
 * slack; figures are right-aligned, nowrap and held apart by the cells'
 * padding; Runner never wraps; Inputs is a summary count whose files open in
 * a row under the step; the sampling chip is in the table's own head row.
 *
 * "No two cells' text boxes overlap" is asked of the fixed-table model in
 * `tablefit.ts` (jsdom lays nothing out), at the width the table has at 1440
 * and at its own minimum width, with the figures the owner measured.
 * MUTATIONS: drop a column's width, the cells' padding, `nowrap` on runner,
 * or put the files back in the column -- each goes red here.
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, crowded, fixedColumns, nowrap, textBox, type CellStyle, type TextBox } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 8000 }
const WF = 'wf_5e5ad3b6f7da4299a839'
/** The table's box at 1440: 1440 less the 84px spine, the 236px panel, two 32px gutters and the card's 16px padding and borders. */
const AT_1440 = 1440 - 84 - 236 - 64 - 34

/** The widest figure the owner measured in each column, and the runner name that wrapped. */
const WORST: Record<string, readonly string[]> = {
  runner: ['claude-code'],
  waited: ['1h 18m so far', 'on parents'],
  ran: ['45m 40s', '23h 59m'],
  attempts: ['2 of 3', '10 of 10'],
  cost: ['$12.3456'],
  tokens: ['656 in · 1084.2k out'],
  inputs: ['12 files'],
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

async function table(): Promise<HTMLElement> {
  window.history.replaceState(null, '', `/workflows/${WF}/table`)
  render(<App />)
  return waitFor(() => {
    const t = document.querySelector<HTMLElement>('.wf-card .wf-table')
    expect(t).not.toBeNull()
    expect(t!.querySelectorAll('tbody tr[data-step]').length).toBeGreaterThan(1)
    return t!
  }, WAIT)
}

/** Each column's cell style, read once off the first row (the cascade is the slow part). */
function stylesOf(row: Element): Map<string, CellStyle> {
  const out = new Map<string, CellStyle>()
  for (const td of row.querySelectorAll('td[data-col]')) out.set(td.getAttribute('data-col')!, cellStyle(td, WIDE))
  return out
}

function boxesOf(
  row: Element,
  styles: ReadonlyMap<string, CellStyle>,
  cols: ReturnType<typeof fixedColumns>['cols'],
  pick: (col: string, cell: Element) => string,
): TextBox[] {
  const out: TextBox[] = []
  for (const box of cols) {
    const cell = row.querySelector(`td[data-col="${box.col}"]`)
    if (cell === null) continue
    const text = pick(box.col, cell).trim()
    if (text === '') continue
    out.push(textBox(styles.get(box.col)!, box, text, WIDE))
  }
  return out
}

describe('A: the Steps table is a fixed table whose cells never overlap', () => {
  it('holds every column apart at 1440 and at its minimum width, with the measured figures', async () => {
    const wrap = await table()
    const t = wrap.querySelector('table')!
    const rows = [...t.querySelectorAll('tbody tr[data-step]')]
    const styles = stylesOf(rows[0]!)
    let visited = 0
    for (const container of [AT_1440, 0]) {
      const { width, cols } = fixedColumns(t, container, WIDE)
      // Why takes the slack, and there is slack to take.
      const why = cols.find((c) => c.col === 'why')!
      // 60px since browser QA D12 (2026-10-04): Ran and Cost grew to what they hold.
      expect(why.end - why.start, `why is ${why.end - why.start}px at ${width}`).toBeGreaterThanOrEqual(60)
      for (const row of rows) {
        // What the row draws today ...
        const drawn = boxesOf(row, styles, cols, (_c, cell) => cell.textContent ?? '')
        expect(crowded(drawn, 12), `${row.getAttribute('data-step')} at ${width}px`).toEqual([])
        // ... and the widest figure each column was measured holding.
        for (let i = 0; i < 2; i++) {
          const worst = boxesOf(row, styles, cols, (c, cell) => WORST[c]?.[i] ?? WORST[c]?.[0] ?? (c === 'why' ? '' : (cell.textContent ?? '')))
          expect(crowded(worst, 12), `${row.getAttribute('data-step')} at ${width}px`).toEqual([])
          const cut = worst.filter((b) => b.col in WORST && b.clipped).map((b) => `${b.col} "${b.text}"`)
          expect(cut, `a measured figure is cut at ${width}px`).toEqual([])
        }
        visited++
      }
    }
    expect(visited).toBe(rows.length * 2)
  })

  it('never wraps a head, a figure or the runner', async () => {
    const wrap = await table()
    const heads = [...wrap.querySelectorAll('thead th')]
    expect(heads.length).toBe(10)
    for (const th of heads) expect(nowrap(th, WIDE), `${th.getAttribute('data-col')} head wraps`).toBe(true)
    const row = wrap.querySelector('tbody tr[data-step]')!
    for (const col of ['runner', 'waited', 'ran', 'attempts', 'cost', 'tokens', 'inputs']) {
      const td = row.querySelector(`td[data-col="${col}"]`)!
      expect(nowrap(td, WIDE), `${col} wraps`).toBe(true)
    }
    for (const col of ['waited', 'ran', 'attempts', 'cost', 'tokens']) {
      expect(painted(row.querySelector(`td[data-col="${col}"]`)!, 'text-align', WIDE), col).toBe('right')
    }
  })
})

describe('A: Inputs is a count in the column and the files in the row under it', () => {
  it('draws a count, not the files, and opens them under the step', async () => {
    const wrap = await table()
    const row = wrap.querySelector('tbody tr[data-step="synthesis"]')!
    const cell = row.querySelector('td[data-col="inputs"]')!
    expect(cell.querySelectorAll('.wf-input'), 'the files are still in the column').toHaveLength(0)
    const toggle = cell.querySelector<HTMLButtonElement>('button[aria-expanded]')
    expect(toggle, 'no summary control').not.toBeNull()
    expect(toggle!.textContent).toMatch(/^5 files/)
    expect(toggle!.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle!)
    expect(toggle!.getAttribute('aria-expanded')).toBe('true')
    const next = row.nextElementSibling!
    expect(next.classList.contains('wf-xrow'), 'the files did not open under the step').toBe(true)
    expect(next.querySelectorAll('.wf-input')).toHaveLength(5)
    expect(next.querySelector('td')!.getAttribute('colspan')).toBe('10')
    expect(toggle!.getAttribute('aria-controls')).toBe(next.querySelector('[id]')!.id)
  })

  it('says none in the column for a step that reads nothing, with no control', async () => {
    const wrap = await table()
    const cell = wrap.querySelector('tbody tr[data-step="fencing"] td[data-col="inputs"]')!
    expect(cell.textContent).toBe('none')
    expect(cell.querySelector('button')).toBeNull()
  })
})

describe('A: the sampling chip is in the table head row, inside the card', () => {
  it('draws no empty chrome strip above the card, and any sampling chip is in the table head', async () => {
    const wrap = await table()
    // The strip above the board held only the chip; empty, it was the band
    // between the tabs and the card (item C).
    for (const strip of document.querySelectorAll('.wf-chrome')) {
      expect(strip.textContent!.trim(), 'an empty chrome strip is drawn').not.toBe('')
      expect(strip.textContent, 'the sampling chip is outside the card').not.toMatch(/sampled/)
    }
    for (const chip of document.querySelectorAll('.ctl-mark.is-partial')) {
      expect(chip.closest('.wf-table-head'), 'the chip is not in the table head row').not.toBeNull()
    }
    expect(wrap.closest('.wf-card')).not.toBeNull()
  })

  it('heads the Steps table under the Graph with its own head row, directly above the table', async () => {
    window.history.replaceState(null, '', `/workflows/${WF}`)
    render(<App />)
    const head = await waitFor(() => {
      const h = document.querySelector<HTMLElement>('.wf-card .wfp-steps .wf-table-head')
      expect(h).not.toBeNull()
      return h!
    }, WAIT)
    expect(head.querySelector('h3')?.textContent).toBe('Steps')
    // The table sits in the box its stacked form asks about (D12), and that
    // box is the head row's next sibling: nothing is drawn between them.
    const box = head.nextElementSibling
    expect(box?.classList.contains('wf-table-box')).toBe(true)
    expect(box?.firstElementChild?.classList.contains('wf-table')).toBe(true)
  })
})
