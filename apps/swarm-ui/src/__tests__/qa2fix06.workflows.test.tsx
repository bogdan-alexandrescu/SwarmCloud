/**
 * LANE QA2-FIX-06 (part of #2694, the console's visual QA pass 2, and #2391):
 * the pass-2 findings whose main file is Workflows.tsx, each pinned on the
 * FIXTURE console's own data (`api.ts` fixtures, which dev mode serves), so
 * the screen a reviewer renders is the screen these assertions read.
 *
 *   ovag2-08  Escape on the step drawer dropped focus to <body>
 *   ovag2-15  the list's Cost note was a mono sentence on four lines
 *   62        a `state unknown` row claimed `9d 22h so far`
 *   63        the page opened with the step drawer open and the graph scrolled
 *   66        `until every step has run` on a workflow that has ended
 *
 * Every assertion that holds a change carries a MUTATION comment naming the
 * change that turns it red.
 */
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { WorkflowsScreen } = await import('../Workflows')

const AUDIT = 'wf_audit_01'
const SYNTH = 'wf_5e5ad3b6f7da4299a839'
const STORED = 'wf_bcdc9180e4fb4a209f31'

beforeEach(() => {
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

async function list(): Promise<void> {
  render(<WorkflowsScreen view="" onView={() => {}} />)
  await waitFor(() => expect(document.querySelector(`.wfg-table tr[data-workflow="${AUDIT}"]`)).toBeTruthy(), { timeout: 4000 })
}

const row = (id: string) => document.querySelector<HTMLElement>(`.wfg-table tr[data-workflow="${id}"]`)!

/**
 * The row's summary in the docked inspector (the picked Work redesign, frame
 * graph-workflows-rest), opened by a click on the row's state cell: the
 * duration moved there from the row.
 */
async function summary(id: string): Promise<HTMLElement> {
  fireEvent.click(row(id).querySelector<HTMLElement>('[data-col="state"]')!)
  return waitFor(() => {
    const s = document.querySelector<HTMLElement>(`aside[aria-label="Workflow inspector"] .wfg-sum[data-workflow="${id}"]`)
    expect(s).toBeTruthy()
    return s!
  })
}

async function page(id: string): Promise<void> {
  render(<WorkflowsScreen view={`wf=${id}`} onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy(), { timeout: 4000 })
}

describe('ovag2-15: the list’s cost note is short', () => {
  it('reads `$… N of M reported`, the long form in its title', async () => {
    await list()
    const cov = await waitFor(() => {
      // The Cost column is the redesign's Spent (frame graph-workflows-rest).
      const c = row(AUDIT).querySelector<HTMLElement>('td[data-col="spent"] .wf-spend-cov')
      expect(c).toBeTruthy()
      return c!
    })
    // MUTATION: print `steps that ran reported` in the short form again.
    expect(flat(cov)).toMatch(/^\d+ of \d+ reported$/)
    expect(cov.getAttribute('title')).toMatch(/^\d+ of \d+ steps? that ran reported a cost/)
    // The figure and the note keep their space (overview-agents-82).
    expect(flat(row(AUDIT).querySelector('td[data-col="spent"] .wf-spend'))).toMatch(/^\$[\d.,]+ \d+ of \d+ reported$/)
  })

  it('keeps the long form on the page head, where there is room', async () => {
    await page(AUDIT)
    const cov = await waitFor(() => {
      const c = document.querySelector<HTMLElement>('.wfp-cost .wf-spend:not(.is-reading) .wf-spend-cov')
      expect(c).toBeTruthy()
      return c!
    })
    expect(flat(cov)).toMatch(/^\d+ of \d+ steps? that ran reported$/)
  })
})

describe('62: a `state unknown` row makes no duration claim', () => {
  it('says when it was submitted, not how long it has run', async () => {
    await list()
    await waitFor(() => expect(row(STORED).querySelector('td[data-col="state"] [data-mark="unknown"]')).toBeTruthy())
    // Duration moved from the row into the summary (frame graph-workflows-rest).
    const sum = await summary(STORED)
    const cell = ([...sum.querySelectorAll('dt')].find((d) => d.textContent === 'Duration')?.nextElementSibling ?? null) as HTMLElement | null
    expect(cell, 'no Duration in the summary').toBeTruthy()
    // MUTATION: drop the unknown-state arm in `WorkflowSummary` -- `9d 22h so far`.
    expect(flat(cell)).not.toMatch(/so far/)
    expect(flat(cell)).toMatch(/^began \d+[dhms] ago$/)
    expect(cell!.className).toMatch(/wfg-absent/)
    expect(cell!.getAttribute('title')).toMatch(/state is unknown/)
  })

  // The Duration column's fit at 1440 and its phone-card label went with the
  // column: the redesign (frame graph-workflows-rest) puts Duration in the
  // summary's wrapping `dl`, held above.
})

describe('66: the longest chain on a workflow that has ended', () => {
  it('names the step that never ran instead of waiting for it', async () => {
    await page(SYNTH)
    const note = await waitFor(() => {
      const n = document.querySelector<HTMLElement>('.wf-critical.is-unmeasured')
      expect(n).toBeTruthy()
      return n!
    })
    // MUTATION: drop the ended arm of `CriticalPathNote` -- `until every step has run`.
    expect(flat(note)).not.toMatch(/until every step has run/)
    expect(flat(note)).toMatch(/synthesis never ran$/)
    // MUTATION: put the `not measured:` prefix back on the words -- the
    // absent Mark already says it, so the line said it twice.
    expect(flat(note).match(/not measured/g)?.length ?? 0).toBe(1)
  })
})

describe('63: the page opens on the whole graph, its failed stage folded', () => {
  it('opens no stage on arrival and opens it, and brings the card nearest, once a reader asks', async () => {
    const seen: ScrollIntoViewOptions[] = []
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value(this: HTMLElement, o: ScrollIntoViewOptions) {
        if (this.classList.contains('wf-panel')) seen.push(o)
      },
    })
    try {
      await page(SYNTH)
      const go = await waitFor(() => {
        const b = document.querySelector<HTMLButtonElement>('.wfp-fail-lead-go')
        expect(b).toBeTruthy()
        return b!
      })
      // The card for the failure is open (QA-120) ...
      await waitFor(() => expect(document.querySelector('aside.wf-panel .wf-inspect-id')?.textContent).toBe('checkpoints'))
      // ... and the graph is whole: the failed stage is a folded band naming
      // the step, so nothing scrolls the canvas to it.
      // MUTATION: drop `|| arrivalPick` from `pickedLevel` in `WorkflowGraph`
      // -- the stage opens on load and the canvas is scrolled into it.
      const band = await waitFor(() => {
        const b = document.querySelector<HTMLElement>('.wf-canvas .wf-band')
        expect(b).toBeTruthy()
        return b!
      })
      expect(band.classList.contains('is-opened'), 'the stage opened on arrival').toBe(false)
      expect(document.querySelector('.wf-canvas button.node[data-step="checkpoints"]')).toBeNull()
      fireEvent.click(go)
      await waitFor(() => expect(document.querySelector('.wf-canvas .wf-band.is-opened')).toBeTruthy())
      expect(document.querySelector('.wf-canvas button.node[data-step="checkpoints"]')).toBeTruthy()
      // MUTATION: drop `inline: 'nearest'` from `pickStep`'s scroll of the card.
      expect(seen.at(-1)).toMatchObject({ block: 'nearest', inline: 'nearest' })
    } finally {
      delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollIntoView
    }
  })
})

