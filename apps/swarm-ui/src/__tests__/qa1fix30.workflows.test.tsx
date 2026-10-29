/**
 * LANE QA1-FIX-30 (part of #2596, the console's visual QA pass 1, and #2391):
 * the findings whose main file is Workflows.tsx, each pinned on the FIXTURE
 * console's own data (`api.ts` fixtures, which dev mode serves), so the screen
 * a reviewer renders is the screen these assertions read.
 *
 *   61  the list's titles were cut by a raw id beside them
 *   62  `state not derived · this API did not derive the state from the steps`
 *   63  the graph scrolled to centre the picked step and cut its neighbours
 *   64  the synthesis node listed every input file, ~260px of card
 *   73  the Graph tab drew the whole Steps table under the graph
 *   80  the loading head had no Copy id, so the loaded head shifted
 *   81  `stored as queued · steps running · stored copy corrected`
 *   82  `$0.518 3 of 4 reported`: no unit, no space
 *   83  stop / Stop step / Cancel workflow / Stop: one action, three words
 *   84  a recorded stop changed nothing on the page
 *   85  the stop dialog's hint asked for a name where the id is typed (QA1C-FOLLOWUPS)
 *   89  two screens named Timeline
 *   C-10 Submitted and started as local clock times with no zone
 *   C-11 a one-step chip cut to `claude-code-browser …`
 *
 * Every assertion that holds a change carries a MUTATION comment naming the
 * change that turns it red.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CancelWorkflowResult, WorkflowRead } from '../api'
import type { Result } from '../fetch'

const api = vi.hoisted(() => ({
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  api.loadWorkflow.mockImplementation(real.loadWorkflow)
  return { ...real, ...api }
})

const real = await vi.importActual<typeof import('../api')>('../api')
const { WorkflowsScreen, utcStamp } = await import('../Workflows')

const AUDIT = 'wf_audit_01'
const AUDIT_TITLE = 'Audit the operator scripts and the Terraform'
const SYNTH = 'wf_5e5ad3b6f7da4299a839'
const STORED = 'wf_bcdc9180e4fb4a209f31'

const restore: (() => void)[] = []

beforeEach(() => {
  api.loadWorkflow.mockImplementation(real.loadWorkflow)
  api.cancelWorkflow.mockReset()
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  for (const r of restore.splice(0).reverse()) r()
})

/** Shadow a layout property on HTMLElement for this test only. */
function stubBox(prop: 'clientWidth' | 'scrollWidth' | 'offsetWidth', of: (el: HTMLElement) => number): void {
  Object.defineProperty(HTMLElement.prototype, prop, {
    configurable: true,
    get(this: HTMLElement) {
      return of(this)
    },
  })
  restore.push(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>)[prop]
  })
}

/** jsdom's `scrollLeft` ignores writes; this one keeps them. */
function keepScroll(): void {
  const at = new WeakMap<HTMLElement, number>()
  Object.defineProperty(HTMLElement.prototype, 'scrollLeft', {
    configurable: true,
    get(this: HTMLElement) {
      return at.get(this) ?? 0
    },
    set(this: HTMLElement, v: number) {
      at.set(this, v)
    },
  })
  restore.push(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollLeft
  })
}

const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

async function list(): Promise<void> {
  render(<WorkflowsScreen view="" onView={() => {}} />)
  await waitFor(() => expect(document.querySelector(`.wfl-table tr[data-workflow="${AUDIT}"]`)).toBeTruthy(), { timeout: 4000 })
}

const row = (id: string) => document.querySelector<HTMLElement>(`.wfl-table tr[data-workflow="${id}"]`)!

async function page(id: string, tab = ''): Promise<void> {
  render(<WorkflowsScreen view={`wf=${id}${tab ? `&tab=${tab}` : ''}`} onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy(), { timeout: 4000 })
}

// ---------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------

