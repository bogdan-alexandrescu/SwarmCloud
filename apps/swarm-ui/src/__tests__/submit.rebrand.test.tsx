// THE SUBMIT FORMS AS DECIDED ON 2026-10-01 (submit.html, #118, #119).
//
// Three claims, each held against the rendered form and the development
// fixture rather than against the source:
//
//   * no runner is preselected on either form, and an untouched form cannot
//     send -- the task form even refuses a submit that bypasses its button;
//   * "+ Add setting ▾" is ONE control, and opening it and picking a key adds
//     that key's setting to the form;
//   * a workflow's stages run top to bottom with a drawn arrow between each
//     pair, and a stage's "+ Add a step to this stage" tile adds a step to
//     THAT stage and to no other.

import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

import { SubmitScreen } from '../Submit'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'

const WAIT = { timeout: 4000 }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('no runner is preselected, and an untouched form cannot send', () => {
  it('the task form: nothing chosen, the button disabled, and a submit is refused until a runner is picked', async () => {
    const { container } = render(<SubmitScreen />)
    const go = await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    const radios = [...container.querySelectorAll<HTMLInputElement>('input[name="runner-profile"]')]
    expect(radios.length, 'the picker drew no runners').toBeGreaterThan(1)
    expect(radios.filter((r) => r.checked), 'a runner is preselected').toEqual([])
    expect(go.hasAttribute('disabled')).toBe(true)
    expect(visible(container.querySelector('.sbf-send h2'))).toBe('Not ready to send')

    // A submit that does not come from the button -- Enter in a field, a
    // script -- is refused the same way, and nothing is created.
    fireEvent.submit(container.querySelector('form.sbf')!)
    const alert = await screen.findByText('Not sent — no runner chosen.')
    expect(alert.getAttribute('role')).toBe('alert')
    expect(container.querySelector('.state[role="status"]'), 'a task was created with no runner').toBeNull()

    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    expect(go.hasAttribute('disabled')).toBe(false)
    expect(visible(container.querySelector('.sbf-send h2'))).toBe('Ready to send')
  })

  it('the workflow form: the first step has no runner, the plan is not ready, and the panel names the step', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    const go = await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const step = container.querySelector<HTMLElement>('.wfb-step')!
    const radios = [...step.querySelectorAll<HTMLInputElement>('input[type="radio"]')]
    expect(radios.length).toBeGreaterThan(1)
    expect(radios.filter((r) => r.checked), 'the first step starts on a runner').toEqual([])
    expect(visible(step)).toContain('no runner chosen')
    expect(go.hasAttribute('disabled')).toBe(true)
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    expect(visible(panel.querySelector('h2'))).toBe('Not ready to send')
    expect(within(panel).getByRole('button', { name: 'step-1' }).getAttribute('title')).toBe('no runner chosen')

    // A step added later starts empty too, rather than copying a default.
    fireEvent.click(container.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!)
    const second = container.querySelectorAll<HTMLElement>('.wfb-step')[1]!
    expect([...second.querySelectorAll<HTMLInputElement>('input[type="radio"]')].filter((r) => r.checked)).toEqual([])

    for (const s of [...container.querySelectorAll<HTMLElement>('.wfb-step')]) {
      fireEvent.click(s.querySelector<HTMLInputElement>('input[type="radio"][value="mock"]')!)
    }
    expect(go.hasAttribute('disabled')).toBe(false)
    expect(visible(panel.querySelector('h2'))).toBe('Ready to send')
  })
})

