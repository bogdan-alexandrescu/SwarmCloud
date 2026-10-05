/**
 * ITEM 1 OF LANE U9 (owner, 2026-10-03, run_d13a2f1b7e1e4eafabfb at 1440):
 * the Steps table WITH THE STEP CARD OPEN beside it.
 *
 * Measured: the table narrowed and the state pill printed inside the step
 * name ('impl-plan-sc[running]hema-and-overlaps', 'impl-forge-w[parked]rite-
 * back'), and Waited / Ran / Attempts overprinted ('on parents' over 'not
 * started' over '0 of 3').
 *
 * Now a long step name WRAPS inside its own cell (an id is never cut: a cut
 * id cannot be pasted anywhere), the pill sits in the State cell only, and
 * below the table's minimum width the table scrolls inside its card rather
 * than squeezing its columns into one another.
 *
 * Asked of U8's fixed-table model (`tablefit.ts`; jsdom lays nothing out) at
 * the width the table has with the card docked beside it at 1440, and with
 * ten long step names of the shape the owner measured.
 * MUTATIONS: put `white-space: nowrap` back on the step cell or its button,
 * take `overflow-wrap: anywhere` off the name, or take `overflow-x: auto` off
 * the table's wrapper -- each goes red here.
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, crowded, fixedColumns, lengthPx, nowrap, textBox, type CellStyle, type TextBox } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const WF = 'wf_5e5ad3b6f7da4299a839'
/** The table's box at 1440 with the side panel open (walk.steps.test.tsx). */
const AT_1440 = 1440 - 84 - 236 - 64 - 34

