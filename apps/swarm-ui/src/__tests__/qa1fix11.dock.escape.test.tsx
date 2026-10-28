// LANE QA1-FIX-11 (console visual QA pass 1, part of #2596), finding
// overview-agents-06: with the Reads dock open, Escape did not close it and
// `aria-expanded` stayed true. Expected: Escape closes the dock and returns
// focus to its toggle, the way every other open thing in the console closes
// (shortcuts.ts: "closes the innermost thing that is open").

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'

import { Dock } from '../Dock'
import { forgetProbes, noteFixtureProbe, route } from '../fetch'

afterEach(() => {
  cleanup()
  forgetProbes()
})

function openDock() {
  noteFixtureProbe(route('/v1/capacity'), 120, true)
  const view = render(<Dock />)
  const line = view.container.querySelector<HTMLButtonElement>('.ctl-dock-line')!
  fireEvent.click(line)
  expect(line.getAttribute('aria-expanded')).toBe('true')
  return { ...view, line }
}

describe('the Reads dock closes on Escape (overview-agents-06)', () => {
  it('collapses from inside the open panel and puts focus back on its toggle', () => {
    // MUTATION: delete the dock's onKeyDown Escape branch in Dock.tsx and
    // aria-expanded stays "true" here, as the sweep measured on the fixture
    // console.
    const { container, line } = openDock()
    const link = container.querySelector<HTMLAnchorElement>('.ctl-dock-body a')!
    link.focus()
    fireEvent.keyDown(link, { key: 'Escape' })
    expect(line.getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelector('.ctl-dock-body')).toBeNull()
    // MUTATION: drop the `toggle.current?.focus()` and focus is left on
    // document.body, because the link it was on has unmounted.
    expect(document.activeElement).toBe(line)
  })

  it('collapses from the toggle itself and keeps the key from closing anything behind it', () => {
    const { line } = openDock()
    line.focus()
    const ev = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    line.dispatchEvent(ev)
    expect(line.getAttribute('aria-expanded')).toBe('false')
    // MUTATION: drop the preventDefault() and a document-level Escape
    // listener (Spine's sheet, AgentSplit) reads this key as its own too.
    expect(ev.defaultPrevented).toBe(true)
  })

  it('leaves a collapsed dock alone, so Escape still reaches what is behind it', () => {
    noteFixtureProbe(route('/v1/capacity'), 120, true)
    const { container } = render(<Dock />)
    const line = container.querySelector<HTMLButtonElement>('.ctl-dock-line')!
    const ev = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    line.dispatchEvent(ev)
    expect(line.getAttribute('aria-expanded')).toBe('false')
    expect(ev.defaultPrevented).toBe(false)
  })
})
