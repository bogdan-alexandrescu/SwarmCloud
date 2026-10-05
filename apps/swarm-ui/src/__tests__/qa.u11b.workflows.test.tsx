/**
 * BROWSER QA U11b (owner, 2026-10-04; live console at main 69416faf, 1440x900
 * light and dark and 390px): the Workflows list, a workflow's step table and
 * its graph page.
 *
 *   N1   The step table at /workflows/<id>/table and the Graph page's Steps
 *        section: each step's "Ran / Attempts / Cost" line printed over the
 *        next step's name. The `wf-steps` container is 996px at a 1440
 *        viewport, so the `max-width: 999px` stacked form applied on a desktop,
 *        and in that form the row is a wrapping flex box that
 *        `.ctl-table tbody tr { height: var(--row-h) }` pins at 30px. The
 *        stacked form is for a container below the table's real minimum, and a
 *        stacked row is as tall as what it holds.
 *   D12  Names one line with an ellipsis and their whole id as the title;
 *        numeric cells sized to what they hold.
 *   N7   A "claude-code ×3" workflow showed only a "+1" chip in Runners.
 *   N8   The Workflow cell was a 37px flex box in a 56px row, so its bottom
 *        border floated ~20px above the row's.
 *   GRAPH  Clicking a node opened its step card under every node, a screen
 *        away from the node: the card is scrolled into view when it opens.
 *
 * jsdom lays nothing out, so the row geometry is the model in `tablefit.ts`
 * fed by the shipped cascade (`marks.painted`). MUTATIONS: put the container
 * query back to 999px, drop the stacked row's `height: auto`, make the name
 * cell a flex box again, put Runners back to 11%, fold a lone profile into
 * "+1", or drop the panel's scrollIntoView -- each turns a case red.
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import { foldMix } from '../dag'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, fixedColumns, fontPx, lengthPx, nowrap, textPx } from './tablefit'

const WF = 'wf_5e5ad3b6f7da4299a839'
/** The list's box at 1440: 1440 less the 84px spine, the 236px panel and two 32px gutters. */
const LIST_AT_1440 = 1440 - 84 - 236 - 64
/** The step table's container (`.wf-table-box`) as measured in the browser QA. */
const STEPS_CLOSED_1440 = 996
/** The same box with the step card docked beside it (1100px and up: 400px card, 12px gap). */
const STEPS_OPEN_1440 = STEPS_CLOSED_1440 + 16 - 400 - 12
/** A 390px phone: the gutters and the card's padding and borders. */
const STEPS_PHONE = 390 - 32 - 32

const LONG = [
  'impl-plan-schema-and-overlaps-for-the-issue-runner',
  'review-the-scheduler-dispatch-guard-and-fencing',
  'write-the-capacity-ledger-migration-and-backfill',
  'fix-the-overview-headroom-for-disabled-profiles',
  'port-the-timeline-gridlines-onto-the-tick-scale',
  'rework-the-run-page-linked-card-label-column',
  'teach-the-submit-summary-to-follow-plan-approval',
  'measure-the-accounts-history-holder-column-fit',
  'close-the-add-setting-menu-on-escape-anywhere',
  'scroll-the-workflow-step-card-into-view-on-pick',
]

afterEach(() => {
  window.history.replaceState(null, '', '/')
  vi.restoreAllMocks()
})

async function stepTable(path: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', path)
  render(<App />)
  return waitFor(() => {
    const t = document.querySelector<HTMLElement>('.wf-card .wf-table')
    expect(t?.querySelectorAll('tbody tr[data-step]').length ?? 0).toBeGreaterThan(1)
    return t!
  })
}

/** Ten rows of long names: the fixture's rows, cloned and renamed. */
function tenLongRows(wrap: HTMLElement): HTMLTableRowElement[] {
  const body = wrap.querySelector('tbody')!
  const proto = body.querySelector<HTMLTableRowElement>('tr[data-step]')!
  for (const r of [...body.querySelectorAll('tr')]) r.remove()
  return LONG.map((name) => {
    const r = proto.cloneNode(true) as HTMLTableRowElement
    r.setAttribute('data-step', name)
    const id = r.querySelector<HTMLElement>('.wf-pick-id')!
    id.textContent = name
    id.setAttribute('title', name)
    body.appendChild(r)
    return r
  })
}