/** Ten long step names, the shape a planned issue run gives its steps. */
const LONG = [
  'impl-plan-schema-and-overlaps',
  'impl-forge-write-back',
  'impl-run-page-leads-with-the-issue',
  'impl-steps-table-inspector-width',
  'impl-agent-header-actions-row',
  'impl-window-over-scroll-contained',
  'impl-links-never-break-mid-name',
  'impl-structured-plan-with-prompts',
  'impl-preview-stored-at-submission',
  'review-every-implementation-step',
] as const
/** What a running and a parked step's cells hold (the owner's frame). */
const WORST: Record<string, string> = {
  waited: 'on parents',
  ran: 'not started',
  attempts: '0 of 3',
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

/** The table's container width with the step card docked beside it: the split's grid less the panel and the gap. */
function besideCard(split: Element): number {
  const cols = painted(split, 'grid-template-columns', WIDE) ?? ''
  const panel = /minmax\(\s*[\d.]+px\s*,\s*([\d.]+)px\s*\)\s*$/.exec(cols)
  expect(panel, `the split is not a column beside a panel: ${cols}`).not.toBeNull()
  const gap = lengthPx(painted(split, ['column-gap', 'gap'], WIDE), 0) ?? 0
  return AT_1440 - Number(panel![1]) - gap
}

/** Ten rows with long names, cloned from a drawn row so every one matches the shipped cascade. */
function longRows(t: HTMLTableElement): HTMLTableRowElement[] {
  const body = t.querySelector('tbody')!
  const proto = body.querySelector<HTMLTableRowElement>('tr[data-step]')!
  return LONG.map((name, i) => {
    const row = proto.cloneNode(true) as HTMLTableRowElement
    row.setAttribute('data-step', name)
    row.querySelector('.wf-pick-id')!.textContent = name
    const state = row.querySelector('td[data-col="state"] .wf-cell-state')
    if (state !== null) {
      // The word the owner saw printed inside the name.
      const word = state.querySelector('.sk-st-w, .sk-vh') ?? state
      word.textContent = i % 2 === 0 ? 'running' : 'parked'
    }
    for (const [col, text] of Object.entries(WORST)) {
      const td = row.querySelector(`td[data-col="${col}"]`)
      if (td !== null) td.textContent = text
    }
    body.appendChild(row)
    return row
  })
}

/** Whether the cascade lets `el`'s text break between any two characters. */
function breaksAnywhere(el: Element): boolean {
  for (let n: Element | null = el; n !== null; n = n.parentElement) {
    const wrap = painted(n, 'overflow-wrap', WIDE) ?? painted(n, 'word-wrap', WIDE)
    const word = painted(n, 'word-break', WIDE)
    if (wrap !== null || word !== null) return wrap === 'anywhere' || wrap === 'break-word' || word === 'break-all'
  }
  return false
}

/** Whether the step cell's name wraps inside its cell, or ellipsizes on its own. */
function nameFits(cell: Element): boolean {
  const name = cell.querySelector('.wf-pick-id')!
  const pick = cell.querySelector('.wf-pick')!
  const wraps = !nowrap(name, WIDE) && !nowrap(pick, WIDE) && breaksAnywhere(name)
  const ellipsizes =
    painted(name, 'text-overflow', WIDE) === 'ellipsis' &&
    /^(hidden|clip)/.test(painted(name, ['overflow-x', 'overflow'], WIDE) ?? '') &&
    painted(pick, 'max-width', WIDE) === '100%'
  return wraps || ellipsizes
}

/**
 * Each cell's text box. The step cell's name WRAPS when the cascade lets it --
 * not nowrap, and breakable anywhere -- so its box is at most its cell's
 * content box; otherwise it is the one-line box `textBox` computes, and a name
 * wider than its cell runs into the State column, which is the overprint.
 */
function boxesOf(row: Element, styles: ReadonlyMap<string, CellStyle>, cols: ReturnType<typeof fixedColumns>['cols']): TextBox[] {
  const out: TextBox[] = []
  for (const box of cols) {
    const cell = row.querySelector(`td[data-col="${box.col}"]`)
    if (cell === null || box.col === 'why') continue
    const text = (cell.textContent ?? '').trim()
    if (text === '') continue
    if (box.col !== 'step') {
      out.push(textBox(styles.get(box.col)!, box, text, WIDE))
      continue
    }
    // The cell's own clip does not count: the name is a button, an atomic
    // inline box, so the cell's ellipsis never applies to it and a clipped
    // name is cut mid-id ('impl-plan-sc') with nothing saying so.
    const b = textBox({ ...styles.get('step')!, clips: false }, box, text, WIDE)
    if (nameFits(cell)) b.end = Math.min(b.end, box.end - styles.get('step')!.pr)
    out.push(b)
  }
  return out
}

async function tableWithCard(): Promise<{ split: HTMLElement; wrap: HTMLElement; t: HTMLTableElement }> {
  window.history.replaceState(null, '', `/workflows/${WF}/table`)
  render(<App />)
  const wrap = await waitFor(() => {
    const w = document.querySelector<HTMLElement>('.wf-card .wf-table')
    expect(w?.querySelectorAll('tbody tr[data-step]').length ?? 0).toBeGreaterThan(1)
    return w!
  })
  fireEvent.click(wrap.querySelector<HTMLButtonElement>('tbody tr[data-step] .wf-pick')!)
  const split = await waitFor(() => {
    const s = document.querySelector<HTMLElement>('.wf-split.has-panel')
    expect(s?.querySelector(':scope > .wf-panel'), 'the step card did not open beside the table').toBeTruthy()
    return s!
  })
  return { split, wrap, t: wrap.querySelector('table')! }
}

describe('item 1: the Steps table with the step card open', () => {
  // Since browser QA D12 (2026-10-04) the rows STACK below the minimum width,
  // by a container query on the table's box (qa.u10b.workflows.test.tsx asks
  // it with a container width). This case models no container, so it asks
  // the fallback a browser without container queries draws: a scroll inside
  // the card, never a column pushing the card.
  it('scrolls inside its card below its minimum width, and its column never pushes the card', async () => {
    const { split, wrap, t } = await tableWithCard()
    const container = besideCard(split)
    const { width } = fixedColumns(t, container, WIDE)
    expect(container, 'the card left the table its minimum width, so this case asks nothing').toBeLessThan(width)
    expect(painted(wrap, ['overflow-x', 'overflow'], WIDE), 'the table is wider than its column and does not scroll').toBe('auto')
    expect(painted(split.querySelector(':scope > .wf-split-main')!, 'min-width', WIDE)).toBe('0')
  })

  it('wraps ten long step names inside their own cells, apart from the state pill and the figures', async () => {
    const { split, t } = await tableWithCard()
    const rows = longRows(t)
    const styles = new Map<string, CellStyle>()
    for (const td of rows[0]!.querySelectorAll('td[data-col]')) styles.set(td.getAttribute('data-col')!, cellStyle(td, WIDE))
    let visited = 0
    for (const container of [besideCard(split), AT_1440]) {
      const { width, cols } = fixedColumns(t, container, WIDE)
      const step = cols.find((c) => c.col === 'step')!
      for (const row of rows) {
        const boxes = boxesOf(row, styles, cols)
        const name = boxes.find((b) => b.col === 'step')!
        expect(name.end, `"${name.text}" runs out of its cell at ${width}px`).toBeLessThanOrEqual(step.end)
        expect(crowded(boxes, 12), `${row.getAttribute('data-step')} at ${width}px`).toEqual([])
        visited++
      }
    }
    expect(visited).toBe(LONG.length * 2)
  })

  // Browser QA D12 (2026-10-04) reversed item 1's wrap: ten names wrapped to
  // two and three lines at their hyphens. A name is one line now, cut, with
  // the whole id as its title (and in the step card it opens).
  it('keeps the whole name in its own cell, one line, and draws the pill only in the State cell', async () => {
    const { t } = await tableWithCard()
    const rows = longRows(t)
    for (const row of rows) {
      const cell = row.querySelector('td[data-col="step"]')!
      expect(cell.querySelector('[data-mark]'), 'a state mark is drawn in the step cell').toBeNull()
      expect(cell.querySelector('.wf-pick-id')!.textContent).toBe(row.getAttribute('data-step'))
      expect(row.querySelector('td[data-col="state"] [data-mark]'), 'no state mark in the State cell').not.toBeNull()
    }
    const cell = rows[0]!.querySelector('td[data-col="step"]')!
    expect(nameFits(cell), 'a long name neither wraps nor ellipsizes in its own cell').toBe(true)
    const id = cell.querySelector('.wf-pick-id')!
    expect(painted(id, 'text-overflow', WIDE)).toBe('ellipsis')
    // The clone keeps its prototype's title; qa.u10b.workflows.test.tsx holds it to the id.
    expect(id.getAttribute('title')).toBeTruthy()
  })
})