describe('61: the id is the line under the title, not a cut beside it', () => {
  it('draws the title alone on its line and the id under it, whole in its title', async () => {
    await list()
    await waitFor(() => expect(row(AUDIT).querySelector('.wfl-line > a')?.textContent).toBe(AUDIT_TITLE))
    const td = row(AUDIT).querySelector<HTMLElement>('td[data-col="workflow"]')!
    // MUTATION: put `<Id>` back inside `.wfl-line` in `WorkflowListRow` -- the
    // id takes its 10ch of the title's line and the title is cut at 1440.
    expect(td.querySelector('.wfl-line > .id'), 'the id is still beside the title').toBeNull()
    const id = td.querySelector<HTMLElement>(':scope > .id')
    expect(id?.textContent).toBe(AUDIT)
    expect(id?.getAttribute('title')).toBe(AUDIT)
    // Under the title: after its line in the cell.
    expect(td.querySelector('.wfl-line')!.compareDocumentPosition(id!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('gives the id its own one-line micro rule, and the sheet no longer cuts it at 10ch beside the title', async () => {
    const CSS = (await import('../styles/workflows.css?raw')).default
    // MUTATION: drop the `.wfl .wfl-name > .id` rule -- the id is an inline
    // span run on after the title.
    const rule = CSS.match(/\.wfl \.wfl-name > \.id\s*\{([^}]*)\}/)
    expect(rule, 'no rule lays the id under the title').not.toBeNull()
    expect(rule![1]).toMatch(/display:\s*block/)
    expect(rule![1]).toMatch(/white-space:\s*nowrap/)
    expect(rule![1]).toMatch(/font-size:\s*var\(--t-micro\)/)
    expect(CSS).not.toMatch(/\.wfl-line > \.id/)
  })
})

describe('C-11: a lone runner chip keeps its step count', () => {
  it('draws the profile and the count as two boxes, the count never cut', async () => {
    await list()
    const chip = await waitFor(() => {
      const c = row(STORED).querySelector<HTMLElement>('.wf-mix > .c-chip')
      expect(c).toBeTruthy()
      return c!
    })
    // MUTATION: print `{m.profile}` bare in `Mix` -- one text run, cut at the
    // chip's end, and `· 3 steps` is what the ellipsis takes.
    expect(chip.querySelector(':scope > .wf-mix-p')?.textContent).toBe('claude-code')
    expect(flat(chip.querySelector(':scope > .wf-mix-n'))).toBe('· 3 steps')
    const CSS = (await import('../styles/workflows.css?raw')).default
    // MUTATION: drop the `.wf-mix-n { flex: none }` rule -- the count shrinks with the profile.
    expect(CSS).toMatch(/\.wf-mix > \.c-chip > \.wf-mix-n\s*\{[^}]*flex:\s*none/)
    expect(CSS).toMatch(/\.wf-mix > \.c-chip > \.wf-mix-p\s*\{[^}]*text-overflow:\s*ellipsis/)
    expect(CSS).toMatch(/span\.wf-mix > \.c-chip\s*\{[^}]*display:\s*flex/)
  })
})

describe('C-10: times are UTC, written with Z', () => {
  it('writes an instant on the same UTC day with seconds and Z, and another day with its date', () => {
    const now = Date.parse('2026-10-28T09:30:00Z')
    // MUTATION: format with getHours()/getMinutes() in `utcStamp` -- a
    // non-UTC browser prints its local clock.
    expect(utcStamp('2026-10-28T02:20:01Z', now)).toBe('02:20:01Z')
    expect(utcStamp('2026-10-27T23:05:09Z', now)).toBe('Oct 27 23:05Z')
    expect(utcStamp(null, now)).toBeNull()
    expect(utcStamp('not a time', now)).toBeNull()
  })

  it('prints the list’s Submitted cell and the page’s start in UTC with Z', async () => {
    await list()
    // MUTATION: print `started.submitted` (`clockTime`, local, no zone) in
    // the Submitted cell, as before.
    await waitFor(() => expect(row(AUDIT).querySelector('.wfl-submitted')?.textContent).toMatch(/Z$/))
    expect(row(AUDIT).querySelector('.wfl-submitted')?.getAttribute('title')).toMatch(/submitted \d{4}-\d\d-\d\dT/)
    cleanup()
    await page(AUDIT)
    // MUTATION: use `workflowStartText` in `pageSummary` -- `started 02:21:40`.
    await waitFor(() => expect(flat(document.querySelector('.c-count-note'))).toMatch(/started (\d\d:\d\d:\d\dZ|[A-Z][a-z]{2} \d{1,2} \d\d:\d\dZ)$/))
  })
})

describe('62: a state the API did not derive reads `state unknown`, the why in its title', () => {
  it('says `state unknown` on the mark and draws no API sentence under the title', async () => {
    await list()
    const mark = await waitFor(() => {
      const m = row(STORED).querySelector<HTMLElement>('td[data-col="state"] [data-mark="unknown"]')
      expect(m).toBeTruthy()
      return m!
    })
    // MUTATION: pass `h.word` straight through in `WorkflowStateMark`.
    expect(flat(row(STORED).querySelector('td[data-col="state"]'))).toBe('state unknown')
    expect(mark.closest('[title]')?.getAttribute('title') ?? mark.getAttribute('title')).toMatch(/did not derive the state/)
    // MUTATION: drop the not-derived arm from `listRowWhy` -- the row's second
    // line says `this API did not derive the state from the steps` again.
    expect(row(STORED).querySelector('.wfl-why'), 'the API’s explanation is still the row’s second line').toBeNull()
    expect(flat(row(STORED))).not.toMatch(/not derived/)
  })
})

// ---------------------------------------------------------------------------
// One workflow's page
// ---------------------------------------------------------------------------

describe('80: the loading head reserves the loaded head’s buttons', () => {
  it('draws Copy id and Copy link, disabled, while the page reads', async () => {
    api.loadWorkflow.mockImplementation(() => new Promise<Result<WorkflowRead>>(() => {}))
    render(<WorkflowsScreen view={`wf=${AUDIT}`} onView={() => {}} />)
    const head = await waitFor(() => {
      const h = document.querySelector<HTMLElement>('.wfp.is-loading .wfp-actions')
      expect(h).toBeTruthy()
      return h!
    })
    // MUTATION: drop `<Button disabled>Copy id</Button>` from `WorkflowPageSkeleton`.
    const buttons = [...head.querySelectorAll('button')]
    expect(buttons.map((b) => b.textContent)).toEqual(['Copy id', 'Copy link'])
    expect(buttons.every((b) => b.disabled)).toBe(true)
  })
})

describe('81: the page says one state', () => {
  it('draws no stored-state line on a workflow whose stored copy disagreed', async () => {
    await page(AUDIT)
    await waitFor(() => expect(document.querySelector('.wf-card.is-page')).toBeTruthy())
    // MUTATION: render `<StateDrift>` on the page again -- `stored as queued ·
    // steps running · stored copy corrected` beside a head that says running.
    expect(document.querySelector('.wf-drift')).toBeNull()
    expect(flat(document.querySelector('.wfp'))).not.toMatch(/stored as|stored copy/)
  })
})

describe('82: the cost pill names its unit and keeps a space', () => {
  it('reads `$… N of M steps that ran reported`, with a space before the count', async () => {
    await page(AUDIT)
    const spend = await waitFor(() => {
      const s = document.querySelector<HTMLElement>('.wfp-cost .wf-spend:not(.is-reading)')
      expect(s?.querySelector('.wf-spend-cov')).toBeTruthy()
      return s!
    })
    // MUTATION: drop the `{' '}` before `.wf-spend-cov` in `Spend` -- the
    // copied text reads `$0.5183 of 4 reported`.
    expect(spend.textContent).toMatch(/^\$[\d.,]+ \d+ of \d+ steps? that ran reported$/)
  })
})

describe('83: one verb, Stop, for every stop on the page', () => {
  it('labels the head Stop workflow, the step card Stop and the running node Stop', async () => {
    await page(AUDIT)
    const head = document.querySelector<HTMLElement>('.wfp-head')!
    // MUTATION: put `Cancel workflow` back on the head's trigger.
    await waitFor(() => expect(within(head).getByRole('button', { name: 'Stop workflow' })).toBeTruthy())
    expect(within(head).queryByRole('button', { name: /cancel/i })).toBeNull()
    // MUTATION: drop `label="Stop"` from the node's `StopRun` -- the node says
    // lower-case `stop`.
    const stops = await waitFor(() => {
      const s = [...document.querySelectorAll<HTMLElement>('.node-stop button')]
      expect(s.length).toBeGreaterThan(0)
      return s
    })
    for (const b of stops) expect(b.textContent).toBe('Stop')
    // MUTATION: put `label="Stop step"` back on the card's `StopRun`.
    const card = document.querySelector<HTMLElement>('.wf-inspect-acts')
    if (card !== null) expect(flat(card.querySelector('button'))).toBe('Stop')
  })
})

describe('overview-agents-85: the stop dialog asks for the id, and its hint says id', () => {
  it('says the button unlocks when the id matches exactly, then that the id matches', async () => {
    await page(AUDIT)
    const head = document.querySelector<HTMLElement>('.wfp:not(.is-loading) .wfp-head')!
    fireEvent.click(await waitFor(() => within(head).getByRole('button', { name: 'Stop workflow' })))
    const dialog = await screen.findByRole('dialog')
    // MUTATION: drop `noun="id"` from CancelWorkflow's TypedConfirm -- the
    // hint asks for a "name" the reader is told nowhere to type (QA1C-FOLLOWUPS).
    expect(flat(dialog.querySelector('.c-hint'))).toBe('The button unlocks when the id matches exactly.')
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: AUDIT } })
    expect(flat(dialog.querySelector('.c-hint'))).toBe('The id matches.')
  })
})

