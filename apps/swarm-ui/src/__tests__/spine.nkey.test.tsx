/**
 * THE N KEY OPENS SUBMIT, AND ONLY WHEN NOBODY IS TYPING.
 *
 * The spine's Submit button says "Submit (N)". The key is a promise the
 * button makes, so it is held here: N from the page opens /submit; N typed
 * into an input, a textarea, a select or a contenteditable is a letter, not a
 * command; N with a modifier held belongs to the browser or the OS; and an N
 * another handler already consumed (the diff view's next-file key calls
 * `preventDefault`) does not also navigate away from the diff.
 *
 * MUTATION: drop any one of the guards in SkyShell's key handler and the case
 * that names it goes red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'

import type { ReactNode } from 'react'

import { SkyShell } from '../Spine'

function shell(children: ReactNode = null) {
  const go = vi.fn()
  render(
    <SkyShell section="overview" tab="now" title="Overview" go={go} foot={null}>
      {children}
    </SkyShell>,
  )
  return go
}

afterEach(() => cleanup())

describe('the N key', () => {
  it('opens /submit from the page', () => {
    const go = shell()
    fireEvent.keyDown(document.body, { key: 'n' })
    expect(go).toHaveBeenCalledWith('submit')
  })

  it('opens /submit with caps lock on (N without shift)', () => {
    const go = shell()
    fireEvent.keyDown(document.body, { key: 'N' })
    expect(go).toHaveBeenCalledWith('submit')
  })

  it('is a letter, not a command, while focus is in a field', () => {
    const go = shell(
      <>
        <input aria-label="field" />
        <textarea aria-label="area" />
        <select aria-label="pick">
          <option>one</option>
        </select>
        <div aria-label="editable" contentEditable suppressContentEditableWarning>
          <span>inside</span>
        </div>
      </>,
    )
    for (const sel of ['input', 'textarea', 'select', '[contenteditable] > span']) {
      const el = document.querySelector<HTMLElement>(sel)!
      fireEvent.keyDown(el, { key: 'n' })
    }
    expect(go).not.toHaveBeenCalled()
  })

  it('does nothing with a modifier held', () => {
    const go = shell()
    for (const mod of ['ctrlKey', 'metaKey', 'altKey', 'shiftKey'] as const) {
      fireEvent.keyDown(document.body, { key: mod === 'shiftKey' ? 'N' : 'n', [mod]: true })
    }
    expect(go).not.toHaveBeenCalled()
  })

  it('leaves an N another handler consumed alone', () => {
    const go = shell(
      <div data-testid="owner" tabIndex={0} onKeyDown={(e) => e.preventDefault()}>
        diff
      </div>,
    )
    fireEvent.keyDown(document.querySelector('[data-testid="owner"]')!, { key: 'n' })
    expect(go).not.toHaveBeenCalled()
  })
})

/**
 * N IS NOT A PAGE COMMAND WHILE SOMETHING SITS OVER THE PAGE. A modal (any
 * `[role=dialog]` or `aria-modal`) or Pool limits' side editor (`aside.adm-side`)
 * owns the keyboard while it is open: an N pressed there -- on its Save
 * button, say -- navigating to /submit throws away what was being edited.
 *
 * MUTATION: drop the open-overlay guard from SkyShell's key handler and each
 * case here goes red.
 */
describe('the N key while a dialog or the side editor is open', () => {
  it('is ignored while a [role=dialog] is open, focus on its button', () => {
    const go = shell(
      <div role="dialog" aria-label="Agent t-1">
        <button type="button">Close</button>
      </div>,
    )
    fireEvent.keyDown(document.querySelector('[role="dialog"] button')!, { key: 'n' })
    fireEvent.keyDown(document.body, { key: 'n' })
    expect(go).not.toHaveBeenCalled()
  })

  it('is ignored while an aria-modal element is open', () => {
    const go = shell(
      <div aria-modal="true" aria-label="confirm">
        <button type="button">OK</button>
      </div>,
    )
    fireEvent.keyDown(document.body, { key: 'n' })
    expect(go).not.toHaveBeenCalled()
  })

  it('is ignored while the Pool limits side editor is open', () => {
    const go = shell(
      <aside className="adm-side" aria-label="tenant:eng">
        <button type="button">Save</button>
      </aside>,
    )
    fireEvent.keyDown(document.querySelector('aside.adm-side button')!, { key: 'n' })
    expect(go).not.toHaveBeenCalled()
  })

  it('opens /submit again once the dialog is gone', () => {
    const go = vi.fn()
    const view = (open: boolean) => (
      <SkyShell section="overview" tab="now" title="Overview" go={go} foot={null}>
        {open && <div role="dialog" aria-label="Agent t-1" />}
      </SkyShell>
    )
    const { rerender } = render(view(true))
    fireEvent.keyDown(document.body, { key: 'n' })
    expect(go).not.toHaveBeenCalled()
    rerender(view(false))
    fireEvent.keyDown(document.body, { key: 'n' })
    expect(go).toHaveBeenCalledWith('submit')
  })
})
