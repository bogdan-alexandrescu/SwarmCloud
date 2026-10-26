/**
 * VISUAL QA RUN 3, LANE QAFIX3-SUBMIT (wf_d598fa7bffdb41fc9d76, 2026-10-21,
 * the fixture build of main at 51d99c53, 1440 and 390 px, light and dark;
 * part of #1680).
 *
 *   work-b-006  the runner picker's unread-room line ended on a dangling
 *               " ·": QA-069 (PR 1710) made "7 of 7 pools could not be read"
 *               one unbroken unit, so the line broke right after the dot
 *               before it, at every size.
 *   work-b-004  at 390 the issue form's step-3 rows stacked their control
 *               ABOVE the heading that names it (`.in-choice` turned into a
 *               column in DOM order: control, then text).
 *   sf-002      an invalid repository URL had `aria-invalid` and nothing that
 *               painted it, and its message under the field could sit below
 *               the fold while the field was in view.
 *
 * Layout and colour are read off the shipped sheets through `painted()`, as
 * access.page.test.tsx does; names and descriptions through Testing Library;
 * copy off the rendered text.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement, useState } from 'react'
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'

import { DispatchChoice, type DispatchDraft } from '../Dispatch'
import { RunnerPicker, type ProviderKeys } from '../RunnerPicker'
import type { ProfileAdmission, RunnerProfile } from '../types'
import type { CascadeEnv } from './cssgate'
import { THEMES, painted } from './marks'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  document.body.innerHTML = ''
})

// ---------------------------------------------------------------------------
// work-b-006
// ---------------------------------------------------------------------------

function admission(over: Partial<ProfileAdmission> = {}): ProfileAdmission {
  return {
    units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

const POOLS = ['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7']
const UNREAD: RunnerProfile = {
  resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1, pools: POOLS,
  admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: POOLS }),
}
const READ: ProviderKeys = { kind: 'read', registered: new Map([['anthropic', true]]) }

describe('work-b-006: the unread-room line never ends on a dangling separator', () => {
  it('puts no " ·" at the one place the line breaks, before the unbroken count', () => {
    const { container } = render(
      <RunnerPicker group="t" label="runner" profiles={[['claude-code-browser', UNREAD]]} chosen="" keys={READ} onPick={() => {}} />,
    )
    const room = container.querySelector('.sbf-runner-room')!
    const clause = room.querySelector('.sb-room-unread')!
    // Precondition: QA-069's unit is still unbroken, so the line can only
    // break before it -- right after whatever text precedes it.
    expect(painted(clause, 'white-space', WIDE)).toBe('nowrap')
    expect(visible(clause)).toBe('7 of 7 pools could not be read')
    const before = [...room.childNodes].slice(0, [...room.childNodes].indexOf(clause)).map((n) => n.textContent ?? '').join('')
    // MUTATION: put `room unknown ·{' '}` back in `UnreadRoom` -> the line breaks after a bare "·".
    expect(before.trimEnd(), 'the text the line breaks after ends on a separator dot').not.toMatch(/·$/)
    // The words themselves are kept: the mark's reading, then the count.
    // MUTATION: drop the separator altogether -> "room unknown 7 of 7 ..." runs the two together.
    expect(visible(room)).toBe('not read room unknown: 7 of 7 pools could not be read')
  })
})

// ---------------------------------------------------------------------------
// work-b-004
// ---------------------------------------------------------------------------

async function issueForm() {
  const { IssueSubmitScreen } = await import('../IssueSubmit')
  const utils = render(<IssueSubmitScreen go={() => {}} />)
  await screen.findByRole('heading', { name: 'Submit from a GitHub issue', level: 1 }, WAIT)
  await screen.findByRole('radiogroup', { name: 'runner' }, WAIT)
  return utils.container as HTMLElement
}

/**
 * The row's boxes in the order a column draws them: an element whose own
 * `display` is `contents` gives its children to the row, and boxes sort by
 * `order`, then by source position.
 */
function drawnOrder(row: Element, env: CascadeEnv): Element[] {
  const boxes: Element[] = []
  const walk = (parent: Element) => {
    for (const child of parent.children) {
      if (painted(child, 'display', env) === 'contents') walk(child)
      else boxes.push(child)
    }
  }
  walk(row)
  const order = (el: Element) => Number(painted(el, 'order', env) ?? '0')
  return boxes.map((el, i) => ({ el, i })).sort((a, b) => order(a.el) - order(b.el) || a.i - b.i).map((x) => x.el)
}

