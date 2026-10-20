/**
 * SUBMIT A TASK, AS THE FIRST CONSOLE VISUAL-QA RUN READ IT (QAFIX-SUBMIT-TASK,
 * part of #1680; run wf_9e9bf74834634db7bcc3, 2026-10-20). One case per
 * finding a unit test can hold, each by the property that was false:
 *
 *   QA-075  the dispatch-strategy radios are painted as the runner radios
 *           are -- a ring, not the platform's native disc, which dark mode
 *           drew as a filled grey dot.
 *   QA-101  a form that cannot be sent says, beside its disabled button,
 *           what is missing.
 *   QA-102  a setting row that will not be sent says so, at the row and in
 *           the send panel.
 *   QA-104  the value box of a setting of one's own has an accessible name.
 *   QA-107  on a phone a setting's "remove" no longer sits beside the name
 *           box with the type select wrapped under it: it drops to its own
 *           line, after the value, in source order.
 *   QA-108  a runner with no room left: the panel says the task will wait.
 *   QAFIX-HELP  the `?` of "Choose a runner" sits after its <h2>, so the
 *           heading is named by its words alone.
 *
 * QA-103 is not held here: see the PR body (TS-15 is the owner's decision).
 */
import { afterEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

import { InputFields, SubmitScreen, type InputField, type ValueKind } from '../Submit'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

let key = 900
function f(over: Partial<InputField> = {}): InputField {
  return {
    key: key++, name: '', kind: 'text' as ValueKind, text: '', flag: false,
    list: [''], actions: [], required: false, own: false, ...over,
  }
}

async function form() {
  const view = render(<SubmitScreen />)
  const button = await screen.findByRole('button', { name: 'Submit one task' }, { timeout: 4000 })
  return { ...view, button }
}

function pickRunner(container: HTMLElement, name: string) {
  fireEvent.click(container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${name}"]`)!)
}

/** A native radio inside the leaf `selector` names. */
function radioIn(selector: string, checked: boolean): HTMLInputElement {
  const leaf = build(selector, hosts)
  const input = document.createElement('input')
  input.type = 'radio'
  input.checked = checked
  leaf.appendChild(input)
  return input
}

describe('QA-075: the dispatch-strategy radios are drawn as the runner radios are', () => {
  const PROPS = ['appearance', 'width', 'height', 'border', 'border-radius', 'background'] as const

  for (const theme of ['dark', 'light'] as const) {
    for (const checked of [false, true]) {
      it(`${theme}, ${checked ? 'chosen' : 'not chosen'}: the same ring, never the native disc`, () => {
        const env: CascadeEnv = { ...WIDE, theme }
        const runner = radioIn('div.sbf-runners.is-cards > label.sbf-runner', checked)
        const strategy = radioIn('div.dsp-options > label.dsp-option', checked)
        // MUTATION: delete the `.dsp-option > input[type='radio']` rule in
        // submit.css and `appearance` falls back to the platform's (null), so
        // dark mode draws its filled grey disc again.
        expect(painted(strategy, 'appearance', env)).toBe('none')
        for (const p of PROPS) {
          // MUTATION: paint the strategy ring in another token (or drop its
          // `:checked` border) and this property parts from the runner's.
          expect(painted(strategy, p, env), `${p} differs from the runner radio`).toBe(painted(runner, p, env))
        }
      })
    }
  }
})

describe('QA-101: a form that cannot be sent says what is missing', () => {
  it('an untouched form names the runner it is waiting for, as the button\'s description', async () => {
    const { button } = await form()
    expect((button as HTMLButtonElement).disabled).toBe(true)
    // MUTATION: drop the `sbf-why` line and the disabled button is described
    // by nothing, which is exactly what the QA run read.
    expect(screen.getByRole('button', { name: 'Submit one task', description: 'Not ready: choose a runner.' })).toBe(button)
  })

  it('a runner chosen without its required key names the key', async () => {
    const { container, button } = await form()
    pickRunner(container, 'claude-code')
    // MUTATION: leave `missing` out of the reasons and this reads only the
    // runner's absence -- or nothing, once one is chosen.
    expect(screen.getByRole('button', { name: 'Submit one task', description: 'Not ready: fill in prompt.' })).toBe(button)
  })

  it('a form that can be sent carries no such line', async () => {
    const { container } = await form()
    pickRunner(container, 'mock')
    // MUTATION: render the line unconditionally and a ready form says it is not.
    expect(container.querySelector('.sbf-why')).toBeNull()
  })
})

describe('QA-102: a setting that will not be sent says so', () => {
  it('a row of one\'s own with no name says, at the row, that it is not sent', () => {
    const { container } = render(
      <InputFields profile="mock" required={[]} idPrefix="t" onChange={() => {}} fields={[f({ own: true, text: 'x' })]} />,
    )
    // MUTATION: drop the row's `sbf-unsent` line and the blank row looks like
    // any other setting.
    expect(container.querySelector('.sbf-unsent')?.textContent).toBe('no name — not sent')
  })

  it('the send panel names the blank row it will leave out', async () => {
    const { container } = await form()
    pickRunner(container, 'mock')
    fireEvent.click(screen.getByRole('button', { name: /Add setting/ }))
    fireEvent.click(within(screen.getByRole('group', { name: 'settings to add' })).getByRole('button', { name: /a setting of your own/ }))
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    const fact = [...panel.querySelectorAll('.ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'not sent')
    // MUTATION: drop the panel's `not sent` fact and the panel says "Ready to
    // send" over a row it silently leaves out.
    expect(fact, 'no "not sent" fact in the send panel').toBeDefined()
    expect(fact!.textContent).toBe('not senta setting with no name')
  })
})

describe('QA-104: the value of a setting of one\'s own has an accessible name', () => {
  it('named by its setting, or as "setting value" while it has none', () => {
    render(
      <InputFields profile="mock" required={[]} idPrefix="t" onChange={() => {}}
        fields={[f({ own: true, name: 'depth' }), f({ own: true }), f({ own: true, name: 'n', kind: 'number' })]} />,
    )
    // MUTATION: drop the `aria-label` ValueEditor takes for an own row and
    // these boxes are a bare "textbox" again.
    expect(screen.getByRole('textbox', { name: 'depth value' })).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'setting value' })).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'n value' })).toBeTruthy()
  })
})

