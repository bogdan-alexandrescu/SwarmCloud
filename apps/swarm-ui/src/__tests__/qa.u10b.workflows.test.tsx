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
 *   D12  The issue-run step table: names wrapped to 2-3 lines with hyphen
 *        breaks while an empty Why took ~110px; Ran and Cost held 94-101px of
 *        text in 64px cells; with the step card open everything after Waited
 *        scrolled sideways; on a phone the table was ~300px in nested borders.
 *        A name is one line, cut, whole in its title; an empty Why is not
 *        drawn; Ran and Cost fit what they hold; below the table's minimum
 *        width the rows stack.
 *
 *   D30  The step card's "same step" row: the ▶ wrapped onto a line of its own
 *        because the shape sat between the position and the arrow. ◀ position
 *        ▶ are one unit that never wraps; the shape follows, cut, with its title.
 *
 * MUTATIONS: put State, Steps done or Duration back to 9%, the ellipsis back
 * on them, drop a title, let a step name wrap, draw Why when no row has one,
 * put Ran back to 64px, or drop the stacked container rule -- each turns a
 * case red.
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import { tableColumns } from '../WorkflowViews'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { cellStyle, fixedColumns, nowrap, textPx } from './tablefit'

const WIDE: CascadeEnv = { width: 1440 }

const hosts: HTMLElement[] = []

afterEach(() => {
  window.history.replaceState(null, '', '/')
  for (const h of hosts.splice(0)) h.remove()
})

async function list(): Promise<HTMLTableElement> {
  window.history.replaceState(null, '', '/workflows')
  render(<App />)
  return waitFor(() => {
    const t = document.querySelector<HTMLTableElement>('.wfg .wfg-table')
    expect(t).not.toBeNull()
    expect(t!.querySelectorAll('tbody tr.wfg-row:not(.is-skel)').length).toBeGreaterThan(0)
    return t!
  })
}

// THE REDESIGN (frame graph-workflows-rest): the list is an auto-layout
// table of one-line rows -- State, Workflow, Steps, Needs you, Spent, Owner,
// Changed. Steps done is the Steps cell's `done/total`; Duration and Shape
// moved to the inspector's summary, which a click on the row opens.
describe('D8: the Workflows list never cuts a state, a count or a duration', () => {
  it('sizes State, Steps and Changed to what they hold, and gives the name the rest', async () => {
    const t = await list()
    const row = t.querySelector('tbody tr.wfg-row')!
    // The old fixed-table column widths went with the redesign: the table
    // is `table-layout: auto`, so a column with no width is as wide as its
    // longest value, and only the name takes the room left over.
    expect(painted(t, 'table-layout', WIDE)).toBe('auto')
    for (const col of ['state', 'steps', 'changed']) {
      expect(painted(row.querySelector(`td[data-col="${col}"]`)!, 'width', WIDE), col).toBeNull()
    }
    expect(painted(row.querySelector('td[data-col="workflow"]')!, 'width', WIDE)).toBe('100%')
    expect(row.querySelector('td[data-col="steps"] small')?.textContent).toMatch(/^\d+\/\d+$|steps?/)
  })

  it('never cuts those three with an ellipsis, and never wraps them', async () => {
    const t = await list()
    const row = t.querySelector('tbody tr.wfg-row')!
    for (const col of ['state', 'steps', 'changed']) {
      const td = row.querySelector(`td[data-col="${col}"]`)!
      expect(painted(td, 'text-overflow', WIDE) ?? 'clip', col).toBe('clip')
      expect(nowrap(td, WIDE), col).toBe(true)
    }
  })

  it('gives the name its whole text as a title, and the shape in the summary its words', async () => {
    const t = await list()
    const rows = [...t.querySelectorAll<HTMLElement>('tbody tr.wfg-row')]
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      const a = row.querySelector<HTMLAnchorElement>('td[data-col="workflow"] > a')!
      // The id joins the name in the title when the name is not the id.
      expect([a.textContent, `${a.textContent} · ${row.getAttribute('data-workflow')}`], 'the name has no title').toContain(a.getAttribute('title'))
      expect(painted(row.querySelector('td[data-col="workflow"]')!, 'text-overflow', WIDE)).toBe('ellipsis')
    }
    fireEvent.click(rows[0]!.querySelector('[data-col="state"]')!)
    const aside = document.querySelector<HTMLElement>('aside[aria-label="Workflow inspector"]')!
    const dt = [...aside.querySelectorAll('.wfg-sum-dl > dt')].find((d) => d.textContent === 'Shape')!
    const shape = dt.nextElementSibling as HTMLElement
    expect(shape.getAttribute('title'), 'the shape has no title').toBeTruthy()
  })
})