describe('work-b-004: at 390 each issue-form choice is headed before its control', () => {
  it('draws "Plan approval", the merge switch\'s and the fix-rounds heading above their controls', async () => {
    const container = await issueForm()
    const rows = [...container.querySelectorAll('.in-choice')]
    // Precondition: the three rows of step 3, each a heading and a control.
    expect(rows.map((r) => visible(r.querySelector('.in-choice-t b')))).toEqual([
      'Plan approval', 'Merge the pull request when it is ready', 'Fix rounds when checks go red',
    ])
    // Precondition: at 390 the row is a column, so its order is read top to bottom.
    expect(painted(rows[0]!, 'flex-direction', PHONE)).toBe('column')
    const controls = [
      screen.getByRole('radiogroup', { name: 'Plan approval' }),
      screen.getByRole('switch', { name: 'Merge the pull request when it is ready' }),
      screen.getByRole('spinbutton', { name: 'Fix rounds when checks go red' }),
    ]
    rows.forEach((row, i) => {
      const drawn = drawnOrder(row, PHONE)
      const at = (el: Element) => drawn.findIndex((box) => box === el || box.contains(el))
      const heading = row.querySelector('.in-choice-t b')!
      const control = controls[i]!
      expect(at(heading), `row ${i}: the heading is not drawn`).toBeGreaterThanOrEqual(0)
      expect(at(control), `row ${i}: the control is not drawn`).toBeGreaterThanOrEqual(0)
      // MUTATION: drop the phone rule's `display: contents` / `order: -1` -> the control is drawn first, its heading under it.
      expect(at(heading), `row ${i}: the control sits above its own heading`).toBeLessThan(at(control))
    })
    // The desktop row is unchanged: control beside its text, in a row.
    // MUTATION: apply the phone rule at every width -> the 1440 rows stack too.
    expect(painted(rows[0]!, 'flex-direction', WIDE) ?? 'row').toBe('row')
    expect(painted(rows[0]!.querySelector('.in-choice-t')!, 'display', WIDE)).toBe('flex')
  })
})

// ---------------------------------------------------------------------------
// sf-002
// ---------------------------------------------------------------------------

function repoField(repositoryUrl: string): HTMLInputElement {
  render(
    createElement(DispatchChoice, {
      draft: { strategy: 'direct-pr', carrier: 'checkpoints', repositoryUrl },
      onChange: () => {},
      steps: 1,
      scale: 'task',
    }),
  )
  return screen.getByLabelText(/repository url/i) as HTMLInputElement
}

describe('sf-002: an invalid repository URL is drawn as invalid and its message is in view', () => {
  for (const theme of THEMES) {
    for (const [w, width] of [['1440', 1440], ['390', 390]] as const) {
      it(`edges the refused field in the warn hue, ${theme} at ${w}`, () => {
        const field = repoField('not a url')
        expect(field.getAttribute('aria-invalid')).toBe('true')
        const env: CascadeEnv = { width, theme }
        // MUTATION: drop `input.dsp-repo[aria-invalid='true']` from submit.css -> the `--ctl-bd` edge of a valid field.
        expect(painted(field, ['border-color', 'border'], env), 'the refused field keeps the plain edge').toContain('var(--warn)')
        // MUTATION: drop the rule's inset ring -> a 1px warn edge, no heavier than the field's own.
        expect(painted(field, 'box-shadow', env), 'the refused field has no ring').toContain('var(--warn)')
      })
    }
  }

  it('leaves a valid field on the plain edge', () => {
    const field = repoField('https://github.com/o/r.git')
    expect(field.getAttribute('aria-invalid')).toBeNull()
    // MUTATION: drop the `[aria-invalid='true']` from the rule -> every field is edged in warn.
    expect(painted(field, ['border-color', 'border'], WIDE)).not.toContain('var(--warn)')
  })

  it('names its message as the field\'s description', () => {
    const field = repoField('not a url')
    const ids = (field.getAttribute('aria-describedby') ?? '').split(/\s+/).filter((s) => s !== '')
    // MUTATION: drop `aria-describedby` from `#dsp-repo` -> a screen reader hears "invalid" and no reason.
    expect(ids.map((id) => visible(document.getElementById(id))).join(' ')).toMatch(/^The API accepts only a URL starting with https:\/\//)
    // One field on the page at a time: both are `#dsp-repo`.
    cleanup()
    expect(repoField('https://github.com/o/r.git').getAttribute('aria-describedby')).toBeNull()
  })

  it('brings the message into view when the field turns invalid, and not on every keystroke', () => {
    const seen: Element[] = []
    const scroll = vi.fn(function (this: Element, _arg?: ScrollIntoViewOptions) { seen.push(this) })
    // jsdom draws no layout and has no `scrollIntoView` to spy on; put back what was there.
    const was = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, writable: true, value: scroll })
    onTestFinished(() => {
      if (was === undefined) delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
      else Object.defineProperty(Element.prototype, 'scrollIntoView', was)
    })
    function Form() {
      const [draft, setDraft] = useState<DispatchDraft>({ strategy: 'direct-pr', carrier: 'checkpoints', repositoryUrl: '' })
      return <DispatchChoice draft={draft} onChange={setDraft} steps={1} scale="task" />
    }
    render(<Form />)
    const field = screen.getByLabelText(/repository url/i) as HTMLInputElement
    expect(scroll).not.toHaveBeenCalled()
    fireEvent.change(field, { target: { value: 'not a url' } })
    // MUTATION: drop the effect that scrolls the scheme warning -> it can stay below the fold under a field in view.
    expect(scroll).toHaveBeenCalledTimes(1)
    expect(visible(seen[0]!)).toMatch(/^The API accepts only a URL starting with/)
    expect(scroll.mock.calls[0]![0]).toEqual({ block: 'nearest' })
    // Still invalid: the page does not move again under the typist.
    fireEvent.change(field, { target: { value: 'not a url either' } })
    // MUTATION: scroll on every change rather than on the turn to invalid -> the page jumps per keystroke.
    expect(scroll).toHaveBeenCalledTimes(1)
    fireEvent.change(field, { target: { value: 'https://github.com/o/r.git' } })
    fireEvent.change(field, { target: { value: 'x' } })
    expect(scroll).toHaveBeenCalledTimes(2)
  })
})