describe('63: a reader’s pick scrolls the canvas, and the opened band still reads whole', () => {
  it('draws the band’s rows inside the part of the canvas on screen', async () => {
    // The 1440 page with the step card open: the ~595px wrapper, the 72px rail.
    const restore: (() => void)[] = []
    const stub = (prop: string, get: (this: HTMLElement) => number, set?: (this: HTMLElement, v: number) => void) => {
      Object.defineProperty(HTMLElement.prototype, prop, { configurable: true, get, ...(set ? { set } : {}) })
      restore.push(() => delete (HTMLElement.prototype as unknown as Record<string, unknown>)[prop])
    }
    const at = new WeakMap<HTMLElement, number>()
    stub(
      'scrollLeft',
      function () {
        return at.get(this) ?? 0
      },
      function (v) {
        at.set(this, v)
      },
    )
    stub('clientWidth', function () {
      return this.classList.contains('wf-canvas-wrap') ? 595 : 0
    })
    stub('scrollWidth', function () {
      return this.classList.contains('wf-canvas-wrap') ? 72 + Number.parseFloat(this.querySelector<HTMLElement>('.wf-canvas')?.style.width ?? '0') : 0
    })
    stub('offsetWidth', function () {
      return this.classList.contains('wf-levels') ? 72 : 0
    })
    try {
      await page(SYNTH)
      fireEvent.click(await waitFor(() => document.querySelector<HTMLButtonElement>('.wfp-fail-lead-go')!))
      const band = await waitFor(() => {
        const b = document.querySelector<HTMLElement>('.wf-canvas .wf-band.is-opened')
        expect(b).toBeTruthy()
        return b!
      })
      const wrap = document.querySelector<HTMLElement>('.wf-canvas-wrap')!
      // The premise: bringing `checkpoints` round scrolled the canvas into the band.
      await waitFor(() => expect(wrap.scrollLeft).toBeGreaterThan(0))
      const lo = wrap.scrollLeft
      const hi = wrap.scrollLeft + 595 - 72
      const x = Number.parseFloat(band.style.left)
      const w = Number.parseFloat(band.style.width)
      expect(x + w, 'the band is wider than the view, so its edges are off screen').toBeGreaterThan(hi)
      // MUTATION: drop `rowsOnScreen` from `StageBand`'s style -- the rows
      // start at the band's left, `lo - x` px off screen (`exit` cut from the
      // cause), and the caret ends past the view (`collapse sta`).
      await waitFor(() => {
        const pl = Number.parseFloat(band.style.paddingLeft || '12')
        const pr = Number.parseFloat(band.style.paddingRight || '12')
        expect(x + pl, 'the rows start off the left of the view').toBeGreaterThanOrEqual(lo)
        expect(x + w - pr, 'the rows end past the right of the view').toBeLessThanOrEqual(hi)
      })
    } finally {
      for (const r of restore.reverse()) r()
    }
  })
})

