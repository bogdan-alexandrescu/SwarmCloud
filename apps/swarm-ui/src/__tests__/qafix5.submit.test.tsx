/**
 * QAFIX5-SUBMIT, WORK › START AN AGENT (part of #1680, visual QA run 5b,
 * wf_025ae735511546d99eff, the fixture build of main at 58cd0c1e, 2026-10-22,
 * 1440 and 390 px, light and dark):
 *
 *   QA-103 (#1680 comment 6283734173, run-5b sf-desktop-01, sf-phone-01)
 *           after choosing claude-code at 1440 the missing prompt was named
 *           only in the send panel ("prompt missing", "Not ready: fill in
 *           prompt."); the field itself showed just a `required` tag, while
 *           at 390 the reason was drawn inline under step 2.
 *   r4:flow-submit-form-phone-390-02 (#1680 comment 6329553459, run-5b
 *           sf-phone-01)  at 390 the send summary, the only place the empty
 *           form said why Start was disabled, was the last block on the page,
 *           below every step and below the fold.
 *
 * Every assertion names the mutation that turns it red.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SubmitScreen } from '../Submit'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

async function form() {
  const view = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  return view.container as HTMLElement
}

function pick(container: HTMLElement, name: string) {
  const r = container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${name}"]`)
  if (r === null) throw new Error(`no runner ${name} on the form`)
  fireEvent.click(r)
}

// ---------------------------------------------------------------------------
// QA-103
// ---------------------------------------------------------------------------

describe('QA-103: the prompt field says it is missing where it is, at both widths', () => {
  it('names the missing prompt under its own field as soon as claude-code is chosen', async () => {
    const container = await form()
    pick(container, 'claude-code')
    const field = container.querySelector<HTMLElement>('.sbf-field.is-required')!
    const editor = field.querySelector('textarea')!
    const line = field.querySelector<HTMLElement>('.sbf-unfilled')
    // MUTATION: drop the unfilled line -> the field shows only its `required` tag, as run 5b saw at 1440.
    expect(line, 'the field does not say the prompt is missing').not.toBeNull()
    // MUTATION: word it without the key -> the reader cannot tell which field the panel's "prompt missing" means.
    expect(visible(line)).toBe('prompt missing')
    // MUTATION: drop the editor's describedby -> a screen reader on the field never hears it.
    expect(editor.getAttribute('aria-describedby')).toBe(line!.id)
    // MUTATION: hide the line from 1080 like `.sbf-why-at` -> the desktop is back to the panel alone.
    expect(painted(line!, 'display', WIDE) ?? 'block').not.toBe('none')
    expect(painted(line!, 'display', PHONE) ?? 'block').not.toBe('none')
  })

  it('does not paint a field the reader has not reached yet in the warning ink', async () => {
    const container = await form()
    pick(container, 'claude-code')
    const field = container.querySelector<HTMLElement>('.sbf-field.is-required')!
    const line = field.querySelector<HTMLElement>('.sbf-unfilled')!
    // TS-15's owner decision stands: a runner just picked is not painted red.
    // MUTATION: draw the unfilled line as `.sbf-miss` -> the freshly picked field reads as an error.
    expect(field.querySelector('textarea')!.getAttribute('aria-invalid')).toBeNull()
    expect(field.querySelector('.sbf-miss')).toBeNull()
    // MUTATION: colour `.sbf-unfilled` with `--warn-ink` -> red before the reader has touched it.
    expect(painted(line, 'color', WIDE)).toBe('var(--text-dim)')
  })

  it('hands over to the warning line once the field is left empty, and says it once', async () => {
    const container = await form()
    pick(container, 'claude-code')
    const field = container.querySelector<HTMLElement>('.sbf-field.is-required')!
    fireEvent.blur(field.querySelector('textarea')!)
    // MUTATION: keep the unfilled line after the blur -> the field says it is missing twice.
    expect(field.querySelector('.sbf-unfilled')).toBeNull()
    expect(field.querySelectorAll('.sbf-miss')).toHaveLength(1)
  })

  it('says nothing once the prompt has text', async () => {
    const container = await form()
    pick(container, 'claude-code')
    const field = container.querySelector<HTMLElement>('.sbf-field.is-required')!
    fireEvent.change(field.querySelector('textarea')!, { target: { value: 'do the thing' } })
    // MUTATION: draw the line for every required field -> a filled prompt still reads "prompt missing".
    expect(field.querySelector('.sbf-unfilled')).toBeNull()
    expect(field.querySelector('textarea')!.getAttribute('aria-describedby')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// r4:flow-submit-form-phone-390-02
// ---------------------------------------------------------------------------

describe('r4:flow-submit-form-phone-390-02: below 1080 the reason Start is disabled stays in view', () => {
  it('docks the empty form\'s reason at the foot of the steps, not after the send panel', async () => {
    const container = await form()
    const dock = container.querySelector<HTMLElement>('.sbf-dock')
    // MUTATION: drop the dock -> at 390 the reason is only in the send panel, below every step.
    expect(dock, 'no reason kept in view of the steps').not.toBeNull()
    expect(visible(dock)).toBe('Not ready: choose a runner.')
    // In the steps' column, after the last step: sticky there, it rides the foot of the screen while the steps scroll.
    // MUTATION: render the dock after the send panel -> it is as far down as the summary was.
    expect(dock!.parentElement?.classList.contains('sbf-build')).toBe(true)
    expect(dock!.previousElementSibling?.classList.contains('sb-step')).toBe(true)
    expect(dock!.nextElementSibling).toBeNull()
  })

  it('sticks to the foot of the screen at 390, opaque, and is not drawn from 1080', async () => {
    const container = await form()
    const dock = container.querySelector<HTMLElement>('.sbf-dock')!
    // MUTATION: drop `position: sticky` -> the dock scrolls away with step 3.
    expect(painted(dock, 'position', PHONE)).toBe('sticky')
    // MUTATION: drop `bottom: 0` -> sticky with no inset never sticks.
    expect(painted(dock, 'bottom', PHONE)).toBe('0')
    // MUTATION: drop the background -> the steps scroll through the words behind it.
    expect(painted(dock, ['background', 'background-color'], PHONE)).toBe('var(--bg)')
    // From 1080 the send panel is sticky beside the steps and says it already.
    // MUTATION: drop the 1080 rule -> the desktop draws the reason twice in view.
    expect(painted(dock, 'display', WIDE)).toBe('none')
    expect(painted(dock, 'display', { width: 1000 }) ?? 'block').not.toBe('none')
  })

  it('follows the reason, and leaves once the form is ready', async () => {
    const container = await form()
    pick(container, 'claude-code')
    // MUTATION: dock a fixed sentence -> it still says "choose a runner" after the pick.
    expect(visible(container.querySelector('.sbf-dock'))).toBe('Not ready: fill in prompt.')
    pick(container, 'mock')
    // MUTATION: draw the dock whatever `blocked` says -> a ready form keeps a "Not ready" line.
    expect(container.querySelector('.sbf-dock')).toBeNull()
  })
})