/**
 * The height a row's own box must have for what it holds, and the height the
 * cascade gives it. A table row is as tall as its tallest cell whatever its
 * `height` says, so only a row the cascade turned into a block or flex box can
 * be shorter than its content -- which is what printed one step over the next.
 * The rows are clones that differ only in their names, so each column's
 * cascade is read once, off the first row.
 */
function rowFits(rows: readonly HTMLTableRowElement[], env: CascadeEnv & { container: number }): { display: string; need: number; box: number | null }[] {
  const first = rows[0]!
  const display = painted(first, 'display', env) ?? 'table-row'
  if (display === 'table-row') return rows.map(() => ({ display, need: 0, box: null }))
  const box = lengthPx(painted(first, 'height', env), 0)
  const cols = [...first.children]
    .map((c, i) => ({ c, i }))
    .filter(({ c }) => (painted(c, 'display', env) ?? 'table-cell') !== 'none')
    .map(({ c, i }) => ({
      i,
      labelled: (painted(c, 'content', env, 'before') ?? '').includes('attr(data-label)'),
      full: /\b100%/.test(painted(c, 'flex', env) ?? ''),
      em: textPx('x', c, env),
      lh: fontPx(c, env) * 1.35,
    }))
  return rows.map((row) => {
    // Greedy line packing, as a wrapping flex line does: each cell at its
    // text's width (its label in front when the sheet draws one), 14px apart.
    let lines = 1
    let x = 0
    let lh = 0
    for (const col of cols) {
      const c = row.children[col.i]!
      const text = ((col.labelled ? `${c.getAttribute('data-label') ?? ''} ` : '') + (c.textContent ?? '')).trim()
      const w = col.full ? env.container : Math.min(env.container, [...text].length * col.em)
      if (x > 0 && x + 14 + w > env.container) {
        lines++
        x = w
      } else x += (x > 0 ? 14 : 0) + w
      lh = Math.max(lh, col.lh)
    }
    return { display, need: lines * lh, box }
  })
}

describe('N1: a step row never prints over the next one', () => {
  for (const path of [`/workflows/${WF}/table`, `/workflows/${WF}`]) {
    it(`at 1440 the table is a table, with no sideways scroll (${path})`, async () => {
      const wrap = await stepTable(path)
      const rows = tenLongRows(wrap)
      const env = { width: 1440, container: STEPS_CLOSED_1440 }
      for (const row of rows) expect(painted(row, 'display', env) ?? 'table-row', 'stacked at 1440').toBe('table-row')
      const { width } = fixedColumns(wrap.querySelector('table')!, STEPS_CLOSED_1440, env)
      expect(width, 'the table is wider than its box at 1440').toBeLessThanOrEqual(STEPS_CLOSED_1440)
    })

    it(`no row is shorter than what it holds, card open and closed, at 1440 and 390 (${path})`, async () => {
      const wrap = await stepTable(path)
      const rows = tenLongRows(wrap)
      const envs = [
        { width: 1440, container: STEPS_CLOSED_1440 },
        { width: 1440, container: STEPS_OPEN_1440 },
        { width: 390, container: STEPS_PHONE },
      ]
      let stacked = 0
      for (const env of envs) {
        rowFits(rows, env).forEach((fit, i) => {
          if (fit.display === 'table-row') return
          stacked++
          // A stacked row whose box is pinned shorter than its lines paints
          // its second and third lines over the next row's name.
          if (fit.box !== null) expect(fit.box, `${LONG[i]} at ${env.width}/${env.container}`).toBeGreaterThanOrEqual(fit.need)
        })
      }
      // The narrow cases really were stacked: the check above ran.
      expect(stacked).toBeGreaterThanOrEqual(rows.length * 2)
    })
  }
})