describe('ovag2-08: Escape on the step drawer gives focus back to its node', () => {
  it('returns focus to the graph node that opened the drawer', async () => {
    await page(SYNTH)
    // The failed stage opens once a reader asks (overview-agents-63).
    fireEvent.click(await waitFor(() => document.querySelector<HTMLButtonElement>('.wfp-fail-lead-go')!))
    const node = await waitFor(() => {
      const n = document.querySelector<HTMLButtonElement>('.wf-canvas button.node[data-step="fencing"]')
      expect(n).toBeTruthy()
      return n!
    })
    node.focus()
    fireEvent.click(node)
    const panel = await waitFor(() => {
      const p = document.querySelector<HTMLElement>('aside.wf-panel')
      expect(p).toBeTruthy()
      return p!
    })
    await waitFor(() => expect(panel.contains(document.activeElement)).toBe(true))
    fireEvent.keyDown(document.activeElement ?? panel, { key: 'Escape' })
    await waitFor(() => expect(document.querySelector('aside.wf-panel')).toBeNull())
    // MUTATION: drop the after-commit `refocus` effect in `WorkflowCard` -- the
    // close re-lays the stage, the node focused at once is a detached button,
    // and focus falls to <body>.
    const back = document.querySelector<HTMLButtonElement>('.wf-canvas button.node[data-step="fencing"]')
    expect(document.activeElement).toBe(back)
  })
})
