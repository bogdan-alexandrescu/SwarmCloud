// QA1-FIX-26 (part of #2596): the Submit findings of console visual QA pass 1.
//
//   * runs-submit-18: the send panel said "input: empty — the runner gets no
//     settings" beside "not sent: prompt (blank)", and a setting's label was
//     its raw key in mono, its button "remove".
//   * runs-submit-19: "+ Add setting" sat on the "Additional repositories"
//     legend with no gap.
//
// Each case is red on main and green with the change; the MUTATION line on
// each names the edit that turns it red again.

import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

import { InputFields, settingWords, SubmitScreen } from '../Submit'
import SUBMIT_CSS from '../styles/submit.css?raw'

const WAIT = { timeout: 4000 }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('runs-submit-18: one statement about the input, and labels in words', () => {
  // MUTATION: draw the input row whatever `unsent` holds and "the runner gets
  // no settings" is said beside "not sent" again.
  it('does not say the input is empty beside a blank setting it lists as not sent', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    fireEvent.click(within(container.querySelector('.sbf-input')!).getByRole('button', { name: /Add setting/ }))
    fireEvent.click(within(screen.getByRole('group', { name: 'settings to add' })).getByRole('button', { name: /^prompt/ }))
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    expect(visible(panel)).toMatch(/not sent\s*prompt \(blank\)/)
    expect(visible(panel)).not.toContain('the runner gets no settings')
  })

  // MUTATION: label the field with `<span className="mono">{f.name}</span>`
  // again, or the button "remove", and this goes red.
  it('labels a setting in words, the key in its title, and its button Remove', () => {
    const field = { key: 1, name: 'timeout_seconds', kind: 'number', text: '', list: [], flag: false, actions: [], required: false, own: false }
    const { container } = render(
      <InputFields profile="generic" required={[]} idPrefix="t" onChange={() => {}} fields={[field as never]} />,
    )
    const name = container.querySelector('.sbf-field .sbf-name span')!
    expect(visible(name)).toBe('Timeout seconds')
    expect(name.getAttribute('title')).toBe('timeout_seconds')
    expect(name.classList.contains('mono')).toBe(false)
    const remove = container.querySelector('.sbf-remove')!
    expect(visible(remove)).toBe('Remove')
    expect(remove.getAttribute('aria-label')).toBe('remove timeout_seconds')
    expect(settingWords('prompt')).toBe('Prompt')
    expect(settingWords('url')).toBe('URL')
  })
})

describe('runs-submit-19: a gap above the next group', () => {
  // MUTATION: delete the `.sbf-input + .btp` rule from styles/submit.css.
  it('keeps one 12px step between the settings and the fieldset after them', () => {
    expect(SUBMIT_CSS).toMatch(/\.sbf-input \+ \.btp \{ margin-top: 12px; \}/)
  })
})

describe('runs-submit-18: the label is drawn in the body face', () => {
  // MUTATION: delete the `.sbf-name > .sbf-words` rule and the label falls
  // back to `.sbf-name`'s mono face.
  it('sets the words label in the body face, not mono', () => {
    expect(SUBMIT_CSS).toMatch(/\.sbf-name > \.sbf-words \{ font: var\(--t-body\)\/var\(--lh-body\) var\(--font\);/)
  })
})