const WF = 'wf_5e5ad3b6f7da4299a839'

async function steps(): Promise<HTMLElement> {
  window.history.replaceState(null, '', `/workflows/${WF}/table`)
  render(<App />)
  return waitFor(() => {
    const t = document.querySelector<HTMLElement>('.wf-card .wf-table')
    expect(t?.querySelectorAll('tbody tr[data-step]').length ?? 0).toBeGreaterThan(1)
    return t!
  })
}

describe('D12: the step table of an issue run', () => {
  it('draws each step name on one line, cut, with the whole id as its title', async () => {
    const wrap = await steps()
    const rows = [...wrap.querySelectorAll('tbody tr[data-step]')]
    let cut = 0
    for (const row of rows) {
      const id = row.querySelector<HTMLElement>('td[data-col="step"] .wf-pick-id')!
      expect(id.getAttribute('title')).toBe(row.getAttribute('data-step'))
      // THE SELECTED STEP'S NAME READS WHOLE (VQ-WORKFLOWS re-review 2,
      // #2391): the page's arrival pick is pressed, and that one name wraps
      // so the name the step card is about is never cut
      // (vq.workflows.test.tsx asks it at the capped column). Every other
      // row keeps D12's one line, cut.
      if (row.querySelector('td[data-col="step"] > .wf-pick')!.getAttribute('aria-pressed') === 'true') {
        expect(nowrap(id, WIDE), 'the selected name is cut').toBe(false)
        continue
      }
      expect(nowrap(id, WIDE), 'the name wraps').toBe(true)
      expect(painted(id, 'text-overflow', WIDE)).toBe('ellipsis')
      expect(painted(id, 'overflow', WIDE)).toBe('hidden')
      cut++
    }
    expect(rows.length).toBeGreaterThan(1)
    expect(cut, 'no unselected row was asked').toBeGreaterThanOrEqual(rows.length - 1)
  })

  it('does not draw Why when no step has one, and the name takes its room', () => {
    const none = tableColumns([{ why: null }, { why: null }] as never).map((c) => c.col)
    expect(none).not.toContain('why')
    expect(none[0]).toBe('step')
    const some = tableColumns([{ why: null }, { why: { text: 'parked', full: 'parked', warn: false } }] as never).map((c) => c.col)
    expect(some).toContain('why')
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="wf-card"><div class="ctl-table wf-table is-scroll no-why"><table><thead><tr><th data-col="step">Step</th></tr></thead></table></div></div>'
    document.body.appendChild(host)
    expect(painted(host.querySelector('th[data-col="step"]')!, 'width', WIDE)).toBe('auto')
    host.remove()
  })

  it('sizes Ran and Cost to what a running step holds', async () => {
    const wrap = await steps()
    const t = wrap.querySelector('table')!
    const { cols } = fixedColumns(t, 0, WIDE)
    const row = t.querySelector('tbody tr[data-step]')!
    for (const [col, text] of [['ran', '23h 59m so far'], ['cost', 'not reported'], ['waited', '1h 18m so far']] as const) {
      const box = cols.find((c) => c.col === col)!
      const td = row.querySelector(`td[data-col="${col}"]`)!
      const st = cellStyle(td, WIDE)
      expect(box.end - box.start - st.pl - st.pr, col).toBeGreaterThanOrEqual(textPx(text, td, WIDE))
    }
  })

  it('stacks its rows below its minimum width, with no second border, and keeps the table at 1100', async () => {
    const wrap = await steps()
    const row = wrap.querySelector('tbody tr[data-step]')!
    // A container query matches only DESCENDANTS of its container, and the
    // resolver below applies `env.container` to any element. So the rules it
    // finds for the table's own box (its border) are real in a browser only if
    // the container is an ancestor of that box, never the box itself.
    expect(painted(wrap, ['container', 'container-name', 'container-type'], WIDE), 'the table is its own container').toBeNull()
    const box = wrap.parentElement!
    expect(box.closest('.wf-card'), 'the container box is not inside the card').not.toBeNull()
    expect(painted(box, 'container', WIDE)).toMatch(/^wf-steps\s*\/\s*inline-size$/)
    for (const width of [1440, 390]) {
      const narrow: CascadeEnv = { width, container: 640 }
      expect(painted(row, 'display', narrow), `at ${width}`).toBe('flex')
      expect(painted(wrap.querySelector('thead')!, 'display', narrow)).toBe('none')
      expect(painted(wrap, 'border', narrow)).toBe('0')
      const ran = row.querySelector('td[data-col="ran"]')!
      expect(painted(ran, 'content', narrow, 'before')).toMatch(/attr\(data-label\)/)
      expect(ran.getAttribute('data-label')).toBe('Ran')
    }
    expect(painted(row, 'display', { width: 1440, container: 1100 }) ?? 'table-row').not.toBe('flex')
  })
})