describe('84: a recorded stop is said, and the head shows it pending', () => {
  it('keeps the API’s answer across the page’s re-read and says what was stopped', async () => {
    api.cancelWorkflow.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: { workflow_id: AUDIT, tasks_cancelled: ['task_wf_scan_a', 'task_wf_scan_b'], tasks_already_terminal: ['task_wf_plan'] },
    } satisfies Result<CancelWorkflowResult>)
    await page(AUDIT)
    const head = () => document.querySelector<HTMLElement>('.wfp:not(.is-loading) .wfp-head')!
    fireEvent.click(await waitFor(() => within(head()).getByRole('button', { name: 'Stop workflow' })))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: AUDIT } })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Stop the workflow' }))
    })
    expect(api.cancelWorkflow).toHaveBeenCalledWith(AUDIT)
    // The stop's own reload remounts the page; the answer is held above it.
    // MUTATION: drop `setSent`/`onSent` in `CancelWorkflow.send` -- the dialog
    // closes and the head is exactly as it was, `Stop workflow` and all.
    const said = await waitFor(
      () => {
        const s = head()?.querySelector<HTMLElement>('.wfp-cancel-sent')
        expect(s).toBeTruthy()
        return s!
      },
      { timeout: 4000 },
    )
    expect(said.getAttribute('role')).toBe('status')
    expect(flat(said)).toBe('Stop recorded: 2 steps asked to stop, 1 step had already ended.')
    expect(flat(head())).toContain('stop requested')
    expect(within(head()).queryByRole('button', { name: 'Stop workflow' }), 'the trigger is still offered after the stop was recorded').toBeNull()
  })

  it('says only that the stop was recorded when the API names no step', async () => {
    api.cancelWorkflow.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: { workflow_id: AUDIT, tasks_cancelled: [], tasks_already_terminal: [] },
    } satisfies Result<CancelWorkflowResult>)
    await page(AUDIT)
    const head = () => document.querySelector<HTMLElement>('.wfp:not(.is-loading) .wfp-head')!
    fireEvent.click(await waitFor(() => within(head()).getByRole('button', { name: 'Stop workflow' })))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: AUDIT } })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Stop the workflow' }))
    })
    // MUTATION: count `tasks_cancelled` whatever its length -- `0 steps asked to stop`.
    await waitFor(() => expect(flat(head()?.querySelector('.wfp-cancel-sent'))).toBe('Stop recorded.'), { timeout: 4000 })
  })
})

