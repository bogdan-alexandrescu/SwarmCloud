/**
 * WALKTHROUGH F (owner, 2026-10-03, live console at 1440x900): Overview.
 *
 * Measured: "Needs a look · 1 of 8 checks still reading" drew an empty dashed
 * "reading" box that looked like a broken input; the cards had uneven heights,
 * leaving a hole under "Running now"; Headroom tiles cut the profile names
 * ("claude-code-…") in mono.
 *
 * While a check is reading, the row is a skeleton of the check card it will
 * become (the canonical `Skeleton`), never the pending mark's dashed box; the
 * two-card rows stretch both cards to the row's height, so neither leaves a
 * hole; the Headroom names are whole, two lines, in sans (asked in
 * walk.long.test.tsx, item C, with the other long names).
 *
 * MUTATIONS: draw `<Mark kind="pending">` for the reading row again, or put
 * `align-items: stretch` back on `.ov-g21` (D19 reversed F's balanced rows) --
 * each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('F: a check still reading is a skeleton card, not an empty dashed box', () => {
  it('draws a skeleton of the check card while any check is reading', async () => {
    window.history.replaceState(null, '', '/')
    render(<App />)
    const lead = await waitFor(() => {
      const l = document.querySelector<HTMLElement>('#ov-needs')
      expect(l).not.toBeNull()
      return l!
    })
    // The first render: every check is still reading.
    expect(lead.querySelector('.ov-cnt')?.textContent).toMatch(/still reading/)
    expect(lead.querySelector('.ctl-mark.is-pending'), 'the dashed pending box is back').toBeNull()
    const skel = lead.querySelector('.ov-atts[aria-busy="true"]')
    expect(skel, 'no skeleton row').not.toBeNull()
    const card = skel!.querySelector('.ov-att.is-skel')!
    expect(card, 'the skeleton is not shaped as a check card').not.toBeNull()
    expect(card.querySelectorAll('.c-sk').length).toBeGreaterThanOrEqual(2)
    // Its sentence is still said, to a screen reader.
    expect(skel!.getAttribute('aria-label')).toMatch(/checks are still reading/)
    // Once the checks have run, the skeleton is gone.
    await waitFor(() => expect(lead.querySelector('.ov-atts[aria-busy="true"]')).toBeNull())
  })
})

describe('F, revised by browser QA D19 (2026-10-04): each card keeps its own height', () => {
  // Walkthrough F stretched both cards of a row; beside Headroom's three
  // stacks that drew ~600px of blank under Running now and Waiting. The owner
  // chose `align-items: start` (qa.u10b.overview.test.tsx asserts it too).
  // U12 N17/D19 (owner QA 2026-10-04): the rows of two became two column
  // stacks, so a short card is followed by the next card in its column. Each
  // column and the grid still align to the top, and no card has a height.
  it('aligns every card of a column to its top, none with a height of its own', async () => {
    window.history.replaceState(null, '', '/')
    render(<App />)
    const cols = await waitFor(() => {
      const r = [...document.querySelectorAll<HTMLElement>('.ov-g21 > .ov-col')]
      expect(r.length).toBe(2)
      return r
    })
    expect(painted(cols[0]!.parentElement!, 'align-items', WIDE), 'a short column is stretched to its neighbour').toBe('start')
    for (const col of cols) {
      expect(painted(col, 'align-items', WIDE), 'a short card is stretched').toBe('start')
      for (const card of col.querySelectorAll(':scope > .ov-card')) {
        expect(painted(card, 'height', WIDE) ?? 'auto', `${card.id} has a height of its own`).toBe('auto')
      }
    }
  })
})