describe('D30: the same-step scrubber keeps its arrows with the position', () => {
  it('holds ◀ position ▶ in one unit that never wraps, and cuts the shape after it', async () => {
    const wrap = await steps()
    fireEvent.click(wrap.querySelector<HTMLButtonElement>('tbody tr[data-step] .wf-pick')!)
    const row = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('[data-scrub="workflow"]')
      expect(r).not.toBeNull()
      return r!
    })
    // THE FIXTURE'S WORKFLOWS ARE EACH OF THEIR OWN SHAPE, so this row has
    // nothing to page: since QA1-FIX-29 (overview-agents-67) it is one
    // sentence, with no arrows, the shape in its title.
    expect(row.querySelector(':scope > .wf-scrub-nav')).toBeNull()
    expect(row.querySelector(':scope > .wf-scrub-pos')?.getAttribute('title')).toMatch(/^This workflow’s shape: .* Only workflows of this shape are compared.$/)
    // With more than one, the unit is drawn by the sheet as before: built
    // here, since no fixture workflow shares a shape (workflow.views.test.tsx
    // U5 pages two of one shape and holds the DOM order).
    const nav = build('div.wf-scrub > span.wf-scrub-nav', hosts)
    const shape = build('div.wf-scrub > span.wf-scrub-shape', hosts)
    expect(painted(nav, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(nav, 'flex', WIDE)).toBe('none')
    expect(painted(shape, 'text-overflow', WIDE)).toBe('ellipsis')
  })
})

describe('D38: the workflow Timeline thins its labels at a narrow track, whatever the viewport', () => {
  it('prints every other label when its own track is narrow, as a phone does', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="wf-timeline"><span class="wf-tl-corner"></span><div class="wf-tl-scale">' +
      ['0', '+1h', '+2h', '+3h', '+4h', '+5h'].map((l) => `<span class="wf-tl-tick"><span class="wf-tl-tick-label">${l}</span></span>`).join('') +
      '</div></div>'
    document.body.appendChild(host)
    const labels = [...host.querySelectorAll('.wf-tl-tick-label')]
    const shown = (env: CascadeEnv) => labels.filter((l) => (painted(l, 'visibility', env) ?? 'visible') !== 'hidden').map((l) => l.textContent)
    // The step card open at 1440: the track is ~290px wide.
    expect(shown({ width: 1440, container: 290 })).toEqual(['0', '+1h', '+3h', '+5h'])
    // A wide track prints them all.
    expect(shown({ width: 1440, container: 800 })).toEqual(['0', '+1h', '+2h', '+3h', '+4h', '+5h'])
    expect(painted(host.querySelector('.wf-tl-scale')!, 'container-type', { width: 1440 }) ?? painted(host.querySelector('.wf-tl-scale')!, 'container', { width: 1440 })).toMatch(/inline-size/)
    host.remove()
  })
})