describe('QA-107: on a phone a setting\'s remove follows its value, in source order', () => {
  it('row 1 beside the name on a desktop; its own line on a phone', () => {
    const remove = build('div.sbf-field.has-remove > button.sbf-remove', hosts)
    // The desktop placement the live QA SUB-17 decided is unchanged.
    expect(painted(remove, 'grid-row', WIDE)).toBe('1')
    // MUTATION: drop the phone block in submit.css and `remove` stays on row 1
    // beside the name box, with the type select wrapped under both.
    expect(painted(remove, 'grid-row', PHONE)).toBe('auto')
    expect(painted(remove, 'grid-column', PHONE)).toBe('1 / -1')
  })
})

describe('QA-108: no room left reads as a task that will wait', () => {
  it('claude-code at room 0: "lands as" says it waits for room', async () => {
    const { container } = await form()
    pickRunner(container, 'claude-code')
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    const lands = [...panel.querySelectorAll('.ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'lands as')!
    // MUTATION: drop the room-0 clause and the panel reads "Ready to send"
    // with nothing saying the task will not start yet.
    expect(lands.textContent).toBe('lands asREADY or PARKED · waits for room · costs nothing')
  })

  it('a runner with room keeps the plain line', async () => {
    const { container } = await form()
    pickRunner(container, 'mock')
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    const lands = [...panel.querySelectorAll('.ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'lands as')!
    // MUTATION: say "waits" unconditionally and a runner with room is told it waits.
    expect(lands.textContent).toBe('lands asREADY or PARKED · costs nothing')
  })
})

describe('QAFIX-HELP: the `?` of "Choose a runner" is after its heading, not in it', () => {
  it('the h2 is named exactly "Choose a runner", and its `?` follows it', async () => {
    await form()
    // MUTATION: put the aside back inside the <h2> and its name becomes
    // "Choose a runner Help: …", which this exact query does not find.
    const heading = screen.getByRole('heading', { level: 2, name: 'Choose a runner' })
    const glyph = heading.nextElementSibling?.querySelector('button[aria-label^="Help: "]')
      ?? (heading.nextElementSibling?.matches('button[aria-label^="Help: "]') ? heading.nextElementSibling : null)
    expect(glyph, 'no `?` after the heading').not.toBeNull()
  })
})
