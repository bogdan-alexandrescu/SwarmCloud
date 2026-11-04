/**
 * LANE QA2-FIX-06 (part of #2694, the console's visual QA pass 2, and #2391):
 * the pass-2 findings whose main file is Workflows.tsx, each pinned on the
 * FIXTURE console's own data (`api.ts` fixtures, which dev mode serves), so
 * the screen a reviewer renders is the screen these assertions read.
 *
 *   ovag2-01  at 390 a card's State cell was a ~10px column, one letter a line
 *   ovag2-08  Escape on the step drawer dropped focus to <body>
 *   ovag2-15  the list's Cost note was a mono sentence on four lines
 *   61        a two-line title was cut to one line at 1440
 *   62        a `state unknown` row claimed `9d 22h so far`
 *   63        the page opened with the step drawer open and the graph scrolled
 *   66        `until every step has run` on a workflow that has ended
 *
 * Every assertion that holds a change carries a MUTATION comment naming the
 * change that turns it red.
 */
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Task, Workflow } from '../types'

const { WorkflowCard, WorkflowsScreen } = await import('../Workflows')
const { loadWorkflowBoard } = await import('../api')

const AUDIT = 'wf_audit_01'
const SYNTH = 'wf_5e5ad3b6f7da4299a839'
const STORED = 'wf_bcdc9180e4fb4a209f31'

const realWidth = window.innerWidth

beforeEach(() => {
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
  // The inspector starts folded below 1366px (graph-notes D·8b): these pages
  // need it docked.
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: realWidth })
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

async function css(): Promise<string> {
  return (await import('../styles/workflows.css?raw')).default
}

/**
 * The fixture's own workflow and tasks, read the same way the screen does
 * (`USE_FIXTURES`): what a canvas-only behaviour's `WorkflowCard` harness
 * below renders, since `WorkflowCard` is no longer reachable through the page
 * (the redesign draws the stage grid there instead -- graph-workflow-rest).
 */
async function fixtureCard(id: string): Promise<{ workflow: Workflow; taskById: Map<string, Task> }> {
  const r = await loadWorkflowBoard()
  if (r.status !== 'ok') throw new Error(`fixture board read failed: ${r.status}`)
  const workflow = r.data.workflows.find((w) => w.workflow_id === id)
  if (workflow === undefined) throw new Error(`no fixture workflow ${id}`)
  return { workflow, taskById: r.data.taskById ?? new Map() }
}

/**
 * `WorkflowCard` DIRECTLY, `page` AND A SEEDED PICK (as `WorkflowPage`'s old
 * `Board` used to drive it before the redesign): the arrival pick, its
 * bring-into-view and the Escape-refocus-to-node are the canvas's own, not
 * the stage-grid page's, and `WorkflowCard` is kept exported for exactly this
 * (brief, "What moved where").
 */
function CanvasHarness({ workflow, taskById, initialPicked }: { workflow: Workflow; taskById: Map<string, Task>; initialPicked: string | null }) {
  const [stages, setStages] = useState<Record<string, boolean>>({})
  const [picked, setPicked] = useState<string | null>(initialPicked)
  return (
    <WorkflowCard
      workflow={workflow}
      taskById={taskById}
      usage={{ kind: 'ready', usage: null }}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      reload={() => {}}
      picked={picked}
      onPick={(_wfId, stepId) => setPicked((p) => (p === stepId ? null : stepId))}
      page
    />
  )
}

/** The body of the first rule in `sheet` whose selector is exactly `selector`, after `from`. */
function ruleBody(sheet: string, selector: string, from = 0): string | null {
  const esc = selector.replace(/[.*+?^${}()|[\]\\>]/g, '\\$&').replace(/\s+/g, '\\s*')
  const m = new RegExp(`(?:^|[}\\s])${esc}\\s*\\{([^}]*)\\}`).exec(sheet.slice(from))
  return m?.[1] ?? null
}

describe('ovag2-01: the phone card gives State a share of the width, not what is left', () => {
  it('lays the card on two fractional tracks, so a wide cost or duration cannot starve the State column', async () => {
    const sheet = await css()
    const at = sheet.indexOf('@media (max-width: 640px)')
    expect(at).toBeGreaterThan(-1)
    const body = ruleBody(sheet, '.wfl > .wfl-scroll > .wfl-table > tbody > tr.wfl-row', at)
    expect(body, 'no phone card rule').not.toBeNull()
    const cols = /grid-template-columns:\s*([^;]+);/.exec(body!)?.[1]?.trim()
    // MUTATION: put back `minmax(0, 1fr) minmax(0, auto)` -- the right track
    // grows to the cost cell's one-line width first and the left one, where
    // State and Steps done sit, gets the ~10px that is left.
    expect(cols).toBe('repeat(2, minmax(0, 1fr))')
  })

  it('keeps the state word on one line beside its mark', async () => {
    const sheet = await css()
    const at = sheet.indexOf('@media (max-width: 640px)')
    // MUTATION: drop the phone State cell's `white-space: nowrap` -- a narrow
    // cell stacks `state unknown` a word a line.
    const body = ruleBody(sheet, '.wfl > .wfl-scroll > .wfl-table > tbody > tr.wfl-row > td[data-col="state"]', at)
    expect(body).toMatch(/white-space:\s*nowrap/)
  })
})