describe('D12: names one line, numbers sized to what they hold', () => {
  it('cuts each long name with an ellipsis and its whole id as its title', async () => {
    const wrap = await stepTable(`/workflows/${WF}/table`)
    const rows = tenLongRows(wrap)
    const env = { width: 1440, container: STEPS_CLOSED_1440 }
    for (const row of rows) {
      const id = row.querySelector<HTMLElement>('.wf-pick-id')!
      expect(id.getAttribute('title')).toBe(row.getAttribute('data-step'))
      expect(nowrap(id, env)).toBe(true)
      expect(painted(id, 'text-overflow', env)).toBe('ellipsis')
      expect(painted(id, 'hyphens', env) ?? 'manual').not.toBe('auto')
    }
  })

  it('gives Attempts and Inputs their whole figure', async () => {
    const wrap = await stepTable(`/workflows/${WF}/table`)
    const t = wrap.querySelector('table')!
    const env = { width: 1440 }
    const { cols } = fixedColumns(t, STEPS_CLOSED_1440, env)
    const row = t.querySelector('tbody tr[data-step]')!
    for (const [col, text] of [['attempts', '10 of 10'], ['inputs', '12 files']] as const) {
      const td = row.querySelector(`td[data-col="${col}"]`)
      expect(td, `no ${col} cell: the case would pass without measuring anything`).not.toBeNull()
      const box = cols.find((c) => c.col === col)
      expect(box, `no ${col} column head`).toBeDefined()
      if (td === null || box === undefined) return
      const st = cellStyle(td, env)
      expect(box.end - box.start - st.pl - st.pr, col).toBeGreaterThanOrEqual(textPx(text, td, env))
    }
  })
})

async function list(): Promise<HTMLTableElement> {
  window.history.replaceState(null, '', '/workflows')
  render(<App />)
  return waitFor(() => {
    const t = document.querySelector<HTMLTableElement>('.wfl .wfl-table')
    expect(t!.querySelectorAll('tbody tr.wfl-row:not(.is-skel)').length).toBeGreaterThan(0)
    return t!
  })
}

describe('N7: the Runners cell names the runner', () => {
  it('fits "claude-code ×3" whole in the Runners column at 1440', async () => {
    const t = await list()
    const env = { width: 1440 }
    const { cols } = fixedColumns(t, LIST_AT_1440, env)
    const box = cols.find((c) => c.col === 'runners')!
    const td = t.querySelector('tbody tr.wfl-row td[data-col="runners"]')!
    const st = cellStyle(td, env)
    const room = box.end - box.start - st.pl - st.pr
    const { shown, rest } = foldMix([{ profile: 'claude-code', count: 3 }], room)
    expect(shown.map((m) => m.profile)).toEqual(['claude-code'])
    expect(rest).toBe(0)
    // Two profiles: the commoner is named, the other counted.
    const two = foldMix(
      [
        { profile: 'claude-code', count: 20 },
        { profile: 'codex', count: 2 },
      ],
      room,
    )
    expect(two.shown.length).toBeGreaterThanOrEqual(1)
  })

  it('never folds a lone profile into a bare "+1", however narrow', () => {
    const { shown, rest } = foldMix([{ profile: 'claude-code', count: 3 }], 40)
    expect(shown.map((m) => m.profile)).toEqual(['claude-code'])
    expect(rest).toBe(0)
  })
})

describe('N8: the Workflow cell is the row\'s own cell', () => {
  it('lays the name cell out as a table cell, so its border is the row\'s', async () => {
    const t = await list()
    const td = t.querySelector('tbody tr.wfl-row td[data-col="workflow"]')!
    for (const width of [1440, 390]) {
      expect(painted(td, 'display', { width }) ?? 'table-cell', `at ${width}`).toBe('table-cell')
    }
    // Its pieces still stack, one under the other.
    const a = td.querySelector(':scope > a')!
    expect(painted(a, 'display', { width: 1440 })).toBe('block')
  })
})

describe('the graph: a picked node\'s card is scrolled to', () => {
  it('scrolls the step card into view when a node opens it', async () => {
    const seen: Element[] = []
    const spy = vi.fn(function (this: Element) {
      seen.push(this)
    })
    Object.defineProperty(Element.prototype, 'scrollIntoView', { value: spy, configurable: true, writable: true })
    window.history.replaceState(null, '', `/workflows/${WF}`)
    render(<App />)
    const node = await waitFor(() => {
      const n = document.querySelector<HTMLButtonElement>('.wf-canvas button.node')
      expect(n).not.toBeNull()
      return n!
    })
    fireEvent.click(node)
    await waitFor(() => expect(seen.some((el) => el.classList.contains('wf-panel'))).toBe(true))
  })
})