describe('89: the workflow’s time view is not a second Timeline', () => {
  it('names the tab Steps over time and keeps its address', async () => {
    await page(AUDIT)
    const tabs = document.querySelector<HTMLElement>('.wfp')!
    // MUTATION: use `VIEW_LABEL` for the page's tabs again -- `Timeline`, the
    // nav's platform-lanes screen's name.
    const tab = within(tabs).getByRole('link', { name: 'Steps over time' })
    expect(tab.getAttribute('href')).toMatch(/\/timeline/)
    expect(within(tabs).queryByRole('link', { name: 'Timeline' })).toBeNull()
  })
})

describe('73: the Graph tab draws the graph, the Table tab the table', () => {
  it('draws no steps table under the graph', async () => {
    await page(AUDIT)
    await waitFor(() => expect(document.querySelector('.wf-canvas')).toBeTruthy())
    // MUTATION: put the `.wfp-steps` `WorkflowSteps` back under the graph.
    expect(document.querySelector('.wfp-steps')).toBeNull()
    expect(document.querySelector('.wfp table')).toBeNull()
  })
})

describe('64: a step that reads many files says how many', () => {
  it('draws `↑ 5 steps · 5 files` on the synthesis node, the whole list its title', async () => {
    await page(SYNTH)
    const dep = await waitFor(() => {
      const d = document.querySelector<HTMLElement>('.node-slot:has(button.node[data-step="synthesis"]) .node-dep')
      expect(d).toBeTruthy()
      return d!
    })
    // MUTATION: drop the `DEP_FILES_LISTED` arm in `StepNode` -- the node
    // names all five parents and their files, ~260px of card.
    expect(flat(dep)).toBe('↑ 5 steps · 5 files')
    expect(dep.getAttribute('title')).toMatch(/fencing \(fencing\.md\),/)
    expect(dep.getAttribute('aria-label')).toBe(dep.getAttribute('title'))
  })

  it('keeps naming the file of a step that reads one', async () => {
    await page(AUDIT)
    const deps = await waitFor(() => {
      const d = [...document.querySelectorAll<HTMLElement>('.node-dep')]
      expect(d.length).toBeGreaterThan(0)
      return d
    })
    for (const d of deps) expect(d.classList.contains('is-count')).toBe(false)
  })
})