describe('"+ Add setting ▾" is one menu, and it adds a setting', () => {
  it('opens on click, lists the runner keys with their notes, and adds the one picked', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)

    // ONE control, not a tile per key.
    const adds = screen.getAllByRole('button', { name: /Add setting/ })
    expect(adds).toHaveLength(1)
    const add = adds[0]!
    expect(add.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('group', { name: 'settings to add' })).toBeNull()

    fireEvent.click(add)
    expect(add.getAttribute('aria-expanded')).toBe('true')
    const menu = screen.getByRole('group', { name: 'settings to add' })
    const item = within(menu).getByRole('button', { name: /^sleep_seconds/ })
    // The runner's own note travels with the key.
    expect(visible(item)).toContain('interruptible sleep')
    expect(within(menu).getByRole('button', { name: /a setting of your own/ })).toBeTruthy()

    const before = container.querySelectorAll('.sbf-input .sbf-field').length
    fireEvent.click(item)
    const fields = [...container.querySelectorAll('.sbf-input .sbf-field')]
    expect(fields).toHaveLength(before + 1)
    expect(visible(fields.at(-1)!.querySelector('.sbf-name'))).toBe('sleep_seconds')
    // Picking closes it, and a key already on the form is not offered again.
    expect(screen.queryByRole('group', { name: 'settings to add' })).toBeNull()
    fireEvent.click(add)
    expect(within(screen.getByRole('group', { name: 'settings to add' })).queryByRole('button', { name: /^sleep_seconds/ })).toBeNull()
  })

  it('adds a setting of your own, and Escape closes the menu', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    const add = screen.getByRole('button', { name: /Add setting/ })
    fireEvent.click(add)
    fireEvent.click(within(screen.getByRole('group', { name: 'settings to add' })).getByRole('button', { name: /a setting of your own/ }))
    expect(screen.getByRole('textbox', { name: 'setting name' })).toBeTruthy()

    fireEvent.click(add)
    const menu = screen.getByRole('group', { name: 'settings to add' })
    fireEvent.keyDown(menu, { key: 'Escape' })
    expect(screen.queryByRole('group', { name: 'settings to add' })).toBeNull()
    expect(document.activeElement).toBe(add)
  })

  it('is on every workflow step once it has a runner', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const step = container.querySelector<HTMLElement>('.wfb-step')!
    expect(within(step).queryByRole('button', { name: /Add setting/ }), 'offered before a runner is chosen').toBeNull()
    fireEvent.click(step.querySelector<HTMLInputElement>('input[type="radio"][value="browser"]')!)
    fireEvent.click(within(step).getByRole('button', { name: /Add setting/ }))
    fireEvent.click(within(within(step).getByRole('group', { name: 'settings to add' })).getByRole('button', { name: /^url/ }))
    expect([...step.querySelectorAll('.sbf-field .sbf-name')].map(visible)).toContain('url')
  })
})

describe('stages run top to bottom, with arrows, and a tile adds a step to its own stage', () => {
  it('draws one arrow between each pair of stages, and no stray tick', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    expect(container.querySelectorAll('.sbf-flow')).toHaveLength(0)
    const addStage = container.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!
    fireEvent.click(addStage)
    fireEvent.click(addStage)
    const stages = [...container.querySelectorAll<HTMLElement>('.wfb-stage')]
    expect(stages).toHaveLength(3)
    const arrows = [...container.querySelectorAll<HTMLElement>('.sbf-flow')]
    expect(arrows).toHaveLength(2)
    // Each arrow sits BETWEEN two stages, in order, and is a drawn shape.
    for (const [i, arrow] of arrows.entries()) {
      expect(arrow.previousElementSibling).toBe(stages[i])
      expect(arrow.nextElementSibling).toBe(stages[i + 1])
      expect(arrow.querySelector('svg path')).not.toBeNull()
    }
    // The old connector tick was `.wfb-stage + .wfb-stage::before`; no stage
    // follows another directly any more.
    expect(container.querySelector('.wfb-stage + .wfb-stage')).toBeNull()
  })

  it('"+ Add a step to this stage" adds a step to that stage and leaves the others alone', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    fireEvent.click(container.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!)
    const stages = () => [...container.querySelectorAll<HTMLElement>('.wfb-stage')]
    const counts = () => stages().map((s) => s.querySelectorAll('.wfb-step').length)
    expect(counts()).toEqual([1, 1])

    const tile = within(stages()[0]!).getByRole('button', { name: '+ Add a step to this stage' })
    // The tile is the last thing in the stage's row.
    expect(tile.parentElement!.lastElementChild).toBe(tile)
    fireEvent.click(tile)
    expect(counts()).toEqual([2, 1])

    fireEvent.click(within(stages()[1]!).getByRole('button', { name: '+ Add a step to this stage' }))
    expect(counts()).toEqual([2, 2])
    expect(visible(stages()[0]!.querySelector('.wfb-stage-say'))).toBe('2 steps start together')
    expect(visible(container.querySelector('.sbf-send'))).toContain('2 · 2 steps, then 2 steps')
  })
})
