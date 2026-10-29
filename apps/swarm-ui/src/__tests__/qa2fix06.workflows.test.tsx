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
  await waitFor(() => expect(document.querySelector(`.wfl-table tr[data-workflow="${AUDIT}"]`)).toBeTruthy(), { timeout: 4000 })
}

const row = (id: string) => document.querySelector<HTMLElement>(`.wfl-table tr[data-workflow="${id}"]`)!

async function page(id: string): Promise<void> {
  render(<WorkflowsScreen view={`wf=${id}`} onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy(), { timeout: 4000 })
}

async function css(): Promise<string> {
  return (await import('../styles/workflows.css?raw')).default
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
      const c = row(AUDIT).querySelector<HTMLElement>('td[data-col="cost"] .wf-spend-cov')
      expect(c).toBeTruthy()
      return c!
    })
    // MUTATION: print `steps that ran reported` in the short form again.
    expect(flat(cov)).toMatch(/^\d+ of \d+ reported$/)
    expect(cov.getAttribute('title')).toMatch(/^\d+ of \d+ steps? that ran reported a cost/)
    // The figure and the note keep their space (overview-agents-82).
    expect(flat(row(AUDIT).querySelector('td[data-col="cost"] .wf-spend'))).toMatch(/^\$[\d.,]+ \d+ of \d+ reported$/)
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
    const cell = row(STORED).querySelector<HTMLElement>('td[data-col="duration"]')!
    // MUTATION: drop the unknown-state arm in `WorkflowListRow` -- `9d 22h so far`.
    expect(flat(cell)).not.toMatch(/so far/)
    expect(flat(cell)).toMatch(/^submitted \S.* ago$/)
    expect(cell.className).toMatch(/is-absent/)
    expect(cell.getAttribute('title')).toMatch(/state is unknown/)
  })
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
    expect(flat(note)).toMatch(/not measured: synthesis never ran$/)
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