describe('63: the graph brings the picked step round without cutting its neighbours', () => {
  it('opens a 595px canvas on whole cards, never mid-card at its left edge', async () => {
    // The 1440 page with the step card open: the ~595px wrapper of the report, the 72px rail.
    keepScroll()
    stubBox('clientWidth', (el) => (el.classList.contains('wf-canvas-wrap') ? 595 : 0))
    stubBox('scrollWidth', (el) =>
      el.classList.contains('wf-canvas-wrap') ? 72 + Number.parseFloat(el.querySelector<HTMLElement>('.wf-canvas')?.style.width ?? '0') : 0,
    )
    stubBox('offsetWidth', (el) => (el.classList.contains('wf-levels') ? 72 : 0))
    await page(SYNTH)
    // THE READER BRINGS IT ROUND (visual QA pass 2, overview-agents-63,
    // #2694): the page's own pick on arrival opens no stage and scrolls
    // nothing, so the scroll measured here is the one the failure line's
    // control makes.
    fireEvent.click(await waitFor(() => document.querySelector<HTMLButtonElement>('.wfp-fail-lead-go')!))
    const wrap = await waitFor(() => {
      const w = document.querySelector<HTMLElement>('.wf-canvas-wrap')
      expect(w?.querySelector('.node.is-picked')).toBeTruthy()
      return w!
    })
    const left = wrap.scrollLeft
    const slots = [...wrap.querySelectorAll<HTMLElement>('.node-slot')]
    expect(slots.length).toBeGreaterThan(0)
    // MUTATION: centre the picked stage again in the arrival scroll -- a card
    // straddles the canvas's left edge (`encing`).
    for (const s of slots) {
      const x = Number.parseFloat(s.style.left)
      const w = Number.parseFloat(s.style.width)
      const cut = x < left && left < x + w
      expect(cut, `${s.querySelector('[data-step]')?.getAttribute('data-step')} is cut at the canvas's left edge (scrollLeft ${left})`).toBe(false)
    }
    // And the picked card is whole in view.
    const picked = slots.find((s) => s.querySelector('.node.is-picked'))!
    expect(72 + Number.parseFloat(picked.style.left) + Number.parseFloat(picked.style.width) - left).toBeLessThanOrEqual(595)
  })
})