describe('ovag2-15: the list’s cost note is short, in the body face', () => {
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
    // `.wfp-cost` went with the redesign (brief, "What moved where"): the
    // head's spend figure is a plain `.wfp-fig` now, still the long form.
    const cov = await waitFor(() => {
      const c = document.querySelector<HTMLElement>('.wfp-top .wf-spend:not(.is-reading) .wf-spend-cov')
      expect(c).toBeTruthy()
      return c!
    })
    expect(flat(cov)).toMatch(/^\d+ of \d+ steps? that ran reported$/)
  })

  it('sets the list’s note in the body face', async () => {
    const sheet = await css()
    // MUTATION: drop `font-family: var(--font)` from the list's coverage rule
    // -- it inherits `.wf-spend`'s mono.
    expect(sheet).toMatch(/\.wfl \.wfl-table td\[data-col="cost"\] \.wf-spend-cov\s*\{[^}]*font-family:\s*var\(--font\)/)
  })
})

describe('61: a long title takes two lines in the list', () => {
  it('clamps the name at two lines instead of cutting it at one', async () => {
    const sheet = await css()
    const body = ruleBody(sheet, '.wfl .wfl-line > a', sheet.indexOf('@media (min-width: 900px)'))
    expect(body).not.toBeNull()
    // MUTATION: put `white-space: nowrap` back on the name -- `Check the five
    // contract invariants, then write one sy…` at 1440.
    expect(body).not.toMatch(/white-space:\s*nowrap/)
    expect(body).toMatch(/-webkit-line-clamp:\s*2/)
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
  // THE LONGEST-CHAIN NOTE IS THE CANVAS'S OWN (`CriticalPathNote`, drawn only
  // inside `WorkflowGraph`): the redesigned page draws the stage grid instead
  // (graph-workflow-rest) and has no equivalent note, so this renders
  // `WorkflowCard` directly, on the fixture's own data (brief, "What moved
  // where" -- "render it directly").
  it('names the step that never ran instead of waiting for it', async () => {
    const { workflow, taskById } = await fixtureCard(SYNTH)
    render(<CanvasHarness workflow={workflow} taskById={taskById} initialPicked={null} />)
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
  // NO ARRIVAL PICK ON THE PAGE ANY MORE (brief, "What moved where"): the
  // redesign opens with nothing picked and the Overview in the inspector: the
  // failed step is red on the stage grid and in the Needs-you row instead,
  // and its "Why it failed" is read from its inspector once a reader picks
  // it -- this is that replacement. The band-folding and scroll-into-view
  // this used to check are the canvas's own (see "63: a reader's pick
  // scrolls the canvas" below, and ovag2-08, both retargeted to `WorkflowCard`).
  it('opens no stage on arrival and opens it, and brings the card nearest, once a reader asks', async () => {
    await page(SYNTH)
    // Nothing is picked on arrival: the Overview sits in the inspector.
    expect(document.querySelector('.wi-dock')?.getAttribute('aria-label')).toBe('Workflow overview')
    const chip = await waitFor(() => {
      const c = document.querySelector<HTMLButtonElement>('.sg [data-step="checkpoints"]')
      expect(c).toBeTruthy()
      return c!
    })
    // MUTATION: drawing the failed chip in any other kind turns this red.
    expect(chip.className).toContain('is-failed')
    const needsRow = document.querySelector<HTMLElement>('.ny-row.is-failed')
    expect(needsRow, 'no failed row in Needs-you').toBeTruthy()
    expect(needsRow!.textContent).toContain('checkpoints')
    // Picking the chip opens the step in the inspector, with its full reason.
    fireEvent.click(chip)
    await waitFor(() => expect(document.querySelector('.wi-dock')?.getAttribute('aria-label')).toBe('Step checkpoints'))
    const dock = document.querySelector<HTMLElement>('.wi-dock')!
    // MUTATION: dropping the cause card from `InspectorSlot` turns this red.
    expect(dock.textContent).toContain('Why it failed')
    expect(dock.textContent).toContain('exit status 2: pytest was interrupted: 1 error during collection')
  })
})

describe('63: a reader’s pick scrolls the canvas, and the opened band still reads whole', () => {
  // THE BAND LAYOUT AND ITS SCROLL ARE THE CANVAS'S OWN (`WorkflowGraph`,
  // `StageBand`), not drawn by the redesigned page at all (it has no
  // horizontal scroller -- the stage grid is a dense table). `WorkflowCard`
  // is kept exported for exactly this case (brief, "What moved where").
  it('draws the band’s rows inside the part of the canvas on screen', async () => {
    // The ~595px wrapper, the 72px rail, as the 1440 page used to size it.
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
      const { workflow, taskById } = await fixtureCard(SYNTH)
      render(<CanvasHarness workflow={workflow} taskById={taskById} initialPicked="checkpoints" />)
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
  // THE DRAWER AND ITS REFOCUS ARE THE CANVAS'S OWN (`.wf-panel`'s Escape in
  // `WorkflowCard`): the redesigned page docks the inspector beside the
  // stage grid instead, and has no node to refocus (brief, "What moved
  // where"), so this renders `WorkflowCard` directly, as the page used to.
  it('returns focus to the graph node that opened the drawer', async () => {
    const { workflow, taskById } = await fixtureCard(SYNTH)
    render(<CanvasHarness workflow={workflow} taskById={taskById} initialPicked="checkpoints" />)
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
