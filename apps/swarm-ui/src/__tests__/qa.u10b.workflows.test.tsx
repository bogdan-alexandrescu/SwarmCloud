/**
 * BROWSER QA U10b (owner, 2026-10-04; live console at 1440x900): the
 * Workflows list.
 *
 *   D8   "9/10" in Steps done was clipped (x897, its cell ends at 880), the
 *        state read "succeeded…", Duration "4h 12m so …", and long titles and
 *        shapes were cut with no tooltip. State, Steps done and Duration are
 *        sized to the longest thing they hold and never cut; the name and the
 *        shape are cut with their whole text as the title.
 *
 * The list is the fixed-table model of `tablefit.ts` (jsdom lays nothing
 * out), at the width the list has at 1440: 1440 less the 84px spine, the
 * 236px panel and two 32px gutters.
 *
 * MUTATIONS: put State, Steps done or Duration back to 9%, the ellipsis back
 * on them, or drop a title -- each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, fixedColumns, nowrap, textPx } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 8000 }
const LIST_AT_1440 = 1440 - 84 - 236 - 64
/** The mark beside a state word: 12px glyph and its 6px gap. */
const MARK = 18

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

async function list(): Promise<HTMLTableElement> {
  window.history.replaceState(null, '', '/workflows')
  render(<App />)
  return waitFor(() => {
    const t = document.querySelector<HTMLTableElement>('.wfl .wfl-table')
    expect(t).not.toBeNull()
    expect(t!.querySelectorAll('tbody tr.wfl-row:not(.is-skel)').length).toBeGreaterThan(0)
    return t!
  }, WAIT)
}

describe('D8: the Workflows list never cuts a state, a count or a duration', () => {
  it('sizes State, Steps done and Duration to the longest value each holds', async () => {
    const t = await list()
    const { cols } = fixedColumns(t, LIST_AT_1440, WIDE)
    const row = t.querySelector('tbody tr.wfl-row')!
    const width = (col: string) => {
      const box = cols.find((c) => c.col === col)!
      const st = cellStyle(row.querySelector(`td[data-col="${col}"]`)!, WIDE)
      return box.end - box.start - st.pl - st.pr
    }
    const state = row.querySelector('td[data-col="state"]')!
    const done = row.querySelector('td[data-col="done"] small')!
    const dur = row.querySelector('td[data-col="duration"]')!
    expect(width('state'), 'state').toBeGreaterThanOrEqual(MARK + textPx('dead-lettered', state, WIDE))
    // The bar keeps 24px beside the count.
    expect(width('done'), 'steps done').toBeGreaterThanOrEqual(24 + 6 + textPx('10/10', done, WIDE))
    expect(width('duration'), 'duration').toBeGreaterThanOrEqual(textPx('23h 59m so far', dur, WIDE))
    // The name keeps the most room of any column.
    const name = width('workflow')
    for (const c of cols) if (c.col !== 'workflow') expect(name, c.col).toBeGreaterThanOrEqual(c.end - c.start - 20)
  })

  it('never cuts those three with an ellipsis, and never wraps them', async () => {
    const t = await list()
    const row = t.querySelector('tbody tr.wfl-row')!
    for (const col of ['state', 'done', 'duration']) {
      const td = row.querySelector(`td[data-col="${col}"]`)!
      expect(painted(td, 'text-overflow', WIDE) ?? 'clip', col).toBe('clip')
      expect(nowrap(td, WIDE), col).toBe(true)
    }
  })

  it('gives the name and the shape their whole text as a title', async () => {
    const t = await list()
    const rows = [...t.querySelectorAll('tbody tr.wfl-row')]
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      const a = row.querySelector<HTMLAnchorElement>('td[data-col="workflow"] > a')!
      expect(a.getAttribute('title'), 'the name has no title').toBe(a.textContent)
      const shape = row.querySelector<HTMLElement>('td[data-col="shape"]')!
      expect(shape.getAttribute('title'), 'the shape has no title').toBeTruthy()
      expect(painted(shape, 'text-overflow', WIDE)).toBe('ellipsis')
    }
  })
})
